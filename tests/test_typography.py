import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

from scripts import typography  # noqa: E402

from scripts.typography import (  # noqa: E402
    LETTERING_GEOMETRY_SCHEMA_VERSION,
    PREFLIGHT_CHECKS,
    SCRIPT_EXTENSION_KEY,
    TYPOGRAPHY_SCHEMA_VERSION,
    TypographyPreflightError,
    lettering_geometry_hash,
    preflight_text_items,
    write_typography_preflight,
)
from scripts import letter_panels  # noqa: E402
from scripts.font_cmap import font_supports  # noqa: E402
from scripts.letter_panels import letter_project  # noqa: E402
from scripts.letter_panels import main as letter_main  # noqa: E402
from scripts.validate_project import validate_lettering_provenance, validate_project  # noqa: E402


FIXTURES = ROOT / "tests/fixtures"


FONT_POLICY = {
    "regular": ROOT / "assets/fonts/ComicNeue-Regular.ttf",
    "bold": ROOT / "assets/fonts/ComicNeue-Bold.ttf",
    "fallback": ROOT / "assets/fonts/NotoSans-Regular.ttf",
}


def item(content, item_id="dialogue-1", kind="dialogue"):
    return {
        "id": item_id,
        "kind": kind,
        "content": content,
    }


class TypographyPreflightTests(unittest.TestCase):
    def test_dialogue_preflight_checks_uppercase_display_codepoints(self):
        """Validate the same uppercase text that dialogue lettering displays."""
        result = preflight_text_items([item("stra\u00dfe")], FONT_POLICY)

        displayed = "".join(entry["character"] for entry in result["glyphs"])
        self.assertIn("STRASSE", displayed)
        self.assertNotIn("\u00df", displayed)

    def test_font_hashes_are_cached_by_path(self):
        """Reuse immutable font digests across separate preflight calls."""
        typography._hash_font_file.cache_clear()
        typography._font_policy_hashes.cache_clear()
        with mock.patch(
            "scripts.typography._hash_font_file", wraps=typography._hash_font_file
        ) as hashed:
            preflight_text_items([item("First")], FONT_POLICY)
            preflight_text_items([item("Second")], FONT_POLICY)
        self.assertEqual(3, hashed.call_count)

    def test_latin_bold_greek_and_cyrillic_select_bundled_fonts(self):
        result = preflight_text_items([item("Stay **LOUD** Ω Ж")], FONT_POLICY)

        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["issues"])
        visible = {
            (entry["character"], entry["style"]): entry["font_id"]
            for entry in result["glyphs"]
            if entry["character"] in {"S", "L", "Ω", "Ж"}
        }
        self.assertEqual("ComicNeue-Regular.ttf", visible[("S", "regular")])
        self.assertEqual("ComicNeue-Bold.ttf", visible[("L", "bold")])
        self.assertEqual("NotoSans-Regular.ttf", visible[("Ω", "regular")])
        self.assertEqual("NotoSans-Regular.ttf", visible[("Ж", "regular")])
        self.assertTrue(all(entry["coverage"] == "supported" for entry in result["glyphs"]))
        self.assertTrue(all(entry["shaping"] == "supported" for entry in result["glyphs"]))

    def test_nfc_combining_mark_is_checked_as_composed_codepoint(self):
        result = preflight_text_items([item("Cafe\u0301")], FONT_POLICY)

        self.assertEqual("pass", result["status"])
        codepoints = [entry["codepoint"] for entry in result["glyphs"]]
        self.assertIn("U+00C9", codepoints)
        self.assertNotIn("U+0301", codepoints)

    def test_whitespace_and_controls_have_explicit_non_glyph_policy(self):
        result = preflight_text_items([item("A\t B\nC\x00D", kind="caption")], FONT_POLICY)

        self.assertEqual("pass", result["status"])
        self.assertTrue(result["non_glyphs"])
        self.assertTrue(
            all(
                entry["policy"] in {"normalized-space", "line-break"}
                for entry in result["non_glyphs"]
            )
        )
        self.assertFalse(any(entry["codepoint"] == "U+0000" for entry in result["glyphs"]))

    def test_missing_glyph_diagnostic_has_remediation_without_private_paths(self):
        with self.assertRaises(TypographyPreflightError) as raised:
            preflight_text_items([item("A\u0378B")], FONT_POLICY)

        message = str(raised.exception)
        self.assertIn("U+0378", message)
        self.assertIn("dialogue-1", message)
        self.assertIn("regular", message)
        self.assertIn("ComicNeue-Regular.ttf", message)
        self.assertIn("NotoSans-Regular.ttf", message)
        self.assertIn("choose supported text or bundle a tested font", message)
        self.assertNotIn(str(ROOT), message)
        self.assertEqual("missing-glyph", raised.exception.issues[0].category)

    def test_shaping_dependent_scripts_are_refused_with_a_reason(self):
        """Refuse only what no font could fix, and say which property forbids it.

        Arabic and emoji were already refused. Hebrew, Devanagari, Thai, and
        conjoining jamo were not: the previous policy matched character names, so
        it caught the scripts someone had named and passed the rest through to a
        coverage check that cannot see reordering or joining at all.
        """
        cases = (
            ("ش", "U+0634", "arabic", "contextual joining"),
            ("א", "U+05D0", "hebrew", "bidirectional reordering"),
            ("क", "U+0915", "devanagari", "cluster reordering"),
            ("ก", "U+0E01", "thai", "mark stacking"),
            ("ᄀ", "U+1100", "hangul", "syllable composition"),
            ("😀", "U+1F600", "unassigned", "basic multilingual plane"),
        )
        for character, codepoint, script, reason in cases:
            with self.subTest(character=character):
                with self.assertRaises(TypographyPreflightError) as raised:
                    preflight_text_items([item(character)], FONT_POLICY)
                issue = raised.exception.issues[0]
                self.assertEqual("unsupported-shaping", issue.category)
                self.assertEqual(codepoint, issue.codepoint)
                self.assertEqual(script, issue.script)
                self.assertIn(reason, issue.reason)
                self.assertEqual("dialogue-1", issue.item_id)
                self.assertIn("unsupported shaping policy", str(raised.exception))
                self.assertIn(reason, str(raised.exception))
                self.assertNotIn(str(ROOT), str(raised.exception))

    def test_linear_scripts_without_a_bundled_face_are_coverage_failures(self):
        """CJK, kana, and Hangul syllables need a font, not a shaping engine.

        They were previously refused as unshapeable, which told an author the text
        was impossible when it was merely unbundled. They are now coverage
        failures that name the vetted face which resolves them.
        """
        cases = (
            ("漢", "U+6F22", "han", "Noto Sans SC"),
            ("あ", "U+3042", "kana", "Noto Sans JP"),
            ("한", "U+D55C", "hangul", "Noto Sans KR"),
            ("ա", "U+0531", "armenian", "Noto Sans Armenian"),
            ("ა", "U+1C90", "georgian", "Noto Sans Georgian"),
        )
        for character, codepoint, script, family in cases:
            with self.subTest(character=character):
                with self.assertRaises(TypographyPreflightError) as raised:
                    preflight_text_items([item(character)], FONT_POLICY)
                issue = raised.exception.issues[0]
                self.assertEqual("missing-glyph", issue.category)
                self.assertEqual(codepoint, issue.codepoint)
                self.assertEqual(script, issue.script)
                self.assertEqual("", issue.reason)
                self.assertIn(family, issue.remediation)
                self.assertIn(script, issue.remediation)
                self.assertNotIn("unsupported shaping policy", str(raised.exception))
                self.assertNotIn(str(ROOT), str(raised.exception))

    def test_declared_script_fixtures_behave_as_documented(self):
        """Drive the supported set from fixtures so adding a script adds a file."""
        fixtures = sorted((FIXTURES / "typography-scripts").glob("*.json"))
        self.assertEqual(22, len(fixtures))
        for path in fixtures:
            scenario = json.loads(path.read_text("utf-8"))
            with self.subTest(fixture=path.name):
                self.assertTrue(scenario["description"])
                text = [item(scenario["content"], kind=scenario["kind"])]
                if scenario["expected_status"] == "pass":
                    result = preflight_text_items(text, FONT_POLICY)
                    self.assertEqual("pass", result["status"])
                    self.assertEqual([], result["issues"])
                    self.assertEqual(
                        scenario["expected_scripts"],
                        {entry["script"]: entry["font_ids"] for entry in result["scripts"]},
                    )
                    continue
                self.assertEqual("fail", scenario["expected_status"])
                with self.assertRaises(TypographyPreflightError) as raised:
                    preflight_text_items(text, FONT_POLICY)
                issue = raised.exception.issues[0]
                self.assertEqual(scenario["expected_category"], issue.category)
                self.assertEqual(scenario["expected_codepoint"], issue.codepoint)
                if scenario["expected_reason_contains"]:
                    self.assertIn(scenario["expected_reason_contains"], issue.reason)
                if scenario["expected_remediation_contains"]:
                    self.assertIn(scenario["expected_remediation_contains"], issue.remediation)

    def test_record_reports_the_checks_that_ran_and_the_scripts_served(self):
        result = preflight_text_items([item("Stay **LOUD** Ω Ж")], FONT_POLICY)

        self.assertEqual(TYPOGRAPHY_SCHEMA_VERSION, result["schema_version"])
        self.assertEqual(
            sorted(PREFLIGHT_CHECKS),
            sorted(check["id"] for check in result["checks"]),
        )
        for check in result["checks"]:
            self.assertEqual("pass", check["result"])
            self.assertEqual("error", check["severity"])
            self.assertEqual("comic-sol", check["reviewer"])
            self.assertTrue(check["evidence"])
        self.assertEqual(
            {
                "cyrillic": ["NotoSans-Regular.ttf"],
                "greek": ["NotoSans-Regular.ttf"],
                "latin": ["ComicNeue-Bold.ttf", "ComicNeue-Regular.ttf"],
            },
            {entry["script"]: entry["font_ids"] for entry in result["scripts"]},
        )
        self.assertEqual(
            sum(entry["codepoints"] for entry in result["scripts"]),
            len(result["glyphs"]),
        )


