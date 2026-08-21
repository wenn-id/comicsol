import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.font_coverage import (  # noqa: E402
    BUNDLED_TARGET_SCRIPTS,
    EXTENSION_TARGET_SCRIPTS,
    LINEAR_SCRIPTS,
    SCRIPT_FONTS,
    SHAPING_COMPLEX,
    SHAPING_LINEAR,
    UNICODE_BLOCKS,
    block_for_codepoint,
    condense_ranges,
    coverage_inventory,
    font_codepoints,
    format_ranges,
    main,
    recommended_font,
    script_for_codepoint,
    shaping_policy,
)
from scripts.font_cmap import font_supports  # noqa: E402
from scripts.typography import display_content  # noqa: E402


FONTS = ROOT / "assets/fonts"
BUNDLED_POLICY = {
    "regular": FONTS / "ComicNeue-Regular.ttf",
    "bold": FONTS / "ComicNeue-Bold.ttf",
    "fallback": FONTS / "NotoSans-Regular.ttf",
}


class UnicodeBlockTableTests(unittest.TestCase):
    def test_blocks_are_ascending_and_non_overlapping(self):
        """Keep the table a partition so a bisect lookup cannot be ambiguous."""
        previous = None
        for block in UNICODE_BLOCKS:
            self.assertLessEqual(block.first, block.last, block.block)
            if previous is not None:
                self.assertGreater(
                    block.first,
                    previous.last,
                    f"{previous.block} overlaps or precedes {block.block}",
                )
            previous = block

    def test_every_refused_block_states_a_reason(self):
        """A refusal a reviewer cannot act on is not a usable diagnostic."""
        for block in UNICODE_BLOCKS:
            with self.subTest(block=block.block):
                if block.shaping == SHAPING_COMPLEX:
                    self.assertTrue(block.reason)
                else:
                    self.assertEqual(SHAPING_LINEAR, block.shaping)
                    self.assertEqual("", block.reason)

    def test_lookup_resolves_boundaries_and_unclaimed_codepoints(self):
        block = block_for_codepoint(0x0041)
        self.assertIsNotNone(block)
        self.assertEqual("Basic Latin", block.block)
        self.assertEqual("Basic Latin", block_for_codepoint(0x0000).block)
        self.assertEqual("Latin-1 Supplement", block_for_codepoint(0x0080).block)
        # U+1380 sits between Ethiopic and Cherokee and belongs to no declared block.
        self.assertIsNone(block_for_codepoint(0x1380))
        self.assertEqual("unassigned", script_for_codepoint(0x1380))


class ShapingPolicyTests(unittest.TestCase):
    def test_linear_scripts_place_with_nominal_advances(self):
        for label, codepoint in (
            ("latin", 0x0041),
            ("vietnamese", 0x1EBF),
            ("polytonic greek", 0x1FB3),
            ("cyrillic extended", 0xA640),
            ("cjk ideograph", 0x6F22),
            ("hiragana", 0x3042),
            ("precomposed hangul syllable", 0xD55C),
            ("armenian", 0x0561),
            ("georgian mkhedruli", 0x10D0),
            ("georgian mtavruli", 0x1C90),
        ):
            with self.subTest(label=label):
                shaping, reason = shaping_policy(codepoint)
                self.assertEqual(SHAPING_LINEAR, shaping)
                self.assertEqual("", reason)

    def test_shaping_dependent_scripts_are_refused_with_their_reason(self):
        """Name matching used to let these through; the block table must not."""
        for label, codepoint, expected in (
            ("arabic", 0x0634, "contextual joining"),
            ("hebrew", 0x05D0, "bidirectional reordering"),
            ("syriac", 0x0710, "contextual joining"),
            ("devanagari", 0x0915, "cluster reordering"),
            ("tamil", 0x0B95, "cluster reordering"),
            ("thai", 0x0E01, "mark stacking"),
            ("khmer", 0x1780, "cluster reordering"),
            ("mongolian", 0x1820, "vertical layout"),
            ("conjoining hangul jamo", 0x1100, "syllable composition"),
        ):
            with self.subTest(label=label):
                shaping, reason = shaping_policy(codepoint)
                self.assertEqual(SHAPING_COMPLEX, shaping)
                self.assertIn(expected, reason)

    def test_codepoints_beyond_the_lettering_plane_are_refused(self):
        for codepoint in (0x1F600, 0x10000, 0x20000):
            with self.subTest(codepoint=codepoint):
                shaping, reason = shaping_policy(codepoint)
                self.assertEqual(SHAPING_COMPLEX, shaping)
                self.assertIn("basic multilingual plane", reason)

    def test_byte_order_mark_is_not_treated_as_an_arabic_form(self):
        """U+FEFF shares a block with Arabic forms but is a format control."""
        self.assertEqual(SHAPING_LINEAR, shaping_policy(0xFEFF)[0])
        self.assertEqual(SHAPING_COMPLEX, shaping_policy(0xFE71)[0])

    def test_unclaimed_lettering_plane_codepoints_place_linearly(self):
        """Silence in the table means symbol territory, not an unknown script."""
        self.assertEqual(SHAPING_LINEAR, shaping_policy(0x1380)[0])


