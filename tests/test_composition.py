import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from comic_sol import atomic_write_json  # noqa: E402
from compose_pages import compose_all_pages, compose_page  # noqa: E402
from layouts import FOUR_GRID_RECTS  # noqa: E402
from tests.support import make_symlink  # noqa: E402


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name)
        for relative in ("plan", "panels/p01-01", "panels/p01-02", "pages"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        self.storyboard = {
            "schema_version": "1.0",
            "pages": [{
                "number": 1,
                "layout": "two-horizontal",
                "panels": [
                    {"id": "p01-01", "rect": {"x": 64, "y": 64, "width": 1472, "height": 1120}},
                    {"id": "p01-02", "rect": {"x": 64, "y": 1216, "width": 1472, "height": 1120}},
                ],
            }],
        }
        self.settings = {"page_width": 1600, "page_height": 2400, "page_count": 1}
        Image.new("RGB", (800, 800), "red").save(
            self.project / "panels/p01-01/lettered.png"
        )
        Image.new("RGB", (800, 800), "green").save(
            self.project / "panels/p01-02/lettered.png"
        )
        atomic_write_json(self.project / "plan/storyboard.json", self.storyboard)
        atomic_write_json(self.project / "project.json", {
            "settings": self.settings, "artifacts": {}, "project_id": "composition-test",
        })

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_single_page_has_exact_dimensions_and_mode(self):
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        with Image.open(path) as page:
            self.assertEqual((1600, 2400), page.size)
            self.assertEqual("RGB", page.mode)
            self.assertEqual("PNG", page.format)

    def test_two_panels_are_pasted_at_exact_rect_centers(self):
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        with Image.open(path) as page:
            self.assertEqual((255, 0, 0), page.getpixel((800, 624)))
            self.assertEqual((0, 128, 0), page.getpixel((800, 1776)))
            self.assertEqual((255, 255, 255), page.getpixel((32, 32)))
            self.assertEqual((255, 255, 255), page.getpixel((800, 1200)))

    def test_panels_have_inward_six_pixel_black_borders(self):
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        with Image.open(path) as page:
            for offset in range(6):
                self.assertEqual((0, 0, 0), page.getpixel((64 + offset, 624)))
                self.assertEqual((0, 0, 0), page.getpixel((1535 - offset, 624)))
                self.assertEqual((0, 0, 0), page.getpixel((800, 64 + offset)))
                self.assertEqual((0, 0, 0), page.getpixel((800, 1183 - offset)))
            self.assertEqual((255, 255, 255), page.getpixel((63, 624)))
            self.assertEqual((255, 255, 255), page.getpixel((800, 1184)))
            self.assertEqual((255, 0, 0), page.getpixel((70, 624)))
            self.assertEqual((255, 0, 0), page.getpixel((800, 70)))

    def test_cover_crop_is_centered_and_preserves_aspect_ratio(self):
        source = Image.new("RGB", (1200, 600), "blue")
        for x in range(100):
            for y in range(600):
                source.putpixel((x, y), (255, 0, 255))
        source.save(self.project / "panels/p01-01/lettered.png")
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        with Image.open(path) as page:
            self.assertEqual((0, 0, 255), page.getpixel((70, 624)))
            self.assertEqual((0, 0, 255), page.getpixel((1529, 624)))

    def test_missing_panel_names_id_and_writes_no_page(self):
        (self.project / "panels/p01-02/lettered.png").unlink()
        output = self.project / "pages/page-001.png"
        with self.assertRaisesRegex(FileNotFoundError, "p01-02"):
            compose_page(self.project, 1, self.storyboard, self.settings, {})
        self.assertFalse(output.exists())

    def test_absolute_manifest_panel_path_is_rejected(self):
        outside = Path(self.temporary_directory.name).parent / "outside-panel.png"
        Image.new("RGB", (800, 800), "blue").save(outside)
        self.addCleanup(outside.unlink, missing_ok=True)
        artifacts = {"p01-01": {"path": str(outside)}}

        with self.assertRaisesRegex(ValueError, "relative project path"):
            compose_page(self.project, 1, self.storyboard, self.settings, artifacts)

    def test_symlink_manifest_panel_path_is_rejected(self):
        outside = Path(self.temporary_directory.name).parent / "outside-linked-panel.png"
        Image.new("RGB", (800, 800), "blue").save(outside)
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.project / "panels/linked.png"
        make_symlink(self, link, outside)

        with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
            compose_page(
                self.project, 1, self.storyboard, self.settings,
                {"p01-01": {"path": "panels/linked.png"}},
            )

    def test_symlink_swap_after_preflight_is_rejected_before_image_open(self):
        outside = Path(self.temporary_directory.name).parent / "outside-swapped-panel.png"
        Image.new("RGB", (800, 800), "blue").save(outside)
        self.addCleanup(outside.unlink, missing_ok=True)
        source = self.project / "panels/p01-01/lettered.png"

        from compose_pages import _page_sources

        def swap_after_preflight(*args, **kwargs):
            sources = _page_sources(*args, **kwargs)
            source.unlink()
            source.symlink_to(outside)
            return sources

        probe = self.project / "symlink-probe"
        make_symlink(self, probe, outside)
        probe.unlink()

        with patch("compose_pages._page_sources", side_effect=swap_after_preflight):
            with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
                compose_page(self.project, 1, self.storyboard, self.settings, {})

    def test_repeated_composition_has_identical_bytes(self):
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        first = hashlib.sha256(path.read_bytes()).hexdigest()
        path = compose_page(self.project, 1, self.storyboard, self.settings, {})
        self.assertEqual(first, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_declared_named_layout_must_match_storyboard_rectangles(self):
        self.storyboard["pages"][0]["layout"] = "full-page"
        with self.assertRaisesRegex(ValueError, "declared layout.*rectangles"):
            compose_page(self.project, 1, self.storyboard, self.settings, {})

    def test_four_grid_golden_slots_gutters_borders_and_order(self):
        colors = ((255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0))
        panels = []
        for index, ((x, y, width, height), color) in enumerate(
            zip(FOUR_GRID_RECTS, colors), 1
        ):
            panel_id = f"p01-{index:02d}"
            panel_dir = self.project / "panels" / panel_id
            panel_dir.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (width, height), color).save(
                panel_dir / "lettered.png"
            )
            panels.append({
                "id": panel_id,
                "order": index,
                "rect": {"x": x, "y": y, "width": width, "height": height},
            })
        self.storyboard["pages"][0] = {
            "number": 1,
            "layout": "four-grid",
            "panels": panels,
        }

        page_path = compose_page(
            self.project, 1, self.storyboard, self.settings, {}
        )
        with Image.open(page_path) as page:
            self.assertEqual((1600, 2400), page.size)
            for rectangle, color in zip(FOUR_GRID_RECTS, colors):
                x, y, width, height = rectangle
                self.assertEqual(color, page.getpixel((x + width // 2, y + height // 2)))
                self.assertEqual((0, 0, 0), page.getpixel((x, y + height // 2)))
                self.assertEqual((0, 0, 0), page.getpixel((x + width - 1, y + height // 2)))
            self.assertEqual((255, 255, 255), page.getpixel((25, 25)))
            self.assertEqual((255, 255, 255), page.getpixel((800, 600)))
            self.assertEqual((255, 255, 255), page.getpixel((800, 1200)))

    def test_composition_cache_binds_layout_storyboard_sources_settings_and_output(self):
        compose_all_pages(self.project)
        cache_path = self.project / "cache/composition.json"
        cache = json.loads(cache_path.read_text("utf-8"))

        self.assertEqual("2.0", cache["schema_version"])
        self.assertEqual("composition-cache", cache["kind"])
        self.assertEqual(1, len(cache["pages"]))
        page = cache["pages"][0]
        self.assertEqual("page-001", page["page_id"])
        self.assertEqual("two-horizontal", page["layout"]["name"])
        self.assertEqual("1", page["layout"]["version"])
        self.assertEqual(["p01-01", "p01-02"], page["panel_ids"])
        self.assertEqual(2, len(page["ordered_lettered_sha256s"]))
        self.assertTrue(all(len(value.split(":", 1)[1]) == 64
                            for value in page["ordered_lettered_sha256s"]))
        self.assertRegex(page["storyboard_page_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(page["settings_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual([1600, 2400], page["output"]["dimensions"])
        self.assertEqual("pages/page-001.png", page["output"]["path"])
        self.assertRegex(page["output"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            cache_path.read_text("utf-8"),
        )

    def test_composition_hashes_the_exact_source_bytes_it_composes(self):
        source = self.project / "panels/p01-01/lettered.png"
        original = source.read_bytes()
        replacement = io.BytesIO()
        Image.new("RGB", (800, 800), "blue").save(replacement, format="PNG")
        self.storyboard["pages"][0] = {
            "number": 1,
            "layout": "full-page",
            "panels": [{
                "id": "p01-01",
                "rect": {"x": 64, "y": 64, "width": 1472, "height": 2272},
            }],
        }
        atomic_write_json(self.project / "plan/storyboard.json", self.storyboard)

        with patch(
            "compose_pages.read_contained_bytes",
            side_effect=(original, replacement.getvalue()),
        ) as read:
            compose_all_pages(self.project)

        cache = json.loads((self.project / "cache/composition.json").read_text("utf-8"))
        self.assertEqual(1, read.call_count)
        self.assertIn(
            hashlib.sha256(original).hexdigest(),
            cache["pages"][0]["ordered_lettered_sha256s"][0],
        )

    def test_all_pages_returns_numeric_paths_and_writes_each_file(self):
        second = {
            "number": 2, "layout": "full-page",
            "panels": [{"id": "p02-01", "rect": {"x": 64, "y": 64, "width": 1472, "height": 2272}}],
        }
        self.storyboard["pages"].append(second)
        self.settings["page_count"] = 2
        (self.project / "panels/p02-01").mkdir(parents=True)
        Image.new("RGB", (512, 768), "blue").save(
            self.project / "panels/p02-01/lettered.png"
        )
        atomic_write_json(self.project / "plan/storyboard.json", self.storyboard)
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        manifest["settings"] = self.settings
        atomic_write_json(self.project / "project.json", manifest)

        paths = compose_all_pages(self.project)

        self.assertEqual(["page-001.png", "page-002.png"], [path.name for path in paths])
        self.assertTrue(all(path.is_file() for path in paths))

    def test_failed_second_page_preserves_entire_prior_page_set(self):
        # Setup two pages
        second = {
            "number": 2, "layout": "full-page",
            "panels": [{"id": "p02-01", "rect": {"x": 64, "y": 64, "width": 1472, "height": 2272}}],
        }
        self.storyboard["pages"].append(second)
        self.settings["page_count"] = 2
        (self.project / "panels/p02-01").mkdir(parents=True)
        Image.new("RGB", (512, 768), "blue").save(self.project / "panels/p02-01/lettered.png")
        atomic_write_json(self.project / "plan/storyboard.json", self.storyboard)
        manifest = json.loads((self.project / "project.json").read_text("utf-8"))
        manifest["settings"] = self.settings
        atomic_write_json(self.project / "project.json", manifest)

        page_one = self.project / "pages/page-001.png"
        compose_all_pages(self.project)
        self.assertTrue(page_one.is_file())
        old_page_one_hash = hashlib.sha256(page_one.read_bytes()).hexdigest()

        # Corrupt page 2 source
        (self.project / "panels/p02-01/lettered.png").write_text("not an image", "utf-8")

        with self.assertRaises(ValueError):
            compose_all_pages(self.project)

        # Page 1 must be unchanged
        self.assertTrue(page_one.is_file())
        self.assertEqual(
            old_page_one_hash,
            hashlib.sha256(page_one.read_bytes()).hexdigest(),
        )
        # No stale staging left behind
        tx_base = self.project / "logs/transactions"
        if tx_base.is_dir():
            entries = list(tx_base.iterdir())
            self.assertEqual(0, len(entries))


if __name__ == "__main__":
    unittest.main()
