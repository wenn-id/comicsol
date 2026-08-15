import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

from scripts import typography  # noqa: E402

from scripts.typography import (  # noqa: E402
    TypographyPreflightError,
    lettering_geometry_hash,
    preflight_text_items,
    write_typography_preflight,
)
from scripts.letter_panels import letter_project  # noqa: E402
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
        result = preflight_text_items(
            [item("Stay **LOUD** Ω Ж")], FONT_POLICY
        )

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
        result = preflight_text_items(
            [item("A\t B\nC\x00D", kind="caption")], FONT_POLICY
        )

        self.assertEqual("pass", result["status"])
        self.assertTrue(result["non_glyphs"])
        self.assertTrue(all(
            entry["policy"] in {"normalized-space", "line-break"}
            for entry in result["non_glyphs"]
        ))
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

    def test_arabic_cjk_and_emoji_are_distinct_unsupported_shaping_failures(self):
        cases = (("ش", "U+0634"), ("漢", "U+6F22"), ("😀", "U+1F600"))
        for character, codepoint in cases:
            with self.subTest(character=character):
                with self.assertRaises(TypographyPreflightError) as raised:
                    preflight_text_items([item(character)], FONT_POLICY)
                issue = raised.exception.issues[0]
                self.assertEqual("unsupported-shaping", issue.category)
                self.assertEqual(codepoint, issue.codepoint)
                self.assertEqual("dialogue-1", issue.item_id)
                self.assertIn("unsupported shaping policy", str(raised.exception))
                self.assertNotIn(str(ROOT), str(raised.exception))

    def test_successful_result_records_stable_policy_and_input_hashes(self):
        first = preflight_text_items([item("Safe text")], FONT_POLICY)
        reordered = preflight_text_items(
            [{"content": "Safe text", "kind": "dialogue", "id": "dialogue-1"}],
            {"fallback": FONT_POLICY["fallback"], "bold": FONT_POLICY["bold"], "regular": FONT_POLICY["regular"]},
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

            self.assertEqual(
                (project / "panels/p01-01/typography.json").resolve(), path
            )
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

        with self.assertRaisesRegex(
            TypographyPreflightError, r"U\+1F600.*p01-02-t01"
        ):
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
            self.assertEqual("1.0", geometry["schema_version"])
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
            self.assertIn(placed["anchor"], {
                "top-left", "top-center", "top-right", "middle-right",
                "bottom-right", "bottom-center", "bottom-left", "middle-left",
            })
            self.assertTrue(all(
                isinstance(placed["box"][key], int) and placed["box"][key] > 0
                for key in ("width", "height")
            ))
            self.assertTrue(placed["font_runs"])
            self.assertTrue(all("/" not in run["font_id"] for run in placed["font_runs"]))
            if placed["kind"] == "dialogue":
                tail = placed["tail"]
                self.assertEqual("organic-cubic-v1", tail["policy_version"])
                self.assertEqual("human", tail["voice_source"])
                self.assertEqual(
                    {
                        "attachment", "base", "control", "length", "policy_version",
                        "source_gap", "speaker_anchor", "tip", "voice_source", "width",
                    },
                    set(tail),
                )
            else:
                self.assertIsNone(placed["tail"])
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
            if Path(source).name.startswith("staged-") and Path(destination).name == "typography.json":
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
        self.assertTrue(any(
            issue.field == field and issue.message.startswith("lettering-record-stale:")
            for issue in issues
        ), issues)

    def test_current_preflight_and_geometry_pass(self):
        self.assertEqual(
            (), validate_lettering_provenance(self.project, self.panel_id)
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
                self.assert_stale(
                    validate_lettering_provenance(self.project, self.panel_id), field
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(before)

    def test_geometry_hash_font_policy_notdef_order_box_and_tail_are_rejected(self):
        geometry_path = self.project / "panels/p01-02/lettering.json"
        typography_path = self.project / "panels/p01-02/typography.json"
        original_geometry = geometry_path.read_bytes()
        original_typography = typography_path.read_bytes()
        cases = (
            ("geometry_sha256", lambda g, t: g.update(geometry_sha256="0" * 64)),
            ("bindings.font_policy_sha256", lambda g, t: g["bindings"].update(font_policy_sha256="0" * 64)),
            ("items.reading_order", lambda g, t: g["items"].append(dict(g["items"][0]))),
            ("items.box", lambda g, t: g["items"][0].update(box={"x": 1, "y": 1, "width": 0, "height": 2})),
            (
                "items.tail",
                lambda g, t: g["items"][0]["tail"]["control"][0][0].__setitem__(
                    0, float("inf")
                ),
            ),
            (
                "items.tail",
                lambda g, t: g["items"][0].update(
                    tail={"origin": [1, 2], "target": [3, 4]}
                ),
            ),
            ("glyphs.font_id", lambda g, t: t["glyphs"][0].update(font_id=".notdef")),
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
                self.assert_stale(
                    validate_lettering_provenance(self.project, self.panel_id), field
                )
                geometry_path.write_bytes(original_geometry)
                typography_path.write_bytes(original_typography)

    def test_export_ready_project_gate_requires_current_lettering_provenance(self):
        current = validate_project(self.project, "export-ready")
        self.assertFalse(any(
            issue.path == "panels/p01-02/lettering.json"
            and issue.message.startswith("lettering-record-stale:")
            for issue in current
        ), current)

        clean = self.project / "panels/p01-02/clean.png"
        clean.write_bytes(clean.read_bytes() + b"changed")
        stale = validate_project(self.project, "export-ready")
        self.assertTrue(any(
            issue.path == "panels/p01-02/lettering.json"
            and issue.field == "bindings.clean_sha256"
            and issue.message.startswith("lettering-record-stale:")
            for issue in stale
        ), stale)


if __name__ == "__main__":
    unittest.main()