class ScriptExtensionPolicyTests(unittest.TestCase):
    """A policy may add one face per script without disturbing existing records."""

    def test_absent_and_empty_extensions_leave_the_policy_digest_unchanged(self):
        """Projects lettered before script extensions existed stay current.

        `font_policy_sha256` is bound into every panel's lettering record, so a
        policy shape that hashed differently would mark every existing project
        stale and force a needless re-letter.
        """
        without = preflight_text_items([item("Safe text")], FONT_POLICY)
        empty = preflight_text_items([item("Safe text")], {**FONT_POLICY, SCRIPT_EXTENSION_KEY: {}})

        self.assertEqual(without, empty)
        self.assertEqual(["bold", "fallback", "regular"], sorted(without["font_policy"]))

    def test_extension_face_serves_the_glyphs_the_base_policy_lacks(self):
        # U+2215 DIVISION SLASH is carried by Comic Neue and absent from Noto
        # Sans, so it isolates the extension lookup using only bundled faces.
        base = {
            "regular": FONT_POLICY["fallback"],
            "bold": FONT_POLICY["fallback"],
            "fallback": FONT_POLICY["fallback"],
        }
        with self.assertRaises(TypographyPreflightError) as raised:
            preflight_text_items([item("a\u2215b", kind="caption")], base)
        self.assertEqual("missing-glyph", raised.exception.issues[0].category)

        extended = preflight_text_items(
            [item("a\u2215b", kind="caption")],
            {**base, SCRIPT_EXTENSION_KEY: {"common": FONT_POLICY["regular"]}},
        )

        self.assertEqual("pass", extended["status"])
        self.assertEqual(
            ["bold", "fallback", "regular", "script:common"],
            sorted(extended["font_policy"]),
        )
        self.assertEqual(
            {
                "common": ["ComicNeue-Regular.ttf"],
                "latin": ["NotoSans-Regular.ttf"],
            },
            {entry["script"]: entry["font_ids"] for entry in extended["scripts"]},
        )
        self.assertNotEqual(
            extended["font_policy_sha256"],
            preflight_text_items([item("ab", kind="caption")], base)["font_policy_sha256"],
        )

    def test_styled_face_outranks_the_extension_for_glyphs_it_already_has(self):
        """A Japanese page keeps its Latin interjections in the comic voice."""
        result = preflight_text_items(
            [item("AB", kind="caption")],
            {**FONT_POLICY, SCRIPT_EXTENSION_KEY: {"latin": FONT_POLICY["fallback"]}},
        )

        self.assertEqual(
            {"latin": ["ComicNeue-Regular.ttf"]},
            {entry["script"]: entry["font_ids"] for entry in result["scripts"]},
        )

    def test_same_named_faces_in_different_directories_are_distinct(self):
        """Faces are compared by role, because a font ID is only a file name.

        A policy may legitimately point two roles at same-named files in
        different directories. Comparing the recorded IDs would then treat a
        combining mark and its base as one face and let two genuinely different
        font files position each other.
        """
        with tempfile.TemporaryDirectory() as temporary:
            elsewhere = Path(temporary) / "ComicNeue-Regular.ttf"
            # Same name as the regular face, different file: Noto Sans bytes.
            elsewhere.write_bytes(FONT_POLICY["fallback"].read_bytes())
            policy = {
                **FONT_POLICY,
                SCRIPT_EXTENSION_KEY: {"inherited": elsewhere},
            }

            self.assertEqual(elsewhere.name, Path(FONT_POLICY["regular"]).name)
            # U+0305 is absent from Comic Neue, so the mark resolves to another
            # role while its base stays on the regular face.
            with self.assertRaises(TypographyPreflightError) as raised:
                preflight_text_items([item("q\u0305", kind="caption")], policy)

        issue = raised.exception.issues[0]
        self.assertEqual("unsupported-shaping", issue.category)
        self.assertIn("different faces", issue.reason)

    def test_extension_is_refused_for_a_script_no_font_could_rescue(self):
        for script in ("arabic", "devanagari", "hebrew", "thai", "not-a-script"):
            with self.subTest(script=script):
                with self.assertRaisesRegex(ValueError, "cannot be lettered"):
                    preflight_text_items(
                        [item("Safe text")],
                        {
                            **FONT_POLICY,
                            SCRIPT_EXTENSION_KEY: {script: FONT_POLICY["fallback"]},
                        },
                    )

    def test_unusable_extension_declarations_are_refused(self):
        for extensions, expected in (
            ({"han": ROOT / "assets/fonts/missing.ttf"}, "is unavailable"),
            ({"han": 42}, "requires a path"),
        ):
            with self.subTest(extensions=extensions):
                with self.assertRaisesRegex(ValueError, expected):
                    preflight_text_items(
                        [item("Safe text")],
                        {**FONT_POLICY, SCRIPT_EXTENSION_KEY: extensions},
                    )
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            preflight_text_items(
                [item("Safe text")], {**FONT_POLICY, SCRIPT_EXTENSION_KEY: ["han"]}
            )

    def test_successful_result_records_stable_policy_and_input_hashes(self):
        first = preflight_text_items([item("Safe text")], FONT_POLICY)
        reordered = preflight_text_items(
            [{"content": "Safe text", "kind": "dialogue", "id": "dialogue-1"}],
            {
                "fallback": FONT_POLICY["fallback"],
                "bold": FONT_POLICY["bold"],
                "regular": FONT_POLICY["regular"],
            },
        )

        self.assertEqual(first, reordered)
        self.assertRegex(first["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(first["font_policy_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            ["ComicNeue-Bold.ttf", "ComicNeue-Regular.ttf", "NotoSans-Regular.ttf"],
            sorted(first["font_policy"].values()),
        )

    def test_preflight_persistence_is_canonical_and_geometry_hash_ignores_key_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            result = preflight_text_items([item("Safe text")], FONT_POLICY)
            path = write_typography_preflight(project, "p01-01", result)

            self.assertEqual((project / "panels/p01-01/typography.json").resolve(), path)
            data = json.loads(path.read_text("utf-8"))
            self.assertEqual(result, data)
            self.assertEqual(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                path.read_text("utf-8"),
            )

        record = {
            "schema_version": "1.0",
            "panel_id": "p01-01",
            "items": [{"id": "dialogue-1", "order": 1, "box": [1, 2, 3, 4]}],
        }
        reordered = {
            "items": [{"box": [1, 2, 3, 4], "order": 1, "id": "dialogue-1"}],
            "panel_id": "p01-01",
            "schema_version": "1.0",
        }
        self.assertEqual(lettering_geometry_hash(record), lettering_geometry_hash(reordered))
        self.assertRegex(lettering_geometry_hash(record), r"^[0-9a-f]{64}$")


class TypographyLetteringIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        shutil.copytree(FIXTURES / "valid-one-page", self.project)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_panel_text(self, panel_id, content, kind="caption"):
        """Replace one fixture panel's text with authored content."""
        storyboard_path = self.project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        for page in storyboard["pages"]:
            for panel in page["panels"]:
                if panel["id"] == panel_id:
                    panel["text"] = [
                        {
                            "anchor": "top-left",
                            "content": content,
                            "id": f"{panel_id}-t01",
                            "kind": kind,
                        }
                    ]
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

    def test_newly_declared_scripts_letter_without_missing_glyph_boxes(self):
        """Render the expanded scripts and prove no run drew a `.notdef` box.

        A passing preflight only promises some face maps every character. This
        checks the faces the renderer actually reached for, so a divergence
        between what preflight authorized and what lettering drew would surface
        as a missing glyph here rather than as a row of boxes on the page.
        """
        contents = {
            "p01-01": "Chào bạn, ế mới",
            "p01-02": "ᾰᾳῆῥὥ",
            "p01-03": "Ⱡꝺꞅ Ꙁꙋ",
        }
        for panel_id, content in contents.items():
            self._write_panel_text(panel_id, content)

        outputs = letter_project(self.project)

        self.assertEqual(3, len(outputs))
        for panel_id, content in contents.items():
            with self.subTest(panel_id=panel_id):
                geometry = json.loads(
                    (self.project / f"panels/{panel_id}/lettering.json").read_text("utf-8")
                )
                runs = geometry["items"][0]["font_runs"]
                self.assertEqual(content, "".join(run["text"] for run in runs))
                for run in runs:
                    face = ROOT / "assets/fonts" / run["font_id"]
                    self.assertTrue(face.is_file(), run["font_id"])
                    for character in run["text"]:
                        self.assertTrue(
                            font_supports(face, character),
                            f"{run['font_id']} drew U+{ord(character):04X} as .notdef",
                        )
                self.assertEqual((), validate_lettering_provenance(self.project, panel_id))

    def test_script_extension_renders_and_is_bound_into_provenance(self):
        """An extension face must reach the page and the provenance record."""
        self._write_panel_text("p01-01", "Ratio 1\u22152 now")
        self._write_panel_text("p01-02", "Plain text")
        self._write_panel_text("p01-03", "Plain text")

        with redirect_stdout(io.StringIO()):
            code = letter_main(
                [
                    str(self.project),
                    "--font",
                    str(FONT_POLICY["fallback"]),
                    "--font-script",
                    f"common={FONT_POLICY['regular']}",
                ]
            )

        self.assertEqual(0, code)
        record = json.loads((self.project / "panels/p01-01/typography.json").read_text("utf-8"))
        self.assertEqual("pass", record["status"])
        self.assertEqual(
            ["bold", "fallback", "regular", "script:common"],
            sorted(record["font_policy"]),
        )
        geometry = json.loads((self.project / "panels/p01-01/lettering.json").read_text("utf-8"))
        self.assertEqual(record["font_policy_sha256"], geometry["bindings"]["font_policy_sha256"])
        self.assertIn(
            "ComicNeue-Regular.ttf",
            {run["font_id"] for run in geometry["items"][0]["font_runs"]},
        )
        self.assertEqual((), validate_lettering_provenance(self.project, "p01-01"))

    def test_script_extension_module_state_is_restored_after_a_run(self):
        """The renderer's extension mapping is a module global; leaking it would
        make one lettering run silently change the next one's font policy."""
        self._write_panel_text("p01-01", "Plain text")
        self._write_panel_text("p01-02", "Plain text")
        self._write_panel_text("p01-03", "Plain text")

        with redirect_stdout(io.StringIO()):
            code = letter_main(
                [
                    str(self.project),
                    "--font-script",
                    f"common={FONT_POLICY['regular']}",
                ]
            )
        self.assertEqual(0, code)

        self.assertEqual({}, letter_panels.SCRIPT_FONT_EXTENSIONS)
        self.assertEqual(letter_panels.DEFAULT_FONT_PATH, letter_panels.FONT_PATH)

    def test_unsupported_codepoint_blocks_entire_batch_before_output_mutation(self):
        retained = {}
        for name, payload in (
            ("lettered.png", b"retained-lettered"),
            ("typography.json", b"retained-typography"),
            ("lettering.json", b"retained-geometry"),
        ):
            path = self.project / "panels/p01-01" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            retained[path] = payload

        storyboard_path = self.project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        storyboard["pages"][0]["panels"][1]["text"][0]["content"] = "Stop 😀"
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        with self.assertRaisesRegex(TypographyPreflightError, r"U\+1F600.*p01-02-t01"):
            letter_project(self.project)

        for path, payload in retained.items():
            self.assertEqual(payload, path.read_bytes())
        self.assertFalse((self.project / "panels/p01-02/lettered.png").exists())
        self.assertFalse((self.project / "panels/p01-02/typography.json").exists())
        self.assertFalse((self.project / "panels/p01-02/lettering.json").exists())

    def test_valid_batch_publishes_preflight_image_and_complete_geometry(self):
        storyboard_path = self.project / "plan/storyboard.json"
        storyboard_before = storyboard_path.read_bytes()

        outputs = letter_project(self.project)

        self.assertEqual(3, len(outputs))
        self.assertEqual(storyboard_before, storyboard_path.read_bytes())
        for panel_id in ("p01-01", "p01-02", "p01-03"):
            panel_dir = self.project / "panels" / panel_id
            lettered = panel_dir / "lettered.png"
            typography = json.loads((panel_dir / "typography.json").read_text("utf-8"))
            geometry_path = panel_dir / "lettering.json"
            geometry = json.loads(geometry_path.read_text("utf-8"))

            self.assertTrue(lettered.is_file())
            self.assertEqual("pass", typography["status"])
            self.assertEqual(LETTERING_GEOMETRY_SCHEMA_VERSION, geometry["schema_version"])
            self.assertEqual(panel_id, geometry["panel_id"])
            self.assertRegex(geometry["bindings"]["clean_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(geometry["bindings"]["storyboard_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                typography["font_policy_sha256"],
                geometry["bindings"]["font_policy_sha256"],
            )
            self.assertRegex(geometry["lettered"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(f"panels/{panel_id}/lettered.png", geometry["lettered"]["path"])
            self.assertEqual(lettering_geometry_hash(geometry), geometry["geometry_sha256"])
            self.assertEqual(1, len(geometry["items"]))
            placed = geometry["items"][0]
            self.assertEqual(1, placed["reading_order"])
            self.assertEqual(f"{panel_id}-t01", placed["id"])
            self.assertIn(placed["kind"], {"dialogue", "caption"})
            self.assertIn(
                placed["anchor"],
                {
                    "top-left",
                    "top-center",
                    "top-right",
                    "middle-right",
                    "bottom-right",
                    "bottom-center",
                    "bottom-left",
                    "middle-left",
                },
            )
            self.assertTrue(
                all(
                    isinstance(placed["box"][key], int) and placed["box"][key] > 0
                    for key in ("width", "height")
                )
            )
            self.assertTrue(placed["font_runs"])
            self.assertTrue(all("/" not in run["font_id"] for run in placed["font_runs"]))
            if placed["kind"] == "dialogue":
                self.assertEqual(
                    {
                        "authored_speaker": "mira",
                        "resolution": "declared",
                        "speaker": "mira",
                        "speaker_anchor": [0.7, 0.55],
                    },
                    placed["attribution"],
                )
                tail = placed["tail"]
                self.assertEqual("organic-cubic-v1", tail["policy_version"])
                self.assertEqual("human", tail["voice_source"])
                self.assertEqual(
                    {
                        "attachment",
                        "base",
                        "control",
                        "length",
                        "policy_version",
                        "source_gap",
                        "speaker_anchor",
                        "tip",
                        "voice_source",
                        "width",
                    },
                    set(tail),
                )
            else:
                self.assertIsNone(placed["tail"])
                self.assertIsNone(placed["attribution"])
            self.assertEqual(
                json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                geometry_path.read_text("utf-8"),
            )

    def test_publish_failure_rolls_back_image_preflight_and_geometry(self):
        letter_project(self.project)
        retained = {
            path: path.read_bytes()
            for panel_id in ("p01-01", "p01-02", "p01-03")
            for name in ("lettered.png", "typography.json", "lettering.json")
            for path in (self.project / "panels" / panel_id / name,)
        }
        storyboard_path = self.project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        storyboard["pages"][0]["panels"][0]["text"][0]["content"] = "Changed safely."
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        from scripts import project_io

        real_replace = project_io.os.replace

        def fail_typography_publish(source, destination, **kwargs):
            if (
                Path(source).name.startswith("staged-")
                and Path(destination).name == "typography.json"
            ):
                raise OSError("injected typography publish failure")
            return real_replace(source, destination, **kwargs)

        with mock.patch.object(project_io.os, "replace", side_effect=fail_typography_publish):
            with self.assertRaisesRegex(OSError, "injected typography publish failure"):
                letter_project(self.project)

        for path, payload in retained.items():
            self.assertEqual(payload, path.read_bytes(), path)


class LetteringProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        shutil.copytree(FIXTURES / "valid-one-page", self.project)
        letter_project(self.project)
        self.panel_id = "p01-02"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assert_stale(self, issues, field):
        self.assertTrue(
            any(
                issue.field == field and issue.message.startswith("lettering-record-stale:")
                for issue in issues
            ),
            issues,
        )

    def test_current_preflight_and_geometry_pass(self):
        self.assertEqual((), validate_lettering_provenance(self.project, self.panel_id))

    def _rewrite_typography(self, mutate):
        """Apply one edit to the panel's preflight record on disk."""
        path = self.project / f"panels/{self.panel_id}/typography.json"
        record = json.loads(path.read_text("utf-8"))
        mutate(record)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        return validate_lettering_provenance(self.project, self.panel_id)

    def test_glyph_script_and_shaping_are_recomputed_not_trusted(self):
        """A record must not be able to assert a refused script is letterable.

        Every other verdict in this record is recomputed, so the script and its
        shaping class are too: otherwise a stale or hand-edited record could
        label a Hebrew or Devanagari glyph as a linear script and carry that
        claim past the export gate.
        """
        hebrew = {
            "character": "\u05d0",
            "codepoint": "U+05D0",
            "coverage": "supported",
            "font_id": "NotoSans-Regular.ttf",
            "item_id": f"{self.panel_id}-t01",
            "script": "latin",
            "shaping": "supported",
            "style": "regular",
        }
        cases = (
            ("glyphs.script", lambda r: r["glyphs"].append(hebrew)),
            ("glyphs.script", lambda r: r["glyphs"][0].update({"script": "han"})),
            (
                "glyphs.character",
                lambda r: r["glyphs"][0].update({"character": "\u05d0"}),
            ),
            ("glyphs.codepoint", lambda r: r["glyphs"][0].update({"codepoint": "0x41"})),
        )
        for field, mutate in cases:
            with self.subTest(field=field):
                self.assert_stale(self._rewrite_typography(mutate), field)

    def test_preflight_check_records_must_be_complete_and_unique(self):
        """Passing IDs alone are not evidence that the checks actually ran."""
        cases = (
            lambda r: r["checks"].append(dict(r["checks"][0])),
            lambda r: r.update(
                {"checks": [{"id": check["id"], "result": "pass"} for check in r["checks"]]}
            ),
            lambda r: r["checks"][0].update({"method": "trust-me"}),
            lambda r: r["checks"][0].update({"reviewer": "someone-else"}),
            lambda r: r["checks"][0].update({"severity": "warning"}),
            lambda r: r["checks"][0].update({"evidence": "   "}),
            lambda r: r["checks"][0].update({"result": "fail"}),
            lambda r: r.update({"checks": []}),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                self.assert_stale(self._rewrite_typography(mutate), "typography.checks")

    def test_unhashable_check_id_is_reported_rather_than_raised(self):
        """Malformed JSON must produce an issue, not a TypeError."""
        for identifier in (["a"], {"a": 1}, None, 42):
            with self.subTest(identifier=identifier):
                self.assert_stale(
                    self._rewrite_typography(lambda r: r["checks"][0].update({"id": identifier})),
                    "typography.checks",
                )

    def test_missing_or_changed_bound_artifacts_are_stale(self):
        cases = (
            ("panels/p01-02/typography.json", "typography.path", "delete"),
            ("panels/p01-02/lettered.png", "lettered.sha256", "append"),
            ("panels/p01-02/clean.png", "bindings.clean_sha256", "append"),
            ("plan/storyboard.json", "bindings.storyboard_sha256", "append"),
        )
        for relative, field, operation in cases:
            with self.subTest(relative=relative):
                path = self.project / relative
                before = path.read_bytes()
                if operation == "delete":
                    path.unlink()
                else:
                    path.write_bytes(before + b"changed")
                self.assert_stale(validate_lettering_provenance(self.project, self.panel_id), field)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)

    def test_geometry_hash_font_policy_notdef_order_box_and_tail_are_rejected(self):
        geometry_path = self.project / "panels/p01-02/lettering.json"
        typography_path = self.project / "panels/p01-02/typography.json"
        original_geometry = geometry_path.read_bytes()
        original_typography = typography_path.read_bytes()
        cases = (
            ("geometry_sha256", lambda g, t: g.update(geometry_sha256="0" * 64)),
            (
                "bindings.font_policy_sha256",
                lambda g, t: g["bindings"].update(font_policy_sha256="0" * 64),
            ),
            ("items.reading_order", lambda g, t: g["items"].append(dict(g["items"][0]))),
            (
                "items.box",
                lambda g, t: g["items"][0].update(box={"x": 1, "y": 1, "width": 0, "height": 2}),
            ),
            (
                "items.tail",
                lambda g, t: g["items"][0]["tail"]["control"][0][0].__setitem__(0, float("inf")),
            ),
            (
                "items.tail",
                lambda g, t: g["items"][0].update(tail={"origin": [1, 2], "target": [3, 4]}),
            ),
            ("glyphs.font_id", lambda g, t: t["glyphs"][0].update(font_id=".notdef")),
            ("schema_version", lambda g, t: g.update(schema_version="1.0")),
            ("items.attribution", lambda g, t: g["items"][0].pop("attribution")),
            (
                "items.attribution.speaker",
                lambda g, t: g["items"][0]["attribution"].update(speaker=""),
            ),
            (
                "items.attribution.resolution",
                lambda g, t: g["items"][0]["attribution"].update(resolution="guessed"),
            ),
            (
                "items.attribution.speaker_anchor",
                lambda g, t: g["items"][0]["attribution"].update(speaker_anchor=[1.4, 0.2]),
            ),
        )
        for field, mutate in cases:
            with self.subTest(field=field):
                geometry = json.loads(original_geometry)
                typography = json.loads(original_typography)
                mutate(geometry, typography)
                geometry_path.write_text(
                    json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    "utf-8",
                )
                typography_path.write_text(
                    json.dumps(typography, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    "utf-8",
                )
                self.assert_stale(validate_lettering_provenance(self.project, self.panel_id), field)
                geometry_path.write_bytes(original_geometry)
                typography_path.write_bytes(original_typography)

    def test_export_ready_project_gate_requires_current_lettering_provenance(self):
        current = validate_project(self.project, "export-ready")
        self.assertFalse(
            any(
                issue.path == "panels/p01-02/lettering.json"
                and issue.message.startswith("lettering-record-stale:")
                for issue in current
            ),
            current,
        )

        clean = self.project / "panels/p01-02/clean.png"
        clean.write_bytes(clean.read_bytes() + b"changed")
        stale = validate_project(self.project, "export-ready")
        self.assertTrue(
            any(
                issue.path == "panels/p01-02/lettering.json"
                and issue.field == "bindings.clean_sha256"
                and issue.message.startswith("lettering-record-stale:")
                for issue in stale
            ),
            stale,
        )


if __name__ == "__main__":
    unittest.main()
