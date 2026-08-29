"""RED -> GREEN contracts for Assisted routing and confirmed provider switches."""

from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.api.approvals import create_approvals_router
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import EngineGateway
from comic_sol_web.generation.approvals import (
    ApprovalConflictError,
    ApprovalRequestError,
    ApprovalUnavailableError,
    ProviderSwitchApprovals,
    SwitchProposal,
    approve,
    propose_switch,
    reject,
)
from comic_sol_web.generation.queue import DurableGenerationQueue
from comic_sol_web.generation.router import RouterRecommendation, recommend
from comic_sol_web.generation.store import GenerationStore
from comic_sol_web.generation.types import AuthMode, ErrorCategory, GenerationRequest, JobState
from comic_sol_web.migrations import (
    APPROVAL_MIGRATIONS,
    PROVIDER_SWITCH_PROPOSAL_MIGRATION,
    apply_migrations,
)


class MutableClock:
    def __init__(self, value: int = 2_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class AssistedRouterTests(unittest.TestCase):
    def request(
        self,
        *,
        capabilities: frozenset[str] = frozenset({"text_to_image"}),
        references: tuple[Path, ...] = (),
    ) -> GenerationRequest:
        return GenerationRequest(
            job_id="engine-job-1",
            project_id="project-1",
            project_revision=3,
            subject_kind="panel",
            subject_id="panel-1",
            prompt="private story prompt",
            negative_prompt=None,
            references=references,
            width=1024,
            height=1024,
            required_capabilities=capabilities,
        )

    def test_required_capabilities_and_reference_images_filter_candidates(self) -> None:
        credentials = {
            "openai": (AuthMode.BYOK,),
            "google": (AuthMode.HOSTED,),
            "bfl": (AuthMode.HOSTED,),
        }
        recommendations = recommend(
            self.request(
                capabilities=frozenset({"text_to_image", "image_to_image"}),
                references=(Path("reference.png"),),
            ),
            credentials,
            {},
        )
        self.assertEqual(["google", "openai"], sorted(item.provider for item in recommendations))
        self.assertTrue(
            all(
                any("reference_images" in reason for reason in item.reasons)
                for item in recommendations
            )
        )

    def test_missing_credentials_are_filtered_without_inventing_auth(self) -> None:
        recommendations = recommend(
            self.request(),
            {"google": (AuthMode.BYOK,)},
            {},
        )
        self.assertEqual(1, len(recommendations))
        self.assertEqual(
            ("google", AuthMode.BYOK), (recommendations[0].provider, recommendations[0].auth_mode)
        )

    def test_ranking_and_identifier_tie_breaks_are_stable_across_input_order(self) -> None:
        first = recommend(
            self.request(),
            {
                "google": (AuthMode.HOSTED,),
                "bfl": (AuthMode.HOSTED,),
                "openai": (AuthMode.HOSTED,),
            },
            {},
        )
        second = recommend(
            self.request(),
            {
                "openai": (AuthMode.HOSTED,),
                "bfl": (AuthMode.HOSTED,),
                "google": (AuthMode.HOSTED,),
            },
            {},
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [("bfl", "flux-1.1-pro"), ("google", "gemini-2.5-flash-image")],
            [(item.provider, item.model) for item in first[:2]],
        )

    def test_reasons_disclose_each_ranking_dimension_and_unknown_cost_honestly(self) -> None:
        recommendation = recommend(
            self.request(),
            {"bfl": (AuthMode.HOSTED,)},
            {("bfl", "flux-1.1-pro"): {"available": True}},
        )[0]
        rendered = " ".join(recommendation.reasons).lower()
        for expected in (
            "capability",
            "reference",
            "hosted",
            "available",
            "cost",
            "unknown",
            "latency",
        ):
            self.assertIn(expected, rendered)
        self.assertIsNone(recommendation.estimated_cost)
        self.assertEqual(
            {"provider", "model", "auth_mode", "reasons", "estimated_cost"},
            {field.name for field in dataclasses.fields(RouterRecommendation)},
        )

    def test_cost_and_latency_use_only_supplied_observations(self) -> None:
        recommendations = recommend(
            self.request(),
            {"bfl": AuthMode.HOSTED, "google": AuthMode.HOSTED},
            {
                ("bfl", "flux-1.1-pro"): {
                    "available": True,
                    "estimated_cost": {"amount": 0.04, "currency": "USD", "unit": "image"},
                    "latency_class": "high",
                },
                ("google", "gemini-2.5-flash-image"): {
                    "available": True,
                    "estimated_cost": {"amount": 0.02, "currency": "USD", "unit": "image"},
                    "latency_class": "low",
                },
            },
        )
        self.assertEqual("google", recommendations[0].provider)
        self.assertEqual(
            {"amount": 0.02, "currency": "USD", "unit": "image"},
            dict(recommendations[0].estimated_cost or {}),
        )

    def test_quota_rate_and_unavailable_history_routes_away_without_fabrication(self) -> None:
        for category in (
            ErrorCategory.QUOTA_EXHAUSTED,
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.UNAVAILABLE,
        ):
            with self.subTest(category=category):
                recommendations = recommend(
                    self.request(),
                    {"bfl": AuthMode.HOSTED, "google": AuthMode.HOSTED},
                    {
                        ("bfl", "flux-1.1-pro"): {
                            "last_error": category,
                            "estimated_cost": {"amount": 0.001, "currency": "USD", "unit": "image"},
                        },
                        ("google", "gemini-2.5-flash-image"): {"available": True},
                    },
                )
                self.assertEqual("google", recommendations[0].provider)
                self.assertIn(category.value, " ".join(recommendations[-1].reasons))

    def test_hard_availability_precedes_capability_excess_across_providers(self) -> None:
        recommendations = recommend(
            self.request(),
            {"cloudflare": AuthMode.HOSTED, "google": AuthMode.HOSTED},
            {
                ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"): {
                    "last_error": ErrorCategory.UNAVAILABLE,
                },
                ("google", "gemini-2.5-flash-image"): {"available": True},
            },
        )

        self.assertEqual("google", recommendations[0].provider)
        self.assertEqual("cloudflare", recommendations[-1].provider)

    def test_availability_cost_and_latency_precede_auth_mode_tie_breaker(self) -> None:
        recommendations = recommend(
            self.request(),
            {"bfl": AuthMode.BYOK, "google": AuthMode.HOSTED},
            {
                ("bfl", "flux-1.1-pro"): {
                    "available": False,
                    "estimated_cost": {"amount": 0.01, "currency": "USD", "unit": "image"},
                    "latency_class": "low",
                },
                ("google", "gemini-2.5-flash-image"): {
                    "available": True,
                    "estimated_cost": {"amount": 0.02, "currency": "USD", "unit": "image"},
                    "latency_class": "high",
                },
            },
        )

        self.assertEqual(
            ("google", AuthMode.HOSTED),
            (
                recommendations[0].provider,
                recommendations[0].auth_mode,
            ),
        )


