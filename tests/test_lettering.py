import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from letter_panels import (  # noqa: E402
    letter_project,
    letter_panel,
    normalize_content,
    normalized_word_count,
    render_text_item,
)

FIXTURES = ROOT / "tests/fixtures"


FONT = ROOT / "assets/fonts/ComicNeue-Regular.ttf"

# The schema caps dialogue at 32 words, the worst case a balloon must contain.
MAXIMUM_DIALOGUE = (
    "We have exactly one shot at the service bridge before the tide turns, "
    "so keep the lantern low, stay right behind me, and do not ever stop "
    "moving until we are through"
)


def dialogue(content="Keep moving.", priority=1, anchor="top-left"):
    return {
        "id": f"dialogue-{priority}", "kind": "dialogue", "speaker": "mira",
        "content": content, "anchor": anchor, "tail_target": [0.75, 0.7],
        "priority": priority,
    }


def caption(content="Below the city, daylight became a delivery.", priority=1, anchor="bottom-right"):
    return {
        "id": f"caption-{priority}", "kind": "caption", "speaker": None,
        "content": content, "anchor": anchor, "tail_target": None,
        "priority": priority,
    }


def sfx(content="KRAK!", priority=1, anchor="middle-right"):
    return {
        "id": f"sfx-{priority}", "kind": "sfx", "speaker": None,
        "content": content, "anchor": anchor, "tail_target": None,
        "priority": priority,
    }


class LetteringTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.panel = self.root / "p01-01.png"
        Image.new("RGB", (800, 1000), (28, 32, 40)).save(self.panel)
        self.characters = [{"id": "mira", "name": "Mira"}]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_normalize_content_and_word_count(self):
        self.assertEqual("Café 😀\nBAM!", normalize_content("  Cafe\u0301\t 😀\x00\n  BAM!  "))
        self.assertEqual(3, normalized_word_count("  one\t two\nthree  "))
        self.assertEqual("Wait... — now!", normalize_content("Wait... — now!"))

    def test_emphasis_parsing(self):
        from letter_panels import _parse_emphasis

        self.assertEqual(
            [("Hello ", False), ("world", True), ("!", False)],
            _parse_emphasis("Hello **world**!"),
        )
        self.assertEqual(
            [("Literal ** missing", False)],
            _parse_emphasis("Literal ** missing"),
        )
        self.assertEqual([("bold", True)], _parse_emphasis("**bold**"))
        self.assertEqual(
            [("mixed **stars", False)],
            _parse_emphasis("mixed **stars"),
        )
        self.assertEqual(
            [("Keep **** literal", False)],
            _parse_emphasis("Keep **** literal"),
        )
        self.assertEqual(
            [("Keep ** ** literal", False)],
            _parse_emphasis("Keep ** ** literal"),
        )

    def test_font_runs_use_exact_fallback_and_preserve_notdef(self):
        from letter_panels import _font_runs, _font_supports, _load_font

        fallback = ROOT / "assets/fonts/NotoSans-Regular.ttf"
        unsupported = "\u0378"
        content = f"AΩBЖC{unsupported}D\nE"
        runs = _font_runs(content, 32)

        self.assertTrue(_font_supports(FONT, "A"))
        self.assertFalse(_font_supports(FONT, "Ω"))
        self.assertTrue(_font_supports(fallback, "Ω"))
        self.assertTrue(_font_supports(fallback, "Ж"))
        self.assertFalse(_font_supports(fallback, unsupported))
        self.assertEqual(content, "".join(text for text, _ in runs))
        self.assertEqual(
            [
                ("A", FONT),
                ("Ω", fallback),
                ("B", FONT),
                ("Ж", fallback),
                ("C", FONT),
                (unsupported, fallback),
                ("D\nE", FONT),
            ],
            [(text, Path(font.path)) for text, font in runs],
        )

        missing_mask = runs[5][1].getmask(unsupported)
        comparison_mask = runs[5][1].getmask("\u0379")
        self.assertIsNotNone(missing_mask.getbbox())
        self.assertEqual(missing_mask.size, comparison_mask.size)
        self.assertEqual(bytes(missing_mask), bytes(comparison_mask))

        with mock.patch("letter_panels.FONT_PATH", self.root / "missing.ttf"):
            self.assertEqual(fallback, Path(_load_font(32).path))

    def test_dialogue_renders_complete_emphasis_with_prominent_bold_pixels(self):
        from letter_panels import _styled_font_runs

        regular = ImageFont.truetype(str(FONT), 48)
        bold_path = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
        runs = _styled_font_runs("Stay **LOUD** now", regular)
        self.assertEqual(["Stay ", "LOUD", " now"], [text for text, _ in runs])
        self.assertEqual(
            [FONT, bold_path, FONT],
            [Path(run_font.path) for _, run_font in runs],
        )

        def rendered_ink(content):
            image = Image.new("RGB", (480, 220), (28, 32, 40))
            draw = ImageDraw.Draw(image)
            item = dialogue(content)
            item["tail_target"] = None
            render_text_item(
                draw,
                item,
                {"x": 20, "y": 20, "width": 440, "height": 180},
                regular,
                self.characters,
            )
            interior = image.crop((40, 50, 440, 170))
            return sum(1 for pixel in interior.getdata() if max(pixel) < 128)

        regular_ink = rendered_ink("LOUD")
        bold_ink = rendered_ink("**LOUD**")
        self.assertGreater(bold_ink, regular_ink * 1.10)

    def test_styled_layout_wraps_visible_runs_and_centers_each_line(self):
        from letter_panels import _layout_styled_text

        image = Image.new("RGB", (600, 300), "white")
        draw = ImageDraw.Draw(image)
        regular = ImageFont.truetype(str(FONT), 48)
        bold_path = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
        bold = ImageFont.truetype(str(bold_path), 48)
        fallback_path = ROOT / "assets/fonts/NotoSans-Regular.ttf"
        fallback = ImageFont.truetype(str(fallback_path), 48)

        content = "AA **wwww**"
        regular_visible_width = draw.textlength("AA wwww", font=regular)
        mixed_visible_width = (
            draw.textlength("AA ", font=regular)
            + draw.textlength("wwww", font=bold)
        )
        literal_marker_width = draw.textlength(content, font=regular)
        self.assertLess(regular_visible_width, mixed_visible_width)
        self.assertLess(mixed_visible_width, literal_marker_width)

        marker_free = _layout_styled_text(
            draw,
            content,
            regular,
            (mixed_visible_width + literal_marker_width) / 2,
        )
        self.assertIsNotNone(marker_free)
        self.assertEqual(
            ["AA wwww"],
            ["".join(text for text, _ in line.runs) for line in marker_free.lines],
        )
        self.assertAlmostEqual(mixed_visible_width, marker_free.lines[0].width)

        wrapped = _layout_styled_text(
            draw,
            content,
            regular,
            (regular_visible_width + mixed_visible_width) / 2,
        )
        self.assertIsNotNone(wrapped)
        self.assertEqual(
            ["AA", "wwww"],
            ["".join(text for text, _ in line.runs) for line in wrapped.lines],
        )
        self.assertEqual(bold_path, Path(wrapped.lines[1].runs[0][1].path))
        self.assertAlmostEqual(
            draw.textlength("wwww", font=bold),
            wrapped.lines[1].width,
        )
        wrapped_maximum = (regular_visible_width + mixed_visible_width) / 2
        self.assertTrue(all(line.width <= wrapped_maximum for line in wrapped.lines))

        regular_fallback_width = draw.textlength("AA ΩΩ", font=regular)
        mixed_fallback_width = (
            draw.textlength("AA ", font=regular)
            + draw.textlength("ΩΩ", font=fallback)
        )
        self.assertLess(regular_fallback_width, mixed_fallback_width)
        fallback_maximum = (regular_fallback_width + mixed_fallback_width) / 2
        fallback_wrapped = _layout_styled_text(
            draw,
            "AA ΩΩ",
            regular,
            fallback_maximum,
        )
        self.assertIsNotNone(fallback_wrapped)
        self.assertEqual(
            ["AA", "ΩΩ"],
            ["".join(text for text, _ in line.runs) for line in fallback_wrapped.lines],
        )
        self.assertEqual(fallback_path, Path(fallback_wrapped.lines[1].runs[0][1].path))
        self.assertAlmostEqual(
            draw.textlength("ΩΩ", font=fallback),
            fallback_wrapped.lines[1].width,
        )
        self.assertTrue(all(line.width <= fallback_maximum for line in fallback_wrapped.lines))

        word_width = max(
            draw.textlength("BOLD", font=bold),
            draw.textlength("WORDS", font=bold),
        )
        spanning = _layout_styled_text(
            draw,
            "**BOLD WORDS**",
            regular,
            word_width + 1,
        )
        self.assertIsNotNone(spanning)
        self.assertEqual(
            ["BOLD", "WORDS"],
            ["".join(text for text, _ in line.runs) for line in spanning.lines],
        )
        self.assertTrue(
            all(Path(run_font.path) == bold_path for line in spanning.lines for _, run_font in line.runs)
        )
        self.assertTrue(all(line.width <= word_width + 1 for line in spanning.lines))

        metrics = _layout_styled_text(draw, "AΩ**B**", regular, 1000)
        self.assertIsNotNone(metrics)
        expected_boxes = (
            draw.textbbox((0, 0), "A", font=regular, anchor="ls"),
            draw.textbbox((0, 0), "Ω", font=fallback, anchor="ls"),
            draw.textbbox((0, 0), "B", font=bold, anchor="ls"),
        )
        self.assertEqual(min(box[1] for box in expected_boxes), metrics.lines[0].top)
        self.assertEqual(max(box[3] for box in expected_boxes), metrics.lines[0].bottom)
        self.assertEqual(
            metrics.lines[0].bottom - metrics.lines[0].top,
            metrics.lines[0].height,
        )

        centered_image = Image.new("RGB", (500, 260), (28, 32, 40))
        centered_draw = ImageDraw.Draw(centered_image)
        centered_item = dialogue("I\n**WIDE**")
        centered_item["tail_target"] = None
        rect = {"x": 20, "y": 20, "width": 460, "height": 220}
        centered_layout = _layout_styled_text(
            centered_draw,
            centered_item["content"],
            regular,
            rect["width"] - 48,
        )
        self.assertIsNotNone(centered_layout)
        self.assertEqual(
            ["I", "WIDE"],
            ["".join(text for text, _ in line.runs) for line in centered_layout.lines],
        )

        starts = []

        def capture_line_start(_draw, _runs, position, _fill):
            starts.append(position)

        with mock.patch("letter_panels._draw_font_runs", side_effect=capture_line_start):
            render_text_item(
                centered_draw,
                centered_item,
                rect,
                regular,
                self.characters,
            )
        expected_center = rect["x"] + rect["width"] / 2
        self.assertEqual(len(centered_layout.lines), len(starts))
        for line, (line_x, _) in zip(centered_layout.lines, starts):
            self.assertAlmostEqual(expected_center - line.width / 2, line_x, delta=0.5)

        centered_image = Image.new("RGB", (500, 260), (28, 32, 40))
        centered_draw = ImageDraw.Draw(centered_image)
        render_text_item(
            centered_draw,
            centered_item,
            rect,
            regular,
            self.characters,
        )
        line_top = rect["y"] + max(24, (rect["height"] - centered_layout.height) / 2)
        for line in centered_layout.lines:
            points = [
                (x, y)
                for y in range(max(0, int(line_top) - 2), min(centered_image.height, int(line_top + line.height) + 3))
                for x in range(rect["x"] + 24, rect["x"] + rect["width"] - 24)
                if max(centered_image.getpixel((x, y))) < 128
            ]
            self.assertTrue(points)
            ink_center = (min(x for x, _ in points) + max(x for x, _ in points)) / 2
            self.assertAlmostEqual(expected_center, ink_center, delta=6)
            line_top += line.height + 6

    def test_letter_panel_produces_valid_png_and_summary(self):
        result = letter_panel(
            str(self.panel), 800, 1000,
            [dialogue(), caption(priority=2), sfx(priority=3)], self.characters,
        )
        with Image.open(self.panel) as image:
            self.assertEqual("PNG", image.format)
            self.assertEqual((800, 1000), image.size)
            image.load()
        self.assertEqual(str(self.panel), result["lettered_path"])
        self.assertEqual(3, result["text_count"])
        self.assertEqual(2, result["rendered_text_count"])
        self.assertEqual(1, result["sfx_count"])
        self.assertEqual(10, result["word_count"])
        self.assertEqual(str(FONT), result["font_used"])

    def test_text_items_render_in_priority_then_id_order(self):
        items = [sfx("THREE", 3), caption("SECOND", 2), dialogue("FIRST", 1)]
        seen = []

        def observe(draw, item, rect, font, character_bible):
            seen.append(item["content"])

        with mock.patch("letter_panels.render_text_item", side_effect=observe):
            letter_panel(str(self.panel), 800, 1000, items, self.characters)
        self.assertEqual(["FIRST", "SECOND"], seen)

    def test_dialogue_has_white_oval_dark_stroke_and_tail(self):
        letter_panel(str(self.panel), 800, 1000, [dialogue()], self.characters)
        image = Image.open(self.panel).convert("RGB")
        self.assertGreater(sum(1 for pixel in image.getdata() if all(channel > 200 for channel in pixel)), 1000)
        self.assertTrue(any(max(image.getpixel((x, 40))) < 80 for x in range(32, 370)))

    def test_dialogue_tail_attachment_has_no_internal_seam(self):
        from letter_panels import _ellipse_tail_polygon

        image = Image.new("RGB", (240, 240), (96, 96, 96))
        draw = ImageDraw.Draw(image)
        rect = {"x": 40, "y": 30, "width": 120, "height": 70}
        item = dialogue("Hi")
        item["tail_target"] = [0.8, 0.8]
        base_one, base_two, _ = _ellipse_tail_polygon(rect, item["tail_target"], 240, 240)
        attachment = tuple(round((first + second) / 2) for first, second in zip(base_one, base_two))

        render_text_item(
            draw, item, rect, ImageFont.truetype(str(FONT), 24), self.characters
        )

        self.assertEqual((255, 255, 255), image.getpixel(attachment))

    def test_dialogue_uses_adaptive_oval_and_boundary_tail_geometry(self):
        from letter_panels import (
            _ellipse_tail_polygon,
            _fitted_item_rect,
            _item_font,
            _layout_styled_text,
        )

        image = Image.new("RGB", (800, 1000), (28, 32, 40))
        draw = ImageDraw.Draw(image)
        maximum = {"x": 32, "y": 40, "width": 336, "height": 300}
        short = dialogue("Go!")
        short_font = _item_font(draw, short, maximum)
        short_rect = _fitted_item_rect(draw, short, maximum, short_font)
        short_layout = _layout_styled_text(
            draw, short["content"], short_font, maximum["width"] - 48
        )
        self.assertIsNotNone(short_layout)
        long = dialogue("The service bridge is collapsing beneath us now!")
        long_font = _item_font(draw, long, maximum)
        long_rect = _fitted_item_rect(draw, long, maximum, long_font)

        self.assertLess(short_rect["width"], maximum["width"])
        self.assertLess(short_rect["height"], maximum["height"])
        self.assertGreaterEqual(short_rect["height"], short_layout.height + 48)
        self.assertGreater(long_rect["width"] * long_rect["height"], short_rect["width"] * short_rect["height"])
        self.assertGreaterEqual(short_rect["x"], maximum["x"])
        self.assertGreaterEqual(short_rect["y"], maximum["y"])

        base_one, base_two, target = _ellipse_tail_polygon(
            short_rect, [-0.25, 1.25], 800, 1000
        )
        self.assertLess(target[0], 800)
        self.assertLess(target[1], 1000)
        self.assertGreaterEqual(target[0], 0)
        self.assertGreaterEqual(target[1], 0)
        attachment = (
            (base_one[0] + base_two[0]) / 2,
            (base_one[1] + base_two[1]) / 2,
        )
        center = (
            short_rect["x"] + short_rect["width"] / 2,
            short_rect["y"] + short_rect["height"] / 2,
        )
        radii = (short_rect["width"] / 2, short_rect["height"] / 2)
        ellipse_value = (
            ((attachment[0] - center[0]) / radii[0]) ** 2
            + ((attachment[1] - center[1]) / radii[1]) ** 2
        )
        self.assertAlmostEqual(1.0, ellipse_value, delta=0.03)
        delta_x, delta_y = target[0] - center[0], target[1] - center[1]
        scale = 1 / math.sqrt((delta_x / radii[0]) ** 2 + (delta_y / radii[1]) ** 2)
        expected_attachment = (center[0] + delta_x * scale, center[1] + delta_y * scale)
        self.assertAlmostEqual(expected_attachment[0], attachment[0], delta=1.0)
        self.assertAlmostEqual(expected_attachment[1], attachment[1], delta=1.0)
        self.assertNotIn(center, (base_one, base_two, target))

        calls = []
        original_line = draw.line
        original_polygon = draw.polygon
        original_ellipse = draw.ellipse

        def record_line(*args, **kwargs):
            calls.append("line")
            return original_line(*args, **kwargs)

        def record_polygon(*args, **kwargs):
            calls.append("polygon")
            return original_polygon(*args, **kwargs)

        def record_ellipse(*args, **kwargs):
            calls.append("ellipse")
            return original_ellipse(*args, **kwargs)

        with (
            mock.patch.object(draw, "line", side_effect=record_line),
            mock.patch.object(draw, "polygon", side_effect=record_polygon),
            mock.patch.object(draw, "ellipse", side_effect=record_ellipse),
        ):
            render_text_item(draw, short, short_rect, short_font, self.characters)

        self.assertNotIn("line", calls)
        self.assertIn("polygon", calls)
        self.assertIn("ellipse", calls)
        self.assertLess(calls.index("polygon"), calls.index("ellipse"))
        self.assertEqual((28, 32, 40), image.getpixel((short_rect["x"] + 2, short_rect["y"] + 2)))
        self.assertGreater(min(image.getpixel((short_rect["x"] + short_rect["width"] // 2, short_rect["y"] + 6))), 220)

    def test_balloon_circumscribes_a_maximum_length_dialogue_block(self):
        from letter_panels import (
            _anchor_rect,
            _fitted_item_rect,
            _item_font,
            _layout_styled_text,
            _text_wrap_width,
        )

        item = dialogue(MAXIMUM_DIALOGUE)
        item["tail_target"] = None
        self.assertEqual(32, normalized_word_count(item["content"]))
        image = Image.new("RGB", (1200, 1600), (28, 32, 40))
        draw = ImageDraw.Draw(image, "RGBA")
        maximum = _anchor_rect("top-left", 1200, 1600)
        font = _item_font(draw, item, maximum)
        rect = _fitted_item_rect(draw, item, maximum, font)
        layout = _layout_styled_text(
            draw, item["content"], font, _text_wrap_width("dialogue", rect["width"])
        )
        self.assertIsNotNone(layout)
        self.assertGreaterEqual(len(layout.lines), 3)

        center_x = rect["x"] + rect["width"] / 2
        center_y = rect["y"] + rect["height"] / 2
        radius_x, radius_y = rect["width"] / 2, rect["height"] / 2
        self.assertLessEqual(
            (layout.width / rect["width"]) ** 2 + (layout.height / rect["height"]) ** 2,
            1.0,
        )

        balloon = image.copy()
        with mock.patch("letter_panels._draw_font_runs"):
            render_text_item(
                ImageDraw.Draw(balloon, "RGBA"), item, rect, font, self.characters
            )
        render_text_item(draw, item, rect, font, self.characters)

        difference = ImageChops.difference(image, balloon).convert("L")
        box = difference.getbbox()
        self.assertIsNotNone(box)
        stride = box[2] - box[0]
        text_pixels = [
            (box[0] + index % stride, box[1] + index // stride)
            for index, value in enumerate(difference.crop(box).getdata())
            if value
        ]
        self.assertGreater(len(text_pixels), 1000)
        escaped = [
            point
            for point in text_pixels
            if ((point[0] - center_x) / radius_x) ** 2
            + ((point[1] - center_y) / radius_y) ** 2 > 1.0
        ]
        self.assertEqual(
            [], escaped[:8], f"{len(escaped)}/{len(text_pixels)} text pixels left the balloon"
        )
        self.assertTrue(all(balloon.getpixel(point) == (255, 255, 255) for point in text_pixels))

    def test_caption_honors_its_authored_anchor(self):
        letter_panel(
            str(self.panel), 800, 1000,
            [caption("A quiet beat.", anchor="bottom-center")], self.characters,
        )
        image = Image.open(self.panel).convert("RGB")
        box = ImageChops.difference(
            image, Image.new("RGB", (800, 1000), (28, 32, 40))
        ).getbbox()

        self.assertIsNotNone(box)
        self.assertGreaterEqual(box[1], 660)
        self.assertLessEqual(box[3], 962)
        self.assertAlmostEqual(400, (box[0] + box[2]) / 2, delta=2)

    def test_non_finite_tail_target_is_rejected_as_value_error(self):
        from letter_panels import _ellipse_tail_polygon

        before = self.panel.read_bytes()
        for value in (float("inf"), float("-inf"), float("nan")):
            item = dialogue()
            item["tail_target"] = [value, 0.5]
            with self.assertRaisesRegex(ValueError, "non-finite tail target"):
                letter_panel(str(self.panel), 800, 1000, [item], self.characters)
            self.assertEqual(before, self.panel.read_bytes())
            with self.assertRaisesRegex(ValueError, "finite"):
                _ellipse_tail_polygon(
                    {"x": 40, "y": 30, "width": 120, "height": 70}, [value, 0.5], 240, 240
                )

    def test_oversized_panel_image_is_rejected_as_value_error(self):
        from letter_panels import MAX_DECODED_PIXELS

        self.assertGreaterEqual(MAX_DECODED_PIXELS, 1600 * 2400)
        before = self.panel.read_bytes()
        with mock.patch("letter_panels.MAX_DECODED_PIXELS", 800 * 1000 - 1):
            with self.assertRaisesRegex(ValueError, "pixel decode limit"):
                letter_panel(str(self.panel), 800, 1000, [dialogue()], self.characters)
        self.assertEqual(before, self.panel.read_bytes())

    def test_sfx_is_validated_counted_and_byte_exact_noop(self):
        before = self.panel.read_bytes()
        result = letter_panel(str(self.panel), 800, 1000, [sfx()], self.characters)

        self.assertEqual(before, self.panel.read_bytes())
        self.assertEqual(1, result["text_count"])
        self.assertEqual(0, result["rendered_text_count"])
        self.assertEqual(1, result["sfx_count"])
        self.assertEqual(1, result["word_count"])

        invalid = sfx()
        invalid["anchor"] = "outside-panel"
        with self.assertRaisesRegex(ValueError, "unknown anchor"):
            letter_panel(str(self.panel), 800, 1000, [invalid], self.characters)
        self.assertEqual(before, self.panel.read_bytes())

    def test_sfx_never_reaches_placement_or_render(self):
        with (
            mock.patch("letter_panels._anchor_rect", side_effect=AssertionError("SFX placement attempted")) as placement,
            mock.patch("letter_panels.render_text_item") as render,
        ):
            result = letter_panel(str(self.panel), 800, 1000, [sfx()], self.characters)

        placement.assert_not_called()
        render.assert_not_called()
        self.assertEqual(0, result["rendered_text_count"])

    def test_sfx_does_not_change_or_reserve_mixed_lettering(self):
        without_sfx = self.root / "without-sfx.png"
        with_sfx = self.root / "with-sfx.png"
        shutil.copy2(self.panel, without_sfx)
        shutil.copy2(self.panel, with_sfx)
        spoken = dialogue("Same placement.", priority=2, anchor="middle-right")

        plain_result = letter_panel(
            str(without_sfx), 800, 1000, [spoken], self.characters
        )
        mixed_result = letter_panel(
            str(with_sfx), 800, 1000,
            [sfx("KRAK!", priority=1, anchor="middle-right"), spoken],
            self.characters,
        )

        self.assertEqual(without_sfx.read_bytes(), with_sfx.read_bytes())
        self.assertEqual(1, plain_result["rendered_text_count"])
        self.assertEqual(1, mixed_result["rendered_text_count"])
        self.assertEqual(1, mixed_result["sfx_count"])
        self.assertEqual(2, mixed_result["text_count"])

    def test_caption_is_drawn_at_top_as_overlay(self):
        letter_panel(str(self.panel), 800, 1000, [caption(anchor="top-left")], self.characters)
        image = Image.open(self.panel).convert("RGB")
        top = ImageChops.difference(image.crop((0, 0, 800, 320)), Image.new("RGB", (800, 320), (28, 32, 40)))
        bottom = ImageChops.difference(image.crop((0, 680, 800, 1000)), Image.new("RGB", (800, 320), (28, 32, 40)))
        self.assertIsNotNone(top.getbbox())
        self.assertIsNone(bottom.getbbox())

    def test_caption_is_compact_light_strip_fitted_at_top(self):
        from letter_panels import _fitted_item_rect, _item_font

        image = Image.new("RGB", (800, 1000), (28, 32, 40))
        draw = ImageDraw.Draw(image)
        item = caption("A quiet beat.", anchor="top-left")
        maximum = {"x": 32, "y": 40, "width": 336, "height": 300}
        font = _item_font(draw, item, maximum)
        fitted = _fitted_item_rect(draw, item, maximum, font)

        self.assertEqual((maximum["x"], maximum["y"]), (fitted["x"], fitted["y"]))
        self.assertLess(fitted["width"], maximum["width"])
        self.assertLess(fitted["height"], maximum["height"] // 2)

        calls = []
        original_rectangle = draw.rectangle
        original_rounded = draw.rounded_rectangle

        def record_rectangle(*args, **kwargs):
            calls.append("rectangle")
            return original_rectangle(*args, **kwargs)

        def record_rounded(*args, **kwargs):
            calls.append("rounded_rectangle")
            return original_rounded(*args, **kwargs)

        with (
            mock.patch.object(draw, "rectangle", side_effect=record_rectangle),
            mock.patch.object(draw, "rounded_rectangle", side_effect=record_rounded),
        ):
            render_text_item(draw, item, fitted, font, self.characters)

        self.assertEqual(["rectangle"], calls)
        self.assertTrue(
            all(channel > 230 for channel in image.getpixel((fitted["x"] + 4, fitted["y"] + 4)))
        )

    def test_caption_uses_per_character_noto_fallback(self):
        from letter_panels import _fitted_item_rect, _item_font

        image = Image.new("RGB", (800, 1000), (28, 32, 40))
        draw = ImageDraw.Draw(image)
        unsupported = "\u0378"
        item = caption(f"A ΩЖ {unsupported}")
        maximum = {"x": 32, "y": 40, "width": 336, "height": 300}
        font = _item_font(draw, item, maximum)
        fitted = _fitted_item_rect(draw, item, maximum, font)
        captured_runs = []

        def capture_runs(_draw, runs, _position, _fill):
            captured_runs.extend(runs)

        with mock.patch("letter_panels._draw_font_runs", side_effect=capture_runs):
            render_text_item(draw, item, fitted, font, self.characters)

        self.assertEqual(item["content"], "".join(text for text, _ in captured_runs))
        self.assertEqual(
            [
                ROOT / "assets/fonts/ComicNeue-Regular.ttf",
                ROOT / "assets/fonts/NotoSans-Regular.ttf",
                ROOT / "assets/fonts/ComicNeue-Regular.ttf",
                ROOT / "assets/fonts/NotoSans-Regular.ttf",
            ],
            [Path(run_font.path) for _, run_font in captured_runs],
        )

    def test_all_anchor_drawing_stays_inside_panel_boundary(self):
        image = Image.new("RGB", (512, 512), "black")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(FONT), 24)
        rect = {"x": 4, "y": 4, "width": 504, "height": 504}
        for anchor in (
            "top-left", "top-center", "top-right", "middle-left",
            "middle-right", "bottom-left", "bottom-center", "bottom-right",
        ):
            item = dialogue(anchor=anchor)
            render_text_item(draw, item, rect, font, self.characters)
        self.assertEqual((512, 512), image.size)

    def test_unknown_dialogue_character_raises_without_partial_write(self):
        before = self.panel.read_bytes()
        item = dialogue(); item["speaker"] = "ghost"
        with self.assertRaisesRegex(ValueError, "ghost"):
            letter_panel(str(self.panel), 800, 1000, [item], self.characters)
        self.assertEqual(before, self.panel.read_bytes())

    def test_cli_letters_project_and_prints_summaries(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, letter_main([str(project)]))
            summaries = json.loads(output.getvalue())
            self.assertEqual(3, len(summaries))
            for summary in summaries:
                self.assertIn("text_count", summary)
                self.assertIn("rendered_text_count", summary)
                self.assertIn("sfx_count", summary)
                self.assertTrue(Path(summary["lettered_path"]).is_file())

    def test_cli_font_override_applies_without_leaking_to_the_next_run(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        override = ROOT / "assets/fonts/NotoSans-Regular.ttf"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom_project = root / "custom"
            default_project = root / "default"
            shutil.copytree(FIXTURES / "valid-one-page", custom_project)
            shutil.copytree(FIXTURES / "valid-one-page", default_project)

            custom_output = io.StringIO()
            with contextlib.redirect_stdout(custom_output):
                self.assertEqual(
                    0,
                    letter_main([str(custom_project), "--font", str(override)]),
                )
            self.assertTrue(all(
                summary["font_used"] == str(override)
                for summary in json.loads(custom_output.getvalue())
            ))

            default_output = io.StringIO()
            with contextlib.redirect_stdout(default_output):
                self.assertEqual(0, letter_main([str(default_project)]))
            self.assertTrue(all(
                summary["font_used"] == str(FONT)
                for summary in json.loads(default_output.getvalue())
            ))

    def test_cli_missing_invocation_uses_house_error_without_traceback(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(1, letter_main([]))
        self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
        self.assertNotIn("Traceback", errors.getvalue())

    def test_cli_malformed_project_uses_house_error_without_traceback(self):
        import contextlib
        import io

        from comic_sol import atomic_write_json
        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            atomic_write_json(project / "plan/storyboard.json", {"pages": [None]})
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(1, letter_main([str(project)]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertNotIn("Traceback", errors.getvalue())

    def test_failed_project_run_preserves_prior_lettered_artifacts(self):
        import contextlib
        import io

        from comic_sol import atomic_write_json
        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            destination = project / "panels/p01-01/lettered.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (736, 1136), "magenta").save(destination)
            before = destination.read_bytes()
            storyboard = json.loads((project / "plan/storyboard.json").read_text("utf-8"))
            storyboard["pages"][0]["panels"][1]["text"][0]["speaker"] = "ghost"
            atomic_write_json(project / "plan/storyboard.json", storyboard)

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(1, letter_main([str(project)]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertEqual(before, destination.read_bytes())

    def test_cli_rejects_missing_font_override_without_traceback(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(1, letter_main([
                    str(project), "--font", str(Path(temporary) / "missing.ttf"),
                ]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertIn("font", errors.getvalue().lower())
            self.assertNotIn("Traceback", errors.getvalue())

    def test_cli_rejects_non_finite_tail_target_without_traceback(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            storyboard_path = project / "plan/storyboard.json"
            storyboard = json.loads(storyboard_path.read_text("utf-8"))
            storyboard["pages"][0]["panels"][1]["text"][0]["tail_target"] = [
                float("inf"), 0.55,
            ]
            storyboard_path.write_text(json.dumps(storyboard), "utf-8")

            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(1, letter_main([str(project)]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertIn("tail target", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())

    def test_cli_rejects_oversized_panel_image_without_traceback(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            errors = io.StringIO()
            with (
                mock.patch("letter_panels.MAX_DECODED_PIXELS", 1024),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(1, letter_main([str(project)]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertIn("pixel decode limit", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())

    def test_cli_normalizes_pillow_safety_error_without_traceback(self):
        import contextlib
        import io

        from letter_panels import main as letter_main

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            errors = io.StringIO()
            with (
                mock.patch(
                    "letter_panels.Image.open",
                    side_effect=Image.DecompressionBombError("unsafe dimensions"),
                ),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(1, letter_main([str(project)]))
            self.assertTrue(errors.getvalue().startswith("ERROR ValueError:"))
            self.assertNotIn("Traceback", errors.getvalue())


class LetteringFixtureIntegrationTests(unittest.TestCase):
    def test_valid_fixture_letters_three_panels_from_semantic_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "valid-one-page", project)
            outputs = letter_project(project)
            # letter_project returns resolved paths, and the temp root is itself
            # a symlink on macOS (/var -> /private/var).
            resolved = project.resolve()
            self.assertEqual(
                [
                    resolved / "panels/p01-01/lettered.png",
                    resolved / "panels/p01-02/lettered.png",
                    resolved / "panels/p01-03/lettered.png",
                ],
                outputs,
            )


if __name__ == "__main__":
    unittest.main()