class ScriptFontSelectionTests(unittest.TestCase):
    def test_every_recommended_font_is_openly_licensed_and_letterable(self):
        for entry in SCRIPT_FONTS:
            with self.subTest(script=entry.script):
                self.assertEqual("OFL-1.1", entry.license_id)
                self.assertIn(entry.script, LINEAR_SCRIPTS)
                self.assertTrue(entry.family)
                self.assertTrue(entry.file_name.endswith((".ttf", ".otf")))
                self.assertTrue(entry.upstream.startswith("https://"))

    def test_every_extension_target_script_has_a_selected_font(self):
        for script in EXTENSION_TARGET_SCRIPTS:
            with self.subTest(script=script):
                self.assertIsNotNone(recommended_font(script))

    def test_extension_target_scripts_survive_the_dialogue_uppercase_transform(self):
        """Dialogue is displayed uppercased, which can change a character's block.

        Georgian is the case that matters: Mkhedruli uppercases to Mtavruli in a
        different Unicode block, so a policy that only classified the lowercase
        form would lose the script exactly when a speaker used it.
        """
        samples = {
            "armenian": "ա",
            "ethiopic": "ሀ",
            "georgian": "ა",
            "han": "漢",
            "hangul": "한",
            "kana": "あ",
        }
        self.assertEqual(sorted(samples), sorted(EXTENSION_TARGET_SCRIPTS))
        for script, character in samples.items():
            with self.subTest(script=script):
                displayed = display_content("dialogue", character)
                resolved = {
                    script_for_codepoint(ord(glyph)) for glyph in displayed
                } | {script_for_codepoint(ord(character))}
                self.assertEqual({script}, resolved)


class CoverageEnumerationTests(unittest.TestCase):
    def test_enumeration_agrees_with_single_character_probes(self):
        """The range walk and the per-character reader must not disagree."""
        path = FONTS / "NotoSans-Regular.ttf"
        covered = font_codepoints(str(path))
        for codepoint in (0x0041, 0x00C9, 0x03A9, 0x0416, 0x1EBF, 0x1FB3, 0xA640):
            with self.subTest(codepoint=codepoint):
                self.assertIn(codepoint, covered)
                self.assertTrue(font_supports(path, chr(codepoint)))
        for codepoint in (0x0378, 0x6F22, 0x3042, 0xD55C, 0x0561):
            with self.subTest(codepoint=codepoint):
                self.assertNotIn(codepoint, covered)
                self.assertFalse(font_supports(path, chr(codepoint)))

    def test_enumeration_stays_inside_the_lettering_plane(self):
        for name in ("ComicNeue-Regular.ttf", "NotoSans-Regular.ttf"):
            with self.subTest(font=name):
                covered = font_codepoints(str(FONTS / name))
                self.assertTrue(covered)
                self.assertLessEqual(max(covered), 0xFFFF)

    def test_ranges_condense_and_format_without_losing_codepoints(self):
        self.assertEqual(
            ((1, 3), (7, 7), (9, 10)), condense_ranges([3, 1, 2, 7, 10, 9])
        )
        self.assertEqual((), condense_ranges([]))
        self.assertEqual(
            ("U+0001-U+0003", "U+0007"), format_ranges(((1, 3), (7, 7)))
        )


class CoverageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.inventory = coverage_inventory(BUNDLED_POLICY)
        self.scripts = {
            entry["script"]: entry for entry in self.inventory["scripts"]
        }

    def test_inventory_reports_every_declared_script_once(self):
        self.assertEqual("font-coverage-inventory", self.inventory["kind"])
        declared = {block.script for block in UNICODE_BLOCKS}
        self.assertEqual(sorted(declared), sorted(self.scripts))
        self.assertEqual(
            sorted(self.scripts), [entry["script"] for entry in self.inventory["scripts"]]
        )

    def test_bundled_policy_still_covers_the_scripts_it_promises(self):
        """Fail loudly if a font swap silently drops a documented script."""
        for script in BUNDLED_TARGET_SCRIPTS:
            with self.subTest(script=script):
                entry = self.scripts[script]
                self.assertEqual(SHAPING_LINEAR, entry["shaping"])
                self.assertIn(entry["status"], {"covered", "partial"})
                self.assertGreater(entry["covered"], 0)

    def test_extension_target_scripts_are_admitted_but_uncovered(self):
        for script in EXTENSION_TARGET_SCRIPTS:
            with self.subTest(script=script):
                entry = self.scripts[script]
                self.assertEqual(SHAPING_LINEAR, entry["shaping"])
                self.assertEqual("uncovered", entry["status"])
                self.assertEqual(0, entry["covered"])
                self.assertIsNotNone(entry["recommended_font"])

    def test_shaping_refused_scripts_report_that_rather_than_coverage(self):
        for script in ("arabic", "devanagari", "hebrew", "thai"):
            with self.subTest(script=script):
                self.assertEqual("shaping-unsupported", self.scripts[script]["status"])

    def test_hangul_is_admitted_through_its_precomposed_syllables(self):
        """A mixed script is letterable when any of its blocks places linearly."""
        entry = self.scripts["hangul"]
        self.assertEqual(SHAPING_LINEAR, entry["shaping"])
        blocks = {block["block"]: block for block in entry["blocks"]}
        self.assertEqual(SHAPING_COMPLEX, blocks["Hangul Jamo"]["shaping"])
        self.assertEqual(SHAPING_LINEAR, blocks["Hangul Syllables"]["shaping"])

    def test_per_font_ranges_are_reported_for_every_role(self):
        fonts = self.inventory["fonts"]
        self.assertEqual(["bold", "fallback", "regular"], sorted(fonts))
        for role, entry in fonts.items():
            with self.subTest(role=role):
                self.assertEqual(BUNDLED_POLICY[role].name, entry["font_id"])
                self.assertGreater(entry["codepoints"], 0)
                self.assertTrue(entry["ranges"])
        self.assertGreater(
            fonts["fallback"]["codepoints"], fonts["regular"]["codepoints"]
        )

    def test_inventory_is_deterministic_and_json_serializable(self):
        again = coverage_inventory(BUNDLED_POLICY)
        self.assertEqual(self.inventory, again)
        self.assertEqual(
            json.dumps(self.inventory, ensure_ascii=False, sort_keys=True),
            json.dumps(again, ensure_ascii=False, sort_keys=True),
        )

    def test_inventory_refuses_a_policy_entry_that_is_not_a_path(self):
        with self.assertRaises(TypeError):
            coverage_inventory({"regular": 42})


class InventoryCommandTests(unittest.TestCase):
    def test_default_invocation_prints_the_bundled_inventory(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(0, main([]))
        inventory = json.loads(stream.getvalue())
        self.assertEqual("font-coverage-inventory", inventory["kind"])
        self.assertEqual(
            ["bold", "fallback", "regular"], sorted(inventory["fonts"])
        )

    def test_explicit_policy_is_inventoried(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--font", f"regular={FONTS / 'NotoSans-Regular.ttf'}"])
        self.assertEqual(0, code)
        inventory = json.loads(stream.getvalue())
        self.assertEqual(["regular"], sorted(inventory["fonts"]))

    def test_malformed_and_unreadable_policy_entries_fail_without_output(self):
        for argv in (["--font", "regular"], ["--font", "=path"], ["--font", "regular="]):
            with self.subTest(argv=argv):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    self.assertEqual(1, main(argv))
                self.assertEqual("", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
