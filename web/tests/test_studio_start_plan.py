"""DOM and browser/API contracts for the static Start and Plan Studio."""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = WEB_ROOT / "comic_sol_web" / "static"


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
            {
                "index.html",
                "app.js",
                "api.js",
                "state.js",
                "styles.css",
                "views/start.js",
                "views/plan.js",
            },
            relative_files,
        )
        self.assertRegex(
            self.index,
            r'<script[^>]+type=["\']module["\'][^>]+src=["\']\./app\.js["\']',
        )
        self.assertNotRegex(self.index, r"https?://|<link[^>]+stylesheet[^>]+(?:cdn|http)")
        self.assertNotRegex(self.scripts, r"\bReact\b|\bVue\b|\bAngular\b|require\(|node_modules")

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
        for section in ("Story plan", "Storyboard", "Visual Identity Pack"):
            self.assertIn(section, self.plan)
        self.assertIn("comic-sol:plan-proposal", self.plan)
        self.assertIn("expectedRevision", self.plan)
        self.assertIn("renderDraftDiff", self.plan)
        self.assertIn("Promote to working copy", self.plan)
        self.assertRegex(self.plan, r"project\.revision\s*!==\s*draft\.expectedRevision")
        self.assertRegex(self.plan, r"textContent\s*=")
        self.assertNotRegex(self.plan, r"JSON\.stringify\([^)]*project")

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

    def test_private_content_is_not_persisted_or_reported(self) -> None:
        self.assertNotRegex(
            self.scripts,
            r"localStorage|sessionStorage|indexedDB|sendBeacon|/telemetry|/errors|location\.(?:search|hash)",
        )
        self.assertNotRegex(self.scripts, r"Authorization|Bearer|api[_-]?key|filesystem|pathname")
        self.assertRegex(self.state, r"Object\.freeze")


if __name__ == "__main__":
    unittest.main()
