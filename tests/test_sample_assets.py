import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples/sunlight-courier"
MATERIALIZER = ROOT / "scripts/materialize_sample.py"


class SampleAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project = Path(self.temporary_directory.name) / "sunlight-courier"
        shutil.copytree(SAMPLE, self.project)
        for relative in ("panels/raw", "panels/clean"):
            shutil.rmtree(self.project / relative, ignore_errors=True)

    def run_materializer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MATERIALIZER), str(self.project)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

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


if __name__ == "__main__":
    unittest.main()