class FakeAuth:
    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal
        self.csrf_checks = 0

    def require_csrf(self, _request: object) -> SessionPrincipal:
        self.csrf_checks += 1
        return self.principal


def write_headers(revision: int = 3, *, key: str | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": key or str(uuid.uuid4()),
        "X-Expected-Revision": str(revision),
    }


class ProviderSwitchFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.database = Database(self.root / "application.sqlite3", timeout_seconds=10)
        apply_migrations(self.database, APPROVAL_MIGRATIONS)
        self.clock = MutableClock()
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")
        with self.database.transaction() as connection:
            connection.executemany(
                "INSERT INTO users (user_id, login, updated_at) VALUES (?, ?, ?)",
                (("alice-id", "alice", 1), ("bob-id", "bob", 1)),
            )
            connection.execute(
                "INSERT INTO web_projects (project_id, owner_id, storage_name, revision, engine_state) "
                "VALUES (?, ?, ?, ?, ?)",
                ("project-1", "alice-id", "project-storage", 3, "STORYBOARDED"),
            )
        self.store = GenerationStore(self.database, self.staging)
        self.approvals = ProviderSwitchApprovals(self.database, clock=self.clock)
        self.recommendation = RouterRecommendation(
            provider="google",
            model="gemini-2.5-flash-image",
            auth_mode=AuthMode.HOSTED,
            reasons=("Observed provider is available.",),
            estimated_cost=None,
        )

    def enqueue(self, suffix: str = "1", *, state: JobState = JobState.FAILED):
        request = GenerationRequest(
            job_id=f"engine-job-{suffix}",
            project_id="project-1",
            project_revision=3,
            subject_kind="panel",
            subject_id=f"panel-{suffix}",
            prompt="private story prompt",
            negative_prompt=None,
            references=(),
            width=1024,
            height=1024,
            required_capabilities=frozenset({"text_to_image"}),
        )
        job = self.store.enqueue(
            owner_id=self.alice.user_id,
            request=request,
            provider="bfl",
            model="flux-1.1-pro",
            auth_mode=AuthMode.HOSTED,
            max_retries=2,
            now=self.clock(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE generation_jobs SET state = ? WHERE job_id = ?",
                (state.value, job.job_id),
            )
        current = self.store.get(job.job_id)
        assert current is not None
        return current

    def propose(self, *jobs, ttl_seconds: int = 60, key: str | None = None) -> SwitchProposal:
        return propose_switch(
            self.approvals,
            self.alice,
            "project-1",
            3,
            tuple(job.job_id for job in jobs),
            self.recommendation,
            ErrorCategory.QUOTA_EXHAUSTED,
            idempotency_key=key or str(uuid.uuid4()),
            ttl_seconds=ttl_seconds,
        )

    def row(self, job_id: str) -> sqlite3.Row:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return row


class ProviderSwitchApprovalTests(ProviderSwitchFixture):
    def test_proposal_is_immutable_bound_and_pauses_exact_jobs(self) -> None:
        first = self.enqueue("1")
        second = self.enqueue("2")
        proposal = self.propose(second, first)
        self.assertEqual(tuple(sorted((first.job_id, second.job_id))), proposal.job_ids)
        self.assertEqual("project-1", proposal.project_id)
        self.assertEqual(3, proposal.project_revision)
        self.assertEqual("bfl", proposal.from_provider)
        self.assertEqual("google", proposal.to_provider)
        self.assertEqual("gemini-2.5-flash-image", proposal.to_model)
        self.assertEqual(ErrorCategory.QUOTA_EXHAUSTED, proposal.reason)
        self.assertEqual(
            {
                "proposal_id",
                "job_ids",
                "project_id",
                "project_revision",
                "from_provider",
                "to_provider",
                "to_model",
                "reason",
                "expires_at",
            },
            {field.name for field in dataclasses.fields(SwitchProposal)},
        )
        self.assertEqual(
            {JobState.AWAITING_PROVIDER_CONFIRMATION.value},
            {self.row(job.job_id)["state"] for job in (first, second)},
        )
        with self.database.transaction() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE provider_switch_proposals SET to_provider = 'openai' WHERE proposal_id = ?",
                    (proposal.proposal_id,),
                )

    def test_proposal_creation_is_idempotent_only_for_the_exact_bound_request(self) -> None:
        job = self.enqueue()
        key = str(uuid.uuid4())
        first = self.propose(job, key=key)
        repeated = self.propose(job, key=key)
        self.assertEqual(first, repeated)
        with self.database.read() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM provider_switch_proposals").fetchone()[0],
            )
        different = RouterRecommendation(
            provider="openai",
            model="gpt-image-1",
            auth_mode=AuthMode.BYOK,
            reasons=("Different server-side recommendation.",),
            estimated_cost=None,
        )
        with self.assertRaises(ApprovalConflictError):
            propose_switch(
                self.approvals,
                self.alice,
                "project-1",
                3,
                (job.job_id,),
                different,
                ErrorCategory.QUOTA_EXHAUSTED,
                idempotency_key=key,
            )

    def test_mixed_type_job_ids_fail_with_a_sanitized_request_error(self) -> None:
        job = self.enqueue()
        mixed_job_ids = cast(tuple[str, ...], (job.job_id, 7))

        with self.assertRaisesRegex(ApprovalRequestError, "provider switch request is invalid"):
            propose_switch(
                self.approvals,
                self.alice,
                "project-1",
                3,
                mixed_job_ids,
                self.recommendation,
                ErrorCategory.QUOTA_EXHAUSTED,
                idempotency_key=str(uuid.uuid4()),
            )

    def test_same_provider_bounded_retry_remains_the_wp5_transition(self) -> None:
        job = self.enqueue()
        queue = DurableGenerationQueue(self.store, self.clock)
        retried = queue.retry_same_provider(self.alice.user_id, job.job_id, 3)
        self.assertEqual(JobState.QUEUED, retried.state)
        self.assertEqual(("bfl", "flux-1.1-pro"), (retried.provider, retried.model))

    def test_approval_uses_only_proposal_destination_and_is_one_shot(self) -> None:
        job = self.enqueue()
        proposal = self.propose(job)
        approved = approve(
            self.approvals,
            self.alice,
            proposal.proposal_id,
            expected_revision=3,
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertEqual(proposal, approved)
        row = self.row(job.job_id)
        self.assertEqual(JobState.QUEUED.value, row["state"])
        self.assertEqual(
            ("google", "gemini-2.5-flash-image", "hosted"),
            tuple(row[key] for key in ("provider", "model", "auth_mode")),
        )
        with self.assertRaises(ApprovalConflictError):
            approve(
                self.approvals,
                self.alice,
                proposal.proposal_id,
                expected_revision=3,
                idempotency_key=str(uuid.uuid4()),
            )

    def test_identical_decision_replay_returns_the_original_result(self) -> None:
        for suffix, operation, opposite in (
            ("approve-replay", approve, reject),
            ("reject-replay", reject, approve),
        ):
            with self.subTest(operation=operation.__name__):
                job = self.enqueue(suffix)
                proposal = self.propose(job)
                key = str(uuid.uuid4())

                first = operation(
                    self.approvals,
                    self.alice,
                    proposal.proposal_id,
                    expected_revision=3,
                    idempotency_key=key,
                )
                replayed = operation(
                    self.approvals,
                    self.alice,
                    proposal.proposal_id,
                    expected_revision=3,
                    idempotency_key=key,
                )

                self.assertEqual(first, replayed)
                with self.assertRaises(ApprovalConflictError):
                    operation(
                        self.approvals,
                        self.alice,
                        proposal.proposal_id,
                        expected_revision=3,
                        idempotency_key=str(uuid.uuid4()),
                    )
                with self.assertRaises(ApprovalConflictError):
                    opposite(
                        self.approvals,
                        self.alice,
                        proposal.proposal_id,
                        expected_revision=3,
                        idempotency_key=key,
                    )

    def test_expiry_atomically_pauses_exact_jobs_and_allows_replacement(self) -> None:
        job = self.enqueue("expiry-release")
        expired = self.propose(job, ttl_seconds=1)
        self.clock.value += 1

        with self.assertRaisesRegex(ApprovalConflictError, "proposal expired"):
            approve(
                self.approvals,
                self.alice,
                expired.proposal_id,
                expected_revision=3,
                idempotency_key=str(uuid.uuid4()),
            )

        self.assertEqual(JobState.PAUSED.value, self.row(job.job_id)["state"])
        with self.database.read() as connection:
            decision = connection.execute(
                "SELECT decision FROM provider_switch_decisions WHERE proposal_id = ?",
                (expired.proposal_id,),
            ).fetchone()
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual("expired", decision["decision"])

        replacement = self.propose(job, key=str(uuid.uuid4()))
        self.assertNotEqual(expired.proposal_id, replacement.proposal_id)
        self.assertEqual(
            JobState.AWAITING_PROVIDER_CONFIRMATION.value,
            self.row(job.job_id)["state"],
        )

    def test_stale_wrong_user_and_tampered_job_set_fail_closed(self) -> None:
        stale_job = self.enqueue("stale")
        stale = self.propose(stale_job)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE web_projects SET revision = 4 WHERE project_id = 'project-1'"
            )
        with self.assertRaises(ApprovalConflictError):
            reject(
                self.approvals,
                self.alice,
                stale.proposal_id,
                expected_revision=3,
                idempotency_key=str(uuid.uuid4()),
            )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE web_projects SET revision = 3 WHERE project_id = 'project-1'"
            )

        wrong_user_job = self.enqueue("owner")
        wrong_user = self.propose(wrong_user_job)
        with self.assertRaises(ApprovalUnavailableError):
            approve(
                self.approvals,
                self.bob,
                wrong_user.proposal_id,
                expected_revision=3,
                idempotency_key=str(uuid.uuid4()),
            )

        tampered_job = self.enqueue("tampered")
        tampered = self.propose(tampered_job)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE generation_jobs SET project_id = 'wrong-project' WHERE job_id = ?",
                (tampered_job.job_id,),
            )
        with self.assertRaises(ApprovalConflictError):
            reject(
                self.approvals,
                self.alice,
                tampered.proposal_id,
                expected_revision=3,
                idempotency_key=str(uuid.uuid4()),
            )

    def test_rejection_preserves_attempts_receipts_staged_data_and_accepted_assets(self) -> None:
        accepted = self.enqueue("accepted", state=JobState.ACCEPTED)
        switching = self.enqueue("switching")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE generation_jobs SET result_checksum = ?, accepted_project_revision = ? "
                "WHERE job_id = ?",
                ("a" * 64, 3, accepted.job_id),
            )
            connection.execute(
                "UPDATE generation_jobs SET staged_raster_name = ?, result_checksum = ? WHERE job_id = ?",
                ("retained.png", "b" * 64, switching.job_id),
            )
            accepted_row = connection.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (accepted.job_id,)
            ).fetchone()
            assert accepted_row is not None
            self.store.append_attempt(connection, accepted_row, JobState.ACCEPTED, self.clock())
            self.store.append_receipt(
                connection, accepted_row, {"images": 1}, "a" * 64, self.clock()
            )
        with self.database.read() as connection:
            accepted_before = tuple(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE job_id = ?", (accepted.job_id,)
                ).fetchone()
            )
            attempts_before = connection.execute(
                "SELECT COUNT(*) FROM generation_attempts"
            ).fetchone()[0]
            receipts_before = connection.execute(
                "SELECT COUNT(*) FROM generation_receipts"
            ).fetchone()[0]

        proposal = self.propose(switching)
        rejected = reject(
            self.approvals,
            self.alice,
            proposal.proposal_id,
            expected_revision=3,
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertEqual(proposal, rejected)
        row = self.row(switching.job_id)
        self.assertEqual(JobState.PAUSED.value, row["state"])
        self.assertEqual("retained.png", row["staged_raster_name"])
        self.assertEqual("b" * 64, row["result_checksum"])
        with self.database.read() as connection:
            accepted_after = tuple(
                connection.execute(
                    "SELECT * FROM generation_jobs WHERE job_id = ?", (accepted.job_id,)
                ).fetchone()
            )
            attempts_after = connection.execute(
                "SELECT COUNT(*) FROM generation_attempts"
            ).fetchone()[0]
            receipts_after = connection.execute(
                "SELECT COUNT(*) FROM generation_receipts"
            ).fetchone()[0]
        self.assertEqual(accepted_before, accepted_after)
        self.assertGreater(attempts_after, attempts_before)
        self.assertEqual(receipts_before, receipts_after)

    def test_concurrent_approval_and_rejection_have_exactly_one_winner(self) -> None:
        job = self.enqueue()
        proposal = self.propose(job)
        barrier = threading.Barrier(2)
        winners: list[str] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def decide(name: str) -> None:
            try:
                barrier.wait()
                operation = approve if name == "approved" else reject
                operation(
                    self.approvals,
                    self.alice,
                    proposal.proposal_id,
                    expected_revision=3,
                    idempotency_key=str(uuid.uuid4()),
                )
            except BaseException as error:  # noqa: BLE001 - exact race outcome asserted below.
                with lock:
                    failures.append(error)
            else:
                with lock:
                    winners.append(name)

        workers = [
            threading.Thread(target=decide, args=(name,)) for name in ("approved", "rejected")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(1, len(winners))
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], ApprovalConflictError)
        with self.database.read() as connection:
            decisions = connection.execute(
                "SELECT decision FROM provider_switch_decisions WHERE proposal_id = ?",
                (proposal.proposal_id,),
            ).fetchall()
        self.assertEqual([(winners[0],)], [tuple(row) for row in decisions])
        expected_state = JobState.QUEUED if winners[0] == "approved" else JobState.PAUSED
        self.assertEqual(expected_state.value, self.row(job.job_id)["state"])


