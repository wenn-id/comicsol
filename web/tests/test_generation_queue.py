"""RED -> GREEN contracts for the durable Web generation queue."""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import io
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
import uuid
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Callable, Mapping, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_product.cli import _load_engine_module
from comic_sol_web.api.approvals import create_approvals_router
from comic_sol_web.api.generation import _consume_queue, create_generation_router
from comic_sol_web.assets import AssetStore
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import StaleProjectRevisionError
from comic_sol_web.generation.approvals import ProviderSwitchApprovals
from comic_sol_web.generation.credentials import CredentialUnavailableError
from comic_sol_web.generation.providers.agent import (
    AGENT_MODEL,
    AgentProvider,
    agent_job_checksum,
    agent_locked_scope_digest,
)
from comic_sol_web.generation.providers.base import ProviderError, ProviderRegistry
from comic_sol_web.generation.providers.fake import FakeProvider
from comic_sol_web.generation.receipts import AUTHORIZED_RECEIPT_FIELDS, sanitize_usage
from comic_sol_web.generation.service import (
    GenerationConflictError,
    GenerationService,
    GenerationUnavailableError,
    RetryLimitError,
)
from comic_sol_web.generation.store import GenerationJob
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


def bounded_png(
    width: int = 1,
    height: int = 1,
    *,
    channel: int = 0,
    trailing_decompressed: int = 0,
) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes((channel, 64, 128)) * width for _ in range(height))
    rows += b"x" * trailing_decompressed
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
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


class BflQuotaProvider(FakeProvider):
    """Fail a BFL request with a normalized quota category."""

    provider_id = "bfl"

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del request, model, credential
        raise ProviderError(ErrorCategory.QUOTA_EXHAUSTED)


class GoogleSuccessProvider(FakeProvider):
    """Accept a server-selected Google route entirely offline."""

    provider_id = "google"

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del request, model, credential
        return GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=PNG,
            media_type="image/png",
            effective_parameters={"fixture": "deterministic", "width": 1, "height": 1},
            usage={"images": 1},
        )


