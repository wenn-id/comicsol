"""Golden deterministic end-to-end pipeline coverage."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.comic_sol import (
    atomic_write_json,
    finalize_project,
    init_project,
    read_json,
    sha256_file,
    transition,
)
from scripts.normalize_panels import normalize_panel
from scripts.letter_panels import letter_project
from scripts.compose_pages import compose_project
from scripts.page_quality import (
    build_page_quality_record,
    write_page_quality_record,
)
from tests.support import bounded_tail_regions


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/golden/mini-comic"


class GoldenPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root,
            "Mini Comic Golden",
            (FIXTURE / "source/input.txt").read_bytes(),
            json.loads((FIXTURE / "source/request.json").read_text(encoding="utf-8")),
        )
        shutil.copy2(FIXTURE / "plan/story-plan.json", self.project / "plan/story-plan.json")
        shutil.copy2(FIXTURE / "plan/character-bible.json", self.project / "plan/character-bible.json")
        shutil.copy2(FIXTURE / "plan/storyboard.json", self.project / "plan/storyboard.json")
        shutil.copy2(FIXTURE / "prompts/panels/p01-01.txt", self.project / "prompts/panels/p01-01.txt")
        reference = self.project / "references/characters/mira.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (256, 256), (220, 180, 80)).save(reference)
        self._prepare_manifest()
        self.addCleanup(self.temporary_directory.cleanup)

    def _prepare_manifest(self):
        manifest = read_json(self.project / "project.json")
        descriptors = {}
        for name in ("story-plan", "character-bible", "storyboard"):
            path = self.project / f"plan/{name}.json"
            descriptors[name.replace("-", "_")] = {
                "path": f"plan/{name}.json",
                "sha256": sha256_file(path),
            }
        manifest.update({
            "project_id": "mini-comic-golden",
            "title": "Mini Comic Golden",
            "panels": ["p01-01"],
            "artifacts": descriptors,
            "settings": {
                **manifest["settings"],
                "page_count": 1,
                "panel_count": 1,
                "page_width": 1600,
                "page_height": 2400,
            },
            "input": {
                **manifest["input"],
                "source_sha256": sha256_file(self.project / "source/input.txt"),
            },
        })
        atomic_write_json(self.project / "project.json", manifest)
        transition(self.project, "PLANNED")
        transition(self.project, "SCRIPTED")
        transition(self.project, "STORYBOARDED")
        transition(self.project, "REFERENCES_READY")

    def _prepare_panel_artifacts(self):
        raw = self.project / "panels/raw/p01-01.png"
        raw.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (736, 1136), (24, 44, 72)).save(raw)
        clean = normalize_panel(
            self.project, "p01-01", "panels/raw/p01-01.png", (736, 1136), "exact"
        )
        record = json.loads((ROOT / "templates/panel-record.json").read_text(encoding="utf-8"))
        record.update({
            "subject_id": "p01-01",
            "decision": "accept",
            "unresolved_warnings": [],
            "review": {
                "method": "golden-fixture",
                "reviewer": "fixture-reviewer",
                "reviewed_at": "2026-08-18T00:00:00Z",
            },
        })
        record["bindings"].update({
            "raw_sha256": sha256_file(raw),
            "clean_sha256": sha256_file(clean),
            "normalization_sha256": sha256_file(
                self.project / "panels/p01-01/normalization.json"
            ),
        })
        for check in record["checks"]:
            check_id = check["id"]
            check.update({
                "result": "pass",
                "evidence": f"Golden fixture review verified the {check_id} requirement for the current panel artifact.",
                "method": "golden-fixture",
                "reviewer": "fixture-reviewer",
                "regions": [],
            })
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)
        transition(self.project, "PANELS_READY")
        transition(self.project, "QA_READY")

    def _prepare_ready_artifacts(self):
        self._prepare_panel_artifacts()
        letter_project(self.project)
        transition(self.project, "LETTERED")
        compose_project(self.project)
        transition(self.project, "COMPOSED")
        self._prepare_page_quality()

    def _prepare_page_quality(self):
        checks = [
            {
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": {
                    "face-action-obstruction": "Reviewer inspected Mira's face and delivery action; no obstruction is present.",
                    "bubble-tail-direction": "Reviewer verified Mira's dialogue tail terminates at the authored speaker anchor.",
                    "accidental-text-watermark": "Reviewer inspected the complete page; no accidental text or watermark is present.",
                }[check_id],
                "method": "golden-fixture",
                "reviewer": "fixture-reviewer",
                "regions": (
                    bounded_tail_regions(self.project, 1)
                    if check_id == "bubble-tail-direction" else [{"scope": "page"}]
                ),
            }
            for check_id in (
            "face-action-obstruction", "bubble-tail-direction",
            "accidental-text-watermark",
        )
        ]
        write_page_quality_record(
            self.project,
            1,
            build_page_quality_record(
                self.project, 1, checks,
                reviewer="fixture-reviewer", reviewed_at="2026-08-18T00:00:00Z",
            ),
        )

    def test_golden_project_reaches_verified_pdf_terminal_state(self):
        self._prepare_ready_artifacts()
        result = finalize_project(self.project)

        self.assertEqual("COMPLETE", result["status"])
        manifest = read_json(self.project / "project.json")
        self.assertEqual("COMPLETE", manifest["status"])
        self.assertEqual(["p01-01"], manifest["panels"])
        for relative in (
            "plan/story-plan.json",
            "plan/character-bible.json",
            "plan/storyboard.json",
            "qa/panels/p01-01.json",
            "panels/p01-01/lettered.png",
            "pages/page-001.png",
            "qa/pages/page-001.json",
            "cache/composition.json",
            "exports/mini-comic-golden.pdf",
            "exports/pdf-verification.json",
            "qa/report.md",
        ):
            self.assertTrue((self.project / relative).is_file(), relative)
        self.assertEqual("exports/mini-comic-golden.pdf", result["pdf"])
        with Image.open(self.project / "pages/page-001.png") as page_image:
            self.assertEqual(1600, page_image.width)
            self.assertEqual(2400, page_image.height)
        verification = read_json(self.project / "exports/pdf-verification.json")
        self.assertEqual("pdf-verification", verification["kind"])
        self.assertEqual(1, verification["page_count"])
        self.assertEqual(
            sha256_file(self.project / "exports/mini-comic-golden.pdf"),
            verification["pdf_sha256"],
        )
        self.assertEqual(64, len(manifest["artifacts"]["pdf"]["sha256"]))
        self.assertEqual(
            manifest["artifacts"]["pdf_verification"]["sha256"],
            hashlib.sha256((self.project / "exports/pdf-verification.json").read_bytes()).hexdigest(),
        )

    def test_golden_pipeline_rejects_changed_page_after_export(self):
        self._prepare_ready_artifacts()
        finalize_project(self.project)
        page = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), "magenta").save(page)
        from scripts.validate_project import ProjectValidationError, require_valid_project
        with self.assertRaises(ProjectValidationError) as context:
            require_valid_project(self.project, "final")
        stale_fields = {
            (issue.path, issue.field)
            for issue in context.exception.issues
        }
        self.assertIn(
            ("exports/pdf-verification.json", "pdf-verification-stale"),
            stale_fields,
        )


if __name__ == "__main__":
    unittest.main()
