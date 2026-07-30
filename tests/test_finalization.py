"""Fail-closed final and export-ready artifact validation tests."""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from comic_sol import (  # noqa: E402
    atomic_write_json,
    init_project,
    read_json,
    sha256_file,
)
from validate_project import (  # noqa: E402
    ProjectValidationError,
    ValidationIssue,
    require_valid_project,
    validate_project,
)

from test_validation import (  # noqa: E402
    valid_characters,
    valid_manifest,
    valid_panel_record_v2,
    valid_story,
    valid_storyboard,
)


class FinalArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root, "Final Test", b"A final test story.",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest = read_json(self.project / "project.json")
        manifest.update(valid_manifest())
        manifest["input"]["source_sha256"] = sha256_file(
            self.project / "source/input.txt"
        )
        atomic_write_json(self.project / "project.json", manifest)
        atomic_write_json(
            self.project / "plan/story-plan.json", valid_story()
        )
        atomic_write_json(
            self.project / "plan/character-bible.json", valid_characters()
        )
        atomic_write_json(
            self.project / "plan/storyboard.json", valid_storyboard()
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _add_panel_files(self):
        (self.project / "prompts/panels/p01-01.txt").write_text(
            "panel prompt", encoding="utf-8"
        )
        Image.new("RGB", (512, 512), "white").save(
            self.project / "references/characters/mira.png"
        )
        raw = self.project / "panels/raw/p01-01.png"
        clean = self.project / "panels/clean/p01-01.png"
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(raw)
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(clean)
        record = valid_panel_record_v2()
        record["bindings"]["raw_sha256"] = sha256_file(raw)
        record["bindings"]["clean_sha256"] = sha256_file(clean)
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

    def _add_lettered_page_qas(self):
        (self.project / "pages").mkdir(exist_ok=True)
        page_png = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), (100, 150, 200)).save(page_png)
        page_hash = sha256_file(page_png)
        page_qa = self.project / "qa/pages/page-001.json"
        page_qa.parent.mkdir(parents=True, exist_ok=True)
        import json
        atomic_write_json(
            page_qa,
            {
                "page": 1,
                "page_path": "pages/page-001.png",
                "page_sha256": page_hash,
                "schema_version": "1.0",
                "status": "reviewed",
            },
        )
        lettered = self.project / "panels/p01-01/lettered.png"
        lettered.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (736, 1136), (10, 20, 30)).save(lettered)

    def test_final_fails_without_any_artifacts(self):
        """RED: an empty project must report missing final artifacts."""
        self._add_panel_files()
        issues = validate_project(self.project, "final")
        missing_paths = {issue.path for issue in issues}
        self.assertIn(
            "project.json",
            missing_paths,
            "final validation must report missing artifact descriptors",
        )
        self.assertGreaterEqual(
            len(issues), 3,
            f"empty artifacts should produce several final issues, got {len(issues)}",
        )

    def test_export_ready_excludes_report_and_pdf(self):
        """export-ready must not require report, PDF, or export cache."""
        self._add_panel_files()
        self._add_lettered_page_qas()
        manifest = read_json(self.project / "project.json")
        comp_cache = self.project / "cache/composition.json"
        comp_cache.parent.mkdir(exist_ok=True)
        import json
        comp_cache.write_text(
            json.dumps({"schema_version": "1.0", "stages": {}})
        )
        comp_hash = sha256_file(comp_cache)
        manifest["artifacts"] = {
            "character_bible": {
                "path": "plan/character-bible.json",
                "sha256": sha256_file(
                    self.project / "plan/character-bible.json"
                ),
            },
            "story_plan": {
                "path": "plan/story-plan.json",
                "sha256": sha256_file(
                    self.project / "plan/story-plan.json"
                ),
            },
            "storyboard": {
                "path": "plan/storyboard.json",
                "sha256": sha256_file(
                    self.project / "plan/storyboard.json"
                ),
            },
            "composition_cache": {
                "path": "cache/composition.json",
                "sha256": comp_hash,
            },
        }
        atomic_write_json(self.project / "project.json", manifest)
        issues = validate_project(self.project, "export-ready")
        self.assertEqual(
            [], issues,
            f"export-ready with panel QA, lettered, page-QA, "
            f"composition cache should pass, got {issues}",
        )

    def test_export_ready_reports_missing_page_qa(self):
        """export-ready must fail on missing page-QA record."""
        self._add_panel_files()
        (self.project / "pages").mkdir(exist_ok=True)
        page_png = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), (0, 0, 0)).save(page_png)
        lettered = self.project / "panels/p01-01/lettered.png"
        lettered.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (736, 1136), (1, 1, 1)).save(lettered)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"] = {
            "character_bible": {
                "path": "plan/character-bible.json",
                "sha256": sha256_file(
                    self.project / "plan/character-bible.json"
                ),
            },
            "story_plan": {
                "path": "plan/story-plan.json",
                "sha256": sha256_file(
                    self.project / "plan/story-plan.json"
                ),
            },
            "storyboard": {
                "path": "plan/storyboard.json",
                "sha256": sha256_file(
                    self.project / "plan/storyboard.json"
                ),
            },
        }
        atomic_write_json(self.project / "project.json", manifest)
        (self.project / "cache").mkdir(exist_ok=True)
        import json
        (self.project / "cache/composition.json").write_text(
            json.dumps({"schema_version": "1.0", "stages": {}})
        )
        issues = validate_project(self.project, "export-ready")
        page_qa_issues = [
            i for i in issues
            if "page-001" in i.message or "qa/pages" in i.path
        ]
        self.assertTrue(
            len(page_qa_issues) > 0,
            f"missing page-QA must be reported, got {issues}",
        )


class GuardedOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root, "Guard Test", b"A guard test story.",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest = read_json(self.project / "project.json")
        manifest.update(valid_manifest())
        manifest["input"]["source_sha256"] = sha256_file(
            self.project / "source/input.txt"
        )
        atomic_write_json(self.project / "project.json", manifest)
        atomic_write_json(
            self.project / "plan/story-plan.json", valid_story()
        )
        atomic_write_json(
            self.project / "plan/character-bible.json", valid_characters()
        )
        atomic_write_json(
            self.project / "plan/storyboard.json", valid_storyboard()
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _add_panel_files(self):
        (self.project / "prompts/panels/p01-01.txt").write_text(
            "panel prompt", encoding="utf-8"
        )
        Image.new("RGB", (512, 512), "white").save(
            self.project / "references/characters/mira.png"
        )
        raw = self.project / "panels/raw/p01-01.png"
        clean = self.project / "panels/clean/p01-01.png"
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(raw)
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(clean)
        record = valid_panel_record_v2()
        record["bindings"]["raw_sha256"] = sha256_file(raw)
        record["bindings"]["clean_sha256"] = sha256_file(clean)
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

    def _add_lettered_page_qas(self):
        (self.project / "pages").mkdir(exist_ok=True)
        page_png = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), (100, 150, 200)).save(page_png)
        page_hash = sha256_file(page_png)
        page_qa = self.project / "qa/pages/page-001.json"
        page_qa.parent.mkdir(parents=True, exist_ok=True)
        import json
        atomic_write_json(
            page_qa,
            {
                "page": 1,
                "page_path": "pages/page-001.png",
                "page_sha256": page_hash,
                "schema_version": "1.0",
                "status": "reviewed",
            },
        )
        lettered = self.project / "panels/p01-01/lettered.png"
        lettered.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (736, 1136), (10, 20, 30)).save(lettered)

    def _make_export_ready(self):
        self._add_panel_files()
        self._add_lettered_page_qas()
        manifest = read_json(self.project / "project.json")
        comp_cache = self.project / "cache/composition.json"
        comp_cache.parent.mkdir(exist_ok=True)
        import json
        comp_cache.write_text(
            json.dumps({"schema_version": "1.0", "stages": {}})
        )
        manifest["artifacts"] = {
            "character_bible": {
                "path": "plan/character-bible.json",
                "sha256": sha256_file(
                    self.project / "plan/character-bible.json"
                ),
            },
            "story_plan": {
                "path": "plan/story-plan.json",
                "sha256": sha256_file(
                    self.project / "plan/story-plan.json"
                ),
            },
            "storyboard": {
                "path": "plan/storyboard.json",
                "sha256": sha256_file(
                    self.project / "plan/storyboard.json"
                ),
            },
            "composition_cache": {
                "path": "cache/composition.json",
                "sha256": sha256_file(comp_cache),
            },
        }
        atomic_write_json(self.project / "project.json", manifest)

    def test_require_valid_project_raises_on_invalid(self):
        with self.assertRaises(ProjectValidationError):
            require_valid_project(self.project, "final")

    def test_require_valid_project_returns_none_on_valid(self):
        self.assertIsNone(
            require_valid_project(self.project, "plan")
        )

    def test_guarded_export_rejects_invalid_panel_qa(self):
        """RED: guarded_export must reject when panel QA has unresolved errors."""
        self._make_export_ready()
        # Corrupt panel QA with unresolved error
        record = read_json(self.project / "qa/panels/p01-01.json")
        record["checks"][0].update({"result": "fail", "severity": "error"})
        record["decision"] = "regenerate"
        record["retry_reason"] = "character identity failure"
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)
        from export_pdf import guarded_export
        with self.assertRaises(ProjectValidationError):
            guarded_export(self.project)
        # No PDF should be written
        self.assertFalse(
            (self.project / "exports/guard-test.pdf").is_file(),
            "guarded_export must not write PDF when validation fails",
        )

    def test_guarded_export_rejects_missing_page_qa(self):
        """RED: guarded_export must reject when page-QA is missing."""
        self._add_panel_files()
        (self.project / "pages").mkdir(exist_ok=True)
        page_png = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), (0, 0, 0)).save(page_png)
        lettered = self.project / "panels/p01-01/lettered.png"
        lettered.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (736, 1136), (1, 1, 1)).save(lettered)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"] = {
            "character_bible": {
                "path": "plan/character-bible.json",
                "sha256": sha256_file(
                    self.project / "plan/character-bible.json"
                ),
            },
            "story_plan": {
                "path": "plan/story-plan.json",
                "sha256": sha256_file(
                    self.project / "plan/story-plan.json"
                ),
            },
            "storyboard": {
                "path": "plan/storyboard.json",
                "sha256": sha256_file(
                    self.project / "plan/storyboard.json"
                ),
            },
        }
        atomic_write_json(self.project / "project.json", manifest)
        (self.project / "cache").mkdir(exist_ok=True)
        import json
        (self.project / "cache/composition.json").write_text(
            json.dumps({"schema_version": "1.0", "stages": {}})
        )
        from export_pdf import guarded_export
        with self.assertRaises(ProjectValidationError):
            guarded_export(self.project)
        self.assertFalse(
            (self.project / "exports/guard-test.pdf").is_file(),
        )

    def test_guarded_export_writes_pdf_and_records_descriptor(self):
        """GREEN: guarded_export with valid export-ready writes PDF and records descriptor."""
        self._make_export_ready()
        from export_pdf import guarded_export
        result = guarded_export(self.project)
        self.assertTrue(result.is_file())
        # Descriptor recorded in manifest
        manifest = read_json(self.project / "project.json")
        self.assertIn("pdf", manifest["artifacts"])
        pdf_desc = manifest["artifacts"]["pdf"]
        self.assertEqual(result.relative_to(self.project).as_posix(), pdf_desc["path"])
        self.assertEqual(64, len(pdf_desc["sha256"]))

    def test_guarded_transition_rejects_incomplete_final(self):
        """RED: transition to COMPLETE must reject when final artifacts missing."""
        self._add_panel_files()
        manifest = read_json(self.project / "project.json")
        manifest["status"] = "EXPORTED"
        atomic_write_json(self.project / "project.json", manifest)
        from comic_sol import transition
        with self.assertRaises(ProjectValidationError):
            transition(self.project, "COMPLETE")
        # Manifest unchanged
        manifest = read_json(self.project / "project.json")
        self.assertNotEqual("COMPLETE", manifest["status"])

    def test_guarded_transition_allows_complete_with_all_artifacts(self):
        """GREEN: transition to COMPLETE succeeds with all final artifacts."""
        self._make_export_ready()
        # Add report and PDF
        (self.project / "qa/report.md").write_text("# QA Report\n", encoding="utf-8")
        from export_pdf import guarded_export
        pdf_path = guarded_export(self.project)
        # Update manifest with report and pdf descriptors, set status to EXPORTED
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["qa_report"] = {
            "path": "qa/report.md",
            "sha256": sha256_file(self.project / "qa/report.md"),
        }
        manifest["artifacts"]["pdf"] = {
            "path": pdf_path.relative_to(self.project).as_posix(),
            "sha256": sha256_file(pdf_path),
        }
        manifest["status"] = "EXPORTED"
        atomic_write_json(self.project / "project.json", manifest)
        from comic_sol import transition
        result = transition(self.project, "COMPLETE")
        self.assertEqual("COMPLETE", result["status"])

    def test_guarded_transition_allows_complete_with_warnings(self):
        """GREEN: transition to COMPLETE_WITH_WARNINGS succeeds with warnings."""
        self._make_export_ready()
        (self.project / "qa/report.md").write_text("# QA Report\n", encoding="utf-8")
        from export_pdf import guarded_export
        pdf_path = guarded_export(self.project)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["qa_report"] = {
            "path": "qa/report.md",
            "sha256": sha256_file(self.project / "qa/report.md"),
        }
        manifest["artifacts"]["pdf"] = {
            "path": pdf_path.relative_to(self.project).as_posix(),
            "sha256": sha256_file(pdf_path),
        }
        manifest["warnings"] = ["minor prop drift"]
        manifest["status"] = "EXPORTED"
        atomic_write_json(self.project / "project.json", manifest)
        from comic_sol import transition
        result = transition(self.project, "COMPLETE_WITH_WARNINGS")
        self.assertEqual("COMPLETE_WITH_WARNINGS", result["status"])


if __name__ == "__main__":
    unittest.main()
