"""DOM and browser/API contracts for the static Start and Plan Studio."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

from fastapi.testclient import TestClient

from web.tests.support import valid_environment


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = WEB_ROOT / "comic_sol_web" / "static"
STATIC_ASSETS = {
    "static/index.html",
    "static/app.js",
    "static/api.js",
    "static/state.js",
    "static/styles.css",
    "static/views/start.js",
    "static/views/plan.js",
}


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class StudioDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()
        self.labels_for: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        self.elements.append((tag, attributes))
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "label" and attributes.get("for"):
            self.labels_for.add(attributes["for"])


class StudioContractTests(unittest.TestCase):
    index: ClassVar[str]
    app: ClassVar[str]
    api: ClassVar[str]
    state: ClassVar[str]
    start: ClassVar[str]
    plan: ClassVar[str]
    styles: ClassVar[str]
    scripts: ClassVar[str]
    parser: ClassVar[StudioDocumentParser]

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.api = (STATIC_ROOT / "api.js").read_text(encoding="utf-8")
        cls.state = (STATIC_ROOT / "state.js").read_text(encoding="utf-8")
        cls.start = (STATIC_ROOT / "views" / "start.js").read_text(encoding="utf-8")
        cls.plan = (STATIC_ROOT / "views" / "plan.js").read_text(encoding="utf-8")
        cls.styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.scripts = "\n".join((cls.app, cls.api, cls.state, cls.start, cls.plan))
        cls.parser = StudioDocumentParser()
        cls.parser.feed(cls.index)

    def test_exact_static_surface_exists_and_uses_vanilla_modules(self) -> None:
        relative_files = {
            path.relative_to(STATIC_ROOT).as_posix()
            for path in STATIC_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            {asset.removeprefix("static/") for asset in STATIC_ASSETS},
            relative_files,
        )
        self.assertRegex(
            self.index,
            r'<script[^>]+type=["\']module["\'][^>]+src=["\']\./app\.js["\']',
        )
        self.assertNotRegex(self.index, r"https?://|<link[^>]+stylesheet[^>]+(?:cdn|http)")
        self.assertNotRegex(self.scripts, r"\bReact\b|\bVue\b|\bAngular\b|require\(|node_modules")

    def test_wheel_declares_and_http_serves_all_seven_studio_assets(self) -> None:
        project = tomllib.loads((WEB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = project["tool"]["setuptools"]["package-data"]["comic_sol_web"]
        self.assertEqual(STATIC_ASSETS, set(package_data))

        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with TestClient(create_app(WebConfig.from_env(valid_environment()))) as client:
            statuses = {
                relative: client.get(f"/{relative}").status_code
                for relative in sorted(STATIC_ASSETS)
            }
        self.assertEqual({relative: 200 for relative in sorted(STATIC_ASSETS)}, statuses)

    def test_document_landmarks_navigation_and_live_status_are_accessible(self) -> None:
        tags = [tag for tag, _attrs in self.parser.elements]
        self.assertIn("header", tags)
        self.assertIn("nav", tags)
        self.assertIn("main", tags)
        self.assertIn("footer", tags)
        self.assertRegex(self.index, r'href=["\']#studio-main["\']')
        self.assertRegex(self.index, r"<nav[^>]+aria-label=")
        self.assertRegex(self.index, r'id=["\']studio-main["\'][^>]+tabindex=["\']-1["\']')
        self.assertRegex(self.index, r'aria-live=["\']polite["\']')
        self.assertRegex(self.index, r'aria-atomic=["\']true["\']')

    def test_start_supports_prompt_story_creation_and_bounded_archive_import(self) -> None:
        for identifier in (
            "project-title",
            "source-mode-prompt",
            "source-mode-story",
            "project-source",
            "project-language",
            "project-page-count",
            "project-archive",
        ):
            self.assertIn(identifier, self.start)
        self.assertIn(".comic-sol-handoff", self.start)
        self.assertIn("createProject", self.start)
        self.assertIn("importProject", self.start)
        self.assertRegex(self.start, r"(?:archive|file)\.size\s*>\s*MAX_ARCHIVE_BYTES")
        self.assertRegex(self.start, r"required")

    def test_start_honors_engine_page_source_and_byte_bounds(self) -> None:
        self.assertRegex(self.start, r'name:\s*["\']page_count["\'][\s\S]{0,160}max:\s*["\']4["\']')
        self.assertNotIn('max: "64"', self.start)
        self.assertIn('maxlength: "204800"', self.start)
        self.assertIn("const MAX_SOURCE_BYTES = 200 * 1024", self.start)
        self.assertRegex(
            self.start,
            r"new TextEncoder\(\)\.encode\(sourceValue\)\.byteLength\s*>\s*MAX_SOURCE_BYTES",
        )
        self.assertIn('"short_prompt"', self.start)
        self.assertIn('"pasted_story"', self.start)
        self.assertRegex(self.start, r"mode:\s*form\.elements\.source_mode\.value")
        self.assertRegex(self.start, r"page_count:\s*Number\(pageCount\.value\)")

    def test_start_and_import_errors_are_safe_and_migration_specific(self) -> None:
        self.assertIn("MigrationValidationError", self.api)
        self.assertRegex(self.api, r"response\.status\s*===\s*409")
        self.assertRegex(self.api, r"response\.status\s*===\s*4(?:00|13|22)")
        self.assertIn("could not be validated or migrated", self.api)
        self.assertNotRegex(self.scripts, r"console\.(?:log|error|warn)|sendBeacon|reportError")
        self.assertNotRegex(self.scripts, r"innerHTML\s*=|insertAdjacentHTML|document\.write")

    def test_every_network_write_has_csrf_idempotency_and_expected_revision(self) -> None:
        self.assertIn("comic_sol_csrf", self.api)
        self.assertIn('"X-CSRF-Token"', self.api)
        self.assertIn('"Idempotency-Key"', self.api)
        self.assertIn('"X-Expected-Revision"', self.api)
        self.assertIn("crypto.randomUUID()", self.api)
        self.assertRegex(self.api, r"function\s+writeRequest\s*\(")
        self.assertRegex(self.api, r"createProject[\s\S]+writeRequest\(")
        self.assertRegex(self.api, r"importProject[\s\S]+writeRequest\(")
        self.assertRegex(self.api, r"updatePlan[\s\S]+writeRequest\(")
        self.assertRegex(self.api, r"expectedRevision:\s*0")
        self.assertIn('credentials: "same-origin"', self.api)

    def test_client_uses_only_the_existing_wp3_project_api(self) -> None:
        self.assertIn('const PROJECTS_PATH = "/api/projects"', self.api)
        self.assertIn("`${PROJECTS_PATH}/import`", self.api)
        self.assertIn("encodeURIComponent(projectId)", self.api)
        route_literals = set(re.findall(r'["\'](/api/[^"\']*)["\']', self.scripts))
        self.assertEqual({"/api/projects"}, route_literals)
        self.assertNotRegex(
            self.scripts,
            r'fetch\s*\(\s*["\']https?://|/api/(?:provider|generation|draft)',
        )

    def test_plan_edits_are_revision_bound_and_agent_changes_are_reviewable(self) -> None:
        for section in ("Story plan", "Character bible", "Storyboard", "Visual Identity Pack"):
            self.assertIn(section, self.plan)
        self.assertIn("comic-sol:plan-proposal", self.plan)
        self.assertIn("expectedRevision", self.plan)
        self.assertIn("renderDraftDiff", self.plan)
        self.assertIn("Promote to working copy", self.plan)
        self.assertRegex(self.plan, r"project\.revision\s*!==\s*draft\.expectedRevision")
        self.assertRegex(self.plan, r"textContent\s*=")
        self.assertNotRegex(self.plan, r"JSON\.stringify\([^)]*project")

    def test_plan_persists_reviewed_draft_before_local_promotion(self) -> None:
        self.assertIn("updatePlan", self.plan)
        helper_position = self.plan.index("export async function persistReviewedDraft")
        persist_position = self.plan.index("await persist()", helper_position)
        promotion_position = self.plan.index("store.promoteDraft", persist_position)
        handler_position = self.plan.index("await persistReviewedDraft", promotion_position)
        success_position = self.plan.index('"success"', handler_position)
        self.assertLess(persist_position, promotion_position)
        self.assertLess(promotion_position, handler_position)
        self.assertLess(handler_position, success_position)
        self.assertRegex(self.state, r"summary\?\.plan")
        self.assertRegex(self.state, r"promoteDraft\(project\)")

    def test_plan_promotion_blocks_conflicting_controls_and_reconciles_drafts(self) -> None:
        self.assertIn("let promotionPending = false", self.plan)
        self.assertGreaterEqual(self.plan.count("if (promotionPending) return"), 3)
        self.assertIn("if (promotionPending && !force) return", self.plan)
        self.assertIn(
            'querySelector("#refresh-project").addEventListener("click", () => refreshProject())',
            self.plan,
        )
        self.assertRegex(
            self.plan,
            r"for \(const control of form\.elements\) control\.disabled = promotionPending",
        )
        self.assertRegex(
            self.plan,
            r"promotionPending\s*=\s*true[\s\S]{0,500}await persistReviewedDraft",
        )
        self.assertRegex(
            self.plan,
            r"currentDraft\s*!==\s*draft[\s\S]{0,300}store\.replaceProject\(persisted\)"
            r"[\s\S]{0,200}store\.createDraft\(currentDraft\.changes, currentDraft\.origin\)"
            r"[\s\S]{0,300}store\.promoteDraft\(persisted\)",
        )
        self.assertRegex(
            self.plan,
            r"finally\s*\{[\s\S]{0,160}promotionPending\s*=\s*false",
        )
        self.assertRegex(
            self.plan,
            r"refreshed\.revision\s*!==\s*previousRevision[\s\S]{0,300}"
            r"controls\[key\]\.value\s*=\s*store\.getState\(\)\.workingPlan\[key\]",
        )

    def test_deferred_plan_promotion_preserves_replacement_and_syncs_controls(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the Studio runtime contract test")
        assert node is not None
        state_uri = (STATIC_ROOT / "state.js").as_uri()
        plan_uri = (STATIC_ROOT / "views" / "plan.js").as_uri()
        script = f"""
