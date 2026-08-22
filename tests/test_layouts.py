import unittest
from dataclasses import FrozenInstanceError

from scripts.layouts import (
    FOUR_GRID_RECTS,
    LAYOUT_VERSION,
    LayoutDefinition,
    get_layout,
    match_layout,
    validate_custom_layout,
)
from scripts.comic_sol import layout_rects


EXPECTED_LAYOUTS = {
    "full-page": 1,
    "two-horizontal": 2,
    "three-horizontal": 3,
    "hero-top-two-bottom": 3,
    "two-top-hero-bottom": 3,
    "four-grid": 4,
}


class LayoutRegistryTests(unittest.TestCase):
    def test_named_layouts_are_immutable_deterministic_and_complete(self):
        for name, panel_count in EXPECTED_LAYOUTS.items():
            with self.subTest(name=name):
                layout = get_layout(name)
                self.assertIsInstance(layout, LayoutDefinition)
                self.assertEqual(name, layout.name)
                self.assertEqual(LAYOUT_VERSION, layout.version)
                self.assertEqual(panel_count, len(layout.rectangles))
                self.assertEqual(tuple(range(1, panel_count + 1)), layout.reading_order)
                self.assertEqual(layout, get_layout(name))
                with self.assertRaises(FrozenInstanceError):
                    layout.name = "changed"

    def test_four_grid_uses_exact_approved_geometry(self):
        self.assertEqual(
            (
                (64, 64, 720, 1120),
                (816, 64, 720, 1120),
                (64, 1216, 720, 1120),
                (816, 1216, 720, 1120),
            ),
            FOUR_GRID_RECTS,
        )
        self.assertEqual(FOUR_GRID_RECTS, get_layout("four-grid").rectangles)
        self.assertEqual(
            [dict(zip(("x", "y", "width", "height"), rectangle)) for rectangle in FOUR_GRID_RECTS],
            layout_rects("four-grid"),
        )

    def test_every_layout_is_contained_positive_non_overlapping_and_ordered(self):
        for name in EXPECTED_LAYOUTS:
            with self.subTest(name=name):
                layout = get_layout(name)
                validate_custom_layout(layout.rectangles, layout.reading_order)
                for x, y, width, height in layout.rectangles:
                    self.assertTrue(
                        all(
                            isinstance(value, int) and not isinstance(value, bool)
                            for value in (x, y, width, height)
                        )
                    )
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertGreater(width, 0)
                    self.assertGreater(height, 0)
                    self.assertLessEqual(x + width, 1600)
                    self.assertLessEqual(y + height, 2400)

    def test_custom_layout_validation_rejects_overlap_bounds_and_bad_order(self):
        valid = ((20, 20, 700, 1000), (760, 20, 700, 1000))
        self.assertEqual(valid, validate_custom_layout(valid, (1, 2)))
        cases = (
            (((20, 20, 700, 1000), (700, 20, 700, 1000)), (1, 2), "overlap"),
            (((-1, 20, 700, 1000),), (1,), "contained"),
            (((20, 20, 0, 1000),), (1,), "positive"),
            (valid, (1, 1), "reading order"),
            (valid, (2,), "reading order"),
        )
        for rectangles, order, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_custom_layout(rectangles, order)

    def test_match_layout_returns_named_or_custom_without_order_dependence(self):
        for name in EXPECTED_LAYOUTS:
            layout = get_layout(name)
            self.assertEqual(name, match_layout(layout.rectangles))
            dictionaries = tuple(
                {"height": h, "x": x, "width": w, "y": y} for x, y, w, h in layout.rectangles
            )
            self.assertEqual(name, match_layout(dictionaries))
        self.assertEqual(
            "custom",
            match_layout(((20, 20, 700, 1000), (760, 20, 700, 1000))),
        )

    def test_unknown_layout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown layout"):
            get_layout("not-a-layout")


if __name__ == "__main__":
    unittest.main()
