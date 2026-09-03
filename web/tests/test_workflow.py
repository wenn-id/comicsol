"""Offline canonical review and finalization boundary coverage."""

import json
from unittest import mock

from comic_sol_web.engine_gateway import (
    EngineGateway,
    GatewayInputError,
    ProjectUnavailableError,
    StaleProjectRevisionError,
)
from comic_sol_web.planning.types import VisualReviewResult
from comic_sol_web.projects import ProjectService
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
