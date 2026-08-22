import math
import unittest

from scripts.core_primitives import (
    PAGE_CHECK_IDS,
    PANEL_CHECK_IDS,
    PANEL_ID_PATTERN,
    balloon_outline_deviation,
    balloon_separation_minimum,
    balloon_subject_clearance,
    canonical_artifact_bytes,
    canonical_json_bytes,
    is_geometry_point,
    is_normalized_point,
    rectangle_overlap_area,
    rectangle_separation,
    rectangles_overlap,
    subject_keepout_radius,
    tail_geometry_result,
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


class BalloonGeometryPrimitiveTests(unittest.TestCase):
    def test_overlap_area_is_zero_for_touching_rectangles(self):
        first = {"x": 0, "y": 0, "width": 10, "height": 10}
        self.assertEqual(
            0, rectangle_overlap_area(first, {"x": 10, "y": 0, "width": 5, "height": 5})
        )
        self.assertEqual(
            4, rectangle_overlap_area(first, {"x": 8, "y": 8, "width": 5, "height": 5})
        )

    def test_separation_is_zero_when_rectangles_touch_or_overlap(self):
        first = {"x": 0, "y": 0, "width": 10, "height": 10}
        self.assertEqual(
            0.0, rectangle_separation(first, {"x": 10, "y": 0, "width": 5, "height": 5})
        )
        self.assertEqual(
            0.0, rectangle_separation(first, {"x": 5, "y": 5, "width": 5, "height": 5})
        )
        self.assertEqual(
            5.0, rectangle_separation(first, {"x": 15, "y": 0, "width": 5, "height": 5})
        )
        self.assertEqual(
            5.0, rectangle_separation(first, {"x": 13, "y": 14, "width": 5, "height": 5})
        )

    def test_keepout_and_separation_budgets_are_clamped(self):
        # 0.025 of the shortest side, clamped into an 8..24 pixel band.
        self.assertEqual(8.0, subject_keepout_radius(100, 200))
        self.assertEqual(18.0, subject_keepout_radius(720, 1064))
        self.assertEqual(24.0, subject_keepout_radius(4000, 4000))
        self.assertEqual(8.0, balloon_separation_minimum(100, 200))
        self.assertEqual(14.4, balloon_separation_minimum(720, 1064))

    def test_subject_clearance_measures_the_drawn_balloon_shape(self):
        box = {"x": 0, "y": 0, "width": 200, "height": 200}
        # A point at the centre is buried by both shapes.
        self.assertEqual(0.0, balloon_subject_clearance(box, (100, 100), ellipse=True))
        self.assertEqual(0.0, balloon_subject_clearance(box, (100, 100), ellipse=False))
        # A box corner lies outside the inscribed ellipse but inside the rectangle.
        self.assertGreater(balloon_subject_clearance(box, (0, 0), ellipse=True), 0.0)
        self.assertEqual(0.0, balloon_subject_clearance(box, (0, 0), ellipse=False))
        # Straight out to the right of both shapes.
        self.assertAlmostEqual(50.0, balloon_subject_clearance(box, (250, 100), ellipse=True))
        self.assertEqual(50.0, balloon_subject_clearance(box, (250, 100), ellipse=False))

    def test_subject_clearance_rejects_an_invalid_point(self):
        with self.assertRaisesRegex(ValueError, "two finite coordinates"):
            balloon_subject_clearance(
                {"x": 0, "y": 0, "width": 10, "height": 10}, (float("nan"), 1), ellipse=True
            )

    def test_geometry_points_require_two_finite_numbers(self):
        self.assertTrue(is_geometry_point([1, 2.5]))
        for value in ([1], [1, 2, 3], "ab", [True, 1], [float("inf"), 1], None):
            self.assertFalse(is_geometry_point(value), value)

    def test_tail_verdict_requires_alignment_bounds_and_a_positive_gap(self):
        # The speaker anchor [0.5, 0.1] of a 1000x1000 panel is (500, 100), so a
        # tip at (500, 150) genuinely leaves 50 pixels of source gap.
        tail = {
            "attachment": [500.0, 200.0],
            "source_gap": 50.0,
            "tip": [500.0, 150.0],
        }
        self.assertEqual("pass", tail_geometry_result(tail, [0.5, 0.1], 1000, 1000))
        # Pointing at a different speaker fails the cosine alignment floor.
        self.assertEqual("fail", tail_geometry_result(tail, [0.02, 0.1], 1000, 1000))
        # A tail must stop short of its voice source.
        self.assertEqual(
            "fail", tail_geometry_result({**tail, "source_gap": 0.0}, [0.5, 0.1], 1000, 1000)
        )
        # A tip outside the panel fails even when it is aimed correctly.
        self.assertEqual(
            "fail", tail_geometry_result({**tail, "tip": [1400.0, 100.0]}, [1.0, 0.1], 1000, 1000)
        )
        self.assertEqual("fail", tail_geometry_result(tail, "not-an-anchor", 1000, 1000))

    def test_outline_deviation_is_zero_only_on_the_ellipse(self):
        box = {"x": 0, "y": 0, "width": 200, "height": 100}
        # Both semi-axis endpoints lie exactly on the outline.
        self.assertAlmostEqual(0.0, balloon_outline_deviation(box, (200, 50)))
        self.assertAlmostEqual(0.0, balloon_outline_deviation(box, (100, 100)))
        # The centre is one semi-minor axis away from the nearest outline, so an
        # attachment resting there is detached even though it is inside the box.
        self.assertAlmostEqual(50.0, balloon_outline_deviation(box, (100, 50)))
        # Deviation is unsigned: inside and outside both report a distance.
        self.assertGreater(balloon_outline_deviation(box, (150, 50)), 0.0)
        self.assertGreater(balloon_outline_deviation(box, (260, 50)), 0.0)

    def test_tail_verdict_recomputes_the_claimed_source_gap(self):
        tail = {
            "attachment": [500.0, 200.0],
            "source_gap": 50.0,
            "tip": [500.0, 150.0],
        }
        # A tail cannot claim clearance it does not hold: the tip is 1 pixel from
        # the speaker anchor while the record still advertises 50.
        self.assertEqual(
            "fail",
            tail_geometry_result({**tail, "tip": [500.0, 101.0]}, [0.5, 0.1], 1000, 1000),
        )
        # Rounding slack is tolerated, a real disagreement is not.
        self.assertEqual(
            "pass",
            tail_geometry_result({**tail, "source_gap": 50.0001}, [0.5, 0.1], 1000, 1000),
        )

    def test_tail_verdict_rejects_an_anchor_outside_the_panel(self):
        # A tail can be internally perfect - attachment, direction, tip bounds and
        # source gap all agreeing - while aiming at a voice source that is not in
        # the panel. The normalized range is the only thing that catches it.
        anchor = [2.0, 0.5]
        target = (round(anchor[0] * 1000), round(anchor[1] * 1000))
        tip = [200.0, 500.0]
        consistent = {
            "attachment": [100.0, 500.0],
            "source_gap": math.hypot(target[0] - tip[0], target[1] - tip[1]),
            "tip": tip,
        }
        self.assertEqual("fail", tail_geometry_result(consistent, anchor, 1000, 1000))
        # The panel edges themselves remain valid anchors.
        for edge in ([0.0, 0.5], [1.0, 0.5], [0.5, 0.0], [0.5, 1.0]):
            self.assertTrue(is_normalized_point(edge), edge)
        for outside in ([2.0, 0.5], [-0.1, 0.5], [0.5, 1.2]):
            self.assertFalse(is_normalized_point(outside), outside)

    def test_tail_verdict_fails_closed_on_a_corrupt_speaker_anchor(self):
        tail = {
            "attachment": [500.0, 200.0],
            "source_gap": 50.0,
            "tip": [500.0, 150.0],
        }
        # A corrupt anchor is a failed check, never an exception out of the record
        # builder.
        for anchor in (
            ["a", "b"],
            [None, 1],
            [float("nan"), 0.5],
            [float("inf"), 0.5],
            [True, 0.5],
            [0.5],
            [0.5, 0.1, 0.2],
        ):
            self.assertEqual("fail", tail_geometry_result(tail, anchor, 1000, 1000), anchor)


if __name__ == "__main__":
    unittest.main()
