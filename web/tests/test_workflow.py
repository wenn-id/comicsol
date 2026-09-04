"""Offline canonical review and finalization boundary coverage."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import asyncio
from contextlib import asynccontextmanager
from unittest import mock

from comic_sol_web.engine_gateway import (
    EngineGateway,
    GatewayInputError,
    ProjectUnavailableError,
    StaleProjectRevisionError,
)
from comic_sol_web.planning.types import VisualReviewResult
from comic_sol_web.projects import ProjectService
from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.generation.types import AuthMode, JobState
from comic_sol_web.migrations import WORKFLOW_MIGRATIONS, apply_migrations
from comic_sol_web.workflow import WorkflowConflictError, WorkflowService
from scripts import comic_sol
from scripts.core_primitives import PANEL_CHECK_IDS
from scripts.project_io import ProjectTransaction
from scripts.reference_strategy import plan_and_write_reference_plan
from scripts.page_quality import SUBJECTIVE_PAGE_CHECK_IDS
from tests.test_finalization import valid_page_reviewer_checks
from web.tests.test_projects import (
    GatewayFixture,
    tree_snapshot,
)


def panel_review(request, *, failure=False):
    checks = tuple(
        {
            "id": check_id,
            "result": "fail" if failure and check_id == "anatomy" else "pass",
            "severity": "error",
            "evidence": f"Inspected {check_id} across the entire courier panel.",
            "method": "bounded-visual-review",
            "reviewer": "offline-fixture",
            "regions": [],
        }
        for check_id in request.check_ids
    )
    assessments = tuple(
        {
            "character_id": character["character_id"],
            "trait": trait["trait"],
            "result": "pass",
            "severity": "error",
            "evidence": f"Courier {trait['trait']} matches the specified appearance.",
        }
        for character in request.context["characters"]
        for trait in character["traits"]
    )
    return VisualReviewResult(checks, assessments, {})


class CanonicalWorkflowGatewayTests(GatewayFixture):
    def ready_panel(self):
        snapshot = self.accept_panel()[0]
        # The older handoff fixture predates registered planning artifacts.
        manifest = comic_sol.read_json(snapshot.root / "project.json")
        for name, path in (
            ("story_plan", "plan/story-plan.json"),
            ("character_bible", "plan/character-bible.json"),
            ("storyboard", "plan/storyboard.json"),
        ):
            manifest["artifacts"][name] = {
                "path": path,
                "sha256": comic_sol.sha256_file(snapshot.root / path),
            }
        with ProjectTransaction(snapshot.root, "fixture-planning-artifacts") as transaction:
            transaction.stage_bytes("project.json", comic_sol.canonical_artifact_bytes(manifest))
        comic_sol.record_stage(snapshot.root, "planning")
        comic_sol.record_stage(snapshot.root, "storyboard")
        self.gateway = EngineGateway.open(self.data_root)
        self.service = ProjectService(self.gateway)
        return self.service.snapshot(self.alice, snapshot.project_id)

    def reviewed_panel(self):
        snapshot = self.ready_panel()
        request = self.service.panel_review_input(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01"
        )
        return self.service.publish_panel_review(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01", panel_review(request)
        )

    def composed_page(self):
        snapshot = self.reviewed_panel()
        return self.service.prepare_pages(self.alice, snapshot.project_id, snapshot.revision)

    def test_real_panel_page_and_pdf_lifecycle(self):
        snapshot = self.ready_panel()
        original = tree_snapshot(snapshot.root)
        request = self.service.panel_review_input(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01"
        )
        self.assertEqual("panel", request.kind)
        self.assertEqual(PANEL_CHECK_IDS, request.check_ids)
        self.assertEqual(original, tree_snapshot(snapshot.root))
        self.assertEqual(
            snapshot.revision, self.service.snapshot(self.alice, snapshot.project_id).revision
        )
        reviewed = self.service.publish_panel_review(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01", panel_review(request)
        )
        self.assertEqual(snapshot.revision + 1, reviewed.revision)
        record = json.loads((reviewed.root / "qa/panels/p01-01.json").read_bytes())
        self.assertEqual("accept", record["decision"])
        self.assertEqual("QA_READY", reviewed.status)
        self.assertTrue((reviewed.root / "panels/p01-01/clean.png").is_file())
        composed = self.service.prepare_pages(self.alice, reviewed.project_id, reviewed.revision)
        self.assertEqual(reviewed.revision + 1, composed.revision)
        self.assertEqual("COMPOSED", composed.status)
        self.assertFalse((composed.root / "qa/pages/page-001.json").exists())
        with self.assertRaises(GatewayInputError):
            self.service.finalize(self.alice, composed.project_id, composed.revision)
        with (
            mock.patch("comic_sol_web.engine_gateway._letter_panels.letter_project") as letter,
            mock.patch("comic_sol_web.engine_gateway._compose_pages.compose_project") as compose,
        ):
            repeated = self.service.prepare_pages(
                self.alice, composed.project_id, composed.revision
            )
        letter.assert_not_called()
        compose.assert_not_called()
        self.assertEqual(composed.revision, repeated.revision)
        before = tree_snapshot(composed.root)
        page_request = self.service.page_review_input(
            self.alice, composed.project_id, composed.revision, 1
        )
        self.assertEqual("page", page_request.kind)
        self.assertEqual(SUBJECTIVE_PAGE_CHECK_IDS, page_request.check_ids)
        self.assertEqual(before, tree_snapshot(composed.root))
        page_reviewed = self.service.publish_page_review(
            self.alice,
            composed.project_id,
            composed.revision,
            1,
            VisualReviewResult(tuple(valid_page_reviewer_checks(composed.root, 1)), (), {}),
        )
        self.assertEqual(composed.revision + 1, page_reviewed.revision)
        final, pdf = self.service.finalize(
            self.alice, page_reviewed.project_id, page_reviewed.revision
        )
        self.assertEqual(page_reviewed.revision + 1, final.revision)
        self.assertEqual("COMPLETE", final.status)
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(final.root / "exports" / f"{final.root.name}.pdf", pdf)

    def test_ownership_revision_and_subject_checks(self):
        snapshot = self.ready_panel()
        with self.assertRaises(ProjectUnavailableError):
            self.service.panel_review_input(
                self.bob, snapshot.project_id, snapshot.revision, "p01-01"
            )
        with self.assertRaises(StaleProjectRevisionError):
            self.service.panel_review_input(
                self.alice, snapshot.project_id, snapshot.revision - 1, "p01-01"
            )
        for panel_id in ("p99-99", "../project"):
            with self.assertRaises(GatewayInputError):
                self.service.panel_review_input(
                    self.alice, snapshot.project_id, snapshot.revision, panel_id
                )
        with self.assertRaises(GatewayInputError):
            self.service.prepare_pages(self.alice, snapshot.project_id, snapshot.revision)

    def test_incomplete_or_generic_review_never_publishes(self):
        snapshot = self.ready_panel()
        request = self.service.panel_review_input(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01"
        )
        valid = panel_review(request)
        generic = [dict(check) for check in valid.checks]
        generic[2]["evidence"] = "looks good"
        overlong_warning = [dict(check) for check in valid.checks]
        overlong_warning[2].update(result="warning", evidence="Warning about visual anatomy. " * 30)
        for review in (
            VisualReviewResult(valid.checks, (), {}),
            VisualReviewResult(tuple(generic), valid.character_assessments, {}),
            VisualReviewResult(tuple(overlong_warning), valid.character_assessments, {}),
        ):
            before = tree_snapshot(snapshot.root)
            with self.assertRaises(ValueError):
                self.service.publish_panel_review(
                    self.alice, snapshot.project_id, snapshot.revision, "p01-01", review
                )
            self.assertEqual(before, tree_snapshot(snapshot.root))

    def test_failed_panel_retains_evidence_and_prepares_visual_retry(self):
        snapshot = self.ready_panel()
        request = self.service.panel_review_input(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01"
        )
        failed = self.service.publish_panel_review(
            self.alice,
            snapshot.project_id,
            snapshot.revision,
            "p01-01",
            panel_review(request, failure=True),
        )
        self.assertEqual(snapshot.revision + 1, failed.revision)
        self.assertNotEqual("QA_READY", failed.status)
        record = json.loads((failed.root / "qa/panels/p01-01.json").read_bytes())
        self.assertEqual("regenerate", record["decision"])
        requests = self.service.prepare_generation(self.alice, failed.project_id, failed.revision)
        self.assertEqual(["p01-01"], [request.subject_id for request in requests])

    def test_page_wrong_subject_failed_qa_and_stale_hash_block_export(self):
        composed = self.composed_page()
        for page in (0, 2, True):
            with self.assertRaises(GatewayInputError):
                self.service.page_review_input(
                    self.alice, composed.project_id, composed.revision, page
                )
        checks = valid_page_reviewer_checks(composed.root, 1)
        checks[0]["result"] = "fail"
        failed = self.service.publish_page_review(
            self.alice,
            composed.project_id,
            composed.revision,
            1,
            VisualReviewResult(tuple(checks), (), {}),
        )
        with (
            self.assertRaises(ValueError),
            mock.patch("comic_sol_web.engine_gateway.comic_sol.finalize_project") as finalize,
        ):
            self.service.finalize(self.alice, failed.project_id, failed.revision)
        finalize.assert_not_called()
        passed = self.service.publish_page_review(
            self.alice,
            failed.project_id,
            failed.revision,
            1,
            VisualReviewResult(tuple(valid_page_reviewer_checks(failed.root, 1)), (), {}),
        )
        page_path = passed.root / "pages/page-001.png"
        page_path.write_bytes(page_path.read_bytes() + b"stale")
        with self.assertRaises(StaleProjectRevisionError):
            self.service.publish_page_review(
                self.alice,
                passed.project_id,
                passed.revision,
                1,
                VisualReviewResult(tuple(checks), (), {}),
            )
        with self.assertRaises(StaleProjectRevisionError):
            self.service.finalize(self.alice, passed.project_id, passed.revision)

    def test_accepted_panel_and_page_warnings_survive_to_pdf(self):
        snapshot = self.ready_panel()
        request = self.service.panel_review_input(
            self.alice, snapshot.project_id, snapshot.revision, "p01-01"
        )
        review = panel_review(request)
        checks = [dict(check) for check in review.checks]
        checks[2]["result"] = "warning"
        reviewed = self.service.publish_panel_review(
            self.alice,
            snapshot.project_id,
            snapshot.revision,
            "p01-01",
            VisualReviewResult(tuple(checks), review.character_assessments, {}),
        )
        self.assertIn(
            checks[2]["evidence"], comic_sol.read_json(reviewed.root / "project.json")["warnings"]
        )
        composed = self.service.prepare_pages(self.alice, reviewed.project_id, reviewed.revision)
        page_checks = valid_page_reviewer_checks(composed.root, 1)
        page_checks[0]["result"] = "warning"
        page_reviewed = self.service.publish_page_review(
            self.alice,
            composed.project_id,
            composed.revision,
            1,
            VisualReviewResult(tuple(page_checks), (), {}),
        )
        self.assertIn(
            page_checks[0]["evidence"],
            comic_sol.read_json(reviewed.root / "project.json")["warnings"],
        )
        final, pdf = self.service.finalize(
            self.alice, page_reviewed.project_id, page_reviewed.revision
        )
        self.assertEqual("COMPLETE_WITH_WARNINGS", final.status)
        self.assertTrue(pdf.is_file())

    def test_character_free_panel_uses_plain_identity_evidence(self):
        snapshot = self.ready_panel()
        storyboard = comic_sol.read_json(snapshot.root / "plan/storyboard.json")
        storyboard["pages"][0]["panels"][0].update(characters=[], text=[])
        with ProjectTransaction(snapshot.root, "fixture-empty-cast") as transaction:
            transaction.stage_bytes(
                "plan/storyboard.json", comic_sol.canonical_artifact_bytes(storyboard)
            )
        plan_and_write_reference_plan(snapshot.root)
        self.gateway = EngineGateway.open(self.data_root)
        self.service = ProjectService(self.gateway)
        current = self.service.snapshot(self.alice, snapshot.project_id)
        request = self.service.panel_review_input(
            self.alice, current.project_id, current.revision, "p01-01"
        )
        self.assertEqual((), request.context["characters"])
        reviewed = self.service.publish_panel_review(
            self.alice, current.project_id, current.revision, "p01-01", panel_review(request)
        )
        record = comic_sol.read_json(reviewed.root / "qa/panels/p01-01.json")
        self.assertEqual("accept", record["decision"])
        self.assertNotIn("provenance", record["checks"][0])

    def test_partial_engine_mutation_reconciles_once(self):
        snapshot = self.reviewed_panel()
        with mock.patch(
            "comic_sol_web.engine_gateway._compose_pages.compose_project",
            side_effect=RuntimeError("interrupted"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.prepare_pages(self.alice, snapshot.project_id, snapshot.revision)
        updated = self.service.snapshot(self.alice, snapshot.project_id)
        self.assertEqual(snapshot.revision + 1, updated.revision)


class _WorkflowProjects:
    def __init__(self):
        self.revision = 4
        self.status = "STORYBOARDED"
        self.finalize_calls = 0
        self.prepare_pages_calls = 0
        self.stale_page = False

    def snapshot(self, principal, project_id, expected_revision=None):
        if principal.user_id != "alice" or project_id != "project-1":
            raise ProjectUnavailableError("project unavailable")
        if expected_revision is not None and expected_revision != self.revision:
            raise StaleProjectRevisionError(expected_revision, self.revision)
        return SimpleNamespace(
            project_id=project_id,
            revision=self.revision,
            status=self.status,
            summary={"page_count": 1},
        )

    def finalize(self, principal, project_id, expected_revision):
        self.snapshot(principal, project_id, expected_revision)
        self.finalize_calls += 1
        self.revision += 1
        self.status = "COMPLETE"
        return self.snapshot(principal, project_id), Path("ignored.pdf")

    def prepare_pages(self, principal, project_id, expected_revision):
        self.snapshot(principal, project_id, expected_revision)
        self.prepare_pages_calls += 1
        self.revision += 1
        self.status = "COMPOSED"
        return self.snapshot(principal, project_id)

    def panel_review_input(self, principal, project_id, expected_revision, panel_id):
        self.snapshot(principal, project_id, expected_revision)
        return SimpleNamespace(subject_id=panel_id)

    def publish_panel_review(self, principal, project_id, expected_revision, panel_id, review):
        self.snapshot(principal, project_id, expected_revision)
        self.revision += 1
        return self.snapshot(principal, project_id)

    def page_review_input(self, principal, project_id, expected_revision, page_number):
        self.snapshot(principal, project_id, expected_revision)
        if self.stale_page:
            raise StaleProjectRevisionError(expected_revision, expected_revision)
        return SimpleNamespace(subject_id=f"page-{page_number:03d}")

    def publish_page_review(self, principal, project_id, expected_revision, page_number, review):
        self.snapshot(principal, project_id, expected_revision)
        self.revision += 1
        return self.snapshot(principal, project_id)


class _WorkflowCredentials:
    @asynccontextmanager
    async def resolve(self, owner_id, provider, auth_mode):
        yield "fixture-credential"


class _WorkflowReviewer:
    provider_id = "openai"
    model = "gpt-5.4-mini"

    def __init__(self):
        self.failed = False
        self.calls = 0

    async def review_visual(self, request, credential):
        self.calls += 1
        return SimpleNamespace(
            checks=(
                {
                    "result": "fail" if self.failed else "pass",
                    "severity": "error",
                },
            )
        )


class _WorkflowPlanning:
    def __init__(self):
        self.job = SimpleNamespace(
            job_id="00000000-0000-4000-8000-000000000001",
            owner_id="alice",
            project_id="project-1",
            project_revision=3,
            provider="openai",
            model="gpt-5.4-mini",
            state="ready_for_review",
            published_revision=4,
        )
        self.reviewer = _WorkflowReviewer()
        self.providers = {(self.job.provider, self.job.model): self.reviewer}
        self.credentials = _WorkflowCredentials()

    def get(self, principal, job_id):
        if principal.user_id != self.job.owner_id or job_id != self.job.job_id:
            raise ProjectUnavailableError("planning job unavailable")
        return self.job


class _WorkflowGeneration:
    def __init__(self, projects):
        self.projects = projects
        self.jobs = []
        self.submit_calls = 0
        self.run_calls = 0

    def _runtime_options(self):
        return (
            SimpleNamespace(
                provider="openai",
                model="gpt-image-2",
                capabilities=frozenset({"text_to_image"}),
                enabled=True,
            ),
        )

    def list_jobs(self, principal, project_id, expected_revision, limit=50):
        self.projects.snapshot(principal, project_id, expected_revision)
        return tuple(self.jobs)

    def queue(
        self,
        principal,
        project_id,
        expected_revision,
        *,
        provider,
        model,
        auth_mode,
        max_retries,
    ):
        self.projects.snapshot(principal, project_id, expected_revision)
        return tuple(self.jobs)

    async def run_once(self, worker_id, lease_seconds=30):
        self.run_calls += 1
        return next((job for job in self.jobs if job.state is JobState.QUEUED), None)

    def submit_staged_raster(self, principal, job_id, expected_revision):
        job = next(job for job in self.jobs if job.job_id == job_id)
        self.projects.snapshot(principal, job.project_id, expected_revision)
        self.submit_calls += 1
        self.projects.revision += 1
        job.state = JobState.ACCEPTED
        job.accepted_project_revision = self.projects.revision
        return job

    def attempts(self, job_id):
        return ()


def _panel_job(*, state=JobState.ACCEPTED, revision=4, suffix="1"):
    return SimpleNamespace(
        job_id=suffix * 64,
        project_id="project-1",
        project_revision=revision,
        state=state,
        provider="openai",
        model="gpt-image-2",
        auth_mode=AuthMode.HOSTED,
        attempt_number=1,
        accepted_project_revision=revision if state is JobState.ACCEPTED else None,
        request=SimpleNamespace(
            job_id=f"engine-panel-{suffix}",
            subject_kind="panel",
            subject_id=f"p01-0{suffix}",
        ),
    )


class DurableWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "workflow.sqlite3")
        apply_migrations(self.database, WORKFLOW_MIGRATIONS)
        self.projects = _WorkflowProjects()
        self.planning = _WorkflowPlanning()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO web_projects (project_id, owner_id, storage_name, revision, engine_state) "
                "VALUES ('project-1', 'alice', 'fixture', 4, 'STORYBOARDED')"
            )
            connection.execute(
                "INSERT INTO planning_jobs (job_id, idempotency_key, owner_id, project_id, "
                "project_revision, provider, model, state, publication_sha256, published_revision, "
                "created_at, completed_at, updated_at) VALUES (?, ?, 'alice', 'project-1', 3, "
                "'openai', 'gpt-5.4-mini', 'ready_for_review', ?, 4, 900, 950, 950)",
                (
                    self.planning.job.job_id,
                    "00000000-0000-4000-8000-000000000002",
                    "a" * 64,
                ),
            )
        self.generation = _WorkflowGeneration(self.projects)
        self.now = 1_000
        self.service = WorkflowService(
            self.database,
            self.projects,
            self.planning,
            self.generation,
            clock=lambda: self.now,
        )
        self.alice = SessionPrincipal("alice", "alice")
        self.bob = SessionPrincipal("bob", "bob")

    def approve(self):
        return self.service.approve_plan(
            self.alice,
            "project-1",
            4,
            planning_job_id=self.planning.job.job_id,
            image_provider="openai",
            image_model="gpt-image-2",
            image_auth_mode="hosted",
            idempotency_key=str(uuid4()),
        )

    def test_approval_requires_reviewed_plan_and_freezes_provider_selection(self):
        workflow = self.approve()
        self.assertEqual("references", workflow.phase)
        self.assertEqual("openai", workflow.planning_provider)
        self.assertEqual("gpt-image-2", workflow.image_model)

        with self.database.transaction() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE production_workflows SET image_model = 'silent-fallback' "
                "WHERE project_id = 'project-1'"
            )

        self.planning.job.state = "failed"
        with self.assertRaises(WorkflowConflictError):
            self.service.approve_plan(
                self.alice,
                "project-1",
                4,
                planning_job_id=self.planning.job.job_id,
                image_provider="openai",
                image_model="gpt-image-2",
                image_auth_mode="hosted",
                idempotency_key=str(uuid4()),
            )

    def test_events_are_append_only_bounded_and_owner_scoped(self):
        self.approve()
        events = self.service.events_after(self.alice, "project-1", 0)
        self.assertEqual(["plan.validated"], [event.type for event in events])
        self.assertEqual((), self.service.events_after(self.bob, "project-1", 0))
        with self.database.transaction() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM workflow_events")
        with self.assertRaises(ValueError):
            self.service.events_after(self.alice, "project-1", 0, limit=101)

    def test_pause_resume_are_revision_bound_and_replay_safe(self):
        approved = self.approve()
        paused = self.service.pause(self.alice, "project-1", approved.revision)
        self.assertEqual("paused", paused.state)
        with self.assertRaises(WorkflowConflictError):
            self.service.resume(self.alice, "project-1", approved.revision - 1)
        resumed = self.service.resume(self.alice, "project-1", approved.revision)
        self.assertEqual("running", resumed.state)
        self.assertEqual("references", resumed.phase)

    def test_export_recovery_does_not_finalize_twice(self):
        approved = self.approve()
        self.projects.status = "COMPLETE"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET phase = 'export' WHERE project_id = ?",
                (approved.project_id,),
            )
        completed = asyncio.run(self.service.advance_once("worker-a"))
        self.assertIsNotNone(completed)
        self.assertEqual("complete", completed.state)
        self.assertEqual(0, self.projects.finalize_calls)

    def test_pause_before_promotion_retains_staged_job(self):
        approved = self.approve()
        self.generation.jobs = [_panel_job(state=JobState.VALIDATING)]
        self.service.pause(self.alice, approved.project_id, approved.revision)
        self.assertIsNone(asyncio.run(self.service.advance_once("worker-a")))
        self.assertEqual(0, self.generation.submit_calls)
        self.assertIs(JobState.VALIDATING, self.generation.jobs[0].state)

    def test_expired_lease_is_reclaimed_without_duplicate_promotion(self):
        approved = self.approve()
        self.generation.jobs = [_panel_job(state=JobState.VALIDATING)]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET lease_token = ?, lease_owner = ?, "
                "lease_expires_at = ? WHERE project_id = ?",
                (str(uuid4()), "dead-worker", self.now - 1, approved.project_id),
            )
        restarted = WorkflowService(
            self.database,
            self.projects,
            self.planning,
            self.generation,
            clock=lambda: self.now,
        )
        first = asyncio.run(restarted.advance_once("worker-b"))
        self.assertEqual(5, first.revision)
        asyncio.run(restarted.advance_once("worker-c"))
        self.assertEqual(1, self.generation.submit_calls)

    def test_eight_extra_calls_blocks_without_silent_provider_switch(self):
        approved = self.approve()
        self.generation.jobs = [_panel_job()]
        self.planning.reviewer.failed = True
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET phase = 'panel-qa', extra_calls = 8 "
                "WHERE project_id = ?",
                (approved.project_id,),
            )
        blocked = asyncio.run(self.service.advance_once("worker-a"))
        self.assertEqual("blocked", blocked.state)
        self.assertEqual("retry_exhausted", blocked.error_category)
        self.assertEqual("openai", blocked.image_provider)
        self.assertEqual("gpt-image-2", blocked.image_model)

    def test_failed_page_qa_blocks_with_evidence(self):
        approved = self.approve()
        self.planning.reviewer.failed = True
        self.projects.status = "COMPOSED"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET phase = 'page-qa' WHERE project_id = ?",
                (approved.project_id,),
            )
        blocked = asyncio.run(self.service.advance_once("worker-a"))
        self.assertEqual("blocked", blocked.state)
        self.assertEqual("page_qa_failed", blocked.error_category)
        self.assertEqual(
            ["qa.page_failed", "workflow.blocked"],
            [event.type for event in self.service.events_after(self.alice, "project-1", 1)],
        )

    def test_resume_from_composition_runs_one_deterministic_boundary(self):
        approved = self.approve()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET phase = 'composition' WHERE project_id = ?",
                (approved.project_id,),
            )
        self.service.pause(self.alice, "project-1", approved.revision)
        self.service.resume(self.alice, "project-1", approved.revision)
        current = asyncio.run(self.service.advance_once("worker-a"))
        self.assertEqual("page-qa", current.phase)
        self.assertEqual(1, self.projects.prepare_pages_calls)

    def test_stale_page_binding_returns_to_composition_without_reusing_review(self):
        approved = self.approve()
        self.projects.status = "COMPOSED"
        self.projects.stale_page = True
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET phase = 'page-qa' WHERE project_id = ?",
                (approved.project_id,),
            )
        current = asyncio.run(self.service.advance_once("worker-a"))
        self.assertEqual("running", current.state)
        self.assertEqual("composition", current.phase)
        self.assertEqual(0, self.planning.reviewer.calls)


class GenerationEnvelopeWorkflowTests(unittest.TestCase):
    def test_job_envelope_exposes_subject_without_request_payload_or_path(self):
        from comic_sol_web.api.generation import _job_envelope

        job = SimpleNamespace(
            job_id="j" * 64,
            project_id="project-1",
            project_revision=3,
            state=JobState.QUEUED,
            provider="openai",
            model="gpt-image-2",
            auth_mode=AuthMode.HOSTED,
            attempt_number=1,
            retry_count=0,
            max_retries=2,
            external_job_id=None,
            accepted_project_revision=None,
            request=SimpleNamespace(
                subject_kind="panel",
                subject_id="p01-01",
                prompt="private prompt",
                references=(Path("C:/private/reference.png"),),
            ),
        )
        envelope = _job_envelope(job, 3)
        self.assertEqual("panel", envelope["subject_kind"])
        self.assertEqual("p01-01", envelope["subject_id"])
        rendered = json.dumps(envelope)
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("C:/private", rendered)

    def test_workflow_routes_register_without_constructing_storage(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from web.tests.support import valid_environment
        from web.tests.test_app import registered_api_routes

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "not-created"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            routes = {
                (path, methods)
                for path, methods in registered_api_routes(app)
                if path.startswith("/api/workflows")
            }
            self.assertEqual(
                {
                    ("/api/workflows", frozenset({"POST"})),
                    ("/api/workflows/{project_id}", frozenset({"GET"})),
                    ("/api/workflows/{project_id}/pause", frozenset({"POST"})),
                    ("/api/workflows/{project_id}/resume", frozenset({"POST"})),
                    ("/api/workflows/{project_id}/events", frozenset({"GET"})),
                },
                routes,
            )
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "workflow"))
