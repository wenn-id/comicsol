"""RED -> GREEN contracts for the durable Web generation queue."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Mapping, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.api.generation import create_generation_router
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import StaleProjectRevisionError
from comic_sol_web.generation.providers.base import ProviderError, ProviderRegistry
from comic_sol_web.generation.providers.fake import FakeProvider
from comic_sol_web.generation.receipts import AUTHORIZED_RECEIPT_FIELDS, sanitize_usage
from comic_sol_web.generation.service import (
    GenerationConflictError,
    GenerationService,
    GenerationUnavailableError,
    RetryLimitError,
)
from comic_sol_web.generation.types import (
    AuthMode,
    ErrorCategory,
    GenerationRequest,
    GenerationResult,
    JobState,
)
from comic_sol_web.migrations import GENERATION_MIGRATIONS, apply_migrations


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0"
    b"\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)
CAPABILITIES_USED = {
    "dimensions": False,
    "localized_edit": False,
    "reference_images": False,
}


class ErrorProvider(FakeProvider):
    """Deterministic provider that raises one normalized category."""

    def __init__(self, category: ErrorCategory) -> None:
        super().__init__()
        self.category = category

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del request, model, credential
        raise ProviderError(self.category)


class HangingProvider(FakeProvider):
    """Deterministic provider that waits until its caller cancels it."""

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del request, model, credential
        await asyncio.Event().wait()
        raise AssertionError("cancelled provider call resumed")


class CredentialRecordingProvider(FakeProvider):
    """Record which owner-scoped credential reaches each provider call."""

    def __init__(self) -> None:
        super().__init__()
        self.credentials: list[str | None] = []

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del request, model
        self.credentials.append(credential)
        return GenerationResult(
            external_job_id=None,
            state=JobState.FAILED,
            raster_bytes=None,
            media_type=None,
            effective_parameters={},
            usage={},
        )


class RecordingCredentialResolver:
    """Yield credentials only for the exact leased owner/provider/mode tuple."""

    def __init__(
        self,
        credentials: Mapping[tuple[str, str, AuthMode], str | None] | None = None,
    ) -> None:
        self.credentials = {} if credentials is None else dict(credentials)
        self.resolutions: list[tuple[str, str, AuthMode]] = []

    def resolve(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> AsyncContextManager[str | None]:
        return self._resolve(user_id, provider, auth_mode)

    @asynccontextmanager
    async def _resolve(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> AsyncIterator[str | None]:
        mode = auth_mode if isinstance(auth_mode, AuthMode) else AuthMode(auth_mode)
        key = (user_id, provider, mode)
        self.resolutions.append(key)
        yield self.credentials.get(key)


class FakeAuth:
    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal

    def require_csrf(self, _request: object) -> SessionPrincipal:
        return self.principal


class LeaseBoundaryClock:
    """Advance to the lease boundary after the lease is issued."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return 1_000 if self.calls == 1 else 1_001