class ApprovalMigrationTests(unittest.TestCase):
    def test_engine_gateway_then_approval_migrations_are_contiguous(self) -> None:
        self.assertEqual(8, PROVIDER_SWITCH_PROPOSAL_MIGRATION.version)
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "application-data"
            gateway = EngineGateway.open(data_root)
            ProviderSwitchApprovals(gateway.database)
            self.assertEqual((), apply_migrations(gateway.database, APPROVAL_MIGRATIONS))
            with gateway.database.read() as connection:
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(tuple(range(1, 9)), versions)
            self.assertIn("generation_jobs", tables)
            self.assertIn("generation_attempts", tables)
            self.assertIn("generation_receipts", tables)
            self.assertIn("provider_switch_proposals", tables)
            self.assertIn("provider_switch_decisions", tables)


class ApprovalApiTests(ProviderSwitchFixture):
    def app_for(self, principal: SessionPrincipal) -> tuple[FastAPI, FakeAuth]:
        app = FastAPI()
        auth = FakeAuth(principal)
        app.state.auth = auth
        app.include_router(create_approvals_router(self.approvals))
        app.dependency_overrides[require_principal] = lambda: principal
        return app, auth

    def test_approval_api_requires_csrf_revision_idempotency_and_only_proposal_id(self) -> None:
        job = self.enqueue()
        proposal = self.propose(job)
        app, auth = self.app_for(self.alice)
        with TestClient(app) as client:
            missing_headers = client.post(f"/api/approvals/{proposal.proposal_id}/approve", json={})
            injected = client.post(
                f"/api/approvals/{proposal.proposal_id}/approve",
                json={"provider": "openai", "model": "attacker-selected"},
                headers=write_headers(),
            )
            approved = client.post(
                f"/api/approvals/{proposal.proposal_id}/approve",
                headers=write_headers(),
            )
        self.assertEqual(400, missing_headers.status_code)
        self.assertEqual(400, injected.status_code)
        self.assertEqual(JobState.QUEUED.value, self.row(job.job_id)["state"])
        self.assertEqual(200, approved.status_code)
        self.assertEqual(proposal.proposal_id, approved.json()["proposal_id"])
        self.assertEqual("approved", approved.json()["decision"])
        self.assertEqual(3, auth.csrf_checks)

    def test_public_errors_are_sanitized(self) -> None:
        app, auth = self.app_for(self.alice)
        secret = "credential-canary-prompt-private-private-path-raw-provider-response"
        with TestClient(app) as client:
            response = client.post(
                f"/api/approvals/{secret}/reject",
                json={},
                headers=write_headers(),
            )
        self.assertEqual(404, response.status_code)
        self.assertEqual(1, auth.csrf_checks)
        rendered = response.text.lower()
        for forbidden in ("credential", "prompt", "private", "raw-provider"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
