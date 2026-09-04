"""Offline durable planning, publication, and HTTP security coverage."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from comic_sol_web.app import create_app
from comic_sol_web.config import WebConfig
from comic_sol_web.engine_gateway import ProjectUnavailableError, StaleProjectRevisionError
from comic_sol_web.generation.credentials import CredentialBroker, UnknownProviderError
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.types import ErrorCategory
from comic_sol_web.planning.service import PlanningConflictError, PlanningService
from comic_sol_web.planning.types import PlanResult
from web.tests.test_projects import GatewayFixture, first_plan_payload, tree_snapshot


class FakePlanner:
    provider_id = "openai"
    model = "gpt-5.4-mini"

    def __init__(self):
        self.results = [PlanResult(first_plan_payload(), {"input_tokens": 12})]
        self.requests = []

    async def generate_plan(self, request, credential):
        self.requests.append(request)
        assert credential == "private-test-credential"
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def invalid_result():
    return PlanResult({key: "{}" for key in first_plan_payload()}, {"input_tokens": 3})


class PlanningServiceTests(GatewayFixture):
    def setUp(self):
        super().setUp()
        self.projects = self.service
        self.created = self.create()
        self.provider = FakePlanner()
        self.now = 1000
        self.credentials = CredentialBroker(
            self.database,
            deployment_environment={"TEST_KEY": "private-test-credential"},
            hosted_secret_references={"openai": "TEST_KEY", "anthropic": "TEST_KEY"},
        )
        self.planning = self.make_service()

    def make_service(self):
        return PlanningService(
            self.database,
            self.projects,
            (self.provider,),
            self.credentials,
            clock=lambda: self.now,
            lease_seconds=120,
        )

    def queue(self, key=None):
        return self.planning.queue(
            self.alice,
            self.created.project_id,
            self.created.revision,
            self.provider.provider_id,
            self.provider.model,
            key or str(uuid4()),
        )

    def run_job(self):
        return asyncio.run(self.planning.run_once("test-worker"))

    def test_input_is_bounded_read_only_and_owner_revision_bound(self):
        before = tree_snapshot(self.created.root)
        request = self.projects.planning_input(self.alice, self.created.project_id, 1)
        self.assertEqual("Gateway Story", request.title)
        self.assertEqual("en", request.language)
        self.assertEqual(1, request.page_count)
        self.assertNotIn(str(self.created.root), repr(request))
        self.assertEqual(before, tree_snapshot(self.created.root))
        with self.assertRaises(ProjectUnavailableError):
            self.projects.planning_input(self.bob, self.created.project_id, 1)
        with self.assertRaises(StaleProjectRevisionError):
            self.projects.planning_input(self.alice, self.created.project_id, 2)
        source = self.created.root / "source/input.txt"
        with patch("comic_sol_web.engine_gateway.read_contained_bytes", return_value=b"\xff"):
            with self.assertRaises(ValueError):
                self.projects.planning_input(self.alice, self.created.project_id, 1)
        self.assertTrue(source.is_file())

    def test_invalid_first_plan_is_repaired_once_and_published_atomically(self):
        self.provider.results.insert(0, invalid_result())
        job = self.queue()
        completed = self.run_job()
        self.assertEqual("ready_for_review", completed.state)
        self.assertEqual(2, completed.attempt_count)
        self.assertEqual(2, completed.published_revision)
        self.assertEqual({"input_tokens": 15}, dict(completed.usage))
        self.assertFalse(self.provider.requests[0].validation_errors)
        self.assertTrue(self.provider.requests[1].validation_errors)
        self.assertEqual(job.job_id, completed.job_id)
        self.assertTrue(
            all(self.projects.read_plan(self.alice, job.project_id).summary["plan"].values())
        )

    def test_second_invalid_plan_fails_without_partial_plan(self):
        self.provider.results = [invalid_result(), invalid_result()]
        self.queue()
        before = tree_snapshot(self.created.root)
        completed = self.run_job()
        self.assertEqual("failed", completed.state)
        self.assertEqual("invalid_output", completed.error_category)
        self.assertEqual(2, completed.attempt_count)
        self.assertEqual(before, tree_snapshot(self.created.root))
        self.assertTrue(
            all(
                value == ""
                for value in self.projects.read_plan(self.alice, self.created.project_id)
                .summary["plan"]
                .values()
            )
        )

    def test_owner_idempotency_and_revision_guards(self):
        key = str(uuid4())
        job = self.queue(key)
        self.assertEqual(job, self.queue(key))
        with self.assertRaises(ProjectUnavailableError):
            self.planning.get(self.bob, job.job_id)
        with self.assertRaises(ValueError):
            self.queue("not-a-uuid")
        with self.assertRaises(ValueError):
            self.planning.queue(self.alice, job.project_id, 1, "openai", "other", str(uuid4()))
        with self.assertRaises(PlanningConflictError):
            self.queue()
        self.run_job()
        self.assertEqual(job.job_id, self.queue(key).job_id)
        with self.assertRaises(StaleProjectRevisionError):
            self.queue()

    def test_expired_lease_is_reclaimed_but_live_lease_is_not(self):
        job = self.queue()
        leased = self.planning._lease("crashed-worker")
        self.assertEqual(job.job_id, leased.job_id)
        self.assertIsNone(self.run_job())
        self.now += 121
        self.planning = self.make_service()
        self.assertEqual("ready_for_review", self.run_job().state)

    def test_late_worker_cannot_publish_after_lease_is_reclaimed(self):
        self.queue()
        original = self.provider.generate_plan

        async def delayed(request, credential):
            result = await original(request, credential)
            self.now += 121
            self.planning._lease("replacement-worker")
            return result

        self.provider.generate_plan = delayed
        with self.assertRaises(PlanningConflictError):
            self.run_job()
        self.assertTrue(
            all(
                value == ""
                for value in self.projects.read_plan(self.alice, self.created.project_id)
                .summary["plan"]
                .values()
            )
        )

    def test_publication_crash_reconciles_without_another_provider_call(self):
        job = self.queue()
        with patch.object(self.planning, "_finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_job()
        self.now += 121
        self.planning = self.make_service()
        self.assertEqual("ready_for_review", self.run_job().state)
        self.assertEqual(1, len(self.provider.requests))
        self.assertEqual(2, self.planning.get(self.alice, job.job_id).published_revision)

    def test_two_interrupted_calls_exhaust_durable_attempt_budget(self):
        self.queue()

        async def crash(request, credential):
            self.provider.requests.append(request)
            raise KeyboardInterrupt

        self.provider.generate_plan = crash
        for _ in range(2):
            with self.assertRaises(KeyboardInterrupt):
                self.run_job()
            self.now += 121
            self.planning = self.make_service()
        self.assertEqual("failed", self.run_job().state)
        self.assertEqual(2, len(self.provider.requests))

    def test_source_rejects_oversize_and_invalid_utf8(self):
        source = self.created.root / "source/input.txt"
        for invalid_source in (b"x" * (200 * 1024 + 1), b"\xff"):
            source.write_bytes(invalid_source)
            with self.assertRaises(ValueError):
                self.projects.planning_input(self.alice, self.created.project_id, 1)

    def test_edit_during_provider_call_never_overwrites_the_user_plan(self):
        self.queue()
        original = self.provider.generate_plan

        async def edit_during_call(request, credential):
            self.projects.update_plan(self.alice, self.created.project_id, 1, first_plan_payload())
            return await original(request, credential)

        self.provider.generate_plan = edit_during_call
        result = self.run_job()
        self.assertEqual("failed", result.state)
        self.assertEqual("stale_revision", result.error_category)
        self.assertEqual(2, self.projects.read_plan(self.alice, self.created.project_id).revision)

    def test_unexpected_and_provider_errors_are_sanitized(self):
        for error, category in (
            (RuntimeError("private-story private-test-credential C:/private"), "provider_error"),
            (ProviderError(ErrorCategory.RATE_LIMITED), "rate_limited"),
        ):
            self.provider.results = [error]
            job = self.queue()
            completed = self.run_job()
            self.assertEqual(category, completed.error_category)
            with self.database.read() as connection:
                row = dict(
                    connection.execute(
                        "SELECT * FROM planning_jobs WHERE job_id = ?", (job.job_id,)
                    ).fetchone()
                )
            self.assertNotIn("private", json.dumps(row))

    def test_migration_is_contiguous_and_retains_no_raw_payload(self):
        with self.database.read() as connection:
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            columns = [row[1] for row in connection.execute("PRAGMA table_info(planning_jobs)")]
        self.assertEqual(list(range(1, 10)), versions)
        for forbidden in ("plan_json", "result_json", "response_json", "request_json"):
            self.assertNotIn(forbidden, columns)

    def test_anthropic_hosted_credential_is_resolved_but_unknown_is_denied(self):
        async def resolve(provider):
            async with self.credentials.resolve(self.alice.user_id, provider, "hosted") as value:
                return value

        self.assertEqual("private-test-credential", asyncio.run(resolve("anthropic")))
        with self.assertRaises(UnknownProviderError):
            asyncio.run(resolve("unknown"))
        with self.assertRaises(UnknownProviderError):
            self.credentials.store_session(self.alice.user_id, "anthropic", "another-key")

    def test_app_lazily_registers_planning_models_and_reuses_hosted_broker(self):
        environment = {
            "COMIC_SOL_WEB_DATA_ROOT": str(self.data_root),
            "OPENAI_API_KEY": "private-test-credential",
            "ANTHROPIC_API_KEY": "private-test-credential",
        }
        app = create_app(WebConfig.local_from_env(environment))
        self.assertFalse(hasattr(app.state, "planning"))
        with (
            patch.dict("os.environ", environment),
            TestClient(app, client=("127.0.0.1", 50001)) as client,
        ):
            self.assertEqual(200, client.get("/healthz").status_code)
            self.assertFalse(hasattr(app.state, "planning"))
            self.assertEqual(200, client.post("/api/auth/local-session").status_code)
            result = client.get("/api/planning/options")
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(
                {"openai", "anthropic"},
                {option["provider"] for option in result.json()["options"] if option["enabled"]},
            )
            self.assertIs(app.state.planning.credentials, app.state.generation_credentials)
            self.assertEqual(200, client.get("/api/generation/options").status_code)
            self.assertIs(app.state.planning.credentials, app.state.generation_credentials)
            self.assertNotIn("private-test-credential", result.text)

    def test_local_http_bootstrap_options_queue_poll_and_security(self):
        config = WebConfig.local_from_env({"COMIC_SOL_WEB_DATA_ROOT": str(self.data_root)})
        app = create_app(config)
        app.state.projects = self.projects
        app.state.planning = self.planning
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            self.assertEqual(401, client.get("/api/planning/options").status_code)
            self.assertEqual(200, client.post("/api/auth/local-session").status_code)
            auth = app.state.auth
            from comic_sol_web.auth import require_principal

            app.dependency_overrides[require_principal] = lambda: self.alice
            missing = client.get("/api/planning/jobs/" + str(uuid4()))
            self.assertEqual(404, missing.status_code)
            self.assertEqual("private, no-store", missing.headers.get("cache-control"))
            with patch.object(auth, "require_csrf", return_value=self.alice):
                options = client.get("/api/planning/options")
                self.assertEqual("private, no-store", options.headers["cache-control"])
                self.assertEqual("openai", options.json()["options"][0]["provider"])
                body = {
                    "project_id": self.created.project_id,
                    "expected_revision": 1,
                    "provider": "openai",
                    "model": self.provider.model,
                }
                rejected = client.post("/api/planning/jobs", json=body)
                self.assertEqual(400, rejected.status_code)
                self.assertEqual("private, no-store", rejected.headers.get("cache-control"))
                response = client.post(
                    "/api/planning/jobs", json=body, headers={"Idempotency-Key": str(uuid4())}
                )
                self.assertEqual(201, response.status_code, response.text)
                result = client.get("/api/planning/jobs/" + response.json()["job_id"])
                self.assertEqual("ready_for_review", result.json()["state"])
                self.assertEqual("private, no-store", result.headers["cache-control"])
                self.assertNotIn("lease", result.text)
                self.assertNotIn("private-test-credential", result.text)
                app.dependency_overrides[require_principal] = lambda: self.bob
                denied = client.get("/api/planning/jobs/" + response.json()["job_id"])
                self.assertEqual(404, denied.status_code)
                self.assertEqual("private, no-store", denied.headers.get("cache-control"))
            app.dependency_overrides[require_principal] = lambda: self.alice
            csrf_denied = client.post("/api/planning/jobs", json=body)
            self.assertEqual(403, csrf_denied.status_code)
            self.assertEqual("private, no-store", csrf_denied.headers.get("cache-control"))
