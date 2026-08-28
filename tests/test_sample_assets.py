import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/sunlight-courier"
MATERIALIZER = ROOT / "scripts/materialize_sample.py"
MAX_TRACKED_RASTER_BYTES = 22 * 1024 * 1024


def tracked_sample_rasters() -> tuple[Path, ...]:
    compatibility = {
        SAMPLE / "panels/raw",
        SAMPLE / "panels/clean",
    }
    return tuple(
        sorted(path for path in SAMPLE.glob("**/*.png") if path.parent not in compatibility)
    )


def expected_sample_raster_size(path: Path) -> tuple[int, int]:
    relative = path.relative_to(SAMPLE)
    if relative.parts[0] == "pages":
        return (1600, 2400)
    if relative.parts[0] == "panels" and relative.name in {"clean.png", "lettered.png"}:
        return (1024, 779)
    if relative == Path("references/characters/mira.png"):
        return (1024, 1536)
    raise AssertionError(f"unclassified tracked sample raster: {relative}")


from tests.support import make_symlink  # noqa: E402


class SampleAssetTests(unittest.TestCase):
    def run_materializer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MATERIALIZER), str(self.project)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "sunlight-courier"
        shutil.copytree(SAMPLE, self.project)
        for relative in ("panels/raw", "panels/clean"):
            shutil.rmtree(self.project / relative, ignore_errors=True)

    def test_tracked_sample_rasters_stay_within_weight_budget(self):
        total = sum(path.stat().st_size for path in tracked_sample_rasters())

        self.assertLessEqual(total, MAX_TRACKED_RASTER_BYTES)
        for path in tracked_sample_rasters():
            with Image.open(path) as image:
                self.assertEqual("PNG", image.format)
                self.assertEqual("RGB", image.mode)
                self.assertEqual(expected_sample_raster_size(path), image.size)
                self.assertNotIn("transparency", image.info)

    def test_cli_materializes_byte_identical_compatibility_panels(self):
        result = self.run_materializer()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("materialized 8 sample panel copies", result.stdout.strip())
        for panel_id in ("p01-01", "p01-02", "p02-01", "p02-02"):
            canonical = (self.project / f"panels/{panel_id}/clean.png").read_bytes()
            self.assertEqual(
                canonical,
                (self.project / f"panels/raw/{panel_id}.png").read_bytes(),
            )
            self.assertEqual(
                canonical,
                (self.project / f"panels/clean/{panel_id}.png").read_bytes(),
            )

    def test_missing_source_fails_before_creating_compatibility_panels(self):
        (self.project / "panels/p02-02/clean.png").unlink()

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn("missing canonical panel: p02-02", result.stderr)
        self.assertFalse((self.project / "panels/raw").exists())
        self.assertFalse((self.project / "panels/clean").exists())

    def test_non_object_manifest_reports_an_actionable_error(self):
        (self.project / "project.json").write_text("[]\n", encoding="utf-8")

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn("project.json must contain an object", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_noncanonical_panel_id_is_rejected_before_copying(self):
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["panels"] = ["../escape"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "project.json panels must contain unique canonical panel IDs",
            result.stderr,
        )
        self.assertFalse((self.project / "panels/raw").exists())
        self.assertFalse((self.project / "panels/clean").exists())

    def test_linked_canonical_source_is_rejected_before_copying(self):
        outside = self.root / "outside-source"
        shutil.copytree(self.project / "panels/p01-01", outside)
        shutil.rmtree(self.project / "panels/p01-01")
        make_symlink(
            self,
            self.project / "panels/p01-01",
            outside,
            directory=True,
        )

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn("symlink", result.stderr)
        self.assertFalse((self.project / "panels/raw").exists())
        self.assertFalse((self.project / "panels/clean").exists())

    def test_linked_output_directory_is_rejected_without_external_write(self):
        outside = self.root / "outside-output"
        outside.mkdir()
        make_symlink(
            self,
            self.project / "panels/raw",
            outside,
            directory=True,
        )

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn("symlink", result.stderr)
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse((self.project / "panels/clean").exists())

    def test_linked_output_file_is_rejected_without_external_write(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"sentinel")
        raw = self.project / "panels/raw"
        raw.mkdir()
        make_symlink(self, raw / "p01-01.png", outside)

        result = self.run_materializer()

        self.assertEqual(1, result.returncode)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(b"sentinel", outside.read_bytes())
        self.assertFalse((self.project / "panels/clean").exists())


class SampleDocumentationContractTests(unittest.TestCase):
    """The tracked sample catalog keeps visual and mechanical evidence distinct."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = (ROOT / "samples/README.md").read_text(encoding="utf-8")
        cls.sunlight = (SAMPLE / "README.md").read_text(encoding="utf-8")

    def test_catalog_claims_exactly_one_visual_quality_sample(self):
        live_rows = [
            line
            for line in self.catalog.splitlines()
            if line.startswith("|") and "Live-generated" in line
        ]
        self.assertEqual(1, len(live_rows))
        self.assertIn("[`sunlight-courier`](sunlight-courier)", live_rows[0])
        self.assertIn("only initial visual-quality sample", self.catalog)

    def test_catalog_labels_deterministic_samples_mechanics_only(self):
        deterministic = self.catalog.split("### Deterministic", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(deterministic.split())
        self.assertIn("mechanics-only", normalized)
        self.assertIn("never proof of illustration quality", normalized)

    def test_sunlight_readme_labels_retained_evidence_and_claim_limit(self):
        normalized = " ".join(self.sunlight.split())
        for phrase in (
            "retained live visual evidence",
            "only initial visual-quality sample",
            "does not establish broad or universal illustration quality",
            "Provider/model provenance",
            "Reviewer and visual-QA evidence",
        ):
            self.assertIn(phrase, normalized, phrase)

    def test_sunlight_readme_links_pages_pdf_project_and_qa(self):
        for target in (
            "pages/page-001.png",
            "pages/page-002.png",
            "exports/sunlight-courier.pdf",
            "project.json",
            "qa/report.md",
        ):
            self.assertIn(f"]({target})", self.sunlight, target)

    def test_sunlight_readme_states_known_limitations(self):
        self.assertIn("## Known limitations", self.sunlight)
        limitations = self.sunlight.split("## Known limitations", 1)[1]
        normalized = " ".join(limitations.split())
        self.assertIn(
            "The retained reviewer label does not identify a human reviewer.",
            normalized,
        )
        self.assertIn("provider and model are not separately identified", normalized)


if __name__ == "__main__":
    unittest.main()
