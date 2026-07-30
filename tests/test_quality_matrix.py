import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from comic_sol import (
    atomic_write_json,
    finalize_project,
    init_project,
    read_json,
    sha256_file,
)
from normalize_panels import normalize_panel
from page_quality import build_page_quality_record, write_page_quality_record
from quality_sample import EvidenceModeError, build_evidence_record, main
from tests.support import QUALITY_SCENARIOS, build_quality_fixture
from tests.test_validation import (
    valid_characters,
    valid_manifest,
    valid_panel_record_v2,
    valid_story,
    valid_storyboard,
)
from validate_project import validate_project


REQUIRED_DIMENSIONS = {
    "characters:recurring-pair",
    "continuity:wardrobe",
    "continuity:prop",
    "continuity:palette",
    "layout:single",
    "layout:two-horizontal",
    "layout:two-vertical",
    "layout:three-top",
    "layout:three-bottom",
    "layout:four-grid",
    "text:dense-dialogue",
    "text:caption",
    "text:sfx",
    "orientation:portrait",
    "orientation:landscape",
    "format:png",
    "format:jpeg",
    "format:webp",
    "format:exif",
    "typography:regular",
    "typography:bold",
    "typography:combining",
    "typography:non-latin-fallback",
    "retry:transient-repeat",
    "retry:visual-retry",
    "outcome:accepted-warning",
    "outcome:hard-failure",
    "resume:interrupted",
}


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def page_reviewer_checks():
    return [
        {
            "id": "face-action-obstruction",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected every panel region for face and action obstruction.",
            "method": "bounded-visual-review",
            "reviewer": "matrix-reviewer",
            "regions": [{"scope": "all-panels"}],
        },
        {
            "id": "bubble-tail-direction",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected every bubble tail against its intended speaker.",
            "method": "bounded-visual-review",
            "reviewer": "matrix-reviewer",
            "regions": [{"scope": "all-bubbles"}],
        },
        {
            "id": "accidental-text-watermark",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected the full page for accidental text and watermarks.",
            "method": "bounded-visual-review",
            "reviewer": "matrix-reviewer",
            "regions": [{"scope": "page"}],
        },
    ]


def build_lifecycle_project(root: Path) -> Path:
    project = init_project(
        root,
        "Quality Matrix Lifecycle",
        b"A deterministic local-only lifecycle fixture.",
        {"mode": "short_prompt", "language": "en"},
    )
    manifest = read_json(project / "project.json")
    manifest.update(valid_manifest())
    manifest["project_id"] = project.name
    manifest["title"] = "Quality Matrix Lifecycle"
    manifest["status"] = "QA_READY"
    manifest["input"]["source_sha256"] = sha256_file(project / "source/input.txt")
    atomic_write_json(project / "project.json", manifest)
    atomic_write_json(project / "plan/story-plan.json", valid_story())
    atomic_write_json(project / "plan/character-bible.json", valid_characters())
    atomic_write_json(project / "plan/storyboard.json", valid_storyboard())
    prompt = project / "prompts/panels/p01-01.txt"
    prompt.write_text("local deterministic panel prompt\n", encoding="utf-8")
    Image.new("RGB", (512, 512), "white").save(
        project / "references/characters/mira.png"
    )
    raw = project / "panels/raw/p01-01.png"
    image = Image.new("RGB", (736, 1136), (20, 30, 40))
    try:
        image.save(raw, format="PNG", optimize=False, compress_level=9)
    finally:
        image.close()
    clean = normalize_panel(
        project, "p01-01", "panels/raw/p01-01.png", (736, 1136), "exact"
    )
    shutil.copyfile(clean, project / "panels/clean/p01-01.png")
    panel_record = valid_panel_record_v2()
    panel_record["bindings"]["raw_sha256"] = sha256_file(raw)
    panel_record["bindings"]["clean_sha256"] = sha256_file(clean)
    panel_record["bindings"]["normalization_sha256"] = sha256_file(
        project / "panels/p01-01/normalization.json"
    )
    atomic_write_json(project / "qa/panels/p01-01.json", panel_record)
    manifest = read_json(project / "project.json")
    manifest["artifacts"] = {
        "character_bible": {
            "path": "plan/character-bible.json",
            "sha256": sha256_file(project / "plan/character-bible.json"),
        },
        "story_plan": {
            "path": "plan/story-plan.json",
            "sha256": sha256_file(project / "plan/story-plan.json"),
        },
        "storyboard": {
            "path": "plan/storyboard.json",
            "sha256": sha256_file(project / "plan/storyboard.json"),
        },
    }
    atomic_write_json(project / "project.json", manifest)
    return project