class MutableClock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class FakeProjects:
    """Owner/revision guard that records only contained staged raster paths."""

    def __init__(self, staging_root: Path) -> None:
        self.staging_root = staging_root
        self.owner_id = "alice-id"
        self.project_id = "project-1"
        self.revision = 3
        self.request_ids: tuple[str, ...] = ("engine-job-1",)
        self.submissions: list[tuple[Path, bytes]] = []
        self.accepted_raster: bytes | None = None

    def prepare_generation(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> tuple[GenerationRequest, ...]:
        self._authorize(principal, project_id, expected_revision)
        return tuple(
            make_request(
                project_revision=self.revision,
                job_id=job_id,
                subject_id=f"panel-{index}",
            )
            for index, job_id in enumerate(self.request_ids, start=1)
        )

    def submit_raster(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        job_id: str,
        raster: Path,
        media_type: str,
        capabilities_used: Mapping[str, object],
    ) -> object:
        self._authorize(principal, project_id, expected_revision)
        self.assert_contained(raster)
        if media_type != "image/png" or capabilities_used != CAPABILITIES_USED:
            raise ValueError("invalid staged raster")
        payload = raster.read_bytes()
        self.submissions.append((raster, payload))
        self.accepted_raster = payload
        self.revision += 1
        return type("Snapshot", (), {"revision": self.revision})()

    def snapshot(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int | None = None,
    ) -> object:
        if principal.user_id != self.owner_id or project_id != self.project_id:
            raise ValueError("project unavailable")
        if expected_revision is not None and expected_revision != self.revision:
            raise ValueError("project revision is stale")
        return type("Snapshot", (), {"revision": self.revision})()

    def _authorize(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> None:
        if principal.user_id != self.owner_id or project_id != self.project_id:
            raise ValueError("project unavailable")
        if expected_revision != self.revision:
            raise StaleProjectRevisionError(expected_revision, self.revision)

    def assert_contained(self, raster: Path) -> None:
        resolved = raster.resolve(strict=True)
        resolved.relative_to(self.staging_root.resolve(strict=True))


class GenerationQueueFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging_root = self.root / "staging"
        self.staging_root.mkdir()
        self.database = Database(self.root / "application.sqlite3")
        apply_migrations(self.database, GENERATION_MIGRATIONS)
        self.clock = MutableClock()
        self.projects = FakeProjects(self.staging_root)
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")
        self.credentials = RecordingCredentialResolver()
        self.service = self.make_service()

    def make_service(
        self,
        provider: FakeProvider | None = None,
        credentials: RecordingCredentialResolver | None = None,
    ) -> GenerationService:
        return GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((FakeProvider() if provider is None else provider,)),
            self.staging_root,
            credentials=self.credentials if credentials is None else credentials,
            clock=self.clock,
        )

    def queue(
        self,
        *,
        max_retries: int = 2,
        auth_mode: AuthMode = AuthMode.AGENT,
    ):
        return self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="fake",
            model="fake-raster-v1",
            auth_mode=auth_mode,
            max_retries=max_retries,
        )[0]

    @staticmethod
    def accepted_result(
        payload: bytes = PNG,
        *,
        external_job_id: str | None = None,
        usage: Mapping[str, int | float | str] | None = None,
        effective_parameters: Mapping[str, object] | None = None,
    ) -> GenerationResult:
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.ACCEPTED,
            raster_bytes=payload,
            media_type="image/png",
            effective_parameters={} if effective_parameters is None else effective_parameters,
            usage={"images": 1} if usage is None else usage,
        )

    @staticmethod
    def failed_result() -> GenerationResult:
        return GenerationResult(
            external_job_id=None,
            state=JobState.FAILED,
            raster_bytes=None,
            media_type=None,
            effective_parameters={},
            usage={},
        )

    @staticmethod
    def polling_result(external_job_id: str) -> GenerationResult:
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters={},
            usage={},
        )


