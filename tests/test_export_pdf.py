import hashlib
import io
import random
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]

from scripts.comic_sol import atomic_write_json, read_json  # noqa: E402
from scripts.export_pdf import (  # noqa: E402
    PdfExportError,
    discover_pages,
    export_pdf,
    guarded_export,
    main,
)
from scripts.compose_pages import compose_project  # noqa: E402
from scripts.letter_panels import letter_project  # noqa: E402


def pdf_frames(path):
    try:
        pdf = Image.open(path)
    except UnidentifiedImageError:
        payload = path.read_bytes()
        frames = []
        for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", payload, re.DOTALL):
            stream = match.group(1)
            if stream.startswith(b"\xff\xd8"):
                image = Image.open(io.BytesIO(stream)).convert("RGB")
                image.load()
                frames.append(image)
        return frames
    frames = []
    for index in range(pdf.n_frames):
        pdf.seek(index)
        frames.append(pdf.convert("RGB").copy())
    return frames


class PdfExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "ordered-comic"
        (self.project / "pages").mkdir(parents=True)
        (self.project / "exports").mkdir()
        manifest = read_json(ROOT / "templates/manifest.json")
        manifest["project_id"] = "ordered-comic"
        manifest["input"]["source_sha256"] = "a" * 64
        manifest["settings"]["page_count"] = 3
        manifest["settings"]["panel_count"] = 3
        manifest["panels"] = ["p01-01", "p02-01", "p03-01"]
        atomic_write_json(self.project / "project.json", manifest)
        self.colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255))
        for number in (3, 1, 2):
            Image.new("RGB", (1600, 2400), self.colors[number - 1]).save(
                self.project / f"pages/page-{number:03d}.png"
            )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_color_close(self, expected, actual, tolerance=3):
        self.assertTrue(
            all(abs(left - right) <= tolerance for left, right in zip(expected, actual)),
            (expected, actual),
        )

    def test_discover_pages_uses_numeric_filename_order_only(self):
        Image.new("RGB", (1600, 2400), "black").save(
            self.project / "pages/page-final.png"
        )
        self.assertEqual(
            ["page-001.png", "page-002.png", "page-003.png"],
            [path.name for path in discover_pages(self.project)],
        )

    def test_default_pdf_has_numeric_order_count_size_rgb_and_no_margin(self):
        output = export_pdf(self.project)
        self.assertEqual(self.project / "exports/ordered-comic.pdf", output)
        frames = pdf_frames(output)
        self.assertEqual(3, len(frames))
        for index, frame in enumerate(frames):
            self.assertEqual("RGB", frame.mode)
            self.assertEqual((1600, 2400), frame.size)
            self.assert_color_close(self.colors[index], frame.getpixel((0, 0)))
            self.assert_color_close(self.colors[index], frame.getpixel((1599, 2399)))

    def test_pdf_records_150_dpi_within_round_trip_tolerance(self):
        output = export_pdf(self.project)
        payload = output.read_bytes()
        media_boxes = re.findall(
            rb"/MediaBox\s*\[\s*0\s+0\s+([0-9.]+)\s+([0-9.]+)\s*\]",
            payload,
        )
        self.assertEqual(3, len(media_boxes))
        for width, height in media_boxes:
            effective_x = 1600 * 72 / float(width)
            effective_y = 2400 * 72 / float(height)
            self.assertAlmostEqual(150.0, effective_x, delta=0.2)
            self.assertAlmostEqual(150.0, effective_y, delta=0.2)

    def test_detailed_page_survives_pdf_fidelity_gate(self):
        noise = Image.frombytes(
            "L", (320, 480), random.Random(0).randbytes(320 * 480)
        )
        page = ImageOps.colorize(
            noise.resize((1600, 2400), Image.Resampling.LANCZOS),
            (4, 12, 28),
            (230, 150, 70),
        )
        noise.close()
        page.save(self.project / "pages/page-001.png")
        page.close()

        self.assertTrue(export_pdf(self.project).is_file())

    def test_custom_output_path_and_cli_are_supported(self):
        custom = self.project / "deliverables/custom.pdf"
        self.assertEqual(custom, export_pdf(self.project, custom))
        self.assertTrue(custom.is_file())
        cli_output = self.project / "deliverables/cli.pdf"
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, main([str(self.project), "--output", str(cli_output)]))
        self.assertTrue(cli_output.is_file())

    def test_repeated_export_is_byte_identical_and_has_fixed_metadata(self):
        output = export_pdf(self.project)
        first = hashlib.sha256(output.read_bytes()).hexdigest()
        output = export_pdf(self.project)
        self.assertEqual(first, hashlib.sha256(output.read_bytes()).hexdigest())
        payload = output.read_bytes()
        self.assertIn(b"\xfe\xff" + "Comic Sol 1.0".encode("utf-16-be"), payload)
        self.assertNotIn(b"CreationDate", payload)
        self.assertNotIn(b"ModDate", payload)

    def test_final_pdf_publication_uses_durable_atomic_writer(self):
        destination = self.project / "exports/durable.pdf"
        from scripts import export_pdf as export_pdf_module

        real_writer = export_pdf_module.durable_atomic_write
        with mock.patch(
            "scripts.export_pdf.durable_atomic_write", wraps=real_writer
        ) as writer:
            self.assertEqual(destination, export_pdf(self.project, destination))
        writer.assert_called_once()
        self.assertEqual(destination, writer.call_args.args[0])
        self.assertTrue(writer.call_args.args[1].startswith(b"%PDF"))

    def test_guarded_export_rejects_traversal_before_creating_directory(self):
        outside = self.project.parent / "outside" / "comic.pdf"
        with self.assertRaisesRegex(PdfExportError, "inside the project"):
            guarded_export(self.project, self.project / ".." / "outside" / "comic.pdf")
        self.assertFalse(outside.parent.exists())

    def test_guarded_export_reports_missing_page_qa_as_pdf_error(self):
        with self.assertRaisesRegex(PdfExportError, "qa/pages/page-001.json"):
            guarded_export(self.project)

    def test_missing_noncontiguous_and_wrong_size_pages_are_refused_atomically(self):
        output = self.project / "exports/ordered-comic.pdf"
        output.write_bytes(b"previous-good-pdf")

        (self.project / "pages/page-002.png").unlink()
        with self.assertRaisesRegex(PdfExportError, "page-002"):
            export_pdf(self.project)
        self.assertEqual(b"previous-good-pdf", output.read_bytes())

        Image.new("RGB", (1600, 2400), self.colors[1]).save(
            self.project / "pages/page-002.png"
        )
        (self.project / "pages/page-003.png").rename(
            self.project / "pages/page-004.png"
        )
        with self.assertRaisesRegex(PdfExportError, "contiguous|page-003"):
            export_pdf(self.project)
        self.assertEqual(b"previous-good-pdf", output.read_bytes())

        (self.project / "pages/page-004.png").rename(
            self.project / "pages/page-003.png"
        )
        Image.new("RGB", (800, 1200), "blue").save(
            self.project / "pages/page-003.png"
        )
        with self.assertRaisesRegex(PdfExportError, "page-003.*1600.*2400"):
            export_pdf(self.project)
        self.assertEqual(b"previous-good-pdf", output.read_bytes())

        for page in (self.project / "pages").glob("page-*.png"):
            page.unlink()
        with self.assertRaisesRegex(PdfExportError, "no composed page"):
            export_pdf(self.project)
        self.assertEqual(b"previous-good-pdf", output.read_bytes())


class ExportFixtureIntegrationTests(unittest.TestCase):
    def test_valid_fixture_exports_one_readable_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            letter_project(project)
            compose_project(project)
            frames = pdf_frames(export_pdf(project))
            self.assertEqual(1, len(frames))
            for frame in frames:
                frame.close()


if __name__ == "__main__":
    unittest.main()
