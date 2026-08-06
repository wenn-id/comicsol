import io
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pdf_quality import (  # noqa: E402
    PDF_TOLERANCE_VERSION,
    PdfQualityError,
    compare_full_page,
    verify_pdf_payload,
)


def synthetic_page(accent, *, size=(240, 360)):
    image = Image.new("RGB", size, (248, 246, 240))
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((0, 0, 18, 18), fill=accent)
    draw.rectangle((width - 19, height - 19, width - 1, height - 1), fill=accent)
    draw.rectangle((width // 4, height // 3, width * 3 // 4, height * 2 // 3), fill=(24, 32, 48))
    for index in range(9):
        y = height // 2 - 20 + index * 5
        draw.line((width // 3, y, width * 2 // 3, y), fill=accent, width=1)
    draw.text((width // 3, height // 2 + 28), "FINE LETTERING 123", fill=(8, 8, 8))
    return image


def pdf_payload(images):
    output = io.BytesIO()
    copies = [image.copy() for image in images]
    try:
        copies[0].save(
            output,
            format="PDF",
            resolution=150.0,
            save_all=True,
            append_images=copies[1:],
            title=False,
            producer="Comic Sol 2.0",
            creationDate=False,
            modDate=False,
        )
        return output.getvalue()
    finally:
        for image in copies:
            image.close()


class PdfMetricTests(unittest.TestCase):
    def test_pillow_round_trip_calibrates_named_full_page_metrics(self):
        source = synthetic_page((180, 20, 40))
        verification = verify_pdf_payload(pdf_payload([source]), [source])

        self.assertEqual("1", PDF_TOLERANCE_VERSION)
        self.assertEqual(1, verification["page_count"])
        metrics = verification["pages"][0]
        self.assertEqual([240, 360], metrics["dimensions"])
        self.assertEqual("RGB", metrics["mode"])
        self.assertLessEqual(metrics["mean_absolute_channel_error"], 2.2)
        self.assertLessEqual(metrics["high_error_pixel_ratio"], 0.021)
        self.assertLessEqual(metrics["maximum_grid_region_error"], 16.0)
        self.assertEqual(240 * 360, metrics["compared_pixels"])

    def test_metrics_are_deterministic_and_cover_every_pixel(self):
        source = synthetic_page((20, 80, 180))
        decoded = source.copy()
        decoded.putpixel((120, 180), (255, 255, 255))

        first = compare_full_page(source, decoded)
        second = compare_full_page(source, decoded)
        self.assertEqual(first, second)
        self.assertEqual(240 * 360, first.compared_pixels)
        self.assertGreater(first.mean_absolute_channel_error, 0)

    def test_size_and_mode_are_explicit(self):
        source = synthetic_page((20, 120, 60))
        with self.assertRaisesRegex(PdfQualityError, "dimensions"):
            compare_full_page(source, source.resize((120, 180)))


class PdfCorruptionTests(unittest.TestCase):
    def setUp(self):
        self.first = synthetic_page((210, 30, 30))
        self.second = synthetic_page((20, 70, 210))

    def tearDown(self):
        self.first.close()
        self.second.close()

    def assert_rejected(self, images, pattern):
        with self.assertRaisesRegex(PdfQualityError, pattern):
            verify_pdf_payload(pdf_payload(images), [self.first, self.second])

    def test_center_erasure_is_rejected_even_when_corners_match(self):
        mutated = self.first.copy()
        draw = ImageDraw.Draw(mutated)
        draw.rectangle((70, 110, 170, 250), fill=(248, 246, 240))
        try:
            self.assert_rejected([mutated, self.second], "content mismatch.*page 1")
        finally:
            mutated.close()

    def test_fine_lettering_erasure_is_rejected(self):
        mutated = self.first.copy()
        draw = ImageDraw.Draw(mutated)
        draw.rectangle((70, 195, 190, 230), fill=(248, 246, 240))
        try:
            self.assert_rejected([mutated, self.second], "content mismatch.*page 1")
        finally:
            mutated.close()

    def test_swapped_duplicate_and_count_mismatch_are_rejected(self):
        self.assert_rejected([self.second, self.first], "content mismatch.*page 1")
        self.assert_rejected([self.first, self.first], "content mismatch.*page 2")
        with self.assertRaisesRegex(PdfQualityError, "page count"):
            verify_pdf_payload(pdf_payload([self.first]), [self.first, self.second])

    def test_corrupt_or_truncated_payload_is_rejected(self):
        payload = pdf_payload([self.first, self.second])
        with self.assertRaisesRegex(PdfQualityError, "decode|corrupt|page count"):
            verify_pdf_payload(payload[: len(payload) // 3], [self.first, self.second])

    def test_decompression_bomb_warning_is_a_decode_failure(self):
        import unittest.mock as mock

        payload = b"%PDF-1.7\nstream\n\xff\xd8fake\nendstream\n%%EOF"
        with mock.patch(
            "pdf_quality.Image.open",
            side_effect=Image.DecompressionBombWarning("unsafe dimensions"),
        ):
            with self.assertRaisesRegex(PdfQualityError, "decode"):
                verify_pdf_payload(payload, [self.first])


if __name__ == "__main__":
    unittest.main()