class DurableQueueTests(GenerationQueueFixture):
    def test_duplicate_enqueue_is_idempotent(self) -> None:
        first = self.queue()
        second = self.queue()
        self.assertEqual(first.job_id, second.job_id)
        with self.database.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0]
        self.assertEqual(1, count)

    def test_lease_expiry_is_bounded_and_compare_and_set(self) -> None:
        queued = self.queue()
        first = self.service.lease_next("worker-a", lease_seconds=10)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(queued.job_id, first.job.job_id)
        self.assertIsNone(self.service.lease_next("worker-b", lease_seconds=10))
        with self.assertRaises(ValueError):
            self.service.lease_next("worker-b", lease_seconds=301)

        self.clock.value += 11
        recovered = self.service.lease_next("worker-b", lease_seconds=10)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(queued.job_id, recovered.job.job_id)
        self.assertNotEqual(first.lease_token, recovered.lease_token)
        self.assertEqual(2, recovered.job.attempt_number)
        self.assertEqual(1, recovered.job.retry_count)

    def test_expired_leases_stop_at_the_retry_ceiling(self) -> None:
        queued = self.queue(max_retries=1)
        first = self.service.lease_next("worker-a", lease_seconds=5)
        assert first is not None
        self.clock.value += 6
        recovered = self.service.lease_next("worker-b", lease_seconds=5)
        assert recovered is not None
        self.assertEqual(2, recovered.job.attempt_number)
        self.assertEqual(1, recovered.job.retry_count)

        self.clock.value += 6
        self.assertIsNone(self.service.lease_next("worker-c", lease_seconds=5))
        failed = self.service.get(self.alice, queued.job_id)
        self.assertEqual(JobState.FAILED, failed.state)
        self.assertEqual(2, failed.attempt_number)
        self.assertEqual(1, failed.retry_count)
        attempts = self.service.attempts(queued.job_id)
        self.assertEqual(JobState.FAILED.value, attempts[-1]["state"])
        self.assertEqual(ErrorCategory.TIMEOUT.value, attempts[-1]["error_category"])

    def test_restart_recovers_an_expired_running_job(self) -> None:
        queued = self.queue()
        leased = self.service.lease_next("worker-before-restart", lease_seconds=5)
        self.assertIsNotNone(leased)
        self.clock.value += 6

        reopened = self.make_service()
        recovered = reopened.lease_next("worker-after-restart", lease_seconds=5)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(queued.job_id, recovered.job.job_id)

    def test_callback_poll_completion_race_is_exactly_once(self) -> None:
        queued = self.queue()
        initial_lease = self.service.lease_next("worker", lease_seconds=30)
        assert initial_lease is not None
        external_job_id = "fake:callback-race"
        polling = self.service.record_result(
            queued.job_id,
            initial_lease.lease_token,
            self.polling_result(external_job_id),
        )
        self.assertEqual(JobState.POLLING, polling.state)

        poll_lease = self.service.lease_next("poll-worker", lease_seconds=30)
        assert poll_lease is not None
        callback = self.service.record_result(
            queued.job_id,
            None,
            self.accepted_result(external_job_id=external_job_id),
        )
        duplicate_poll = self.service.record_result(
            queued.job_id,
            poll_lease.lease_token,
            self.accepted_result(external_job_id=external_job_id),
        )
        self.assertEqual(JobState.VALIDATING, callback.state)
        self.assertEqual(callback, duplicate_poll)
        self.assertEqual(1, len(self.service.receipts(queued.job_id)))

        promoted = self.service.submit_staged_raster(self.alice, queued.job_id, 3)
        promoted_again = self.service.submit_staged_raster(self.alice, queued.job_id, 3)
        self.assertEqual(JobState.ACCEPTED, promoted.state)
        self.assertEqual(promoted, promoted_again)
        self.assertEqual(1, len(self.projects.submissions))
        receipts = self.service.receipts(queued.job_id)
        self.assertEqual(1, len(receipts))
        usage = cast(Mapping[str, object], receipts[0]["usage"])
        self.assertEqual(1, usage["images"])

    def test_failed_poll_preserves_the_external_provider_job_id(self) -> None:
        queued = self.queue()
        initial = self.service.lease_next("worker", lease_seconds=30)
        assert initial is not None
        external_job_id = "fake:preserved-after-poll-failure"
        self.service.record_result(
            queued.job_id,
            initial.lease_token,
            self.polling_result(external_job_id),
        )
        polling = self.service.lease_next("poll-worker", lease_seconds=30)
        assert polling is not None

        failed = self.service.record_result(
            queued.job_id,
            polling.lease_token,
            self.failed_result(),
        )

        self.assertEqual(JobState.FAILED, failed.state)
        self.assertEqual(external_job_id, failed.external_job_id)
        self.assertEqual(
            external_job_id, self.service.attempts(queued.job_id)[-1]["external_job_id"]
        )

    def test_malformed_provider_results_fail_durably_as_invalid_output(self) -> None:
        malformed_results = (
            GenerationResult(
                external_job_id=None,
                state=JobState.ACCEPTED,
                raster_bytes=PNG,
                media_type="image/jpeg",
                effective_parameters={},
                usage={},
            ),
            GenerationResult(
                external_job_id=None,
                state=JobState.POLLING,
                raster_bytes=None,
                media_type=None,
                effective_parameters={},
                usage={},
            ),
            GenerationResult(
                external_job_id=None,
                state=JobState.FAILED,
                raster_bytes=PNG,
                media_type="image/png",
                effective_parameters={},
                usage={},
            ),
        )
        for index, result in enumerate(malformed_results, start=1):
            with self.subTest(state=result.state, media_type=result.media_type):
                self.projects.request_ids = (f"malformed-result-{index}",)
                queued = self.queue()
                lease = self.service.lease_next(f"malformed-worker-{index}", lease_seconds=30)
                assert lease is not None

                failed = self.service.record_result(queued.job_id, lease.lease_token, result)

                self.assertEqual(JobState.FAILED, failed.state)
                attempt = self.service.attempts(queued.job_id)[-1]
                self.assertEqual(JobState.FAILED.value, attempt["state"])
                self.assertEqual(ErrorCategory.INVALID_OUTPUT.value, attempt["error_category"])

    def test_external_provider_ids_are_bounded_opaque_and_non_sensitive(self) -> None:
        invalid_ids = (
            "api_key=raw-provider-secret",
            "account@example.com",
            "line\nbreak",
            "x" * 129,
        )
        for index, external_job_id in enumerate(invalid_ids, start=1):
            with self.subTest(external_job_id=repr(external_job_id)):
                self.projects.request_ids = (f"invalid-external-id-{index}",)
                queued = self.queue()
                lease = self.service.lease_next(f"external-id-worker-{index}", lease_seconds=30)
                assert lease is not None

                failed = self.service.record_result(
                    queued.job_id,
                    lease.lease_token,
                    self.polling_result(external_job_id),
                )

                self.assertEqual(JobState.FAILED, failed.state)
                self.assertIsNone(failed.external_job_id)
                attempt = self.service.attempts(queued.job_id)[-1]
                self.assertEqual(ErrorCategory.INVALID_OUTPUT.value, attempt["error_category"])
                self.assertIsNone(attempt["external_job_id"])

        with self.database.read() as connection:
            persisted = repr(
                tuple(connection.execute("SELECT * FROM generation_jobs").fetchall())
                + tuple(connection.execute("SELECT * FROM generation_attempts").fetchall())
            )
        for external_job_id in invalid_ids:
            self.assertNotIn(external_job_id, persisted)

        self.projects.request_ids = ("valid-external-id",)
        queued = self.queue()
        lease = self.service.lease_next("valid-external-id-worker", lease_seconds=30)
        assert lease is not None
        polling = self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.polling_result("fake:job_123-abc"),
        )
        self.assertEqual("fake:job_123-abc", polling.external_job_id)

    def test_malformed_callback_fails_without_overwriting_a_lease_race(self) -> None:
        queued = self.queue()
        lease = self.service.lease_next("callback-worker", lease_seconds=30)
        assert lease is not None
        external_job_id = "fake:callback-validation"
        self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.polling_result(external_job_id),
        )

        with self.assertRaises(GenerationConflictError):
            self.service.record_result(
                queued.job_id,
                None,
                self.accepted_result(external_job_id="fake:different-job"),
            )
        self.assertEqual(JobState.POLLING, self.service.get(self.alice, queued.job_id).state)

        malformed = GenerationResult(
            external_job_id=external_job_id,
            state=JobState.ACCEPTED,
            raster_bytes=PNG,
            media_type="image/jpeg",
            effective_parameters={},
            usage={},
        )
        failed = self.service.record_result(queued.job_id, None, malformed)
        self.assertEqual(JobState.FAILED, failed.state)
        self.assertEqual(
            ErrorCategory.INVALID_OUTPUT.value,
            self.service.attempts(queued.job_id)[-1]["error_category"],
        )

    def test_same_provider_retry_has_a_hard_ceiling(self) -> None:
        queued = self.queue(max_retries=1)
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        failed = self.service.record_result(queued.job_id, lease.lease_token, self.failed_result())
        self.assertEqual(JobState.FAILED, failed.state)

        retry = self.service.retry_same_provider(self.alice, queued.job_id, 3)
        self.assertEqual(JobState.QUEUED, retry.state)
        self.assertEqual("fake", retry.provider)
        self.assertEqual("fake-raster-v1", retry.model)
        second_lease = self.service.lease_next("worker", lease_seconds=30)
        assert second_lease is not None
        self.service.record_result(queued.job_id, second_lease.lease_token, self.failed_result())
        with self.assertRaises(RetryLimitError):
            self.service.retry_same_provider(self.alice, queued.job_id, 3)

    def test_attempts_and_receipts_are_database_enforced_append_only(self) -> None:
        queued = self.queue(max_retries=1)
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        self.service.record_result(queued.job_id, lease.lease_token, self.failed_result())
        before = self.service.attempts(queued.job_id)
        self.service.retry_same_provider(self.alice, queued.job_id, 3)
        after = self.service.attempts(queued.job_id)
        self.assertEqual(before, after[: len(before)])
        self.assertGreater(len(after), len(before))

        with self.database.transaction() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE generation_attempts SET state = 'accepted' WHERE job_id = ?",
                    (queued.job_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM generation_attempts WHERE job_id = ?", (queued.job_id,)
                )

    def test_receipts_contain_only_authorized_sanitized_fields(self) -> None:
        secret = "raw-provider-secret"
        queued = self.queue()
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.accepted_result(
                usage={"images": 1, "credential": secret, "private": secret},
                effective_parameters={"prompt": secret, "response": secret},
            ),
        )
        receipt = self.service.receipts(queued.job_id)[0]
        self.assertEqual(AUTHORIZED_RECEIPT_FIELDS, frozenset(receipt))
        self.assertEqual({"images": 1}, receipt["usage"])
        rendered = repr(receipt)
        self.assertNotIn(secret, rendered)
        with self.database.read() as connection:
            persisted = repr(
                tuple(connection.execute("SELECT * FROM generation_receipts").fetchall())
            )
        self.assertNotIn(secret, persisted)

    def test_receipt_accounting_strings_require_canonical_formats(self) -> None:
        self.assertEqual(
            {"currency": "USD", "unit": "image", "quantity": 1},
            dict(sanitize_usage({"currency": "USD", "unit": "image", "quantity": 1})),
        )
        secret = "api_key=raw-provider-secret"
        sanitized = dict(
            sanitize_usage(
                {
                    "currency": secret,
                    "unit": "account-identifier",
                    "images": secret,
                }
            )
        )
        self.assertEqual({}, sanitized)
        self.assertNotIn(secret, repr(sanitized))

    def test_stale_revisions_fail_before_queue_or_state_mutation(self) -> None:
        with self.assertRaises(ValueError):
            self.service.queue(
                self.alice,
                self.projects.project_id,
                2,
                provider="fake",
                model="fake-raster-v1",
                auth_mode=AuthMode.AGENT,
            )
        queued = self.queue()
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        self.service.record_result(queued.job_id, lease.lease_token, self.failed_result())
        with self.assertRaises(GenerationConflictError):
            self.service.retry_same_provider(self.alice, queued.job_id, 2)
        with self.assertRaises(GenerationConflictError):
            self.service.pause_for_switch(self.alice, queued.job_id, 2)

    def test_promotion_failure_releases_claim_without_rebinding_revision(self) -> None:
        queued = self.queue()
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        validating = self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.accepted_result(),
        )
        self.projects.revision += 1

        with self.assertRaises(StaleProjectRevisionError):
            self.service.submit_staged_raster(self.alice, queued.job_id, 3)

        current = self.service.get(self.alice, queued.job_id)
        self.assertEqual(JobState.VALIDATING, current.state)
        self.assertEqual(validating.staged_raster, current.staged_raster)
        assert current.staged_raster is not None
        self.assertEqual(PNG, current.staged_raster.read_bytes())
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT lease_token, lease_owner, lease_expires_at "
                "FROM generation_jobs WHERE job_id = ?",
                (queued.job_id,),
            ).fetchone()
        assert row is not None
        self.assertIsNone(row["lease_token"])
        self.assertIsNone(row["lease_owner"])
        self.assertIsNone(row["lease_expires_at"])
        self.assertEqual([], self.projects.submissions)

    def test_sibling_jobs_follow_only_each_successful_batch_promotion_revision(self) -> None:
        self.projects.request_ids = ("engine-job-1", "engine-job-2")
        first, second = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="fake",
            model="fake-raster-v1",
            auth_mode=AuthMode.AGENT,
        )
        for worker in ("first-worker", "second-worker"):
            lease = self.service.lease_next(worker, lease_seconds=30)
            assert lease is not None
            self.service.record_result(
                lease.job.job_id,
                lease.lease_token,
                self.accepted_result(),
            )

        promoted_first = self.service.submit_staged_raster(self.alice, first.job_id, 3)
        rebound_second = self.service.get(self.alice, second.job_id)
        self.assertEqual(4, promoted_first.accepted_project_revision)
        self.assertEqual(4, rebound_second.project_revision)
        self.assertEqual(3, rebound_second.request.project_revision)

        promoted_second = self.service.submit_staged_raster(self.alice, second.job_id, 4)

        self.assertEqual(JobState.ACCEPTED, promoted_first.state)
        self.assertEqual(JobState.ACCEPTED, promoted_second.state)
        self.assertEqual(5, promoted_second.accepted_project_revision)
        self.assertEqual(2, len(self.projects.submissions))

    def test_owner_isolation_applies_to_reads_retries_pauses_and_submission(self) -> None:
        queued = self.queue()
        for operation in (
            lambda: self.service.get(self.bob, queued.job_id),
            lambda: self.service.retry_same_provider(self.bob, queued.job_id, 3),
            lambda: self.service.pause_for_switch(self.bob, queued.job_id, 3),
            lambda: self.service.submit_staged_raster(self.bob, queued.job_id, 3),
        ):
            with self.subTest(operation=operation), self.assertRaises(GenerationUnavailableError):
                operation()

    def test_provider_switch_pauses_without_fallback(self) -> None:
        queued = self.queue()
        paused = self.service.pause_for_switch(self.alice, queued.job_id, 3)
        self.assertEqual(JobState.AWAITING_PROVIDER_CONFIRMATION, paused.state)
        self.assertEqual("fake", paused.provider)
        self.assertEqual("fake-raster-v1", paused.model)
        self.assertIsNone(self.service.lease_next("worker", lease_seconds=30))

    def test_provider_bytes_cross_wp3_only_as_a_contained_staged_file(self) -> None:
        queued = self.queue()
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        validating = self.service.record_result(
            queued.job_id, lease.lease_token, self.accepted_result()
        )
        assert validating.staged_raster is not None
        self.projects.assert_contained(validating.staged_raster)
        self.assertEqual(PNG, validating.staged_raster.read_bytes())
        self.service.submit_staged_raster(self.alice, queued.job_id, 3)
        self.assertEqual(1, len(self.projects.submissions))
        submitted, payload = self.projects.submissions[0]
        self.assertIsInstance(submitted, Path)
        self.assertEqual(PNG, payload)

    def test_failed_or_duplicate_completion_preserves_last_accepted_raster_bytes(self) -> None:
        queued = self.queue()
        lease = self.service.lease_next("worker", lease_seconds=30)
        assert lease is not None
        self.service.record_result(queued.job_id, lease.lease_token, self.accepted_result())
        self.service.submit_staged_raster(self.alice, queued.job_id, 3)
        accepted = self.projects.accepted_raster
        self.assertEqual(PNG, accepted)

        with self.assertRaises(GenerationConflictError):
            self.service.record_result(
                queued.job_id,
                lease.lease_token,
                self.accepted_result(b"different-provider-bytes"),
            )
        with self.assertRaises(GenerationConflictError):
            self.service.record_result(queued.job_id, lease.lease_token, self.failed_result())
        self.assertEqual(accepted, self.projects.accepted_raster)
        self.assertEqual(1, len(self.projects.submissions))

    def test_worker_resolves_credentials_after_leasing_each_job_owner(self) -> None:
        provider = CredentialRecordingProvider()
        resolver = RecordingCredentialResolver(
            {
                ("alice-id", "fake", AuthMode.BYOK): "alice-credential",
                ("bob-id", "fake", AuthMode.BYOK): "bob-credential",
            }
        )
        self.service = self.make_service(provider, resolver)
        self.queue(auth_mode=AuthMode.BYOK)
        self.clock.value += 1
        self.projects.owner_id = self.bob.user_id
        self.service.queue(
            self.bob,
            self.projects.project_id,
            self.projects.revision,
            provider="fake",
            model="fake-raster-v1",
            auth_mode=AuthMode.BYOK,
        )

        asyncio.run(self.service.run_once("credential-worker-1"))
        asyncio.run(self.service.run_once("credential-worker-2"))

        self.assertEqual(
            [
                ("alice-id", "fake", AuthMode.BYOK),
                ("bob-id", "fake", AuthMode.BYOK),
            ],
            resolver.resolutions,
        )
        self.assertEqual(["alice-credential", "bob-credential"], provider.credentials)

    def test_queue_request_schedules_application_job_consumption(self) -> None:
        app = FastAPI()
        app.state.auth = FakeAuth(self.alice)
        app.include_router(create_generation_router(self.service))
        app.dependency_overrides[require_principal] = lambda: self.alice

        with TestClient(app) as client:
            response = client.post(
                "/api/generation/queue",
                json={
                    "project_id": self.projects.project_id,
                    "expected_revision": self.projects.revision,
                    "provider": "fake",
                    "model": "fake-raster-v1",
                    "auth_mode": "agent",
                },
            )

        self.assertEqual(201, response.status_code)
        job_id = response.json()["jobs"][0]["job_id"]
        self.assertEqual(JobState.ACCEPTED, self.service.get(self.alice, job_id).state)
        self.assertEqual(PNG, self.projects.accepted_raster)

    def test_worker_pauses_when_the_persisted_provider_is_unavailable(self) -> None:
        queued = self.queue()
        unavailable = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry(()),
            self.staging_root,
            credentials=self.credentials,
            clock=self.clock,
        )

        paused = asyncio.run(unavailable.run_once("missing-provider-worker"))

        assert paused is not None
        self.assertEqual(queued.job_id, paused.job_id)
        self.assertEqual(JobState.AWAITING_PROVIDER_CONFIRMATION, paused.state)
        attempt = unavailable.attempts(queued.job_id)[-1]
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING.value, attempt["error_category"])

    def test_worker_preserves_provider_cancellation_as_terminal_state(self) -> None:
        self.service = self.make_service(ErrorProvider(ErrorCategory.CANCELLED))
        queued = self.queue()

        cancelled = asyncio.run(self.service.run_once("cancelled-worker"))

        assert cancelled is not None
        self.assertEqual(queued.job_id, cancelled.job_id)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertEqual(
            ErrorCategory.CANCELLED.value,
            self.service.attempts(queued.job_id)[-1]["error_category"],
        )

    def test_worker_bounds_provider_call_by_remaining_lease_lifetime(self) -> None:
        self.service = self.make_service(HangingProvider())
        queued = self.queue()
        boundary_clock = LeaseBoundaryClock()
        self.service._clock = boundary_clock
        self.service._queue.clock = boundary_clock

        timed_out = asyncio.run(self.service.run_once("timeout-worker", lease_seconds=1))

        assert timed_out is not None
        self.assertEqual(queued.job_id, timed_out.job_id)
        self.assertEqual(JobState.FAILED, timed_out.state)
        self.assertEqual(
            ErrorCategory.TIMEOUT.value,
            self.service.attempts(queued.job_id)[-1]["error_category"],
        )

    def test_worker_executes_the_wp4_fake_provider_offline(self) -> None:
        queued = self.queue()
        completed = asyncio.run(self.service.run_once("offline-worker"))
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(queued.job_id, completed.job_id)
        self.assertEqual(JobState.ACCEPTED, completed.state)
        self.assertEqual(PNG, self.projects.accepted_raster)


def make_request(
    *,
    project_revision: int,
    job_id: str = "engine-job-1",
    subject_id: str = "panel-1",
) -> GenerationRequest:
    return GenerationRequest(
        job_id=job_id,
        project_id="project-1",
        project_revision=project_revision,
        subject_kind="panel",
        subject_id=subject_id,
        prompt="private generation prompt",
        negative_prompt=None,
        references=(),
        width=1024,
        height=1024,
        required_capabilities=frozenset({"text_to_image"}),
        provider_options={"fixture": "success"},
    )


if __name__ == "__main__":
    unittest.main()
