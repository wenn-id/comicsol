import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from comic_sol_product.engine.compose_pages import compose_project  # noqa: E402
from comic_sol_product.engine.export_pdf import export_pdf  # noqa: E402
from comic_sol_product.engine.letter_panels import letter_project  # noqa: E402
from comic_sol_product.engine.render_report import render_report  # noqa: E402
from comic_sol_product.engine.validate_project import validate_project  # noqa: E402
from tests.test_export_pdf import pdf_frames  # noqa: E402


class OfflinePipelineIntegrationTests(unittest.TestCase):
    def test_valid_fixture_runs_deterministic_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            self.assertEqual([], validate_project(project, "panels"))
            lettered = letter_project(project)
            pages = compose_project(project)
            first_page = pages[0].read_bytes()
            pdf = export_pdf(project)
            report = render_report(project)
            self.assertEqual(3, len(lettered))
            self.assertEqual(["page-001.png"], [path.name for path in pages])
            frames = pdf_frames(pdf)
            self.assertEqual(1, len(frames))
            for frame in frames:
                frame.close()
            self.assertIn("No unresolved warnings", report.read_text("utf-8"))
            self.assertEqual(first_page, compose_project(project)[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
