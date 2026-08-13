import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from normalize_panels import (  # noqa: E402
    NormalizationSpec,
    normalization_geometry,
    normalize_panel,
    normalize_panels,
)
from validate_project import validate_panel_provenance  # noqa: E402
import letter_panels  # noqa: E402
import pdf_quality  # noqa: E402
import raster_limits  # noqa: E402
import normalize_panels as normalize_panels_module  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class NormalizationGeometryTests(unittest.TestCase):
    def test_raster_modules_share_one_decode_ceiling(self):
        self.assertEqual(raster_limits.MAX_DECODED_PIXELS, normalize_panels_module.MAX_DECODED_PIXELS)
        self.assertEqual(raster_limits.MAX_DECODED_PIXELS, letter_panels.MAX_DECODED_PIXELS)
        self.assertEqual(raster_limits.MAX_DECODED_PIXELS, pdf_quality.MAX_DECODED_PIXELS)

    def test_runtime_modules_do_not_mutate_pillow_global_pixel_limit(self):
        for name in ("comic_sol.py", "normalize_panels.py", "letter_panels.py", "pdf_quality.py"):
            with self.subTest(name=name):
                source = (ROOT / "scripts" / name).read_text("utf-8")
                self.assertNotIn("Image.MAX_IMAGE_PIXELS =", source)
    def test_center_crop_records_oriented_source_box(self):
        geometry = normalization_geometry((1200, 800), (600, 600), "crop")
        self.assertEqual((200, 0, 1000, 800), geometry.crop_box)
        self.assertEqual((600, 600), geometry.target_size)
        self.assertEqual((1200, 800), geometry.source_size)
        self.assertEqual("crop", geometry.mode)

    def test_portrait_center_crop_uses_integer_source_coordinates(self):
        geometry = normalization_geometry((801, 1201), (600, 600), "crop")
        self.assertEqual((0, 200, 801, 1001), geometry.crop_box)

    def test_fit_and_exact_geometry_are_explicit(self):
        fitted = normalization_geometry((1200, 800), (600, 600), "fit")
        self.assertIsNone(fitted.crop_box)
        self.assertEqual((600, 400), fitted.resized_size)
        self.assertEqual((0, 100), fitted.paste_origin)

        exact = normalization_geometry((600, 600), (600, 600), "exact")
        self.assertIsNone(exact.crop_box)
        self.assertEqual((600, 600), exact.resized_size)
        self.assertEqual((0, 0), exact.paste_origin)

    def test_exact_rejects_aspect_mismatch_and_all_modes_reject_bad_sizes(self):
        with self.assertRaisesRegex(ValueError, "exact.*aspect"):
            normalization_geometry((1200, 800), (600, 600), "exact")
        with self.assertRaisesRegex(ValueError, "mode"):
            normalization_geometry((100, 100), (50, 50), "stretch")
        for source, target in (((0, 100), (50, 50)), ((100, 100), (-1, 50))):
            with self.subTest(source=source, target=target):
                with self.assertRaisesRegex(ValueError, "positive"):
                    normalization_geometry(source, target, "crop")


class NormalizePanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        (self.project / "panels/raw").mkdir(parents=True)
        (self.project / "logs").mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _source(self, name: str, size=(120, 80), fmt=None, exif_orientation=None) -> Path:
        path = self.project / "panels/raw" / name
        image = Image.new("RGB", size, "red")
        # Distinguishable edge regions expose crop/orientation mistakes.
        for x in range(size[0] // 3):
            for y in range(size[1]):
                image.putpixel((x, y), (0, 0, 255))
        for x in range(size[0] * 2 // 3, size[0]):
            for y in range(size[1]):
                image.putpixel((x, y), (0, 255, 0))
        kwargs = {}
        if exif_orientation is not None:
            exif = Image.Exif()
            exif[274] = exif_orientation
            kwargs["exif"] = exif
        image.save(path, format=fmt, **kwargs)
        return path

    def test_png_jpeg_and_webp_publish_canonical_clean_and_record(self):
        for suffix, fmt in (("png", "PNG"), ("jpg", "JPEG"), ("webp", "WEBP")):
            with self.subTest(fmt=fmt):
                panel_id = f"p01-0{len(list((self.project / 'panels').glob('p*'))) + 1}"
                source = self._source(f"{panel_id}.{suffix}", fmt=fmt)
                output = normalize_panel(
                    self.project, panel_id, source.relative_to(self.project).as_posix(),
                    (60, 60), "crop",
                )
                self.assertEqual(self.project / f"panels/{panel_id}/clean.png", output)
                with Image.open(output) as clean:
                    self.assertEqual((60, 60), clean.size)
                    self.assertEqual("RGB", clean.mode)
                    self.assertEqual("PNG", clean.format)
                record_path = self.project / f"panels/{panel_id}/normalization.json"
                record = json.loads(record_path.read_text("utf-8"))
                self.assertEqual("1.0", record["schema_version"])
                self.assertEqual("1", record["implementation_version"])
                self.assertEqual(panel_id, record["panel_id"])
                self.assertEqual(fmt, record["source"]["format"])
                self.assertEqual([120, 80], record["source"]["size"])
                self.assertEqual(sha256(source), record["source"]["sha256"])
                self.assertEqual([20, 0, 100, 80], record["operation"]["crop_box"])
                self.assertEqual([60, 60], record["target_size"])
                self.assertEqual("panels/%s/clean.png" % panel_id, record["clean"]["path"])
                self.assertEqual(sha256(output), record["clean"]["sha256"])
                expected = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                self.assertEqual(expected.encode(), record_path.read_bytes())

    def test_exif_orientation_is_applied_before_geometry(self):
        source = self._source("oriented.jpg", size=(120, 80), fmt="JPEG", exif_orientation=6)
        normalize_panel(self.project, "p01-01", "panels/raw/oriented.jpg", (60, 60), "crop")
        record = json.loads(
            (self.project / "panels/p01-01/normalization.json").read_text("utf-8")
        )
        self.assertEqual(6, record["source"]["exif_orientation"])
        self.assertEqual([120, 80], record["source"]["encoded_size"])
        self.assertEqual([80, 120], record["source"]["size"])
        self.assertEqual([0, 20, 80, 100], record["operation"]["crop_box"])

    def test_identical_second_run_is_byte_identical(self):
        self._source("panel.png")
        clean = normalize_panel(
            self.project, "p01-01", "panels/raw/panel.png", (60, 60), "fit"
        )
        record = self.project / "panels/p01-01/normalization.json"
        first = (clean.read_bytes(), record.read_bytes())
        normalize_panel(
            self.project, "p01-01", "panels/raw/panel.png", (60, 60), "fit"
        )
        self.assertEqual(first, (clean.read_bytes(), record.read_bytes()))

    def test_failure_preserves_existing_clean_and_record(self):
        output_dir = self.project / "panels/p01-01"
        output_dir.mkdir(parents=True)
        clean = output_dir / "clean.png"
        record = output_dir / "normalization.json"
        clean.write_bytes(b"old-clean")
        record.write_bytes(b"old-record")
        bad = self.project / "panels/raw/bad.png"
        bad.write_bytes(b"not an image")

        with self.assertRaisesRegex(ValueError, "readable image"):
            normalize_panel(self.project, "p01-01", "panels/raw/bad.png", (60, 60), "crop")
        self.assertEqual(b"old-clean", clean.read_bytes())
        self.assertEqual(b"old-record", record.read_bytes())

    def test_traversal_invalid_mode_and_pixel_limit_fail_before_publication(self):
        self._source("panel.png")
        for source, mode, message in (
            ("../outside.png", "crop", "relative project path"),
            ("panels/raw/panel.png", "stretch", "mode"),
        ):
            with self.subTest(source=source, mode=mode):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_panel(self.project, "p01-01", source, (60, 60), mode)
        with self.assertRaisesRegex(ValueError, "pixel limit"):
            normalize_panel(
                self.project, "p01-01", "panels/raw/panel.png",
                (100_000, 100_000), "crop",
            )
        self.assertFalse((self.project / "panels/p01-01/clean.png").exists())

    def test_batch_preflights_every_source_before_mutating_any_panel(self):
        self._source("one.png")
        bad = self.project / "panels/raw/two.png"
        bad.write_bytes(b"broken")
        existing = self.project / "panels/p01-01"
        existing.mkdir(parents=True)
        (existing / "clean.png").write_bytes(b"retained")

        specs = (
            NormalizationSpec("p01-01", "panels/raw/one.png", (60, 60), "crop"),
            NormalizationSpec("p01-02", "panels/raw/two.png", (60, 60), "crop"),
        )
        with self.assertRaisesRegex(ValueError, "readable image"):
            normalize_panels(self.project, specs)
        self.assertEqual(b"retained", (existing / "clean.png").read_bytes())
        self.assertFalse((self.project / "panels/p01-02").exists())


class PanelProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        (self.project / "panels/raw").mkdir(parents=True)
        (self.project / "logs").mkdir()
        raw = self.project / "panels/raw/p01-01.png"
        Image.new("RGB", (120, 80), "navy").save(raw)
        normalize_panel(
            self.project, "p01-01", "panels/raw/p01-01.png", (60, 60), "crop"
        )
        clean = self.project / "panels/p01-01/clean.png"
        normalization = self.project / "panels/p01-01/normalization.json"
        self.record = {
            "schema_version": "2.0",
            "kind": "panel-qa",
            "subject_id": "p01-01",
            "bindings": {
                "raw_path": "panels/raw/p01-01.png",
                "raw_sha256": sha256(raw),
                "raw_width": 120,
                "raw_height": 80,
                "clean_path": "panels/p01-01/clean.png",
                "clean_sha256": sha256(clean),
                "clean_width": 60,
                "clean_height": 60,
                "normalization_path": "panels/p01-01/normalization.json",
                "normalization_sha256": sha256(normalization),
            },
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_stale(self, issues, binding):
        self.assertTrue(any(
            issue.field == f"bindings.{binding}"
            and issue.message.startswith("quality-record-stale:")
            for issue in issues
        ), issues)

    def test_current_raw_clean_and_normalization_bindings_pass(self):
        self.assertEqual((), validate_panel_provenance(self.project, self.record))

    def test_raw_clean_and_normalization_mutation_are_each_stale(self):
        paths = {
            "raw_sha256": self.project / "panels/raw/p01-01.png",
            "clean_sha256": self.project / "panels/p01-01/clean.png",
            "normalization_sha256": self.project / "panels/p01-01/normalization.json",
        }
        for binding, path in paths.items():
            with self.subTest(binding=binding):
                before = path.read_bytes()
                path.write_bytes(before + b"changed")
                self.assert_stale(
                    validate_panel_provenance(self.project, self.record), binding
                )
                path.write_bytes(before)

    def test_clean_dimensions_are_recomputed(self):
        self.record["bindings"]["clean_width"] = 59
        self.assert_stale(
            validate_panel_provenance(self.project, self.record), "clean_width"
        )

    def test_missing_normalization_and_traversal_fail_closed(self):
        (self.project / "panels/p01-01/normalization.json").unlink()
        self.assert_stale(
            validate_panel_provenance(self.project, self.record),
            "normalization_path",
        )
        self.record["bindings"]["raw_path"] = "../outside.png"
        self.assert_stale(
            validate_panel_provenance(self.project, self.record), "raw_path"
        )

    def test_one_panel_change_leaves_unrelated_panel_byte_identical_and_current(self):
        raw_two = self.project / "panels/raw/p01-02.png"
        Image.new("RGB", (100, 100), "green").save(raw_two)
        normalize_panel(
            self.project, "p01-02", "panels/raw/p01-02.png", (50, 50), "exact"
        )
        clean_two = self.project / "panels/p01-02/clean.png"
        normalization_two = self.project / "panels/p01-02/normalization.json"
        record_two = {
            "schema_version": "2.0",
            "kind": "panel-qa",
            "subject_id": "p01-02",
            "bindings": {
                "raw_path": "panels/raw/p01-02.png",
                "raw_sha256": sha256(raw_two),
                "raw_width": 100,
                "raw_height": 100,
                "clean_path": "panels/p01-02/clean.png",
                "clean_sha256": sha256(clean_two),
                "clean_width": 50,
                "clean_height": 50,
                "normalization_path": "panels/p01-02/normalization.json",
                "normalization_sha256": sha256(normalization_two),
            },
        }
        retained = {
            "raw": raw_two.read_bytes(),
            "clean": clean_two.read_bytes(),
            "normalization": normalization_two.read_bytes(),
        }

        changed = self.project / "panels/raw/p01-01.png"
        changed.write_bytes(changed.read_bytes() + b"changed")

        self.assert_stale(
            validate_panel_provenance(self.project, self.record), "raw_sha256"
        )
        self.assertEqual((), validate_panel_provenance(self.project, record_two))
        self.assertEqual(retained["raw"], raw_two.read_bytes())
        self.assertEqual(retained["clean"], clean_two.read_bytes())
        self.assertEqual(retained["normalization"], normalization_two.read_bytes())


if __name__ == "__main__":
    unittest.main()