import {{ createStore }} from {json.dumps(state_uri)};
import {{ persistReviewedDraft, safeProposal }} from {json.dumps(plan_uri)};

function check(condition, message) {{
  if (!condition) throw new Error(message);
}}

const originalPlan = Object.freeze({{
  storyPlan: "story-v1",
  characterBible: "characters-v1",
  storyboard: "storyboard-v1",
  visualIdentityPack: "identity-v1",
}});
const submittedPlan = Object.freeze({{
  storyPlan: "story-v2",
  characterBible: "characters-v2",
  storyboard: "storyboard-v2",
  visualIdentityPack: "identity-v2",
}});
const replacementPlan = Object.freeze({{
  storyPlan: "story-v3",
  characterBible: "characters-v3",
  storyboard: "storyboard-v3",
  visualIdentityPack: "identity-v3",
}});
const project = (revision, plan) => Object.freeze({{
  project_id: "project_0123456789abcdef01234567",
  revision,
  status: "INIT",
  summary: Object.freeze({{ plan }}),
}});

const store = createStore();
store.setProject(project(1, originalPlan));
store.createDraft(submittedPlan);
const submittedDraft = store.getState().draft;
let resolveUpdatePlan;
let updatePlanCalls = 0;
const deferredUpdatePlan = new Promise((resolve) => {{ resolveUpdatePlan = resolve; }});
const updatePlan = () => {{
  updatePlanCalls += 1;
  return deferredUpdatePlan;
}};
const operation = persistReviewedDraft(store, submittedDraft, updatePlan);
await Promise.resolve();
check(updatePlanCalls === 1, "deferred updatePlan was not called exactly once");
store.createDraft(replacementPlan, "agent");
resolveUpdatePlan(project(2, submittedPlan));
const result = await operation;
const state = store.getState();