class FailOnceProvider(FakeProvider):
    """Fail the first generation call and accept the explicit retry."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        self.calls += 1
        if self.calls == 1:
            return GenerationResult(
                external_job_id=None,
                state=JobState.FAILED,
                raster_bytes=None,
                media_type=None,
                effective_parameters={},
                usage={},
            )
        return await super().generate(request, model, credential)


class NeverCompletingProvider(FakeProvider):
    """Keep one asynchronous provider job polling until the drain budget ends."""

    def __init__(self) -> None:
        super().__init__()
        self.poll_calls = 0

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        self.poll_calls += 1
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters={},
            usage={},
        )


class RevisionAdvancingProvider(FakeProvider):
    """Simulate a sibling promotion while this leased provider call runs."""

    def __init__(self, advance_revision: Callable[[], None]) -> None:
        super().__init__()
        self.advance_revision = advance_revision

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        self.advance_revision()
        return await super().generate(request, model, credential)


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


class SelectiveCredentialResolver:
    """Expose only explicitly available owner/provider/mode tuples."""

    def __init__(
        self,
        credentials: Mapping[tuple[str, str, AuthMode], str | None],
    ) -> None:
        self.credentials = dict(credentials)

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
        if key not in self.credentials:
            raise CredentialUnavailableError("credential is unavailable")
        yield self.credentials[key]


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
        self.project_root = staging_root.parent / "project"
        self.project_root.mkdir()
        self.owner_id = "alice-id"
        self.project_id = "project-1"
        self.revision = 3
        self.prepare_calls = 0
        self.request_ids: tuple[str, ...] = ("engine-job-1",)
        self.subject_ids: dict[str, str] = {}
        self.fixtures: dict[str, str] = {}
        self.prompt = "private generation prompt"
        self.request_width = 1024
        self.request_height = 1024
        self.raster_rejection: str | None = None
        self.promotion_conflict_once = False
        self.after_accept: Callable[[], None] | None = None
        self.submissions: list[tuple[Path, bytes]] = []
        self.accepted_raster: bytes | None = None

    def prepare_generation(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> tuple[GenerationRequest, ...]:
        self.prepare_calls += 1
        self._authorize(principal, project_id, expected_revision)
        return tuple(
            make_request(
                project_revision=self.revision,
                job_id=job_id,
                subject_id=self.subject_ids.get(job_id, f"panel-{index}"),
                fixture=self.fixtures.get(job_id, "success"),
                prompt=self.prompt,
                width=self.request_width,
                height=self.request_height,
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
        if self.promotion_conflict_once:
            self.promotion_conflict_once = False
            raise StaleProjectRevisionError(expected_revision, expected_revision + 1)
        if self.raster_rejection == "handoff":
            handoff = _load_engine_module("handoff")
            raise handoff.HandoffResultError(["result raster: must be a readable PNG"])
        if self.raster_rejection == "resource-limit":
            input_limits = _load_engine_module("input_limits")
            raise input_limits.InputResourceLimitError("decoded raster pixel limit")
        if self.raster_rejection == "handoff-state":
            handoff = _load_engine_module("handoff")
            raise handoff.HandoffResultError(["job is completed and cannot accept a result"])
        if media_type != "image/png" or capabilities_used != CAPABILITIES_USED:
            raise ValueError("invalid staged raster")
        payload = raster.read_bytes()
        self.submissions.append((raster, payload))
        self.accepted_raster = payload
        self._record_canonical_acceptance(job_id, payload)
        self.revision += 1
        if self.after_accept is not None:
            self.after_accept()
        return type(
            "Snapshot",
            (),
            {"revision": self.revision, "root": self.project_root},
        )()

    def _record_canonical_acceptance(self, job_id: str, payload: bytes) -> None:
        if len(job_id) != 64 or any(character not in "0123456789abcdef" for character in job_id):
            return
        handoff = _load_engine_module("handoff")
        raster_relative = "panels/attempts/p01-01/initial-001.png"
        raster = self.project_root / raster_relative
        raster.parent.mkdir(parents=True, exist_ok=True)
        raster.write_bytes(payload)
        receipt = handoff.build_generation_receipt(
            attempt_id=handoff.attempt_id(job_id=job_id, attempt=1),
            job_id=job_id,
            job_sha256="a" * 64,
            raster_path=raster_relative,
            raster_sha256=hashlib.sha256(payload).hexdigest(),
            executor_kind="external-tool",
            executor_id="comic-sol-web",
            provider=None,
            model=None,
            capabilities_used=CAPABILITIES_USED,
            outcome="success",
            category="accepted",
        )
        receipt_path = (
            self.project_root / "generation" / "receipts" / f"{receipt['attempt_id']}.json"
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

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
        return type(
            "Snapshot",
            (),
            {"revision": self.revision, "root": self.project_root},
        )()

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


class FailingPromotionBookkeepingService(GenerationService):
    """Simulate a process failure after WP3 commits but before queue bookkeeping."""

    fail_bookkeeping_once = True

    def _finish_promotion(
        self,
        job: GenerationJob,
        token: str,
        accepted_revision: int,
        *,
        rebind_siblings: bool,
    ) -> GenerationJob:
        if self.fail_bookkeeping_once:
            self.fail_bookkeeping_once = False
            raise sqlite3.OperationalError("simulated queue bookkeeping failure")
        return super()._finish_promotion(
            job,
            token,
            accepted_revision,
            rebind_siblings=rebind_siblings,
        )


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
    def test_queue_options_are_validated_before_project_preparation(self) -> None:
        cases = (
            (self.alice, "fake-raster-v1", 11),
            (self.alice, "malformed model", 2),
            (SessionPrincipal("malformed owner", "alice"), "fake-raster-v1", 2),
        )
        for principal, model, max_retries in cases:
            with self.subTest(
                owner=principal.user_id,
                model=model,
                max_retries=max_retries,
            ):
                prepare_calls = self.projects.prepare_calls
                revision = self.projects.revision

                with self.assertRaises(ValueError):
                    self.service.queue(
                        principal,
                        self.projects.project_id,
                        revision,
                        provider="fake",
                        model=model,
                        auth_mode=AuthMode.AGENT,
                        max_retries=max_retries,
                    )

                self.assertEqual(prepare_calls, self.projects.prepare_calls)
                self.assertEqual(revision, self.projects.revision)

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

    def test_committed_promotion_recovers_after_queue_bookkeeping_failure(self) -> None:
        self.projects.request_ids = ("a" * 64,)
        queued = self.queue()
        lease = self.service.lease_next("promotion-gap-worker", lease_seconds=30)
        assert lease is not None
        validating = self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.accepted_result(),
        )
        assert validating.staged_raster is not None
        failing = FailingPromotionBookkeepingService(
            self.database,
            self.projects,
            ProviderRegistry((FakeProvider(),)),
            self.staging_root,
            credentials=self.credentials,
            clock=self.clock,
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "bookkeeping"):
            failing.submit_staged_raster(self.alice, queued.job_id, 3)

        stranded = failing.get(self.alice, queued.job_id)
        self.assertEqual(JobState.VALIDATING, stranded.state)
        self.assertEqual(4, self.projects.revision)
        self.assertEqual(1, len(self.projects.submissions))
        self.assertTrue(validating.staged_raster.exists())

        self.clock.value += 301
        restarted = self.make_service()
        recovered = restarted.submit_staged_raster(self.alice, queued.job_id, 3)

        self.assertEqual(JobState.ACCEPTED, recovered.state)
        self.assertEqual(4, recovered.accepted_project_revision)
        self.assertEqual(1, len(self.projects.submissions))
        self.assertFalse(validating.staged_raster.exists())
        accepted_attempts = [
            attempt
            for attempt in restarted.attempts(queued.job_id)
            if attempt["state"] == JobState.ACCEPTED.value
        ]
        self.assertEqual(1, len(accepted_attempts))

    def test_promotion_failure_releases_claim_without_rebinding_revision(self) -> None:
        self.projects.request_ids = ("b" * 64,)
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

    def test_canonical_raster_rejection_fails_durably_as_invalid_output(self) -> None:
        for rejection in ("handoff", "resource-limit"):
            with self.subTest(rejection=rejection):
                self.projects.request_ids = (f"invalid-raster-{rejection}",)
                queued = self.queue()
                lease = self.service.lease_next(
                    f"invalid-raster-worker-{rejection}", lease_seconds=30
                )
                assert lease is not None
                validating = self.service.record_result(
                    queued.job_id,
                    lease.lease_token,
                    self.accepted_result(),
                )
                assert validating.staged_raster is not None
                self.projects.raster_rejection = rejection

                failed = self.service.submit_staged_raster(self.alice, queued.job_id, 3)

                self.assertEqual(JobState.FAILED, failed.state)
                self.assertIsNone(failed.staged_raster)
                self.assertFalse(validating.staged_raster.exists())
                attempt = self.service.attempts(queued.job_id)[-1]
                self.assertEqual(ErrorCategory.INVALID_OUTPUT.value, attempt["error_category"])
                self.assertEqual([], self.projects.submissions)
                self.projects.raster_rejection = None

    def test_handoff_state_conflict_retains_valid_staged_raster(self) -> None:
        queued = self.queue()
        lease = self.service.lease_next("handoff-conflict-worker", lease_seconds=30)
        assert lease is not None
        validating = self.service.record_result(
            queued.job_id,
            lease.lease_token,
            self.accepted_result(),
        )
        assert validating.staged_raster is not None
        self.projects.raster_rejection = "handoff-state"

        with self.assertRaises(GenerationConflictError):
            self.service.submit_staged_raster(self.alice, queued.job_id, 3)

        current = self.service.get(self.alice, queued.job_id)
        self.assertEqual(JobState.VALIDATING, current.state)
        self.assertEqual(validating.staged_raster, current.staged_raster)
        self.assertEqual(PNG, validating.staged_raster.read_bytes())
        self.assertNotEqual(
            ErrorCategory.INVALID_OUTPUT.value,
            self.service.attempts(queued.job_id)[-1]["error_category"],
        )

    def test_worker_uses_the_queue_reconciled_revision_for_promotion(self) -> None:
        def advance_revision() -> None:
            with self.database.transaction() as connection:
                updated = connection.execute(
                    "UPDATE generation_jobs SET project_revision = 4 "
                    "WHERE state = 'running' AND project_revision = 3"
                ).rowcount
            self.assertEqual(1, updated)
            self.projects.revision = 4

        self.service = self.make_service(RevisionAdvancingProvider(advance_revision))
        queued = self.queue()

        completed = asyncio.run(self.service.run_once("revision-refresh-worker"))

        assert completed is not None
        self.assertEqual(queued.job_id, completed.job_id)
        self.assertEqual(JobState.ACCEPTED, completed.state)
        self.assertEqual(4, completed.project_revision)
        self.assertEqual(5, completed.accepted_project_revision)
        self.assertEqual(1, len(self.projects.submissions))

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
        self.assertEqual(4, rebound_second.request.project_revision)

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

    def test_queued_jobs_are_consumed_before_older_polling_jobs(self) -> None:
        self.projects.request_ids = ("older-async-job",)
        self.projects.fixtures["older-async-job"] = "async"
        older = self.queue()
        polling = asyncio.run(self.service.run_once("initial-async-worker"))
        assert polling is not None
        self.assertEqual(JobState.POLLING, polling.state)

        self.clock.value += 1
        self.projects.request_ids = ("newer-queued-job",)
        newer = self.queue()
        completed = asyncio.run(self.service.run_once("fair-queue-worker"))

        assert completed is not None
        self.assertEqual(newer.job_id, completed.job_id)
        self.assertEqual(JobState.ACCEPTED, completed.state)
        self.assertEqual(JobState.POLLING, self.service.get(self.alice, older.job_id).state)

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

    def test_request_queue_drain_bounds_recurring_provider_polling(self) -> None:
        provider = NeverCompletingProvider()
        self.service = self.make_service(provider)
        self.projects.fixtures["engine-job-1"] = "async"
        queued = self.queue()

        asyncio.run(asyncio.wait_for(_consume_queue(self.service), timeout=0.5))

        polling = self.service.get(self.alice, queued.job_id)
        self.assertEqual(JobState.POLLING, polling.state)
        self.assertGreater(provider.poll_calls, 0)
        self.assertLessEqual(provider.poll_calls, 4)

    def test_queue_request_continues_until_an_async_job_is_accepted(self) -> None:
        self.projects.fixtures["engine-job-1"] = "async"
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

    def test_queue_drain_continues_after_one_promotion_conflict(self) -> None:
        self.projects.request_ids = ("conflicted-job", "accepted-sibling")
        self.projects.promotion_conflict_once = True
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
        jobs = [self.service.get(self.alice, item["job_id"]) for item in response.json()["jobs"]]
        self.assertEqual(
            [JobState.ACCEPTED, JobState.VALIDATING],
            sorted((job.state for job in jobs), key=lambda state: state.value),
        )
        self.assertEqual(1, len(self.projects.submissions))

    def test_retry_request_schedules_the_failed_job_for_consumption(self) -> None:
        provider = FailOnceProvider()
        self.service = self.make_service(provider)
        app = FastAPI()
        app.state.auth = FakeAuth(self.alice)
        app.include_router(create_generation_router(self.service))
        app.dependency_overrides[require_principal] = lambda: self.alice

        with TestClient(app) as client:
            queued = client.post(
                "/api/generation/queue",
                json={
                    "project_id": self.projects.project_id,
                    "expected_revision": self.projects.revision,
                    "provider": "fake",
                    "model": "fake-raster-v1",
                    "auth_mode": "agent",
                },
            )
            self.assertEqual(201, queued.status_code)
            job_id = queued.json()["jobs"][0]["job_id"]
            self.assertEqual(JobState.FAILED, self.service.get(self.alice, job_id).state)

            retried = client.post(
                f"/api/generation/{job_id}/retry",
                json={"expected_revision": self.projects.revision},
            )

        self.assertEqual(200, retried.status_code)
        self.assertEqual(JobState.ACCEPTED, self.service.get(self.alice, job_id).state)
        self.assertEqual(2, provider.calls)

    def test_switch_route_creates_server_recommendation_and_approval_resumes_worker(
        self,
    ) -> None:
        resolver = SelectiveCredentialResolver(
            {
                ("alice-id", "bfl", AuthMode.AGENT): None,
                ("alice-id", "google", AuthMode.HOSTED): "hosted-google-credential",
            }
        )
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((BflQuotaProvider(), GoogleSuccessProvider())),
            self.staging_root,
            credentials=resolver,
            clock=self.clock,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users (user_id, login, updated_at) VALUES (?, ?, ?)",
                (self.alice.user_id, self.alice.login, self.clock()),
            )
            connection.execute(
                "INSERT INTO web_projects "
                "(project_id, owner_id, storage_name, revision, engine_state) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self.projects.project_id,
                    self.alice.user_id,
                    "project-storage",
                    self.projects.revision,
                    "STORYBOARDED",
                ),
            )
        queued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="bfl",
            model="flux-1.1-pro",
            auth_mode=AuthMode.AGENT,
        )[0]
        failed = asyncio.run(self.service.run_once("initial-bfl-worker"))
        assert failed is not None
        self.assertEqual(JobState.FAILED, failed.state)
        approvals = ProviderSwitchApprovals(self.database, clock=self.clock)
        app = FastAPI()
        app.state.auth = FakeAuth(self.alice)
        app.include_router(create_generation_router(self.service, approvals, resolver))
        app.include_router(create_approvals_router(approvals, self.service))
        app.dependency_overrides[require_principal] = lambda: self.alice

        with TestClient(app) as client:
            injected = client.post(
                f"/api/generation/{queued.job_id}/pause-for-switch",
                json={
                    "expected_revision": self.projects.revision,
                    "provider": "attacker-provider",
                    "model": "attacker-model",
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            proposed = client.post(
                f"/api/generation/{queued.job_id}/pause-for-switch",
                json={"expected_revision": self.projects.revision},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            self.assertEqual(400, injected.status_code)
            self.assertEqual(200, proposed.status_code)
            proposal = proposed.json()
            self.assertEqual([queued.job_id], proposal["job_ids"])
            self.assertEqual("bfl", proposal["from_provider"])
            self.assertEqual("google", proposal["to_provider"])
            self.assertEqual("gemini-2.5-flash-image", proposal["to_model"])
            self.assertEqual(
                JobState.AWAITING_PROVIDER_CONFIRMATION,
                self.service.get(self.alice, queued.job_id).state,
            )

            approved = client.post(
                f"/api/approvals/{proposal['proposal_id']}/approve",
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-Expected-Revision": str(self.projects.revision),
                },
            )

        self.assertEqual(200, approved.status_code)
        self.assertEqual(JobState.ACCEPTED, self.service.get(self.alice, queued.job_id).state)
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


class AgentSubmissionTests(GenerationQueueFixture):
    def setUp(self) -> None:
        super().setUp()
        self.projects.request_ids = ("a" * 64,)
        self.projects.request_width = 1
        self.projects.request_height = 1
        self.assets = AssetStore(
            self.database,
            self.root / "asset-data",
            max_upload_bytes=4096,
            max_pixels=100,
            max_decoded_bytes=4096,
        )
        self.agent = AgentProvider(frozenset({"text_to_image"}))
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((self.agent,)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )

    def queue_agent(self) -> GenerationJob:
        queued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )[0]
        waiting = asyncio.run(self.service.run_once("agent-package-worker"))
        assert waiting is not None
        self.assertEqual(JobState.POLLING, waiting.state)
        return queued

    def upload(self, principal: SessionPrincipal, payload: bytes | None = None):
        content = bounded_png() if payload is None else payload
        return self.assets.create_upload(principal, io.BytesIO(content), "image/png")

    def test_agent_provider_requires_agent_auth_before_project_preparation(self) -> None:
        for mode in (AuthMode.HOSTED, AuthMode.BYOK):
            with self.subTest(auth_mode=mode):
                prepare_calls = self.projects.prepare_calls

                with self.assertRaises(ValueError):
                    self.service.queue(
                        self.alice,
                        self.projects.project_id,
                        self.projects.revision,
                        provider="agent",
                        model=AGENT_MODEL,
                        auth_mode=mode,
                    )

                self.assertEqual(prepare_calls, self.projects.prepare_calls)
        with self.database.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0]
        self.assertEqual(0, count)

    def test_missing_agent_capability_is_resumable_without_resolving_credentials(self) -> None:
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((AgentProvider(frozenset()),)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )
        queued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )[0]

        blocked = asyncio.run(self.service.run_once("missing-agent-capability"))

        assert blocked is not None
        self.assertEqual(queued.job_id, blocked.job_id)
        self.assertEqual(JobState.AWAITING_PROVIDER_CONFIRMATION, blocked.state)
        self.assertEqual([], self.credentials.resolutions)
        self.assertEqual(
            ErrorCategory.CAPABILITY_MISSING.value,
            self.service.attempts(queued.job_id)[-1]["error_category"],
        )
        with self.assertRaises(GenerationConflictError):
            self.service.retry_same_provider(
                self.alice,
                queued.job_id,
                queued.project_revision,
            )

        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((AgentProvider(frozenset({"text_to_image"})),)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )
        with self.assertRaises(GenerationUnavailableError):
            self.service.retry_same_provider(
                self.bob,
                queued.job_id,
                queued.project_revision,
            )
        resumed = self.service.retry_same_provider(
            self.alice,
            queued.job_id,
            queued.project_revision,
        )
        polling = asyncio.run(self.service.run_once("restored-agent-capability"))

        assert polling is not None
        self.assertEqual(JobState.QUEUED, resumed.state)
        self.assertEqual(JobState.POLLING, polling.state)
        self.assertEqual(0, polling.retry_count)
        self.assertEqual([], self.credentials.resolutions)

    def test_stale_agent_capability_handoff_remains_blocked_on_resume(self) -> None:
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((AgentProvider(frozenset()),)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )
        queued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )[0]
        blocked = asyncio.run(self.service.run_once("missing-agent-capability"))
        assert blocked is not None
        self.projects.revision += 1
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((AgentProvider(frozenset({"text_to_image"})),)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )

        with self.assertRaises(GenerationConflictError):
            self.service.retry_same_provider(
                self.alice,
                queued.job_id,
                queued.project_revision,
            )

        current = self.service.get(self.alice, queued.job_id)
        self.assertEqual(JobState.AWAITING_PROVIDER_CONFIRMATION, current.state)
        self.assertEqual([], self.credentials.resolutions)

    def test_agent_asset_submission_promotes_exactly_once_without_credentials(self) -> None:
        queued = self.queue_agent()
        handle = self.upload(self.alice)

        accepted = self.service.submit_agent_asset(
            self.alice, queued.job_id, handle.asset_id, queued.project_revision
        )
        duplicate = self.service.submit_agent_asset(
            self.alice, queued.job_id, handle.asset_id, queued.project_revision
        )

        self.assertEqual(JobState.ACCEPTED, accepted.state)
        self.assertEqual(accepted, duplicate)
        self.assertEqual([], self.credentials.resolutions)
        self.assertEqual(1, len(self.projects.submissions))
        self.assertEqual(bounded_png(), self.projects.accepted_raster)
        self.assertEqual(1, len(self.service.receipts(queued.job_id)))
        self.assertEqual(
            1,
            sum(
                event["state"] == JobState.ACCEPTED.value
                for event in self.service.attempts(queued.job_id)
            ),
        )

    def test_two_sibling_agent_rasters_promote_sequentially_with_bound_requests(self) -> None:
        self.projects.request_ids = ("a" * 64, "b" * 64)
        queued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )
        for index in range(2):
            waiting = asyncio.run(self.service.run_once(f"agent-package-{index}"))
            assert waiting is not None
            self.assertEqual(JobState.POLLING, waiting.state)

        first_before, second_before = (self.service.get(self.alice, job.job_id) for job in queued)
        first_checksum = agent_job_checksum(first_before.request)
        second_checksum = agent_job_checksum(second_before.request)
        locked_scope = agent_locked_scope_digest(second_before.request)
        first_asset = self.upload(self.alice, bounded_png(channel=1))
        second_asset = self.upload(self.alice, bounded_png(channel=2))

        first = self.service.submit_agent_asset(
            self.alice,
            first_before.job_id,
            first_asset.asset_id,
            first_before.project_revision,
        )
        rebound = self.service.get(self.alice, second_before.job_id)
        second = self.service.submit_agent_asset(
            self.alice,
            rebound.job_id,
            second_asset.asset_id,
            rebound.project_revision,
        )

        self.assertEqual(JobState.ACCEPTED, first.state)
        self.assertEqual(JobState.ACCEPTED, second.state)
        self.assertEqual(first.accepted_project_revision, rebound.project_revision)
        self.assertEqual(rebound.project_revision, rebound.request.project_revision)
        self.assertEqual(first_checksum, agent_job_checksum(first_before.request))
        self.assertEqual(second_checksum, agent_job_checksum(rebound.request))
        self.assertEqual(locked_scope, agent_locked_scope_digest(rebound.request))
        self.assertEqual(2, len(self.projects.submissions))
        self.assertEqual(bounded_png(channel=2), self.projects.accepted_raster)

    def test_requeued_rebound_agent_sibling_reuses_existing_job(self) -> None:
        self.projects.request_ids = ("a" * 64, "b" * 64)
        first_before, second_before = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )
        for index in range(2):
            waiting = asyncio.run(self.service.run_once(f"agent-package-{index}"))
            assert waiting is not None
            self.assertEqual(JobState.POLLING, waiting.state)
        first_asset = self.upload(self.alice, bounded_png(channel=1))

        first = self.service.submit_agent_asset(
            self.alice,
            first_before.job_id,
            first_asset.asset_id,
            first_before.project_revision,
        )
        self.projects.request_ids = ("b" * 64,)
        self.projects.subject_ids["b" * 64] = "panel-2"
        requeued = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )[0]

        with self.database.read() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0]
            rebound_key = connection.execute(
                "SELECT idempotency_key FROM generation_jobs WHERE job_id = ?",
                (second_before.job_id,),
            ).fetchone()[0]
        self.assertEqual(second_before.job_id, requeued.job_id)
        self.assertEqual(2, rows)
        self.assertEqual(first.accepted_project_revision, requeued.project_revision)
        self.assertEqual(requeued.project_revision, requeued.request.project_revision)
        self.assertNotEqual(second_before.job_id, rebound_key)

        second_asset = self.upload(self.alice, bounded_png(channel=2))
        second = self.service.submit_agent_asset(
            self.alice,
            requeued.job_id,
            second_asset.asset_id,
            requeued.project_revision,
        )

        self.assertEqual(JobState.ACCEPTED, second.state)
        self.assertEqual(2, len(self.projects.submissions))
        self.assertEqual(bounded_png(channel=2), self.projects.accepted_raster)

    def test_rebound_sibling_reconciles_enqueue_during_canonical_promotion(self) -> None:
        self.projects.request_ids = ("a" * 64, "b" * 64)
        first_before, second_before = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )
        for index in range(2):
            waiting = asyncio.run(self.service.run_once(f"agent-package-{index}"))
            assert waiting is not None
        first_asset = self.upload(self.alice, bounded_png(channel=1))
        concurrent: list[GenerationJob] = []

        def enqueue_after_accept() -> None:
            self.projects.request_ids = ("b" * 64,)
            self.projects.subject_ids["b" * 64] = "panel-2"
            concurrent.extend(
                self.service.queue(
                    self.alice,
                    self.projects.project_id,
                    self.projects.revision,
                    provider="agent",
                    model=AGENT_MODEL,
                    auth_mode=AuthMode.AGENT,
                )
            )

        self.projects.after_accept = enqueue_after_accept
        accepted = self.service.submit_agent_asset(
            self.alice,
            first_before.job_id,
            first_asset.asset_id,
            first_before.project_revision,
        )

        rebound = self.service.get(self.alice, second_before.job_id)
        replay = self.service.queue(
            self.alice,
            self.projects.project_id,
            self.projects.revision,
            provider="agent",
            model=AGENT_MODEL,
            auth_mode=AuthMode.AGENT,
        )[0]
        self.assertEqual(JobState.ACCEPTED, accepted.state)
        self.assertEqual(1, len(concurrent))
        self.assertNotEqual(second_before.job_id, concurrent[0].job_id)
        self.assertEqual(second_before.job_id, replay.job_id)
        self.assertEqual(accepted.accepted_project_revision, rebound.project_revision)
        with self.database.read() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0]
        self.assertEqual(2, rows)

    def test_restart_restores_polling_package_and_accepts_asset(self) -> None:
        queued = self.queue_agent()
        external_job_id = self.service.get(self.alice, queued.job_id).external_job_id
        restarted_provider = AgentProvider(frozenset({"text_to_image"}))
        self.service = GenerationService(
            self.database,
            self.projects,
            ProviderRegistry((restarted_provider,)),
            self.staging_root,
            credentials=self.credentials,
            assets=self.assets,
            clock=self.clock,
        )

        polled = asyncio.run(self.service.run_once("post-restart-poll"))
        assert polled is not None
        package = self.service.agent_package(
            self.alice,
            queued.project_id,
            queued.job_id,
            queued.project_revision,
        )
        handle = self.upload(self.alice)
        accepted = self.service.submit_agent_asset(
            self.alice,
            queued.job_id,
            handle.asset_id,
            queued.project_revision,
        )

        self.assertEqual(JobState.POLLING, polled.state)
        self.assertEqual(external_job_id, polled.external_job_id)
        self.assertEqual(queued.request.job_id, package["job_id"])
        self.assertEqual(queued.request.prompt, package["prompt"])
        self.assertEqual(JobState.ACCEPTED, accepted.state)
        self.assertEqual(1, len(self.projects.submissions))

    def test_agent_package_is_owner_project_revision_and_scope_authorized(self) -> None:
        queued = self.queue_agent()

        package = self.service.agent_package(
            self.alice,
            queued.project_id,
            queued.job_id,
            queued.project_revision,
        )

        self.assertEqual(queued.request.prompt, package["prompt"])
        self.assertEqual(
            {"height": queued.request.height, "width": queued.request.width},
            package["dimensions"],
        )
        self.assertEqual(
            {"id": queued.request.subject_id, "kind": queued.request.subject_kind},
            package["subject"],
        )
        self.assertEqual(queued.project_revision, package["project_revision"])
        self.assertEqual(agent_job_checksum(queued.request), package["job_checksum"])
        self.assertEqual(agent_locked_scope_digest(queued.request), package["locked_scope_digest"])
        rendered = json.dumps(package, sort_keys=True)
        for forbidden in ("provider_options", "/private/", "https://", "token", "cookie"):
            self.assertNotIn(forbidden, rendered)

        with self.assertRaises(GenerationUnavailableError):
            self.service.agent_package(
                self.bob,
                queued.project_id,
                queued.job_id,
                queued.project_revision,
            )
        with self.assertRaises(GenerationConflictError):
            self.service.agent_package(
                self.alice,
                "project-other",
                queued.job_id,
                queued.project_revision,
            )
        self.projects.revision += 1
        with self.assertRaises(GenerationConflictError):
            self.service.agent_package(
                self.alice,
                queued.project_id,
                queued.job_id,
                queued.project_revision,
            )

    def test_submission_validates_owner_project_revision_and_locked_scope_first(self) -> None:
        wrong_owner = self.queue_agent()
        owner_handle = self.upload(self.alice)
        with self.assertRaises(GenerationUnavailableError):
            self.service.submit_agent_asset(
                self.bob,
                wrong_owner.job_id,
                owner_handle.asset_id,
                wrong_owner.project_revision,
            )

        self.projects.request_ids = ("b" * 64,)
        wrong_project = self.queue_agent()
        project_handle = self.upload(self.alice)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE generation_jobs SET project_id = ? WHERE job_id = ?",
                ("project-other", wrong_project.job_id),
            )
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice,
                wrong_project.job_id,
                project_handle.asset_id,
                wrong_project.project_revision,
            )

        self.projects.request_ids = ("c" * 64,)
        stale_revision = self.queue_agent()
        revision_handle = self.upload(self.alice)
        self.projects.revision += 1
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice,
                stale_revision.job_id,
                revision_handle.asset_id,
                stale_revision.project_revision,
            )
        self.projects.revision -= 1

        self.projects.request_ids = ("d" * 64,)
        stale_scope = self.queue_agent()
        scope_handle = self.upload(self.alice)
        self.projects.prompt = "changed locked scope"
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice,
                stale_scope.job_id,
                scope_handle.asset_id,
                stale_scope.project_revision,
            )

        for job_id in (
            wrong_owner.job_id,
            wrong_project.job_id,
            stale_revision.job_id,
            stale_scope.job_id,
        ):
            self.assertEqual(JobState.POLLING, self.service.get(self.alice, job_id).state)
        self.assertEqual([], self.projects.submissions)

        with self.assertRaises(GenerationUnavailableError):
            self.service.submit_agent_asset(self.alice, "0" * 64, "A" * 32, 3)

    def test_submission_refuses_nonopaque_handles_and_invalid_rasters_before_wp3(self) -> None:
        queued = self.queue_agent()
        for asset_id in (
            "../outside.png",
            "/etc/passwd",
            "C:\\Windows\\system.ini",
            "https://example.test/image.png",
            "file:///tmp/image.png",
        ):
            with self.subTest(asset_id=asset_id), self.assertRaises(GenerationConflictError):
                self.service.submit_agent_asset(
                    self.alice, queued.job_id, asset_id, queued.project_revision
                )
        self.assertEqual(JobState.POLLING, self.service.get(self.alice, queued.job_id).state)

        wrong_dimensions = self.upload(self.alice, bounded_png(2, 1))
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice,
                queued.job_id,
                wrong_dimensions.asset_id,
                queued.project_revision,
            )

        malformed = self.upload(self.alice, bounded_png())
        owner = self.assets.owner_storage_id(self.alice)
        malformed_path = self.assets.assets_root / owner / f"{malformed.asset_id}.png"
        malformed_bytes = b"x" * malformed.byte_size
        malformed_path.write_bytes(malformed_bytes)
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice, queued.job_id, malformed.asset_id, queued.project_revision
            )

        bomb = self.upload(self.alice, bounded_png(channel=3))
        bomb_bytes = bounded_png(channel=3, trailing_decompressed=5000)
        bomb_path = self.assets.assets_root / owner / f"{bomb.asset_id}.png"
        bomb_path.write_bytes(bomb_bytes)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assets SET byte_size = ? WHERE asset_id = ?",
                (len(bomb_bytes), bomb.asset_id),
            )
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice, queued.job_id, bomb.asset_id, queued.project_revision
            )

        wrong_mime = self.upload(self.alice, bounded_png(channel=1))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assets SET media_type = 'image/jpeg' WHERE asset_id = ?",
                (wrong_mime.asset_id,),
            )
        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice, queued.job_id, wrong_mime.asset_id, queued.project_revision
            )

        self.assertEqual(JobState.POLLING, self.service.get(self.alice, queued.job_id).state)
        self.assertEqual([], self.projects.submissions)
        self.assertEqual(2, len(self.service.attempts(queued.job_id)))
        self.assertEqual(0, len(self.service.receipts(queued.job_id)))

    def test_duplicate_checksum_mismatch_preserves_last_accepted_raster(self) -> None:
        queued = self.queue_agent()
        first = self.upload(self.alice, bounded_png(channel=1))
        accepted = self.service.submit_agent_asset(
            self.alice, queued.job_id, first.asset_id, queued.project_revision
        )
        accepted_bytes = self.projects.accepted_raster
        second = self.upload(self.alice, bounded_png(channel=2))

        with self.assertRaises(GenerationConflictError):
            self.service.submit_agent_asset(
                self.alice, queued.job_id, second.asset_id, queued.project_revision
            )

        self.assertEqual(JobState.ACCEPTED, accepted.state)
        self.assertEqual(accepted_bytes, self.projects.accepted_raster)
        self.assertEqual(1, len(self.projects.submissions))
        self.assertEqual(1, len(self.service.receipts(queued.job_id)))

    def test_concurrent_duplicate_submission_has_one_canonical_promotion(self) -> None:
        queued = self.queue_agent()
        handle = self.upload(self.alice)
        rendezvous = threading.Barrier(2)
        results: list[GenerationJob] = []
        errors: list[BaseException] = []

        def submit() -> None:
            try:
                rendezvous.wait(timeout=5)
                results.append(
                    self.service.submit_agent_asset(
                        self.alice,
                        queued.job_id,
                        handle.asset_id,
                        queued.project_revision,
                    )
                )
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertTrue(results)
        self.assertTrue(all(item.state is JobState.ACCEPTED for item in results))
        self.assertTrue(all(isinstance(error, GenerationConflictError) for error in errors))
        self.assertEqual(JobState.ACCEPTED, self.service.get(self.alice, queued.job_id).state)
        self.assertEqual(1, len(self.projects.submissions))
        self.assertEqual(1, len(self.service.receipts(queued.job_id)))


def make_request(
    *,
    project_revision: int,
    job_id: str = "engine-job-1",
    subject_id: str = "panel-1",
    fixture: str = "success",
    prompt: str = "private generation prompt",
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id=job_id,
        project_id="project-1",
        project_revision=project_revision,
        subject_kind="panel",
        subject_id=subject_id,
        prompt=prompt,
        negative_prompt=None,
        references=(),
        width=width,
        height=height,
        required_capabilities=frozenset({"text_to_image"}),
        provider_options={"fixture": fixture},
    )


if __name__ == "__main__":
    unittest.main()
