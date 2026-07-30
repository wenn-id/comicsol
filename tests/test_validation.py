import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from comic_sol import (  # noqa: E402
    atomic_write_json,
    init_project,
    layout_rects,
    read_json,
    rectangles_overlap,
    sha256_file,
)
from validate_project import (  # noqa: E402
    ProjectValidationError,
    ValidationIssue,
    main as validation_main,
    validate_character_bible,
    validate_manifest,
    validate_panel_record,
    validate_project,
    validate_story_plan,
    validate_storyboard,
)
from quality_records import PANEL_CHECK_IDS, PAGE_CHECK_IDS  # noqa: E402
from normalize_panels import normalize_panel  # noqa: E402


def valid_manifest():
    data = json.loads((ROOT / "templates/manifest.json").read_text("utf-8"))
    data["project_id"] = "sunlight-courier"
    data["title"] = "Sunlight Courier"
    data["created_at"] = "2026-07-18T04:00:00Z"
    data["updated_at"] = "2026-07-18T04:01:00Z"
    data["input"]["source_sha256"] = "a" * 64
    data["settings"]["page_count"] = 1
    data["settings"]["panel_count"] = 1
    data["panels"] = ["p01-01"]
    return data


def valid_characters():
    return {
        "schema_version": "1.0",
        "characters": [{
            "id": "mira", "name": "Mira", "role": "courier",
            "age_band": "young-adult", "pronouns": "she/her",
            "visual_fingerprint": {
                "silhouette": "short compact build", "face": "round face",
                "hair": "chin-length black bob", "wardrobe": "cream jacket",
                "palette": ["charcoal", "cream", "amber"],
                "signature_props": ["courier bag"],
                "invariants": ["amber scarf", "circular bag clasp"],
                "avoid": ["logos", "generated text"],
            },
            "personality": ["resourceful"], "motivation": "finish delivery",
            "speech": "short practical sentences",
            "reference_path": "references/characters/mira.png",
        }],
    }


def valid_story():
    scene = {
        "purpose": "launch the delivery", "location": "dispatch hall",
        "time": "artificial dusk", "characters": ["mira"],
        "continuity_anchor": "brass walls and amber strips",
    }
    first = {"id": "delivery-hall", **scene}
    second = {"id": "generator-shaft", **scene, "purpose": "resolve delivery"}
    return {
        "schema_version": "1.0", "title": "Sunlight Courier",
        "logline": "A courier delivers the last vial of sunlight.",
        "theme": "Hope is shared.", "tone": ["urgent", "tender"],
        "rating": "teen", "setting": "An underground city.",
        "beginning": "Mira receives the vial.", "turn": "A bridge collapses.",
        "climax": "Mira crosses the shaft.", "ending": "The city relights.",
        "scenes": [first, second],
    }


def valid_storyboard():
    return {
        "schema_version": "1.0",
        "pages": [{
            "number": 1, "layout": "full-page",
            "panels": [{
                "id": "p01-01", "order": 1, "scene_id": "delivery-hall",
                "rect": layout_rects("full-page")[0],
                "beat": "Mira catches the vial.", "characters": ["mira"],
                "shot": "wide establishing shot",
                "composition": "Mira on right third with safe top-left",
                "action": "Mira catches the vial.", "expression": "focused surprise",
                "lighting": "amber key light", "continuity": ["mira:amber scarf"],
                "negative": ["text", "speech bubbles", "watermark"],
                "text": [{
                    "id": "p01-01-t01", "kind": "dialogue", "speaker": "mira",
                    "content": "I have one delivery left.", "anchor": "top-left",
                    "tail_target": [0.7, 0.5], "priority": 1,
                }],
            }],
        }],
    }


def valid_panel_record():
    check_ids = (
        "character-identity", "anatomy", "action", "composition",
        "continuity", "text-free", "technical",
    )
    return {
        "schema_version": "1.0", "panel_id": "p01-01",
        "source_prompt_path": "prompts/panels/p01-01.txt",
        "raw_path": "panels/raw/p01-01.png",
        "clean_path": "panels/clean/p01-01.png", "raw_sha256": "b" * 64,
        "dimensions": {"width": 736, "height": 1136}, "attempts": 1,
        "generation": {
            "capability_name": "agent-image-generation",
            "reference_paths": ["references/characters/mira.png"],
            "completed_at": "2026-07-18T04:10:00Z",
        },
        "checks": [{
            "id": check_id, "result": "pass", "severity": "error",
            "evidence": "verified",
        } for check_id in check_ids],
        "decision": "accept", "retry_reason": None, "unresolved_warnings": [],
    }


