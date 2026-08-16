import unittest

from scripts.core_primitives import (
    PAGE_CHECK_IDS,
    PANEL_CHECK_IDS,
    PANEL_ID_PATTERN,
    canonical_artifact_bytes,
    canonical_json_bytes,
    rectangles_overlap,
)


class CorePrimitiveTests(unittest.TestCase):
    def test_canonical_json_is_sorted_compact_utf8_without_newline(self):
        self.assertEqual(
            b'{"a":"\xc3\xa9","z":1}',
            canonical_json_bytes({"z": 1, "a": "é"}),
        )

    def test_canonical_artifact_json_is_sorted_indented_utf8_with_newline(self):
        self.assertEqual(
            '{\n  "a": "é",\n  "z": 1\n}\n'.encode("utf-8"),
            canonical_artifact_bytes({"z": 1, "a": "é"}),
        )

    def test_rectangles_use_half_open_edges(self):
        first = {"x": 0, "y": 0, "width": 10, "height": 10}
        touching = {"x": 10, "y": 0, "width": 5, "height": 5}
        overlapping = {"x": 9, "y": 2, "width": 5, "height": 5}
        self.assertFalse(rectangles_overlap(first, touching))
        self.assertTrue(rectangles_overlap(first, overlapping))

    def test_panel_id_pattern_is_canonical_and_anchored(self):
        for value in ("p01-01", "p99-12"):
            self.assertIsNotNone(PANEL_ID_PATTERN.fullmatch(value))
        for value in ("p1-01", "p01-1", "xp01-01", "p01-01x"):
            self.assertIsNone(PANEL_ID_PATTERN.fullmatch(value))

    def test_quality_check_ids_are_canonical_sequences(self):
        self.assertEqual(
            (
                "character-identity",
                "anatomy",
                "action",
                "composition",
                "continuity",
                "text-free",
                "technical",
            ),
            PANEL_CHECK_IDS,
        )
        self.assertEqual(len(PAGE_CHECK_IDS), len(set(PAGE_CHECK_IDS)))


if __name__ == "__main__":
    unittest.main()