check(result.outcome === "replacement-preserved", "replacement outcome was not preserved");
check(state.project.revision === 2, "persisted revision was not installed");
check(state.workingPlan.storyPlan === submittedPlan.storyPlan, "persisted Plan was not installed");
check(state.draft !== submittedDraft, "submitted draft identity survived reconciliation");
check(state.draft.origin === "agent", "replacement draft origin was not preserved");
check(state.draft.expectedRevision === 2, "replacement draft was not rebound to revision 2");
for (const key of Object.keys(replacementPlan)) {{
  check(state.draft.changes[key] === replacementPlan[key], `replacement field ${{key}} was lost`);
}}

check(
  safeProposal({{ expectedRevision: 2, changes: {{ storyPlan: "incomplete" }} }}) === null,
  "incomplete proposal was accepted",
);
const completeProposal = safeProposal({{ expectedRevision: 2, changes: replacementPlan }});
check(completeProposal !== null, "complete proposal was rejected");
check(
  Object.keys(completeProposal.changes).length === 4 &&
    completeProposal.changes.characterBible === replacementPlan.characterBible,
  "complete proposal did not retain the canonical envelope",
);
"""
        completed = subprocess.run(
            [
                node,
                "--experimental-default-type=module",
                "--input-type=module",
                "--eval",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"Node Studio runtime contract failed:\n{completed.stdout}\n{completed.stderr}",
        )

    def test_proposal_listener_survives_bad_events_and_preserves_pending_review(self) -> None:
        self.assertNotIn("{ once: true }", self.plan)
        self.assertNotIn("removeEventListener", self.plan)
        self.assertEqual(1, self.plan.count('addEventListener("comic-sol:plan-proposal"'))
        self.assertRegex(self.plan, r"if\s*\(store\.getState\(\)\.draft\)[\s\S]{0,240}return")
        self.assertRegex(self.plan, r"if\s*\(!activeProposalHandler\)")
        malformed_return = self.plan.index("if (!proposal) return")
        listener_registration = self.plan.index('addEventListener("comic-sol:plan-proposal"')
        self.assertLess(malformed_return, listener_registration)

    def test_stale_revisions_refresh_deterministically_without_implicit_promotion(self) -> None:
        self.assertIn("StaleRevisionError", self.api)
        self.assertIn("refreshProject", self.plan)
        self.assertIn("clearDraft", self.plan)
        self.assertIn("Revision changed", self.plan)
        self.assertNotRegex(self.plan, r"catch[\s\S]{0,180}promoteDraft")

    def test_controls_have_programmatic_labels_and_logical_keyboard_order(self) -> None:
        self.assertRegex(self.start, r"labelFor\(")
        self.assertRegex(self.plan, r"labelFor\(")
        self.assertNotRegex(self.scripts, r'tabindex\s*[=:]\s*["\']?[1-9]')
        self.assertRegex(self.start, r'addEventListener\(["\']submit["\']')
        self.assertRegex(self.plan, r'addEventListener\(["\']submit["\']')
        self.assertRegex(self.app, r'addEventListener\(["\']keydown["\']')
        self.assertIn('event.key === "ArrowRight"', self.app)
        self.assertIn('event.key === "ArrowLeft"', self.app)

    def test_visible_focus_and_reduced_motion_are_mandatory(self) -> None:
        self.assertRegex(self.styles, r":focus-visible\s*\{[^}]*outline:\s*(?!none)")
        self.assertRegex(self.styles, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
        reduced = self.styles.split("prefers-reduced-motion: reduce", 1)[1]
        self.assertRegex(reduced, r"animation-duration:\s*0\.01ms")
        self.assertRegex(reduced, r"scroll-behavior:\s*auto")

    def test_dark_success_and_danger_text_meet_wcag_aa_contrast(self) -> None:
        dark = self.styles.split("@media (prefers-color-scheme: dark)", 1)[1]
        variables = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", dark))
        for name in ("success", "danger"):
            with self.subTest(name=name):
                self.assertIn(name, variables)
                self.assertGreaterEqual(contrast_ratio(variables[name], variables["paper"]), 4.5)

    def test_root_custom_properties_are_separated_from_declarations(self) -> None:
        root = self.styles.split("}", 1)[0]
        self.assertRegex(root, r"--focus:\s*#[0-9a-fA-F]{6};\n\n\s*font-family:")

    def test_private_content_is_not_persisted_or_reported(self) -> None:
        self.assertNotRegex(
            self.scripts,
            r"localStorage|sessionStorage|indexedDB|sendBeacon|/telemetry|/errors|location\.(?:search|hash)",
        )
        self.assertNotRegex(self.scripts, r"Authorization|Bearer|api[_-]?key|filesystem|pathname")
        self.assertRegex(self.state, r"Object\.freeze")


if __name__ == "__main__":
    unittest.main()