def valid_panel_record_v2():
    return {
        "schema_version": "2.0",
        "kind": "panel-qa",
        "subject_id": "p01-01",
        "bindings": {
            "raw_path": "panels/raw/p01-01.png",
            "raw_sha256": "b" * 64,
            "raw_width": 736,
            "raw_height": 1136,
            "clean_path": "panels/p01-01/clean.png",
            "clean_sha256": "c" * 64,
            "clean_width": 736,
            "clean_height": 1136,
            "normalization_path": "panels/p01-01/normalization.json",
            "normalization_sha256": "d" * 64,
        },
        "checks": [{
            "id": check_id,
            "result": "pass",
            "severity": "error",
            "evidence": f"Observed {check_id} against current panel artifacts",
            "method": "agent-review",
            "reviewer": "fixture-reviewer",
            "regions": [],
        } for check_id in PANEL_CHECK_IDS],
        "review": {
            "method": "agent-review",
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-07-30T01:00:00Z",
        },
        "decision": "accept",
        "unresolved_warnings": [],
    }


class TemplateContractTests(unittest.TestCase):
    def test_templates_are_canonical_with_quality_records_at_v2(self):
        names = (
            "manifest.json", "character-bible.json", "story-plan.json",
            "storyboard.json", "panel-record.json",
        )
        for name in names:
            raw = (ROOT / "templates" / name).read_bytes()
            self.assertTrue(raw.endswith(b"\n"), name)
            data = json.loads(raw)
            expected_schema = "2.0" if name == "panel-record.json" else "1.0"
            self.assertEqual(expected_schema, data["schema_version"])
            expected = (json.dumps(data, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n").encode("utf-8")
            self.assertEqual(expected, raw, name)

        page_raw = (ROOT / "templates/page-qa.json").read_bytes()
        page = json.loads(page_raw)
        self.assertEqual("2.0", page["schema_version"])
        self.assertEqual("page-qa", page["kind"])
        self.assertEqual(list(PAGE_CHECK_IDS), [check["id"] for check in page["checks"]])
        self.assertEqual(
            (json.dumps(page, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            page_raw,
        )

        panel = json.loads((ROOT / "templates/panel-record.json").read_text("utf-8"))
        self.assertEqual(
            {
                "raw_path", "raw_sha256", "raw_width", "raw_height",
                "clean_path", "clean_sha256", "clean_width", "clean_height",
                "normalization_path", "normalization_sha256",
            },
            set(panel["bindings"]),
        )

        font_path = ROOT / "assets/fonts/NotoSans-Regular.ttf"
        ImageFont.truetype(str(font_path), 42)
        digest = hashlib.sha256(font_path.read_bytes()).hexdigest()
        asset_notes = (ROOT / "assets/README.md").read_text("utf-8")
        self.assertIn(digest, asset_notes)
        self.assertIn("SIL Open Font License", asset_notes)


class LayoutTextStepFieldTests(unittest.TestCase):
    def test_normalization_preserves_semantics_and_counts_limits(self):
        from letter_panels import normalize_content, normalized_word_count

        self.assertEqual("Already normalized.", normalize_content("Already normalized."))
        self.assertEqual("Café", normalize_content("Cafe\u0301"))
        self.assertEqual("one two\nthree", normalize_content(" one\x00  two\n three "))
        self.assertEqual("Emoji 😀 stays!", normalize_content(" Emoji 😀 stays! "))
        self.assertEqual("BAM!", normalize_content("BAM!"))
        self.assertEqual(33, normalized_word_count("word " * 33))


class LayoutGeometryTests(unittest.TestCase):
    def test_all_layouts_use_exact_page_geometry(self):
        expected_counts = {
            "full-page": 1,
            "two-horizontal": 2,
            "three-horizontal": 3,
            "hero-top-two-bottom": 3,
            "two-top-hero-bottom": 3,
        }
        for name, count in expected_counts.items():
            rects = layout_rects(name)
            self.assertEqual(count, len(rects))
            for rect in rects:
                self.assertGreaterEqual(rect["x"], 64)
                self.assertGreaterEqual(rect["y"], 64)
                self.assertLessEqual(rect["x"] + rect["width"], 1536)
                self.assertLessEqual(rect["y"] + rect["height"], 2336)
            self.assertFalse(any(
                rectangles_overlap(a, b)
                for index, a in enumerate(rects)
                for b in rects[index + 1:]
            ))

    def test_layout_rectangles_are_exact_and_unknown_layout_is_rejected(self):
        self.assertEqual(
            [{"x": 64, "y": 64, "width": 1472, "height": 2272}],
            layout_rects("full-page"),
        )
        self.assertEqual([
            {"x": 64, "y": 64, "width": 1472, "height": 1120},
            {"x": 64, "y": 1216, "width": 1472, "height": 1120},
        ], layout_rects("two-horizontal"))
        with self.assertRaisesRegex(ValueError, "unknown layout"):
            layout_rects("diagonal-chaos")


class StrictSchemaValidationTests(unittest.TestCase):
    def assert_issue(self, issues, field_fragment):
        self.assertTrue(
            any(field_fragment in issue.field for issue in issues),
            (field_fragment, issues),
        )
        self.assertEqual(
            sorted(issues, key=lambda item: (item.path, item.field, item.message)),
            issues,
        )

    def test_manifest_rejects_unknown_fields_ids_limits_paths_and_hashes(self):
        cases = []
        data = valid_manifest(); data["surprise"] = True; cases.append((data, "surprise"))
        data = valid_manifest(); data["project_id"] = "Bad ID"; cases.append((data, "project_id"))
        data = valid_manifest(); data["settings"]["page_count"] = 5; cases.append((data, "settings.page_count"))
        data = valid_manifest(); data["settings"]["panel_count"] = 13; cases.append((data, "settings.panel_count"))
        data = valid_manifest(); data["input"]["source_path"] = "../private.txt"; cases.append((data, "input.source_path"))
        data = valid_manifest(); data["input"]["source_path"] = "source/other.txt"; cases.append((data, "input.source_path"))
        data = valid_manifest(); data["input"]["source_sha256"] = "ABC"; cases.append((data, "input.source_sha256"))
        data = valid_manifest(); data["created_at"] = "2026-07-18 04:00:00Z"; cases.append((data, "created_at"))
        data = valid_manifest(); data["capability"]["status"] = "available"; cases.append((data, "capability.name"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_manifest(data), field)
        self.assertEqual([], validate_manifest(valid_manifest()))

    def test_manifest_terminal_status_matches_warning_presence(self):
        complete = valid_manifest()
        complete.update({"status": "COMPLETE", "warnings": ["minor prop drift"]})
        self.assert_issue(validate_manifest(complete), "status")

        warned = valid_manifest()
        warned["status"] = "COMPLETE_WITH_WARNINGS"
        self.assert_issue(validate_manifest(warned), "warnings")

        warned["warnings"] = ["minor prop drift"]
        self.assertEqual([], validate_manifest(warned))

    def test_character_and_story_rules_are_strict(self):
        character_cases = []
        data = valid_characters(); data["unknown"] = 1; character_cases.append((data, "unknown"))
        data = valid_characters(); data["characters"][0]["id"] = "Mira!"; character_cases.append((data, "characters[0].id"))
        data = valid_characters(); data["characters"][0]["visual_fingerprint"]["invariants"] = ["one"]; character_cases.append((data, "invariants"))
        data = valid_characters(); data["characters"][0]["reference_path"] = "/tmp/mira.png"; character_cases.append((data, "reference_path"))
        data = valid_characters(); data["characters"][0]["reference_path"] = "references/characters/other.png"; character_cases.append((data, "reference_path"))
        for data, field in character_cases:
            with self.subTest(character_field=field):
                self.assert_issue(validate_character_bible(data), field)
        self.assertEqual([], validate_character_bible(valid_characters()))

        story_cases = []
        data = valid_story(); data["scenes"] = data["scenes"][:1]; story_cases.append((data, "scenes"))
        data = valid_story(); data["scenes"] = data["scenes"] * 3; story_cases.append((data, "scenes"))
        data = valid_story(); data["scenes"][0]["characters"] = ["Unknown!"]; story_cases.append((data, "scenes[0].characters"))
        data = valid_story(); data["scenes"][0]["characters"] = ["mira", "mira"]; story_cases.append((data, "scenes[0].characters"))
        for data, field in story_cases:
            with self.subTest(story_field=field):
                self.assert_issue(validate_story_plan(data), field)
        self.assertEqual([], validate_story_plan(valid_story()))

    def test_storyboard_rejects_references_text_and_geometry(self):
        story, characters = valid_story(), valid_characters()
        cases = []
        data = valid_storyboard(); data["pages"][0]["panels"][0]["surprise"] = True; cases.append((data, "surprise"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["scene_id"] = "missing"; cases.append((data, "scene_id"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["characters"] = ["ghost"]; cases.append((data, "characters"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["characters"] = ["mira", "mira"]; cases.append((data, "characters"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["continuity"] = ["ghost:blue hat"]; cases.append((data, "continuity"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["continuity"] = ["delivery-hall:invented lighting"]; cases.append((data, "continuity"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["text"][0]["speaker"] = "ghost"; cases.append((data, "speaker"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["text"][0]["content"] = "word " * 33; cases.append((data, "content"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["text"][0]["anchor"] = "center"; cases.append((data, "anchor"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["text"][0]["tail_target"] = [1.2, 0.5]; cases.append((data, "tail_target"))
        data = valid_storyboard(); data["pages"][0]["panels"][0]["rect"]["x"] = 0; cases.append((data, "rect"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_storyboard(data, story, characters), field)
        self.assertEqual([], validate_storyboard(valid_storyboard(), story, characters))

    def test_storyboard_rejects_page_panel_and_text_limits(self):
        story, characters = valid_story(), valid_characters()
        data = valid_storyboard()
        data["pages"] = [deepcopy(data["pages"][0]) for _ in range(5)]
        for number, page in enumerate(data["pages"], 1):
            page["number"] = number
            page["panels"][0]["id"] = f"p{number:02d}-01"
        self.assert_issue(validate_storyboard(data, story, characters), "pages")

        data = valid_storyboard(); panel = data["pages"][0]["panels"][0]
        data["pages"][0]["panels"] = [deepcopy(panel) for _ in range(5)]
        for number, current in enumerate(data["pages"][0]["panels"], 1):
            current["id"], current["order"] = f"p01-{number:02d}", number
        self.assert_issue(validate_storyboard(data, story, characters), "panels")

        data = valid_storyboard(); data["pages"] = []
        for page_number in range(1, 5):
            page = {"number": page_number, "layout": "full-page", "panels": []}
            for panel_number in range(1, 5 if page_number < 4 else 2):
                current = deepcopy(panel)
                current["id"], current["order"] = f"p{page_number:02d}-{panel_number:02d}", panel_number
                page["panels"].append(current)
            data["pages"].append(page)
        self.assert_issue(validate_storyboard(data, story, characters), "pages.panels")

        data = valid_storyboard(); item = data["pages"][0]["panels"][0]["text"][0]
        data["pages"][0]["panels"][0]["text"] = [deepcopy(item) for _ in range(4)]
        for number, text in enumerate(data["pages"][0]["panels"][0]["text"], 1):
            text["id"] = f"p01-01-t{number:02d}"
        self.assert_issue(validate_storyboard(data, story, characters), "text")

        for kind, limit in (("caption", 45), ("sfx", 3)):
            data = valid_storyboard(); text = data["pages"][0]["panels"][0]["text"][0]
            text.update({"kind": kind, "speaker": None, "tail_target": None, "content": "word " * (limit + 1)})
            with self.subTest(kind=kind):
                self.assert_issue(validate_storyboard(data, story, characters), "content")

    def test_storyboard_rejects_overlapping_rectangles(self):
        data = valid_storyboard(); panel = data["pages"][0]["panels"][0]
        data["pages"][0]["layout"] = "two-horizontal"
        second = deepcopy(panel); second["id"], second["order"] = "p01-02", 2
        data["pages"][0]["panels"].append(second)
        self.assert_issue(
            validate_storyboard(data, valid_story(), valid_characters()), "rect"
        )

    def test_panel_record_requires_exact_checks_paths_hashes_and_cross_fields(self):
        cases = []
        data = valid_panel_record(); data["surprise"] = True; cases.append((data, "surprise"))
        data = valid_panel_record(); data["checks"] = data["checks"][:-1]; cases.append((data, "checks"))
        data = valid_panel_record(); data["raw_path"] = "/tmp/panel.png"; cases.append((data, "raw_path"))
        data = valid_panel_record(); data["raw_sha256"] = "B" * 64; cases.append((data, "raw_sha256"))
        data = valid_panel_record(); data["decision"] = "regenerate"; data["retry_reason"] = None; cases.append((data, "retry_reason"))
        data = valid_panel_record(); data["checks"][0]["result"] = "fail"; cases.append((data, "decision"))
        data = valid_panel_record(); data["checks"][0]["result"] = "warning"; cases.append((data, "decision"))
        data = valid_panel_record(); data["checks"][0].update({"result": "fail", "severity": "warning"}); cases.append((data, "decision"))
        data = valid_panel_record(); data["decision"] = "accept_with_warnings"; cases.append((data, "unresolved_warnings"))
        data = valid_panel_record(); data["raw_path"] = None; cases.append((data, "raw_path"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_panel_record(data), field)
        self.assertEqual([], validate_panel_record(valid_panel_record()))

        data = valid_panel_record()
        data["checks"][0].update({"result": "warning", "severity": "warning"})
        data.update({"decision": "regenerate", "retry_reason": "warning impairs readability"})
        self.assertEqual([], validate_panel_record(data))

    def test_panel_record_v2_uses_shared_quality_contract(self):
        self.assertEqual([], validate_panel_record(valid_panel_record_v2()))

        generic = valid_panel_record_v2()
        for check in generic["checks"]:
            check["evidence"] = "verified"
        self.assert_issue(validate_panel_record(generic), "quality-evidence-generic")

        wrong_kind = valid_panel_record_v2()
        wrong_kind["kind"] = "page-qa"
        self.assert_issue(validate_panel_record(wrong_kind), "kind")

        private_path = valid_panel_record_v2()
        private_path["bindings"]["raw_path"] = "/home/private/panel.png"
        self.assert_issue(validate_panel_record(private_path), "bindings.raw_path")

    def test_panel_override_fields_require_a_recorded_visual_warning(self):
        reason = "minor prop drift is acceptable"
        valid = valid_panel_record()
        valid["checks"][0].update({"result": "fail", "severity": "warning"})
        valid.update({
            "decision": "accept_with_warnings",
            "failure_category": "visual_qa",
            "override_reason": reason,
            "unresolved_warnings": [reason],
        })
        self.assertEqual([], validate_panel_record(valid))

        cases = []
        data = deepcopy(valid); data["failure_category"] = "safety_refusal"; cases.append((data, "failure_category"))
        data = deepcopy(valid); data["decision"] = "accept"; cases.append((data, "override_reason"))
        data = deepcopy(valid); data["unresolved_warnings"] = ["different warning"]; cases.append((data, "override_reason"))
        data = deepcopy(valid); data["checks"][0].update({"result": "pass", "severity": "error"}); cases.append((data, "override_reason"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_panel_record(data), field)


class ProjectValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root, "Sunlight Courier", b"A courier carries the last light.",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest = read_json(self.project / "project.json")
        manifest.update(valid_manifest())
        manifest["input"]["source_sha256"] = sha256_file(self.project / "source/input.txt")
        atomic_write_json(self.project / "project.json", manifest)
        atomic_write_json(self.project / "plan/story-plan.json", valid_story())
        atomic_write_json(self.project / "plan/character-bible.json", valid_characters())
        atomic_write_json(self.project / "plan/storyboard.json", valid_storyboard())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def add_panel_files(self, mode="RGB"):
        (self.project / "prompts/panels/p01-01.txt").write_text(
            "original panel prompt", encoding="utf-8"
        )
        Image.new("RGB", (512, 512), "white").save(
            self.project / "references/characters/mira.png"
        )
        raw = self.project / "panels/raw/p01-01.png"
        clean = self.project / "panels/clean/p01-01.png"
        color = (20, 30, 40, 128) if mode == "RGBA" else (20, 30, 40)
        Image.new(mode, (736, 1136), color).save(raw)
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(clean)
        record = valid_panel_record(); record["raw_sha256"] = sha256_file(raw)
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

    def test_plan_stage_reads_only_plan_files(self):
        (self.project / "plan/storyboard.json").unlink()
        self.assertEqual([], validate_project(self.project, "plan"))

    def test_storyboard_stage_cross_checks_manifest_counts(self):
        manifest = read_json(self.project / "project.json")
        manifest["settings"]["page_count"] = 4
        manifest["settings"]["panel_count"] = 12
        atomic_write_json(self.project / "project.json", manifest)
        issues = validate_project(self.project, "storyboard")
        self.assertTrue(any(issue.field == "settings.page_count" for issue in issues), issues)
        self.assertTrue(any(issue.field == "settings.panel_count" for issue in issues), issues)

        manifest["settings"].update({"page_count": 1, "panel_count": 1})
        manifest["panels"] = []
        atomic_write_json(self.project / "project.json", manifest)
        issues = validate_project(self.project, "storyboard")
        self.assertTrue(any(issue.field == "panels" for issue in issues), issues)

    def test_panel_stage_validates_hash_dimensions_aspect_and_alpha(self):
        self.add_panel_files()
        issues = validate_project(self.project, "panels")
        self.assertTrue(any(
            issue.field == "quality-migration-required"
            and "schema 1.0" in issue.message
            for issue in issues
        ), issues)
        self.assertFalse(any(
            issue.field != "quality-migration-required" for issue in issues
        ), issues)

        self.add_panel_files(mode="RGBA")
        issues = validate_project(self.project, "panels")
        self.assertTrue(any("alpha" in issue.message for issue in issues), issues)

        Image.new("RGB", (512, 512), "black").save(
            self.project / "panels/raw/p01-01.png"
        )
        issues = validate_project(self.project, "panels")
        self.assertTrue(any("hash" in issue.message for issue in issues), issues)
        self.assertTrue(any("aspect" in issue.message for issue in issues), issues)

    def test_schema_two_panel_record_clears_migration_issue(self):
        self.add_panel_files()
        canonical_clean = normalize_panel(
            self.project, "p01-01", "panels/raw/p01-01.png",
            (736, 1136), "exact",
        )
        record = valid_panel_record_v2()
        raw = self.project / record["bindings"]["raw_path"]
        clean = canonical_clean
        normalization = self.project / record["bindings"]["normalization_path"]
        record["bindings"]["raw_sha256"] = sha256_file(raw)
        record["bindings"]["clean_sha256"] = sha256_file(clean)
        record["bindings"]["normalization_sha256"] = sha256_file(normalization)
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

        issues = validate_project(self.project, "panels")
        self.assertEqual([], issues)

    def test_panel_stage_normalizes_pillow_safety_error_as_validation_issue(self):
        self.add_panel_files()
        with patch(
            "validate_project.Image.open",
            side_effect=Image.DecompressionBombError("unsafe dimensions"),
        ):
            issues = validate_project(self.project, "panels")
        self.assertTrue(any("unreadable" in issue.message for issue in issues), issues)

    def test_final_stage_rejects_safety_failure_despite_complete_manifest(self):
        self.add_panel_files()
        record_path = self.project / "qa/panels/p01-01.json"
        record = read_json(record_path)
        record.update({
            "decision": "regenerate",
            "retry_reason": "provider safety refusal",
            "failure_category": "safety_refusal",
        })
        atomic_write_json(record_path, record)

        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest.update({"status": "COMPLETE", "warnings": []})
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(any(
            issue.path == "qa/panels/p01-01.json"
            and issue.field == "decision"
            and "unresolved" in issue.message
            for issue in issues
        ), issues)
        self.assertTrue(any(
            issue.path == "project.json"
            and issue.field == "status"
            and "unresolved panel errors" in issue.message
            for issue in issues
        ), issues)

    def test_final_stage_requires_panel_warnings_and_warning_terminal(self):
        self.add_panel_files()
        reason = "minor prop drift is acceptable"
        record_path = self.project / "qa/panels/p01-01.json"
        record = read_json(record_path)
        record["checks"][0].update({"result": "fail", "severity": "warning"})
        record.update({
            "decision": "accept_with_warnings",
            "failure_category": "visual_qa",
            "override_reason": reason,
            "unresolved_warnings": [reason],
        })
        atomic_write_json(record_path, record)

        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest.update({"status": "COMPLETE", "warnings": []})
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(any(
            issue.path == "project.json"
            and issue.field == "warnings"
            and reason in issue.message
            for issue in issues
        ), issues)
        self.assertTrue(any(
            issue.path == "project.json"
            and issue.field == "status"
            and "COMPLETE_WITH_WARNINGS" in issue.message
            for issue in issues
        ), issues)

        manifest.update({"status": "COMPLETE_WITH_WARNINGS", "warnings": [reason]})
        atomic_write_json(manifest_path, manifest)
        issues = validate_project(self.project, "final")
        self.assertFalse(any(
            issue.path == "project.json"
            and issue.field in {"status", "warnings"}
            for issue in issues
        ), issues)

    def test_final_stage_reports_malformed_manifest_warnings_without_raising(self):
        self.add_panel_files()
        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest["warnings"] = [{"invalid": "warning"}]
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(any(
            issue.path == "project.json" and issue.field.startswith("warnings")
            for issue in issues
        ), issues)

    def test_missing_files_are_sorted_validation_issues(self):
        shutil.rmtree(self.project / "plan")
        issues = validate_project(self.project, "storyboard")
        self.assertGreaterEqual(len(issues), 3)
        self.assertEqual(
            sorted(issues, key=lambda item: (item.path, item.field, item.message)),
            issues,
        )

    def test_project_rejects_noncanonical_json_and_symlink_escape(self):
        story_path = self.project / "plan/story-plan.json"
        data = json.loads(story_path.read_text("utf-8"))
        story_path.write_text(json.dumps(data), encoding="utf-8")
        issues = validate_project(self.project, "plan")
        self.assertTrue(any("canonical" in issue.message for issue in issues), issues)

        atomic_write_json(story_path, data)
        self.add_panel_files()
        outside = self.root / "outside.png"
        Image.new("RGB", (736, 1136), "black").save(outside)
        raw = self.project / "panels/raw/p01-01.png"
        raw.unlink()
        try:
            raw.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        record_path = self.project / "qa/panels/p01-01.json"
        record = read_json(record_path)
        record["raw_sha256"] = sha256_file(outside)
        atomic_write_json(record_path, record)
        def guarded_hash(path):
            if Path(path).resolve() == outside.resolve():
                raise AssertionError("validator followed an escaping image symlink")
            return sha256_file(path)

        with patch("validate_project.sha256_file", side_effect=guarded_hash):
            issues = validate_project(self.project, "panels")
        self.assertTrue(any("escapes the project" in issue.message for issue in issues), issues)

    def test_hash_reads_repeat_shared_resolution_for_original_relative_paths(self):
        import validate_project as validation_module

        self.add_panel_files()
        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        artifact_relative = "plan/story-plan.json"
        manifest["artifacts"]["story_plan"] = {
            "path": artifact_relative,
            "sha256": sha256_file(self.project / artifact_relative),
        }
        atomic_write_json(manifest_path, manifest)

        for stage, relative, minimum_calls in (
            ("panels", "panels/raw/p01-01.png", 3),
            ("panels", "source/input.txt", 2),
            ("final", artifact_relative, 2),
        ):
            with self.subTest(stage=stage, relative=relative):
                calls = []
                real_resolver = validation_module.contained_project_path

                def recording_resolver(project_dir, candidate, **kwargs):
                    if candidate == relative:
                        calls.append((candidate, kwargs.get("must_exist", False)))
                    return real_resolver(project_dir, candidate, **kwargs)

                with patch("validate_project.contained_project_path", side_effect=recording_resolver):
                    validate_project(self.project, stage)
                self.assertGreaterEqual(len(calls), minimum_calls, calls)
                self.assertTrue(calls[-1][1], calls)

    def test_project_validation_error_exposes_immutable_issue_tuple(self):
        issue = ValidationIssue("project.json", "status", "invalid")
        error = ProjectValidationError([issue])
        self.assertEqual((issue,), error.issues)
        with self.assertRaises(Exception):
            issue.path = "changed"

    def test_cli_returns_two_for_issues_and_one_for_io_failure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = validation_main([str(self.project), "--stage", "panels", "--json"])
        self.assertEqual(2, result)
        self.assertTrue(json.loads(stdout.getvalue())["issues"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = validation_main([str(self.root / "missing"), "--stage", "plan"])
        self.assertEqual(1, result)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = validation_main([])
        self.assertEqual(1, result)


class SkillContractTests(unittest.TestCase):
    EXACT_CAPABILITY_ERROR = (
        "Comic Sol cannot generate panels because this agent session has no compatible "
        "text-to-image capability. Enable or install an image-generation skill/tool that "
        "can return a local raster image, then say “resume this Comic Sol project.” Your "
        "story plan and editable project files have been preserved at the project path "
        "printed below."
    )

    def skill_text(self):
        return (ROOT / "SKILL.md").read_text("utf-8")

    def all_agent_text(self):
        paths = [ROOT / "SKILL.md"] + sorted((ROOT / "references").glob("*.md"))
        return "\n".join(path.read_text("utf-8") for path in paths)

    def test_skill_is_trigger_focused_and_all_progressive_links_exist(self):
        text = self.skill_text()
        self.assertLess(len(text.splitlines()), 220)
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]
        self.assertEqual(["name", "description"], keys)
        self.assertIn("name: comic-sol", frontmatter)
        description = next(
            line.split(":", 1)[1].strip()
            for line in frontmatter.splitlines() if line.startswith("description:")
        ).lower()
        for term in ("create", "storyboard", "render", "resume", "export", "prompt", "story", ".txt", ".md"):
            self.assertIn(term, description)
        for name in (
            "workflow", "creative-direction", "capability-detection",
            "visual-qa", "safety-ip", "schemas",
        ):
            self.assertIn(f"references/{name}.md", text)
            self.assertTrue((ROOT / "references" / f"{name}.md").is_file())

    def test_scripts_and_capability_reference_are_provider_neutral(self):
        forbidden = re.compile(
            r"OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_ -]?key\s*=|requests\.|httpx\.|urllib\.request",
            re.IGNORECASE,
        )
        paths = sorted((ROOT / "scripts").glob("*.py")) + [
            ROOT / "references/capability-detection.md"
        ]
        for path in paths:
            self.assertIsNone(forbidden.search(path.read_text("utf-8")), path)

    def test_workflow_has_exact_error_questions_defaults_and_state_order(self):
        text = self.all_agent_text()
        self.assertIn(self.EXACT_CAPABILITY_ERROR, text)
        for condition in (
            "source is unreadable or the intended source among multiple named files is ambiguous",
            "page count exceeds 4 or panel count exceeds 12",
            "audience rating is materially ambiguous",
            "output directory exists but is not a valid Comic Sol project",
        ):
            self.assertIn(condition, text)
        for default in (
            "Pages: 2", "Panels: 4–8", "Left-to-right", "1600 × 2400",
            "32 px gutter", "64 px outer margin", "Teen", "./comic-sol-output/",
        ):
            self.assertIn(default, text)
        self.assertIn(
            "INIT → PLANNED → SCRIPTED → STORYBOARDED → REFERENCES_READY → "
            "PANELS_READY → QA_READY → LETTERED → COMPOSED → EXPORTED → COMPLETE",
            text,
        )

    def test_visual_qa_budgets_checks_and_completion_contract(self):
        text = self.all_agent_text()
        for check_id in (
            "character-identity", "anatomy", "action", "composition",
            "continuity", "text-free", "technical",
        ):
            self.assertIn(check_id, text)
        for phrase in (
            "2 regenerations per panel", "8 extra calls project-wide",
            "one immediate transient repeat", "one correction clause",
            "COMPLETE_WITH_WARNINGS", "BLOCKED", "PDF path", "page directory",
            "manifest path", "QA report path",
        ):
            self.assertIn(phrase, text)

    def test_hybrid_sfx_prompt_lettering_and_qa_contract(self):
        skill = self.skill_text().lower()
        creative = (ROOT / "references/creative-direction.md").read_text("utf-8").lower()
        visual = (ROOT / "references/visual-qa.md").read_text("utf-8").lower()
        schemas = (ROOT / "references/schemas.md").read_text("utf-8").lower()
        workflow = (ROOT / "references/workflow.md").read_text("utf-8").lower()

        self.assertEqual(
            [
                "character-identity", "anatomy", "action", "composition",
                "continuity", "text-free", "technical",
            ],
            re.findall(r"^\d+\. `([^`]+)`:", visual, re.MULTILINE),
        )
        for phrase in (
            "exact storyboard-authored sfx",
            "dynamic motion/action typography",
            "no generated dialogue, captions, or speech bubbles",
            "if no sfx is authored, prohibit generated sfx",
        ):
            self.assertIn(phrase, creative)
        for phrase in (
            "exact storyboard-authored sfx is allowed and required",
            "missing, misspelled, duplicated, or unauthorized sfx",
            "dialogue", "caption", "speech bubbles", "logos", "signatures", "watermarks",
        ):
            self.assertIn(phrase, visual)
        self.assertIn(
            "pillow neither draws sfx nor allocates a placement rectangle or overlap reservation",
            schemas,
        )
        self.assertNotIn("no dialogue, captions, sfx", schemas)
        for phrase in ("text_count", "rendered_text_count", "sfx_count"):
            self.assertIn(phrase, schemas)
            self.assertIn(phrase, workflow)
        self.assertIn("exact storyboard sfx", skill)
        self.assertIn("image model", skill)

    def test_all_deterministic_cli_commands_are_routed(self):
        text = self.skill_text()
        commands = (
            "comic_sol.py init", "comic_sol.py transition", "comic_sol.py status",
            "comic_sol.py doctor", "comic_sol.py resume-plan", "comic_sol.py invalidate",
            "comic_sol.py record-stage", "comic_sol.py record-attempt",
            "comic_sol.py promote-attempt",
            "comic_sol.py override-panel", "validate_project.py", "letter_panels.py",
            "compose_pages.py", "export_pdf.py", "render_report.py",
        )
        for command in commands:
            self.assertIn(command, text)


class OfflineFixtureValidationTests(unittest.TestCase):
    def test_valid_fixture_reaches_panel_stage(self):
        self.assertEqual([], validate_project(ROOT / "tests/fixtures/valid-one-page", "panels"))

    def test_demo_story_is_complete_utf8_source(self):
        words = (ROOT / "tests/fixtures/demo-story.md").read_text("utf-8").split()
        self.assertGreaterEqual(len(words), 150)
        self.assertLessEqual(len(words), 250)


class PackagingTests(unittest.TestCase):
    def readme(self):
        return (ROOT / "README.md").read_text("utf-8")

    def test_readme_is_judge_runnable_without_a_build_service(self):
        readme = self.readme()
        for required in (
            "Pillow==12.3.0",
            "python3.11 -m unittest discover -s tests -v",
            "python3.11 scripts/comic_sol.py doctor",
            "One natural-language",
        ):
            self.assertIn(required, readme)
        self.assertNotRegex(readme.lower(), r"npm run|start the server|docker compose")

    def test_package_files_install_and_artifact_contract_are_documented(self):
        for name in ("LICENSE", "README.md", "SKILL.md"):
            self.assertTrue((ROOT / name).is_file(), name)
        readme = self.readme()
        for phrase in (
            "Python 3.11", "clone", "copy", "Codex skills directory",
            "project.json", "panel PNGs", "page PNGs", "comic PDF", "qa/report.md",
            "Linux", "Windows", "macOS", "WSL",
            "references/capability-detection.md", "Limitations", "clean-room",
            "tests/fixtures/valid-one-page", "No build service",
        ):
            self.assertIn(phrase, readme)

    def test_project_and_font_licenses_remain_distinct(self):
        license_text = (ROOT / "LICENSE").read_text("utf-8")
        assets = (ROOT / "assets/README.md").read_text("utf-8")
        self.assertGreater(len(license_text), 500)
        self.assertIn("MIT License", license_text)
        self.assertIn("2026 The Comic Sol Authors", license_text)
        self.assertIn("SIL Open Font License 1.1", assets)
        self.assertIn("does not replace the font license", assets)

    def test_readme_documents_hybrid_lettering_capabilities_and_limits(self):
        readme = self.readme()
        for phrase in (
            "Comic Neue Regular", "Comic Neue Bold", "per-character",
            "Greek and Cyrillic", ".notdef", "**bold**", "adaptive oval",
            "compact light caption", "image model", "visual QA", "CJK",
            "fallback boxes",
        ):
            self.assertIn(phrase, readme)

    def test_readme_and_ci_are_portable_and_describe_optional_mcp(self):
        readme = self.readme()
        workflow = (ROOT / ".github/workflows/tests.yml").read_text("utf-8")
        recovery = (ROOT / "references/capability-detection.md").read_text("utf-8")
        self.assertNotIn("/home/acer", readme)
        self.assertNotIn("/mnt/c/Users/acer", readme)
        self.assertIn("Base environment", readme)
        self.assertIn("MCP-extra environment", readme)
        self.assertIn("scripts/mcp_server.py", readme)
        self.assertIn("resume", recovery)
        self.assertIn("17 `comic_*` tools", readme)
        for platform in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(platform, workflow)
        self.assertNotIn("/tmp", workflow)