class QualityMatrixContractTests(unittest.TestCase):
    def test_registry_covers_every_required_quality_dimension(self):
        covered = {
            dimension
            for scenario in QUALITY_SCENARIOS.values()
            for dimension in scenario["dimensions"]
        }
        self.assertEqual(set(), REQUIRED_DIMENSIONS - covered)
        self.assertGreaterEqual(len(QUALITY_SCENARIOS), 8)
        for name, scenario in QUALITY_SCENARIOS.items():
            with self.subTest(name=name):
                self.assertEqual(name, scenario["name"])
                self.assertEqual("deterministic", scenario["evidence_mode"])
                self.assertTrue(scenario["dimensions"])

    def test_fixture_generation_is_local_only_and_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for scenario in QUALITY_SCENARIOS:
                first = build_quality_fixture(root / "first", scenario)
                second = build_quality_fixture(root / "second", scenario)
                with self.subTest(scenario=scenario):
                    self.assertEqual(tree_digest(first), tree_digest(second))
                    metadata = json.loads(
                        (first / "quality-fixture.json").read_text("utf-8")
                    )
                    self.assertEqual(scenario, metadata["scenario"])
                    self.assertEqual("deterministic", metadata["evidence_mode"])
                    self.assertTrue(metadata["local_only"])
                    serialized = json.dumps(metadata, sort_keys=True).lower()
                    self.assertNotIn("http://", serialized)
                    self.assertNotIn("https://", serialized)
                    self.assertNotIn("api_key", serialized)

    def test_unknown_scenario_is_rejected_before_allocation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            before = list(root.iterdir())
            with self.assertRaisesRegex(ValueError, "unknown quality scenario"):
                build_quality_fixture(root, "not-a-scenario")
            self.assertEqual(before, list(root.iterdir()))


class EvidenceModeContractTests(unittest.TestCase):
    def test_deterministic_evidence_is_labeled_as_mechanics_only(self):
        record = build_evidence_record("deterministic")
        self.assertEqual("deterministic", record["mode"])
        self.assertEqual("mechanics-only", record["scope"])
        self.assertFalse(record["proves_visual_quality"])
        self.assertNotIn("provider", record)

    def test_live_visual_requires_retained_attempt_and_discloses_provenance(self):
        with self.assertRaisesRegex(EvidenceModeError, "retained attempt"):
            build_evidence_record("live-visual")
        with self.assertRaisesRegex(EvidenceModeError, "provider and model"):
            build_evidence_record(
                "live-visual", retained_attempt="panels/raw/p01-01/attempt-001.png"
            )
        record = build_evidence_record(
            "live-visual",
            retained_attempt="panels/raw/p01-01/attempt-001.png",
            attempt_sha256="a" * 64,
            provider="local-test-provider",
            model="test-model-v1",
            references=["references/characters/aria.png"],
            reviewer_method="bounded-visual-review",
            limitations=["synthetic fixture"],
        )
        self.assertEqual("live-visual", record["mode"])
        self.assertTrue(record["proves_visual_quality"])
        self.assertEqual("a" * 64, record["attempt_sha256"])
        self.assertEqual("local-test-provider", record["provider"])

    def test_runner_writes_canonical_deterministic_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            self.assertEqual(0, main([str(project), "--mode", "deterministic"]))
            path = project / "qa/evidence.json"
            self.assertTrue(path.is_file())
            record = json.loads(path.read_text("utf-8"))
            self.assertEqual(build_evidence_record("deterministic"), record)
            expected = (
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            self.assertEqual(expected, path.read_bytes())

    def test_runner_refuses_live_mode_without_retained_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            self.assertEqual(2, main([str(project), "--mode", "live-visual"]))
            self.assertFalse((project / "qa/evidence.json").exists())


class DeterministicLifecycleTests(unittest.TestCase):
    def test_engine_lifecycle_gates_review_then_completes_without_regenerating_panel(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = build_lifecycle_project(Path(temporary_directory))
            raw_hash = sha256_file(project / "panels/raw/p01-01.png")
            self.assertEqual(0, main([str(project), "--mode", "deterministic"]))

            with self.assertRaisesRegex(ValueError, "page_qa_required"):
                finalize_project(project)
            self.assertTrue((project / "pages/page-001.png").is_file())
            write_page_quality_record(
                project,
                1,
                build_page_quality_record(project, 1, page_reviewer_checks()),
            )
            result = finalize_project(project)

            self.assertEqual("COMPLETE", result["status"])
            self.assertEqual([], validate_project(project, "final"))
            self.assertEqual(raw_hash, sha256_file(project / "panels/raw/p01-01.png"))
            self.assertTrue((project / result["pdf"]).is_file())
            report = (project / result["report"]).read_text("utf-8")
            self.assertIn("Mode: deterministic", report)
            self.assertIn("does not prove live visual quality", report)
            verification = read_json(project / "exports/pdf-verification.json")
            self.assertEqual(1, verification["page_count"])

    def test_interrupted_export_resume_preserves_upstream_panel_hash(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = build_lifecycle_project(Path(temporary_directory))
            raw_hash = sha256_file(project / "panels/raw/p01-01.png")
            with self.assertRaisesRegex(ValueError, "page_qa_required"):
                finalize_project(project)
            write_page_quality_record(
                project,
                1,
                build_page_quality_record(project, 1, page_reviewer_checks()),
            )
            first_result = finalize_project(project)
            pdf = project / first_result["pdf"]
            pdf.unlink()
            manifest = read_json(project / "project.json")
            manifest["status"] = "COMPOSED"
            for key in ("pdf", "pdf_verification", "qa_report"):
                manifest["artifacts"].pop(key, None)
            atomic_write_json(project / "project.json", manifest)
            for relative in ("exports/pdf-verification.json", "qa/report.md"):
                path = project / relative
                if path.is_file():
                    path.unlink()

            result = finalize_project(project)
            self.assertEqual("COMPLETE", result["status"])
            self.assertEqual(raw_hash, sha256_file(project / "panels/raw/p01-01.png"))


if __name__ == "__main__":
    unittest.main()
