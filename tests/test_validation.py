import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFont

ROOT = Path(__file__).resolve().parents[1]

import scripts.validate_project as validation  # noqa: E402
from scripts.comic_sol import (  # noqa: E402
    atomic_write_json,
    init_project,
    layout_rects,
    read_json,
    rectangles_overlap,
    sha256_file,
)
from scripts.handoff import (  # noqa: E402
    attempt_id,
    build_generation_batches,
    build_generation_job,
    build_generation_receipt,
    build_handoff_manifest,
    generation_job_sha256,
    locked_scope_sha256,
)
from scripts.project_io import MAX_READ_BYTES, ProjectLock  # noqa: E402
from scripts.validate_project import (  # noqa: E402
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
from tests.support import make_symlink  # noqa: E402
from scripts.page_quality import (  # noqa: E402
    CURRENT_PAGE_QA_SCHEMA_VERSION,
    PAGE_BINDING_FIELDS,
)
from scripts.quality_records import PANEL_CHECK_IDS, PAGE_CHECK_IDS  # noqa: E402
from scripts.normalize_panels import normalize_panel  # noqa: E402


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
        "characters": [
            {
                "id": "mira",
                "name": "Mira",
                "role": "courier",
                "age_band": "young-adult",
                "pronouns": "she/her",
                "visual_fingerprint": {
                    "silhouette": "short compact build",
                    "face": "round face",
                    "hair": "chin-length black bob",
                    "wardrobe": "cream jacket",
                    "palette": ["charcoal", "cream", "amber"],
                    "signature_props": ["courier bag"],
                    "invariants": ["amber scarf", "circular bag clasp"],
                    "avoid": ["logos", "generated text"],
                },
                "personality": ["resourceful"],
                "motivation": "finish delivery",
                "speech": "short practical sentences",
                "reference_path": "references/characters/mira.png",
            }
        ],
    }


def valid_story():
    scene = {
        "purpose": "launch the delivery",
        "location": "dispatch hall",
        "time": "artificial dusk",
        "characters": ["mira"],
        "continuity_anchor": "brass walls and amber strips",
    }
    first = {"id": "delivery-hall", **scene}
    second = {"id": "generator-shaft", **scene, "purpose": "resolve delivery"}
    return {
        "schema_version": "1.0",
        "title": "Sunlight Courier",
        "logline": "A courier delivers the last vial of sunlight.",
        "theme": "Hope is shared.",
        "tone": ["urgent", "tender"],
        "rating": "teen",
        "setting": "An underground city.",
        "beginning": "Mira receives the vial.",
        "turn": "A bridge collapses.",
        "climax": "Mira crosses the shaft.",
        "ending": "The city relights.",
        "scenes": [first, second],
    }


def valid_storyboard():
    return {
        "schema_version": "1.0",
        "pages": [
            {
                "number": 1,
                "layout": "full-page",
                "panels": [
                    {
                        "id": "p01-01",
                        "order": 1,
                        "scene_id": "delivery-hall",
                        "rect": layout_rects("full-page")[0],
                        "beat": "Mira catches the vial.",
                        "characters": ["mira"],
                        "shot": "wide establishing shot",
                        "composition": "Mira on right third with safe top-left",
                        "action": "Mira catches the vial.",
                        "expression": "focused surprise",
                        "lighting": "amber key light",
                        "continuity": ["mira:amber scarf"],
                        "negative": ["text", "speech bubbles", "watermark"],
                        "text": [
                            {
                                "id": "p01-01-t01",
                                "kind": "dialogue",
                                "speaker": "mira",
                                "content": "I have one delivery left.",
                                "anchor": "top-left",
                                "voice_source": "human",
                                "speaker_anchor": [0.7, 0.5],
                                "priority": 1,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def valid_panel_record():
    check_ids = (
        "character-identity",
        "anatomy",
        "action",
        "composition",
        "continuity",
        "text-free",
        "technical",
    )
    return {
        "schema_version": "1.0",
        "panel_id": "p01-01",
        "source_prompt_path": "prompts/panels/p01-01.txt",
        "raw_path": "panels/raw/p01-01.png",
        "clean_path": "panels/clean/p01-01.png",
        "raw_sha256": "b" * 64,
        "dimensions": {"width": 736, "height": 1136},
        "attempts": 1,
        "generation": {
            "capability_name": "agent-image-generation",
            "reference_paths": ["references/characters/mira.png"],
            "completed_at": "2026-07-18T04:10:00Z",
        },
        "checks": [
            {
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": "verified",
            }
            for check_id in check_ids
        ],
        "decision": "accept",
        "retry_reason": None,
        "unresolved_warnings": [],
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
        "checks": [
            {
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": f"Observed {check_id} against current panel artifacts",
                "method": "agent-review",
                "reviewer": "fixture-reviewer",
                "regions": [],
            }
            for check_id in PANEL_CHECK_IDS
        ],
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
            "manifest.json",
            "character-bible.json",
            "story-plan.json",
            "storyboard.json",
            "panel-record.json",
        )
        for name in names:
            raw = (ROOT / "templates" / name).read_bytes()
            self.assertTrue(raw.endswith(b"\n"), name)
            data = json.loads(raw)
            if name == "panel-record.json":
                expected_schema = "2.0"
            elif name == "manifest.json":
                expected_schema = "1.1"
            else:
                expected_schema = "1.0"
            self.assertEqual(expected_schema, data["schema_version"])
            expected = (
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            self.assertEqual(expected, raw, name)

        page_raw = (ROOT / "templates/page-qa.json").read_bytes()
        page = json.loads(page_raw)
        self.assertEqual(CURRENT_PAGE_QA_SCHEMA_VERSION, page["schema_version"])
        self.assertEqual("page-qa", page["kind"])
        # The template and the validator must agree, or a record authored from the
        # template fails its own binding contract.
        self.assertEqual(PAGE_BINDING_FIELDS, set(page["bindings"]))
        self.assertEqual(list(PAGE_CHECK_IDS), [check["id"] for check in page["checks"]])
        self.assertEqual(
            (json.dumps(page, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            page_raw,
        )

        panel = json.loads((ROOT / "templates/panel-record.json").read_text("utf-8"))
        self.assertEqual(
            {
                "raw_path",
                "raw_sha256",
                "raw_width",
                "raw_height",
                "clean_path",
                "clean_sha256",
                "clean_width",
                "clean_height",
                "normalization_path",
                "normalization_sha256",
            },
            set(panel["bindings"]),
        )
        self.assertEqual([], validate_panel_record(panel))

        font_path = ROOT / "assets/fonts/NotoSans-Regular.ttf"
        ImageFont.truetype(str(font_path), 42)
        digest = hashlib.sha256(font_path.read_bytes()).hexdigest()
        asset_notes = (ROOT / "assets/README.md").read_text("utf-8")
        self.assertIn(digest, asset_notes)
        self.assertIn("SIL Open Font License", asset_notes)


class LayoutTextStepFieldTests(unittest.TestCase):
    def test_normalization_preserves_semantics_and_counts_limits(self):
        from scripts.letter_panels import normalize_content, normalized_word_count

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
            "four-grid": 4,
        }
        for name, count in expected_counts.items():
            rects = layout_rects(name)
            self.assertEqual(count, len(rects))
            for rect in rects:
                self.assertGreaterEqual(rect["x"], 64)
                self.assertGreaterEqual(rect["y"], 64)
                self.assertLessEqual(rect["x"] + rect["width"], 1536)
                self.assertLessEqual(rect["y"] + rect["height"], 2336)
            self.assertFalse(
                any(
                    rectangles_overlap(a, b)
                    for index, a in enumerate(rects)
                    for b in rects[index + 1 :]
                )
            )

    def test_layout_rectangles_are_exact_and_unknown_layout_is_rejected(self):
        self.assertEqual(
            [{"x": 64, "y": 64, "width": 1472, "height": 2272}],
            layout_rects("full-page"),
        )
        self.assertEqual(
            [
                {"x": 64, "y": 64, "width": 1472, "height": 1120},
                {"x": 64, "y": 1216, "width": 1472, "height": 1120},
            ],
            layout_rects("two-horizontal"),
        )
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
        future = valid_manifest()
        future["schema_version"] = "9.0"
        issues = validate_manifest(future)
        self.assertTrue(
            any(
                issue.field == "schema_version"
                and "unsupported project schema version" in issue.message
                for issue in issues
            )
        )

        malformed = valid_manifest()
        malformed["schema_version"] = ["1.0"]
        malformed_issues = validate_manifest(malformed)
        self.assertTrue(any(issue.field == "schema_version" for issue in malformed_issues))

        legacy = valid_manifest()
        legacy.pop("schema_version")
        legacy.pop("handoff", None)
        self.assertEqual([], validate_manifest(legacy))

        explicit_legacy = deepcopy(legacy)
        explicit_legacy["schema_version"] = "1.0"
        self.assertEqual([], validate_manifest(explicit_legacy))

        populated = valid_manifest()
        populated["handoff"] = {
            "contract_version": "1.0",
            "locked_scope_sha256": "a" * 64,
            "manifest_path": "handoff/manifest.json",
        }
        self.assertEqual([], validate_manifest(populated))

        cases = []
        data = valid_manifest()
        data.pop("handoff")
        cases.append((data, "handoff"))
        data = valid_manifest()
        data["handoff"]["unexpected"] = True
        cases.append((data, "handoff.unexpected"))
        data = valid_manifest()
        data["handoff"]["contract_version"] = "1.1"
        cases.append((data, "handoff.contract_version"))
        data = valid_manifest()
        data["handoff"]["locked_scope_sha256"] = "ABC"
        cases.append((data, "handoff.locked_scope_sha256"))
        data = valid_manifest()
        data["handoff"]["locked_scope_sha256"] = "a" * 64
        cases.append((data, "handoff"))
        data = valid_manifest()
        data["handoff"]["manifest_path"] = "handoff/manifest.json"
        cases.append((data, "handoff"))
        for unsafe_path in ("../handoff/manifest.json", "/tmp/manifest.json", "C:/manifest.json"):
            data = valid_manifest()
            data["handoff"]["manifest_path"] = unsafe_path
            cases.append((data, "handoff.manifest_path"))
        data = valid_manifest()
        data["surprise"] = True
        cases.append((data, "surprise"))
        data = valid_manifest()
        data["project_id"] = "Bad ID"
        cases.append((data, "project_id"))
        data = valid_manifest()
        data["settings"]["page_count"] = 5
        cases.append((data, "settings.page_count"))
        data = valid_manifest()
        data["settings"]["panel_count"] = 13
        cases.append((data, "settings.panel_count"))
        data = valid_manifest()
        data["input"]["source_path"] = "../private.txt"
        cases.append((data, "input.source_path"))
        data = valid_manifest()
        data["input"]["source_path"] = "source/other.txt"
        cases.append((data, "input.source_path"))
        data = valid_manifest()
        data["input"]["source_sha256"] = "ABC"
        cases.append((data, "input.source_sha256"))
        data = valid_manifest()
        data["created_at"] = "2026-07-18 04:00:00Z"
        cases.append((data, "created_at"))
        data = valid_manifest()
        data["capability"]["status"] = "available"
        cases.append((data, "capability.name"))
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
        data = valid_characters()
        data["unknown"] = 1
        character_cases.append((data, "unknown"))
        data = valid_characters()
        data["characters"][0]["id"] = "Mira!"
        character_cases.append((data, "characters[0].id"))
        data = valid_characters()
        data["characters"][0]["visual_fingerprint"]["invariants"] = ["one"]
        character_cases.append((data, "invariants"))
        data = valid_characters()
        data["characters"][0]["reference_path"] = "/tmp/mira.png"
        character_cases.append((data, "reference_path"))
        data = valid_characters()
        data["characters"][0]["reference_path"] = "references/characters/other.png"
        character_cases.append((data, "reference_path"))
        for data, field in character_cases:
            with self.subTest(character_field=field):
                self.assert_issue(validate_character_bible(data), field)
        self.assertEqual([], validate_character_bible(valid_characters()))

        story_cases = []
        data = valid_story()
        data["scenes"] = data["scenes"][:1]
        story_cases.append((data, "scenes"))
        data = valid_story()
        data["scenes"] = data["scenes"] * 3
        story_cases.append((data, "scenes"))
        data = valid_story()
        data["scenes"][0]["characters"] = ["Unknown!"]
        story_cases.append((data, "scenes[0].characters"))
        data = valid_story()
        data["scenes"][0]["characters"] = ["mira", "mira"]
        story_cases.append((data, "scenes[0].characters"))
        for data, field in story_cases:
            with self.subTest(story_field=field):
                self.assert_issue(validate_story_plan(data), field)
        self.assertEqual([], validate_story_plan(valid_story()))

    def test_storyboard_rejects_references_text_and_geometry(self):
        story, characters = valid_story(), valid_characters()
        cases = []
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["surprise"] = True
        cases.append((data, "surprise"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["scene_id"] = "missing"
        cases.append((data, "scene_id"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["characters"] = ["ghost"]
        cases.append((data, "characters"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["characters"] = ["mira", "mira"]
        cases.append((data, "characters"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["continuity"] = ["ghost:blue hat"]
        cases.append((data, "continuity"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["continuity"] = ["delivery-hall:invented lighting"]
        cases.append((data, "continuity"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["text"][0]["speaker"] = "ghost"
        cases.append((data, "speaker"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["text"][0]["content"] = "word " * 33
        cases.append((data, "content"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["text"][0]["anchor"] = "center"
        cases.append((data, "anchor"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["text"][0]["speaker_anchor"] = [1.2, 0.5]
        cases.append((data, "speaker_anchor"))
        data = valid_storyboard()
        data["pages"][0]["panels"][0]["rect"]["x"] = 0
        cases.append((data, "rect"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_storyboard(data, story, characters), field)
        self.assertEqual([], validate_storyboard(valid_storyboard(), story, characters))

    def test_storyboard_requires_speaker_aware_dialogue_semantics(self):
        story, characters = valid_story(), valid_characters()
        current = valid_storyboard()
        text = current["pages"][0]["panels"][0]["text"][0]
        text.update({"voice_source": "human", "speaker_anchor": [0.7, 0.5]})
        self.assertEqual([], validate_storyboard(current, story, characters))

        cases = []
        for field in ("voice_source", "speaker_anchor"):
            data = deepcopy(current)
            data["pages"][0]["panels"][0]["text"][0].pop(field)
            cases.append((data, field))
        data = deepcopy(current)
        data["pages"][0]["panels"][0]["text"][0]["voice_source"] = "system"
        cases.append((data, "voice_source"))
        data = deepcopy(current)
        data["pages"][0]["panels"][0]["text"][0]["speaker_anchor"] = [0.7, True]
        cases.append((data, "speaker_anchor"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_storyboard(data, story, characters), field)

    def test_storyboard_accepts_and_separates_multi_speaker_attribution(self):
        story, characters = valid_story(), valid_characters()
        characters["characters"].append(
            {
                **deepcopy(characters["characters"][0]),
                "id": "ren",
                "name": "Ren",
                "role": "gatekeeper",
                "reference_path": "references/characters/ren.png",
            }
        )

        def two_speakers(first_anchor, second_anchor, second_speaker="ren"):
            """Return a storyboard whose only panel letters two spoken balloons."""
            data = valid_storyboard()
            panel = data["pages"][0]["panels"][0]
            panel["characters"] = ["mira", "ren"]
            first = panel["text"][0]
            first["speaker_anchor"] = first_anchor
            second = deepcopy(first)
            second.update(
                {
                    "id": "p01-01-t02",
                    "speaker": second_speaker,
                    "speaker_anchor": second_anchor,
                    "priority": 2,
                    "content": "Then I hold the gate.",
                }
            )
            panel["text"] = [first, second]
            return data

        self.assertEqual(
            [],
            validate_storyboard(two_speakers([0.78, 0.34], [0.22, 0.62]), story, characters),
        )

        # The renderer can infer a speaker from a unique display name, but a
        # storyboard may not author one: `speaker` is a character-bible ID, so a
        # validated project always records `declared` attribution.
        display_name = two_speakers([0.78, 0.34], [0.22, 0.62])
        display_name["pages"][0]["panels"][0]["text"][1]["speaker"] = "Ren"
        self.assert_issue(
            validate_storyboard(display_name, story, characters),
            "text[1].speaker",
        )

        for description, data in (
            ("shared anchor", two_speakers([0.78, 0.34], [0.78, 0.35])),
            ("split anchor", two_speakers([0.78, 0.34], [0.22, 0.62], "mira")),
        ):
            with self.subTest(description=description):
                issues = validate_storyboard(data, story, characters)
                self.assertTrue(
                    any(
                        issue.field == "pages[0].panels[0].text"
                        and issue.message.startswith("dialogue-attribution-ambiguous:")
                        and "p01-01-t01 and p01-01-t02" in issue.message
                        for issue in issues
                    ),
                    issues,
                )

    def test_storyboard_reports_legacy_tail_migration_and_tail_free_captions(self):
        story, characters = valid_story(), valid_characters()
        legacy = valid_storyboard()
        legacy_item = legacy["pages"][0]["panels"][0]["text"][0]
        legacy_item.pop("voice_source")
        legacy_item.pop("speaker_anchor")
        legacy_item["tail_target"] = [0.7, 0.5]
        legacy_issues = validate_storyboard(legacy, story, characters)
        self.assertTrue(
            any("balloon-tail-migration-required" in issue.message for issue in legacy_issues)
        )

        for kind in ("caption", "sfx"):
            data = valid_storyboard()
            item = data["pages"][0]["panels"][0]["text"][0]
            item.update(
                {
                    "kind": kind,
                    "speaker": None,
                    "content": "System status." if kind == "caption" else "KRAK!",
                }
            )
            item.pop("voice_source")
            item.pop("speaker_anchor")
            self.assertEqual([], validate_storyboard(data, story, characters))
            item.update({"voice_source": "human", "speaker_anchor": [0.7, 0.5]})
            issues = validate_storyboard(data, story, characters)
            self.assert_issue(issues, "voice_source")
            self.assert_issue(issues, "speaker_anchor")

    def test_storyboard_rejects_page_panel_and_text_limits(self):
        story, characters = valid_story(), valid_characters()
        data = valid_storyboard()
        data["pages"] = [deepcopy(data["pages"][0]) for _ in range(5)]
        for number, page in enumerate(data["pages"], 1):
            page["number"] = number
            page["panels"][0]["id"] = f"p{number:02d}-01"
        self.assert_issue(validate_storyboard(data, story, characters), "pages")

        data = valid_storyboard()
        panel = data["pages"][0]["panels"][0]
        data["pages"][0]["panels"] = [deepcopy(panel) for _ in range(5)]
        for number, current in enumerate(data["pages"][0]["panels"], 1):
            current["id"], current["order"] = f"p01-{number:02d}", number
        self.assert_issue(validate_storyboard(data, story, characters), "panels")

        data = valid_storyboard()
        data["pages"] = []
        for page_number in range(1, 5):
            page = {"number": page_number, "layout": "full-page", "panels": []}
            for panel_number in range(1, 5 if page_number < 4 else 2):
                current = deepcopy(panel)
                current["id"], current["order"] = (
                    f"p{page_number:02d}-{panel_number:02d}",
                    panel_number,
                )
                page["panels"].append(current)
            data["pages"].append(page)
        self.assert_issue(validate_storyboard(data, story, characters), "pages.panels")

        data = valid_storyboard()
        item = data["pages"][0]["panels"][0]["text"][0]
        data["pages"][0]["panels"][0]["text"] = [deepcopy(item) for _ in range(4)]
        for number, text in enumerate(data["pages"][0]["panels"][0]["text"], 1):
            text["id"] = f"p01-01-t{number:02d}"
        self.assert_issue(validate_storyboard(data, story, characters), "text")

        for kind, limit in (("caption", 45), ("sfx", 3)):
            data = valid_storyboard()
            text = data["pages"][0]["panels"][0]["text"][0]
            text.update({"kind": kind, "speaker": None, "content": "word " * (limit + 1)})
            text.pop("voice_source")
            text.pop("speaker_anchor")
            with self.subTest(kind=kind):
                self.assert_issue(validate_storyboard(data, story, characters), "content")

    def test_storyboard_rejects_overlapping_rectangles(self):
        data = valid_storyboard()
        panel = data["pages"][0]["panels"][0]
        data["pages"][0]["layout"] = "two-horizontal"
        second = deepcopy(panel)
        second["id"], second["order"] = "p01-02", 2
        data["pages"][0]["panels"].append(second)
        self.assert_issue(validate_storyboard(data, valid_story(), valid_characters()), "rect")

    def test_panel_record_requires_exact_checks_paths_hashes_and_cross_fields(self):
        cases = []
        data = valid_panel_record()
        data["surprise"] = True
        cases.append((data, "surprise"))
        data = valid_panel_record()
        data["checks"] = data["checks"][:-1]
        cases.append((data, "checks"))
        data = valid_panel_record()
        data["raw_path"] = "/tmp/panel.png"
        cases.append((data, "raw_path"))
        data = valid_panel_record()
        data["raw_sha256"] = "B" * 64
        cases.append((data, "raw_sha256"))
        data = valid_panel_record()
        data["decision"] = "regenerate"
        data["retry_reason"] = None
        cases.append((data, "retry_reason"))
        data = valid_panel_record()
        data["checks"][0]["result"] = "fail"
        cases.append((data, "decision"))
        data = valid_panel_record()
        data["checks"][0]["result"] = "warning"
        cases.append((data, "decision"))
        data = valid_panel_record()
        data["checks"][0].update({"result": "fail", "severity": "warning"})
        cases.append((data, "decision"))
        data = valid_panel_record()
        data["decision"] = "accept_with_warnings"
        cases.append((data, "unresolved_warnings"))
        data = valid_panel_record()
        data["raw_path"] = None
        cases.append((data, "raw_path"))
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

        reordered = valid_panel_record_v2()
        reordered["checks"] = list(reversed(reordered["checks"]))
        self.assert_issue(validate_panel_record(reordered), "quality-check-ids")

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

    def test_v2_panel_record_rejects_legacy_panel_id(self):
        record = valid_panel_record_v2()
        record["panel_id"] = record["subject_id"]

        self.assert_issue(validate_panel_record(record), "panel_id")

    def test_v2_bindings_must_match_subject_id_paths(self):
        record = valid_panel_record_v2()
        record["bindings"]["raw_path"] = "panels/raw/p99-99.png"

        self.assert_issue(validate_panel_record(record), "bindings.raw_path")

    def test_v2_error_failure_requires_regenerate(self):
        record = valid_panel_record_v2()
        record["checks"][0]["result"] = "fail"

        self.assert_issue(validate_panel_record(record), "decision")

    def test_v2_warning_requires_accept_warning_or_regenerate(self):
        record = valid_panel_record_v2()
        record["checks"][0].update({"result": "warning", "severity": "warning"})

        self.assert_issue(validate_panel_record(record), "decision")
        record.update(
            {
                "decision": "accept-warning",
                "unresolved_warnings": ["minor visual drift"],
            }
        )
        self.assertEqual([], validate_panel_record(record))

    def test_v2_override_reason_requires_a_recorded_failed_warning(self):
        reason = "minor prop drift is acceptable"
        valid = valid_panel_record_v2()
        valid["checks"][0].update({"result": "fail", "severity": "warning"})
        valid.update(
            {
                "decision": "accept-warning",
                "override_reason": reason,
                "unresolved_warnings": [reason],
            }
        )
        self.assertEqual([], validate_panel_record(valid))

        cases = []
        data = deepcopy(valid)
        data["decision"] = "accept"
        cases.append(data)
        data = deepcopy(valid)
        data["unresolved_warnings"] = ["different warning"]
        cases.append(data)
        data = deepcopy(valid)
        data["checks"][0].update({"result": "pass", "severity": "error"})
        cases.append(data)
        data = deepcopy(valid)
        data["override_reason"] = " "
        cases.append(data)
        data = deepcopy(valid)
        data["override_reason"] = None
        cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assert_issue(validate_panel_record(data), "override_reason")

    def test_panel_override_fields_require_a_recorded_visual_warning(self):
        reason = "minor prop drift is acceptable"
        valid = valid_panel_record()
        valid["checks"][0].update({"result": "fail", "severity": "warning"})
        valid.update(
            {
                "decision": "accept_with_warnings",
                "failure_category": "visual_qa",
                "override_reason": reason,
                "unresolved_warnings": [reason],
            }
        )
        self.assertEqual([], validate_panel_record(valid))

        cases = []
        data = deepcopy(valid)
        data["failure_category"] = "safety_refusal"
        cases.append((data, "failure_category"))
        data = deepcopy(valid)
        data["decision"] = "accept"
        cases.append((data, "override_reason"))
        data = deepcopy(valid)
        data["unresolved_warnings"] = ["different warning"]
        cases.append((data, "override_reason"))
        data = deepcopy(valid)
        data["checks"][0].update({"result": "pass", "severity": "error"})
        cases.append((data, "override_reason"))
        data = deepcopy(valid)
        data["override_reason"] = None
        cases.append((data, "override_reason"))
        for data, field in cases:
            with self.subTest(field=field):
                self.assert_issue(validate_panel_record(data), field)


class ProjectValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root,
            "Sunlight Courier",
            b"A courier carries the last light.",
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
        Image.new("RGB", (512, 512), "white").save(self.project / "references/characters/mira.png")
        raw = self.project / "panels/raw/p01-01.png"
        clean = self.project / "panels/clean/p01-01.png"
        color = (20, 30, 40, 128) if mode == "RGBA" else (20, 30, 40)
        Image.new(mode, (736, 1136), color).save(raw)
        Image.new("RGB", (736, 1136), (20, 30, 40)).save(clean)
        record = valid_panel_record()
        record["raw_sha256"] = sha256_file(raw)
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

    def bind_handoff(self, handoff_manifest=None):
        manifest = read_json(self.project / "project.json")
        manifest["handoff"] = {
            "contract_version": "1.0",
            "locked_scope_sha256": "a" * 64,
            "manifest_path": "handoff/manifest.json",
        }
        atomic_write_json(self.project / "project.json", manifest)
        if handoff_manifest is not None:
            (self.project / "handoff").mkdir(exist_ok=True)
            atomic_write_json(self.project / "handoff/manifest.json", handoff_manifest)
        return manifest

    def write_generation_batches(self, value=None):
        path = self.project / "generation/batches.json"
        path.parent.mkdir(exist_ok=True)
        atomic_write_json(path, build_generation_batches([]) if value is None else value)
        return sha256_file(path)

    def valid_generation_job(self, *, batch_id="panels-001", references=()):
        prompt = self.project / "prompts/panels/p01-01.txt"
        prompt.write_text("original panel prompt", encoding="utf-8")
        return build_generation_job(
            subject_kind="panel",
            subject_id="p01-01",
            prompt_path="prompts/panels/p01-01.txt",
            prompt_sha256=sha256_file(prompt),
            references=references,
            requested_dimensions={"width": 736, "height": 1136},
            requested_aspect_ratio="46:71",
            attempt_kind="initial",
            retry_limit=2,
            batch_id=batch_id,
            target_path="panels/attempts/p01-01/initial-001.png",
        )

    def write_generation_job(self, job, *, path_job_id=None):
        job_id = job["job_id"] if path_job_id is None else path_job_id
        path = self.project / f"generation/jobs/{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, job)
        return sha256_file(path)

    @staticmethod
    def raster_bytes(color=(20, 30, 40), *, image_format="PNG"):
        stream = io.BytesIO()
        Image.new("RGB", (736, 1136), color).save(stream, format=image_format)
        return stream.getvalue()

    def prepare_generation_handoff(self, *, status="ready", references=()):
        job = self.valid_generation_job(references=references)
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256, status=status)
        self.refresh_locked_scope(reference_paths=[reference["path"] for reference in references])
        return job, job_sha256

    def clear_generation_receipts(self):
        receipt_dir = self.project / "generation/receipts"
        if receipt_dir.exists():
            shutil.rmtree(receipt_dir)
        receipt_dir.mkdir(parents=True)
        return receipt_dir

    def write_generation_receipt(
        self,
        job,
        *,
        attempt=1,
        outcome="failure",
        category=None,
        raster_sha256=None,
        updates=None,
        filename=None,
    ):
        success = outcome == "success"
        if success and raster_sha256 is None:
            raise ValueError("successful fixture receipts require a raster SHA-256")
        receipt = build_generation_receipt(
            attempt_id=attempt_id(job_id=job["job_id"], attempt=attempt),
            job_id=job["job_id"],
            job_sha256=generation_job_sha256(job),
            raster_path=job["target_path"] if success else None,
            raster_sha256=raster_sha256 if success else None,
            executor_kind="external-tool",
            executor_id="fixture-renderer",
            provider="fixture-provider",
            model="fixture-model",
            capabilities_used={
                "reference_images": bool(job["references"]),
                "dimensions": job["requested_dimensions"] is not None,
                "localized_edit": False,
            },
            outcome=outcome,
            category=category or ("accepted" if success else "transient-tool-error"),
        )
        if updates:
            receipt.update(updates)
        receipt_dir = self.project / "generation/receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_name = filename or f"{receipt['attempt_id']}.json"
        relative = f"generation/receipts/{receipt_name}"
        atomic_write_json(self.project / relative, receipt)
        return receipt, relative

    def add_valid_panel_record_v2(self):
        self.add_panel_files()
        clean = normalize_panel(
            self.project,
            "p01-01",
            "panels/raw/p01-01.png",
            (736, 1136),
            "exact",
        )
        record = valid_panel_record_v2()
        record["bindings"].update(
            {
                "raw_sha256": sha256_file(self.project / record["bindings"]["raw_path"]),
                "clean_sha256": sha256_file(clean),
                "normalization_sha256": sha256_file(
                    self.project / record["bindings"]["normalization_path"]
                ),
            }
        )
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)
        return record

    def assert_project_issue(self, issues, path, field, *message_fragments):
        self.assertTrue(
            any(
                issue.path == path
                and issue.field == field
                and all(fragment in issue.message for fragment in message_fragments)
                for issue in issues
            ),
            f"expected {path}:{field} containing {message_fragments}; got {issues!r}",
        )

    @staticmethod
    def handoff_job(job_id, *, sha256="d" * 64, status="ready"):
        return {
            "job_id": job_id,
            "path": f"generation/jobs/{job_id}.json",
            "sha256": sha256,
            "status": status,
        }

    def valid_handoff_manifest(
        self,
        *,
        batches_sha256="b" * 64,
        jobs=(),
        required_artifacts=(),
    ):
        manifest = read_json(self.project / "project.json")
        return build_handoff_manifest(
            project_id=manifest["project_id"],
            project_schema_version=manifest["schema_version"],
            stage=manifest["status"],
            locked_scope_sha256="a" * 64,
            batches_path="generation/batches.json",
            batches_sha256=batches_sha256,
            jobs=jobs,
            required_artifacts=required_artifacts,
        )

    def bind_generation_handoff(
        self,
        job,
        *,
        job_sha256="d" * 64,
        status="ready",
        batch_id="panels-001",
        batch_kind="panel",
        batch_job_ids=None,
        handoff_jobs=None,
    ):
        job_ids = [job["job_id"]] if batch_job_ids is None else batch_job_ids
        batches = (
            [] if not job_ids else [{"batch_id": batch_id, "kind": batch_kind, "job_ids": job_ids}]
        )
        batches_sha256 = self.write_generation_batches(build_generation_batches(batches))
        jobs = (
            [self.handoff_job(job["job_id"], sha256=job_sha256, status=status)]
            if handoff_jobs is None
            else handoff_jobs
        )
        self.bind_handoff(self.valid_handoff_manifest(batches_sha256=batches_sha256, jobs=jobs))

    def refresh_locked_scope(self, *, reference_paths=()):
        selection_path = self.project / "logs/reference-selection.json"
        if not selection_path.exists():
            atomic_write_json(selection_path, {"panels": [], "schema_version": "1.0"})
        prompt_paths = sorted(
            path.relative_to(self.project).as_posix()
            for relative_dir in ("prompts/panels", "prompts/references")
            for path in (self.project / relative_dir).glob("*.txt")
        )
        digest = locked_scope_sha256(
            self.project,
            prompt_paths=prompt_paths,
            reference_paths=reference_paths,
        )
        manifest = read_json(self.project / "project.json")
        manifest["handoff"]["locked_scope_sha256"] = digest
        atomic_write_json(self.project / "project.json", manifest)
        handoff = read_json(self.project / "handoff/manifest.json")
        handoff["locked_scope_sha256"] = digest
        atomic_write_json(self.project / "handoff/manifest.json", handoff)
        return digest

    def test_populated_handoff_requires_manifest_on_disk(self):
        self.bind_handoff()

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "handoff/manifest.json"
                and issue.field == "file"
                and "missing" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_unpopulated_handoff_validation_does_not_create_project_lock(self):
        lock_path = self.project / ".comic-sol.lock"
        lock_path.unlink(missing_ok=True)

        self.assertEqual([], validate_project(self.project, "plan"))

        self.assertFalse(lock_path.exists())

    def test_populated_handoff_rejects_invalid_manifest_contract(self):
        self.bind_handoff({"schema_version": "1.0"})

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "handoff/manifest.json" and issue.field == "locked_scope_sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_manifest_must_match_project_binding(self):
        handoff = self.valid_handoff_manifest(batches_sha256=self.write_generation_batches())
        self.bind_handoff(handoff)
        self.refresh_locked_scope()
        self.assertEqual([], validate_project(self.project, "plan"))
        handoff = read_json(self.project / "handoff/manifest.json")

        cases = (
            ("locked_scope_sha256", "b" * 64),
            ("project_id", "another-project"),
            ("stage", "PLANNED"),
        )
        for field, replacement in cases:
            with self.subTest(field=field):
                mismatched = deepcopy(handoff)
                mismatched[field] = replacement
                atomic_write_json(self.project / "handoff/manifest.json", mismatched)

                issues = validate_project(self.project, "plan")

                self.assertTrue(
                    any(
                        issue.path == "handoff/manifest.json" and issue.field == field
                        for issue in issues
                    ),
                    issues,
                )

    def test_populated_handoff_requires_batch_map_on_disk(self):
        handoff = self.valid_handoff_manifest()
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "generation/batches.json"
                and issue.field == "file"
                and "missing" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_invalid_batch_contract(self):
        batches_sha256 = self.write_generation_batches({"schema_version": "1.0"})
        handoff = self.valid_handoff_manifest(batches_sha256=batches_sha256)
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "generation/batches.json" and issue.field == "batches"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_requires_matching_batch_hash(self):
        self.write_generation_batches()
        self.bind_handoff(self.valid_handoff_manifest())

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "generation/batches.json" and issue.field == "sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_accepts_valid_ready_job_artifact(self):
        job = self.valid_generation_job()
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        self.refresh_locked_scope()

        self.assertEqual([], validate_project(self.project, "plan"))

    def test_populated_handoff_rejects_invalid_receipt_inventory_entries(self):
        job, _ = self.prepare_generation_handoff()
        valid_attempt_id = attempt_id(job_id=job["job_id"], attempt=1)
        cases = (
            (
                "linked-json",
                f"generation/receipts/{valid_attempt_id}.json",
                "file",
                ("regular JSON file",),
            ),
            (
                "malformed-json",
                f"generation/receipts/{valid_attempt_id}.json",
                "file",
                ("cannot read JSON",),
            ),
            (
                "non-json-entry",
                "generation/receipts/receipt.txt",
                "file",
                ("regular JSON file",),
            ),
            (
                "filename-attempt-mismatch",
                "generation/receipts/wrong-attempt.json",
                "attempt_id",
                ("filename",),
            ),
        )

        for label, expected_path, field, fragments in cases:
            with self.subTest(label=label):
                self.clear_generation_receipts()
                if label == "linked-json":
                    _, relative = self.write_generation_receipt(job)
                    outside = self.root / "outside-receipt.json"
                    outside.write_bytes((self.project / relative).read_bytes())
                    (self.project / relative).unlink()
                    make_symlink(self, self.project / relative, outside)
                elif label == "malformed-json":
                    (self.project / expected_path).write_text("{", encoding="utf-8")
                elif label == "non-json-entry":
                    (self.project / expected_path).write_text("not a receipt", encoding="utf-8")
                else:
                    self.write_generation_receipt(job, filename="wrong-attempt.json")

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(issues, expected_path, field, *fragments)

    def test_populated_handoff_reconciles_receipt_bindings_ordinals_and_budget(self):
        job, _ = self.prepare_generation_handoff()
        cases = (
            ("unknown-job", 1, "job_id", ("current handoff job",)),
            ("job-hash-mismatch", 1, "job_sha256", ("generation job",)),
            ("ordinal-gap", 2, "attempt_id", ("contiguous",)),
            ("ordinal-conflict", 1, "attempt_id", ("conflicting duplicate",)),
            ("retry-budget", 4, "attempt_id", ("retry budget",)),
        )

        for label, ordinal, field, fragments in cases:
            with self.subTest(label=label):
                self.clear_generation_receipts()
                if label == "unknown-job":
                    _, relative = self.write_generation_receipt(
                        job,
                        attempt=ordinal,
                        updates={"job_id": "f" * 64},
                    )
                elif label == "job-hash-mismatch":
                    _, relative = self.write_generation_receipt(
                        job,
                        attempt=ordinal,
                        updates={"job_sha256": "e" * 64},
                    )
                elif label == "ordinal-conflict":
                    receipt, _ = self.write_generation_receipt(job, attempt=ordinal)
                    conflicting = deepcopy(receipt)
                    conflicting["category"] = "provider-refusal"
                    filename = f"duplicate-{receipt['attempt_id']}.json"
                    relative = f"generation/receipts/{filename}"
                    atomic_write_json(self.project / relative, conflicting)
                else:
                    _, relative = self.write_generation_receipt(job, attempt=ordinal)

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(issues, relative, field, *fragments)

    def test_populated_handoff_rejects_every_receipt_after_terminal_success(self):
        job, _ = self.prepare_generation_handoff(status="completed")
        payload = self.raster_bytes((20, 30, 40))
        target = self.project / job["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        raster_sha256 = hashlib.sha256(payload).hexdigest()
        cases = (
            ("failure", "after successful receipt"),
            ("success", "multiple successful receipts"),
        )

        for later_outcome, message in cases:
            with self.subTest(later_outcome=later_outcome):
                self.clear_generation_receipts()
                self.write_generation_receipt(
                    job,
                    attempt=1,
                    outcome="success",
                    raster_sha256=raster_sha256,
                )
                _, later_relative = self.write_generation_receipt(
                    job,
                    attempt=2,
                    outcome=later_outcome,
                    raster_sha256=raster_sha256 if later_outcome == "success" else None,
                )

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(
                    issues,
                    later_relative,
                    "attempt_id",
                    message,
                )

    def test_populated_handoff_binds_success_receipts_to_retained_raster_bytes(self):
        job, _ = self.prepare_generation_handoff(status="completed")
        expected_payload = self.raster_bytes((20, 30, 40))
        expected_sha256 = hashlib.sha256(expected_payload).hexdigest()
        cases = (
            ("missing", "file", ("successful receipt",)),
            ("digest-changed", "sha256", ("successful receipt",)),
        )

        for label, field, fragments in cases:
            with self.subTest(label=label):
                self.clear_generation_receipts()
                retained = self.project / job["target_path"]
                retained.unlink(missing_ok=True)
                self.write_generation_receipt(
                    job,
                    outcome="success",
                    raster_sha256=expected_sha256,
                )
                if label == "digest-changed":
                    retained.parent.mkdir(parents=True, exist_ok=True)
                    retained.write_bytes(self.raster_bytes((200, 30, 40)))

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(
                    issues,
                    job["target_path"],
                    field,
                    *fragments,
                )

    def test_successful_reference_receipt_requires_unambiguous_planning_identity(self):
        for label, subject_id in (("unknown", "orphan"), ("ambiguous", "mira")):
            with self.subTest(label=label):
                self.clear_generation_receipts()
                if label == "ambiguous":
                    story = read_json(self.project / "plan/story-plan.json")
                    story["scenes"][0]["id"] = subject_id
                    atomic_write_json(self.project / "plan/story-plan.json", story)
                prompt_relative = f"prompts/references/{subject_id}.txt"
                prompt = self.project / prompt_relative
                prompt.write_text("identity reference", encoding="utf-8")
                job = build_generation_job(
                    subject_kind="reference",
                    subject_id=subject_id,
                    prompt_path=prompt_relative,
                    prompt_sha256=sha256_file(prompt),
                    references=[],
                    requested_dimensions=None,
                    requested_aspect_ratio=None,
                    attempt_kind="initial",
                    retry_limit=2,
                    batch_id="references-001",
                    target_path=f"references/attempts/{subject_id}/initial-001.png",
                )
                job_sha256 = self.write_generation_job(job)
                self.bind_generation_handoff(
                    job,
                    job_sha256=job_sha256,
                    status="completed",
                    batch_id="references-001",
                    batch_kind="reference",
                )
                self.refresh_locked_scope()
                retained = self.project / job["target_path"]
                retained.parent.mkdir(parents=True, exist_ok=True)
                payload = self.raster_bytes()
                retained.write_bytes(payload)
                _, receipt_relative = self.write_generation_receipt(
                    job,
                    outcome="success",
                    raster_sha256=hashlib.sha256(payload).hexdigest(),
                )

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(
                    issues,
                    receipt_relative,
                    "raster_path",
                    "exactly one",
                    "canonical",
                )

    def test_populated_handoff_declared_status_matches_receipt_effective_status(self):
        job = self.valid_generation_job()
        job_sha256 = self.write_generation_job(job)
        payload = self.raster_bytes()
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        cases = (
            ("completed-without-receipt", "completed", None, "ready"),
            ("ready-with-success", "ready", "success", "completed"),
            ("failed-with-budget-left", "failed", "failure", "ready"),
        )

        for label, declared, outcome, effective in cases:
            with self.subTest(label=label):
                self.clear_generation_receipts()
                retained = self.project / job["target_path"]
                retained.unlink(missing_ok=True)
                self.bind_generation_handoff(job, job_sha256=job_sha256, status=declared)
                self.refresh_locked_scope()
                if outcome == "success":
                    retained.parent.mkdir(parents=True, exist_ok=True)
                    retained.write_bytes(payload)
                    self.write_generation_receipt(
                        job,
                        outcome="success",
                        raster_sha256=payload_sha256,
                    )
                elif outcome == "failure":
                    self.write_generation_receipt(job)

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(
                    issues,
                    "handoff/manifest.json",
                    "jobs[0].status",
                    declared,
                    effective,
                )

    def test_populated_handoff_references_require_exact_png_payloads(self):
        reference_relative = "references/characters/mira.png"
        reference_path = self.project / reference_relative
        for image_format in ("JPEG", "WEBP"):
            with self.subTest(image_format=image_format):
                stream = io.BytesIO()
                Image.new("RGB", (512, 512), "white").save(stream, format=image_format)
                reference_path.write_bytes(stream.getvalue())
                references = [
                    {
                        "path": reference_relative,
                        "sha256": sha256_file(reference_path),
                    }
                ]
                job, _ = self.prepare_generation_handoff(references=references)

                issues = validate_project(self.project, "plan")

                self.assert_project_issue(
                    issues,
                    f"generation/jobs/{job['job_id']}.json",
                    "references[0].path",
                    "PNG",
                )

    def test_completed_panel_receipt_requires_qa_accepted_raw_not_the_initial_raster(self):
        self.add_valid_panel_record_v2()
        self.assertEqual([], validate_project(self.project, "panels"))
        job, _ = self.prepare_generation_handoff(status="completed")
        retained_payload = self.raster_bytes((220, 180, 80))
        retained = self.project / job["target_path"]
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(retained_payload)
        _, receipt_relative = self.write_generation_receipt(
            job,
            outcome="success",
            raster_sha256=hashlib.sha256(retained_payload).hexdigest(),
        )
        record_path = self.project / "qa/panels/p01-01.json"
        accepted_record = read_json(record_path)

        # The promoted raster is the repaired one, so it deliberately differs
        # from the initial handoff receipt while its QA record still accepts it.
        self.assertNotEqual(
            accepted_record["bindings"]["raw_sha256"],
            hashlib.sha256(retained_payload).hexdigest(),
        )
        self.assertEqual([], validate_project(self.project, "panels"))

        raw_path = self.project / "panels/raw/p01-01.png"
        accepted_raw = raw_path.read_bytes()
        cases = {
            "unpromoted": lambda: raw_path.unlink(),
            "faulted-review": lambda: atomic_write_json(
                record_path,
                {**accepted_record, "decision": "regenerate"},
            ),
            "stale-binding": lambda: atomic_write_json(
                record_path,
                {
                    **accepted_record,
                    "bindings": {**accepted_record["bindings"], "raw_sha256": "0" * 64},
                },
            ),
        }
        for name, break_acceptance in cases.items():
            with self.subTest(case=name):
                break_acceptance()
                try:
                    self.assert_project_issue(
                        validate_project(self.project, "panels"),
                        receipt_relative,
                        "raster_path",
                        "panels/raw/p01-01.png",
                        "visual QA",
                    )
                finally:
                    raw_path.write_bytes(accepted_raw)
                    atomic_write_json(record_path, accepted_record)

    def test_populated_handoff_holds_project_lock_through_binding_validation(self):
        job = self.valid_generation_job()
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        self.refresh_locked_scope()
        binding_entered = threading.Event()
        release_binding = threading.Event()
        scope_entered = threading.Event()
        release_scope = threading.Event()
        validation_issues = []
        validation_errors = []
        real_validate_binding = validation._validate_handoff_binding
        real_assert_current_scope = validation.assert_current_locked_scope

        def paused_validate_binding(*args, **kwargs):
            binding_entered.set()
            if not release_binding.wait(5):
                raise TimeoutError("test did not release handoff binding validation")
            return real_validate_binding(*args, **kwargs)

        def paused_assert_current_scope(*args, **kwargs):
            scope_entered.set()
            if not release_scope.wait(5):
                raise TimeoutError("test did not release locked-scope recomputation")
            return real_assert_current_scope(*args, **kwargs)

        def run_validation():
            try:
                validation_issues.extend(validate_project(self.project, "plan"))
            except BaseException as error:
                validation_errors.append(error)

        def attempt_project_lock():
            outcome = []

            def contend_for_project():
                try:
                    with ProjectLock(self.project, timeout=0):
                        outcome.append("acquired")
                except TimeoutError:
                    outcome.append("blocked")

            contender = threading.Thread(target=contend_for_project)
            contender.start()
            contender.join(5)
            self.assertFalse(contender.is_alive())
            self.assertEqual(1, len(outcome))
            return outcome[0]

        validator = threading.Thread(target=run_validation)
        before_binding_outcome = None
        during_scope_outcome = None
        binding_observed = False
        scope_observed = False
        with (
            patch.object(
                validation,
                "_validate_handoff_binding",
                side_effect=paused_validate_binding,
            ),
            patch.object(
                validation,
                "assert_current_locked_scope",
                side_effect=paused_assert_current_scope,
            ),
        ):
            validator.start()
            try:
                binding_observed = binding_entered.wait(5)
                if binding_observed:
                    before_binding_outcome = attempt_project_lock()
                    release_binding.set()
                    scope_observed = scope_entered.wait(5)
                    if scope_observed:
                        during_scope_outcome = attempt_project_lock()
            finally:
                release_binding.set()
                release_scope.set()
                validator.join(5)

        self.assertTrue(binding_observed)
        self.assertTrue(scope_observed)
        self.assertFalse(validator.is_alive())
        self.assertEqual([], validation_errors)
        self.assertEqual([], validation_issues)
        self.assertEqual("blocked", before_binding_outcome)
        self.assertEqual("blocked", during_scope_outcome)
        self.assertEqual("acquired", attempt_project_lock())

    def test_populated_handoff_rejects_changed_locked_scope(self):
        job = self.valid_generation_job()
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        self.refresh_locked_scope()
        self.assertEqual([], validate_project(self.project, "plan"))
        atomic_write_json(
            self.project / "logs/reference-selection.json",
            {"panels": [], "revision": "changed", "schema_version": "1.0"},
        )

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "handoff/manifest.json" and issue.field == "locked_scope_sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_changed_ready_job_prompt(self):
        job = self.valid_generation_job()
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        (self.project / job["prompt_path"]).write_text("changed prompt", encoding="utf-8")

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json"
                and issue.field == "prompt_sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_changed_ready_job_reference(self):
        reference_path = self.project / "references/characters/mira.png"
        Image.new("RGB", (512, 512), "white").save(reference_path)
        job = self.valid_generation_job(
            references=[
                {
                    "path": "references/characters/mira.png",
                    "sha256": sha256_file(reference_path),
                }
            ]
        )
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        Image.new("RGB", (512, 512), "black").save(reference_path)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json"
                and issue.field == "references[0].sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_undecodable_ready_job_reference(self):
        reference_path = self.project / "references/characters/mira.png"
        reference_path.write_bytes(b"not a PNG")
        relative_path = "references/characters/mira.png"
        job = self.valid_generation_job(
            references=[{"path": relative_path, "sha256": sha256_file(reference_path)}]
        )
        job_sha256 = self.write_generation_job(job)
        self.bind_generation_handoff(job, job_sha256=job_sha256)
        self.refresh_locked_scope(reference_paths=[relative_path])

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json"
                and issue.field == "references[0].path"
                and "unreadable" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_requires_non_missing_job_artifact(self):
        job = self.valid_generation_job()
        self.bind_generation_handoff(job)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json"
                and issue.field == "file"
                and "missing" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_invalid_job_contract(self):
        job = self.valid_generation_job()
        invalid_job = {"schema_version": "1.0"}
        job_sha256 = self.write_generation_job(invalid_job, path_job_id=job["job_id"])
        self.bind_generation_handoff(job, job_sha256=job_sha256)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json" and issue.field == "job_id"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_requires_matching_job_hash(self):
        job = self.valid_generation_job()
        self.write_generation_job(job)
        self.bind_generation_handoff(job)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{job['job_id']}.json" and issue.field == "sha256"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_requires_job_content_id_to_match_manifest(self):
        declared_job = self.valid_generation_job()
        stored_job = self.valid_generation_job(batch_id="panels-002")
        job_sha256 = self.write_generation_job(stored_job, path_job_id=declared_job["job_id"])
        self.bind_generation_handoff(declared_job, job_sha256=job_sha256)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == f"generation/jobs/{declared_job['job_id']}.json"
                and issue.field == "job_id"
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_reconciles_batch_and_manifest_job_ids(self):
        job = self.valid_generation_job()
        cases = (
            {"handoff_jobs": []},
            {"batch_job_ids": [], "status": "missing"},
        )
        for options in cases:
            with self.subTest(options=options):
                self.bind_generation_handoff(job, **options)

                issues = validate_project(self.project, "plan")

                self.assertTrue(
                    any(
                        issue.path == "handoff/manifest.json" and issue.field == "jobs"
                        for issue in issues
                    ),
                    issues,
                )

    def test_populated_handoff_requires_job_to_match_batch_membership(self):
        cases = (
            ("panels-002", "panel", "batch_id"),
            ("panels-001", "reference", "subject_kind"),
        )
        for job_batch_id, batch_kind, expected_field in cases:
            with self.subTest(expected_field=expected_field):
                job = self.valid_generation_job(batch_id=job_batch_id)
                job_sha256 = self.write_generation_job(job)
                self.bind_generation_handoff(
                    job,
                    job_sha256=job_sha256,
                    batch_kind=batch_kind,
                )

                issues = validate_project(self.project, "plan")

                self.assertTrue(
                    any(
                        issue.path == f"generation/jobs/{job['job_id']}.json"
                        and issue.field == expected_field
                        for issue in issues
                    ),
                    issues,
                )

    def test_populated_handoff_allows_missing_status_without_job_artifact(self):
        job = self.valid_generation_job()
        self.bind_generation_handoff(job, status="missing")
        self.refresh_locked_scope()

        self.assertEqual([], validate_project(self.project, "plan"))

    def test_populated_handoff_requires_declared_artifact_on_disk(self):
        batches_sha256 = self.write_generation_batches()
        handoff = self.valid_handoff_manifest(
            batches_sha256=batches_sha256,
            required_artifacts=[{"path": "plan/missing.txt", "sha256": "1" * 64}],
        )
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "plan/missing.txt"
                and issue.field == "file"
                and "missing" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_requires_matching_artifact_hash(self):
        batches_sha256 = self.write_generation_batches()
        handoff = self.valid_handoff_manifest(
            batches_sha256=batches_sha256,
            required_artifacts=[{"path": "source/input.txt", "sha256": "2" * 64}],
        )
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "source/input.txt"
                and issue.field == "sha256"
                and "handoff manifest" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_accepts_valid_required_artifact(self):
        batches_sha256 = self.write_generation_batches()
        handoff = self.valid_handoff_manifest(
            batches_sha256=batches_sha256,
            required_artifacts=[
                {
                    "path": "source/input.txt",
                    "sha256": sha256_file(self.project / "source/input.txt"),
                }
            ],
        )
        self.bind_handoff(handoff)
        self.refresh_locked_scope()

        self.assertEqual([], validate_project(self.project, "plan"))

    def test_populated_handoff_rejects_oversized_required_artifact(self):
        artifact_path = self.project / "plan/oversized.bin"
        with artifact_path.open("wb") as stream:
            stream.truncate(MAX_READ_BYTES + 1)
        batches_sha256 = self.write_generation_batches()
        handoff = self.valid_handoff_manifest(
            batches_sha256=batches_sha256,
            required_artifacts=[{"path": "plan/oversized.bin", "sha256": "3" * 64}],
        )
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "plan/oversized.bin"
                and issue.field == "file"
                and "InputResourceLimitError" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_populated_handoff_rejects_linked_required_artifact(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        linked = self.project / "plan/linked.txt"
        make_symlink(self, linked, outside)
        batches_sha256 = self.write_generation_batches()
        handoff = self.valid_handoff_manifest(
            batches_sha256=batches_sha256,
            required_artifacts=[{"path": "plan/linked.txt", "sha256": sha256_file(outside)}],
        )
        self.bind_handoff(handoff)

        issues = validate_project(self.project, "plan")

        self.assertTrue(
            any(
                issue.path == "plan/linked.txt"
                and issue.field == "file"
                and "required artifact" in issue.message
                for issue in issues
            ),
            issues,
        )

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
        self.assertTrue(
            any(
                issue.field == "quality-migration-required" and "schema 1.0" in issue.message
                for issue in issues
            ),
            issues,
        )
        self.assertFalse(
            any(issue.field != "quality-migration-required" for issue in issues), issues
        )

        self.add_panel_files(mode="RGBA")
        issues = validate_project(self.project, "panels")
        self.assertTrue(any("alpha" in issue.message for issue in issues), issues)

        Image.new("RGB", (512, 512), "black").save(self.project / "panels/raw/p01-01.png")
        issues = validate_project(self.project, "panels")
        self.assertTrue(any("hash" in issue.message for issue in issues), issues)
        self.assertTrue(any("aspect" in issue.message for issue in issues), issues)

    def test_schema_two_panel_record_clears_migration_issue(self):
        self.add_panel_files()
        canonical_clean = normalize_panel(
            self.project,
            "p01-01",
            "panels/raw/p01-01.png",
            (736, 1136),
            "exact",
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
            "scripts.validate_project.Image.open",
            side_effect=Image.DecompressionBombError("unsafe dimensions"),
        ):
            issues = validate_project(self.project, "panels")
        self.assertTrue(any("unreadable" in issue.message for issue in issues), issues)

    def test_panel_stage_rejects_raster_over_decode_limit(self):
        self.add_panel_files()
        with patch("scripts.validate_project.MAX_DECODED_PIXELS", 1):
            issues = validate_project(self.project, "panels")
        self.assertTrue(
            any(issue.field == "raw_path" and "unreadable" in issue.message for issue in issues),
            issues,
        )

    def test_non_object_normalization_record_is_a_validation_issue(self):
        self.add_panel_files()
        clean = normalize_panel(
            self.project, "p01-01", "panels/raw/p01-01.png", (736, 1136), "exact"
        )
        record = valid_panel_record_v2()
        record["bindings"].update(
            {
                "raw_sha256": sha256_file(self.project / "panels/raw/p01-01.png"),
                "clean_sha256": sha256_file(clean),
                "normalization_sha256": sha256_file(
                    self.project / "panels/p01-01/normalization.json"
                ),
            }
        )
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)
        (self.project / "panels/p01-01/normalization.json").write_text("[]\n", "utf-8")
        issues = validate_project(self.project, "panels")
        self.assertTrue(
            any("normalization record must be an object" in issue.message for issue in issues),
            issues,
        )

    def test_truncated_raster_payload_is_reported_as_unreadable(self):
        self.add_panel_files()
        raw = self.project / "panels/raw/p01-01.png"
        raw.write_bytes(raw.read_bytes()[:32])
        issues = validate_project(self.project, "panels")
        self.assertTrue(
            any(issue.field == "raw_path" and "unreadable" in issue.message for issue in issues),
            issues,
        )

    def test_invalid_project_id_still_checks_terminal_artifact_paths(self):
        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest["project_id"] = "../escape"
        manifest["artifacts"] = {
            "qa_report": {"path": "wrong.md", "sha256": "a" * 64},
            "pdf_verification": {"path": "wrong.json", "sha256": "a" * 64},
        }
        atomic_write_json(manifest_path, manifest)
        with patch(
            "scripts.validate_project.validate_pdf_verification",
            side_effect=AssertionError("invalid project ID must not drive PDF validation"),
        ):
            issues = validate_project(self.project, "final")
        fields = {issue.field for issue in issues}
        self.assertIn("artifacts.qa_report.path", fields)
        self.assertIn("artifacts.pdf_verification.path", fields)

    def test_escaped_artifact_descriptor_skips_hashing(self):
        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest["artifacts"] = {
            "story_plan": {"path": "../outside.json", "sha256": "a" * 64},
        }
        atomic_write_json(manifest_path, manifest)
        outside = self.root / "outside.json"
        outside.write_text("must not be hashed", "utf-8")

        def guarded_hash(path):
            if Path(path).resolve() == outside.resolve():
                raise AssertionError("validator hashed an escaped artifact")
            return sha256_file(path)

        with patch("scripts.validate_project.sha256_file", side_effect=guarded_hash):
            issues = validate_project(self.project, "final")
        self.assertTrue(
            any(
                issue.field == "artifacts.story_plan.path" and "escapes" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_final_stage_rejects_safety_failure_despite_complete_manifest(self):
        self.add_panel_files()
        record_path = self.project / "qa/panels/p01-01.json"
        record = read_json(record_path)
        record.update(
            {
                "decision": "regenerate",
                "retry_reason": "provider safety refusal",
                "failure_category": "safety_refusal",
            }
        )
        atomic_write_json(record_path, record)

        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest.update({"status": "COMPLETE", "warnings": []})
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(
            any(
                issue.path == "qa/panels/p01-01.json"
                and issue.field == "decision"
                and "unresolved" in issue.message
                for issue in issues
            ),
            issues,
        )
        self.assertTrue(
            any(
                issue.path == "project.json"
                and issue.field == "status"
                and "unresolved panel errors" in issue.message
                for issue in issues
            ),
            issues,
        )

    def test_final_stage_requires_panel_warnings_and_warning_terminal(self):
        self.add_panel_files()
        reason = "minor prop drift is acceptable"
        record_path = self.project / "qa/panels/p01-01.json"
        record = read_json(record_path)
        record["checks"][0].update({"result": "fail", "severity": "warning"})
        record.update(
            {
                "decision": "accept_with_warnings",
                "failure_category": "visual_qa",
                "override_reason": reason,
                "unresolved_warnings": [reason],
            }
        )
        atomic_write_json(record_path, record)

        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest.update({"status": "COMPLETE", "warnings": []})
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(
            any(
                issue.path == "project.json"
                and issue.field == "warnings"
                and reason in issue.message
                for issue in issues
            ),
            issues,
        )
        self.assertTrue(
            any(
                issue.path == "project.json"
                and issue.field == "status"
                and "COMPLETE_WITH_WARNINGS" in issue.message
                for issue in issues
            ),
            issues,
        )

        manifest.update({"status": "COMPLETE_WITH_WARNINGS", "warnings": [reason]})
        atomic_write_json(manifest_path, manifest)
        issues = validate_project(self.project, "final")
        self.assertFalse(
            any(
                issue.path == "project.json" and issue.field in {"status", "warnings"}
                for issue in issues
            ),
            issues,
        )

    def test_final_stage_reports_malformed_manifest_warnings_without_raising(self):
        self.add_panel_files()
        manifest_path = self.project / "project.json"
        manifest = read_json(manifest_path)
        manifest["warnings"] = [{"invalid": "warning"}]
        atomic_write_json(manifest_path, manifest)

        issues = validate_project(self.project, "final")
        self.assertTrue(
            any(
                issue.path == "project.json" and issue.field.startswith("warnings")
                for issue in issues
            ),
            issues,
        )

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

        with patch("scripts.validate_project.sha256_file", side_effect=guarded_hash):
            issues = validate_project(self.project, "panels")
        self.assertTrue(any("escapes the project" in issue.message for issue in issues), issues)

    def test_hash_reads_repeat_shared_resolution_for_original_relative_paths(self):
        from scripts import validate_project as validation_module

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

                with patch(
                    "scripts.validate_project.contained_project_path",
                    side_effect=recording_resolver,
                ):
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
            for line in frontmatter.splitlines()
            if line.startswith("description:")
        ).lower()
        for term in (
            "create",
            "storyboard",
            "render",
            "resume",
            "export",
            "prompt",
            "story",
            ".txt",
            ".md",
        ):
            self.assertIn(term, description)
        for name in (
            "workflow",
            "creative-direction",
            "capability-detection",
            "visual-qa",
            "safety-ip",
            "schemas",
        ):
            self.assertIn(f"references/{name}.md", text)
            self.assertTrue((ROOT / "references" / f"{name}.md").is_file())

    def test_progressive_loading_names_safety_and_json_write_triggers(self):
        for path in (ROOT / "SKILL.md", ROOT / "skills/comic-sol/SKILL.md"):
            with self.subTest(path=path):
                text = path.read_text("utf-8")
                progressive_loading = text.split("### Progressive loading", 1)[1].split(
                    "### No subagents", 1
                )[0]
                for trigger in (
                    "external prompts",
                    "people",
                    "minors",
                    "sensitive content",
                    "named styles",
                    "franchises",
                    "refusals",
                    "every JSON write or revision",
                ):
                    self.assertIn(trigger, progressive_loading)

    def test_fast_mode_rules_present_in_skill(self):
        """Fast Mode contract: black-box engine, no scaffolding, per-panel QA,
        split resolution checks, two-call finalize, and scoped batch precedence."""
        text = self.skill_text()
        self.assertIn("## Fast Mode", text)
        for rule in (
            "Never read engine source",
            "Use init, never hand-write setup scripts",
            "Right-size reasoning per stage",
            "Parallel independent panels",
            "One resolution check per artifact",
            "Two-call finalization",
            "Lock the brief",
        ):
            self.assertIn(rule, text)
        for phrase in (
            "Do not read, grep, or open any file under `scripts/`, `comic_sol_product/`, or",
            "Do not write `setup_batch_*.py`, `build_plans.py`, or equivalent helpers.",
            "Visual QA still inspects every accepted attempt before",
            "Check panel/source at original resolution during panel QA.",
            "Apply the 390px readability check once,",
            "run `finalize` once to letter and compose",
            "then run `finalize` once more.",
            "The batch map takes precedence only over contradictory internal checklist counts;",
            "safety/IP rules, engine validation, visual QA, and final-acceptance gates remain",
            "authoritative.",
        ):
            self.assertIn(phrase, text)

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
            "Pages: 2",
            "Panels: 4–8",
            "Left-to-right",
            "1600 × 2400",
            "32 px gutter",
            "64 px outer margin",
            "Teen",
            "./comic-sol-output/",
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
            "character-identity",
            "anatomy",
            "action",
            "composition",
            "continuity",
            "text-free",
            "technical",
        ):
            self.assertIn(check_id, text)
        for phrase in (
            "2 regenerations per panel",
            "8 extra calls project-wide",
            "one immediate transient repeat",
            "one correction clause",
            "COMPLETE_WITH_WARNINGS",
            "BLOCKED",
            "PDF path",
            "page directory",
            "manifest path",
            "QA report path",
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
                "character-identity",
                "anatomy",
                "action",
                "composition",
                "continuity",
                "text-free",
                "technical",
            ],
            re.findall(r"^\d+\. `([^`]+)`:", visual, re.MULTILINE),
        )
        for phrase in (
            "exact storyboard-authored sfx",
            "dynamic motion/action typography",
            "no generated dialogue, captions, or speech bubbles",
            "prohibit\n   generated sfx when no `generated-visual` item is authored",
        ):
            self.assertIn(phrase, creative)
        # The panel-prompt instruction must agree with the render-mode split. Asking
        # the image model for an effect Comic Sol also letters bakes a duplicate
        # into the artwork, so both documents state the same rule.
        self.assertIn("every exact authored\n`generated-visual` sfx once", workflow)
        self.assertIn("never ask it for a\n`deterministic-lettering` effect", workflow)
        for phrase in (
            "exact storyboard-authored `generated-visual` sfx is allowed and required",
            "missing, misspelled, duplicated, or unauthorized sfx",
            "dialogue",
            "caption",
            "speech bubbles",
            "logos",
            "signatures",
            "watermarks",
            # The repair path is part of the reviewer contract: a faulty generated
            # effect has a cheaper remedy than re-rolling the panel, and whether it
            # still needs a regeneration has to be stated rather than assumed.
            "sfx_repair.py",
            "re-review the panel to `regenerate`",
        ):
            self.assertIn(phrase, visual)
        self.assertIn(
            "pillow neither draws generated sfx nor allocates a placement rectangle "
            "or overlap reservation",
            schemas,
        )
        self.assertNotIn("no dialogue, captions, sfx", schemas)
        for phrase in (
            "text_count",
            "rendered_text_count",
            "sfx_count",
            "lettered_sfx_count",
        ):
            self.assertIn(phrase, schemas)
            self.assertIn(phrase, workflow)
        # Render mode and origin are the two claims that make SFX verifiable, so
        # both are documented wherever an author or a reviewer would look.
        for phrase in ("generated-visual", "deterministic-lettering"):
            self.assertIn(phrase, schemas)
            self.assertIn(phrase, workflow)
            self.assertIn(phrase, creative)
        for phrase in (
            "image-model",
            "comic-sol-lettering",
            "sfx-audit.json",
            "sfx-glyph-risk",
            "sfx-duplicate-content",
            "sfx-legibility-budget",
            "sfx-unprohibited-generation",
        ):
            self.assertIn(phrase, schemas)
        self.assertIn("exact `generated-visual` storyboard sfx", skill)
        self.assertIn("`deterministic-lettering` sfx", skill)
        self.assertIn("sfx_repair.py", skill)
        self.assertIn("image model", skill)

    def test_all_deterministic_cli_commands_are_routed(self):
        text = self.skill_text()
        commands = (
            "comic_sol.py init",
            "comic_sol.py transition",
            "comic_sol.py status",
            "comic_sol.py doctor",
            "comic_sol.py resume-plan",
            "comic_sol.py invalidate",
            "comic_sol.py record-stage",
            "comic_sol.py record-attempt",
            "comic_sol.py promote-attempt",
            "comic_sol.py override-panel",
            "validate_project.py",
            "letter_panels.py",
            "sfx_repair.py",
            "compose_pages.py",
            "export_pdf.py",
            "render_report.py",
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

    def test_readme_is_creator_runnable_without_a_build_service(self):
        readme = self.readme()
        for required in (
            "comic-sol skill-install",
            "comic-sol doctor",
            "One natural-language",
            "requires no build service",
        ):
            self.assertIn(required, readme)
        self.assertNotRegex(readme.lower(), r"npm run|start the server|docker compose")

    def test_runtime_instructions_use_active_python_not_a_fixed_minor_version(self):
        documents = (
            ROOT / "SKILL.md",
            ROOT / "skills/comic-sol/SKILL.md",
            ROOT / "README.md",
            ROOT / "CONTRIBUTING.md",
            ROOT / "SUPPORT.md",
            ROOT / "references/workflow.md",
            ROOT / "skills/comic-sol/references/workflow.md",
            ROOT / "references/image-provider-setup.md",
        )
        fixed_minor = re.compile(r"(?i)\b(?:python3?\.\d+|py\s+-3\.\d+)\b")
        for launcher in ("python3.11", "python3.12", "py -3.11"):
            self.assertIsNotNone(fixed_minor.search(launcher), launcher)
        for path in documents:
            text = path.read_text("utf-8")
            self.assertFalse(fixed_minor.search(text), path)

        for path in (ROOT / "SKILL.md", ROOT / "skills/comic-sol/SKILL.md"):
            self.assertIn("`PYTHON`", path.read_text("utf-8"), path)
        self.assertIn("$PYTHON", (ROOT / "README.md").read_text("utf-8"))
        self.assertIn("Python 3.11+", (ROOT / "README.md").read_text("utf-8"))
        self.assertIn("Python 3.11+", (ROOT / "SKILL.md").read_text("utf-8"))

    def test_public_surface_uses_the_canonical_independent_repository(self):
        readme = self.readme()
        workflow = (ROOT / ".github/workflows/tests.yml").read_text("utf-8")
        self.assertIn("https://github.com/wenn-id/comicsol", readme)
        self.assertIn("canonical, independent home", readme)
        self.assertNotIn("https://github.com/wenn-id/comic-sol.git", readme)
        self.assertNotIn("comic-sol-lab", readme)
        self.assertIn("branches: [ main ]", workflow)
        self.assertNotIn("ai/post-event-development", workflow)
        for name in ("CONTRIBUTING.md", "SECURITY.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_readme_badges_report_live_project_contracts(self):
        readme = self.readme()
        preamble, separator, _ = readme.partition("**Plan anywhere")
        self.assertTrue(separator, "README product introduction is missing")
        badges = set(re.findall(r"\[!\[([^]]+)\]\(([^)]+)\)\]\(([^)]+)\)", preamble))
        expected = {
            (
                "Tests",
                "https://github.com/wenn-id/comicsol/actions/workflows/tests.yml/badge.svg?branch=main",
                "https://github.com/wenn-id/comicsol/actions/workflows/tests.yml",
            ),
            (
                "Release",
                "https://img.shields.io/github/v/release/wenn-id/comicsol?include_prereleases&label=release",
                "https://github.com/wenn-id/comicsol/releases",
            ),
            ("License", "https://img.shields.io/github/license/wenn-id/comicsol", "LICENSE"),
            (
                "Python",
                "https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white",
                "https://www.python.org/",
            ),
            (
                "MCP tools",
                "https://img.shields.io/badge/MCP_tools-17-brightgreen",
                "docs/surfaces.md",
            ),
            (
                "Platforms",
                "https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-blue",
                "docs/install.md",
            ),
        }
        self.assertEqual(expected, badges)

    def test_install_docs_recommend_superpowers_without_making_it_a_dependency(self):
        readme = self.readme()
        install = (ROOT / "docs/install.md").read_text("utf-8")
        for document in (readme, install):
            self.assertIn("https://github.com/obra/superpowers", document)
            self.assertIn("Superpowers", document)
            self.assertIn("optional", document)
            self.assertIn("separately", document)
        self.assertIn("not required for Comic Sol to run", " ".join(readme.split()))
        self.assertIn("not bundled with or required by Comic Sol", " ".join(install.split()))

        dependency_and_packaging_inputs = (
            "pyproject.toml",
            "setup.py",
            "packaging/comic-sol.spec",
            "scripts/build_portable.py",
            "Dockerfile",
            ".github/workflows/release.yml",
        )
        for relative_path in dependency_and_packaging_inputs:
            content = (ROOT / relative_path).read_text("utf-8").lower()
            self.assertNotIn("superpowers", content, relative_path)
            self.assertNotIn("github.com/obra/superpowers", content, relative_path)

    def test_package_files_install_and_artifact_contract_are_documented(self):
        for name in ("LICENSE", "README.md", "SKILL.md"):
            self.assertTrue((ROOT / name).is_file(), name)
        readme = self.readme()
        for phrase in (
            "Python 3.11",
            "project.json",
            "page PNGs",
            "PDF",
            "qa/report.md",
            "Linux",
            "Windows",
            "macOS",
            "docs/install-manual.md",
            "docs/typography.md",
            "CONTRIBUTING.md",
            "requires no build service",
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

    def test_readme_links_the_hybrid_lettering_contract(self):
        readme = self.readme()
        typography = (ROOT / "docs/typography.md").read_text("utf-8")
        self.assertIn("docs/typography.md", readme)
        for phrase in (
            "Comic Neue Regular",
            "Comic Neue Bold",
            "Polytonic Greek",
            "Cyrillic",
            ".notdef",
            "`**emphasis**`",
            "CJK",
        ):
            self.assertIn(phrase, typography)

    def test_readme_and_ci_are_portable_and_describe_optional_mcp(self):
        readme = self.readme()
        workflow = (ROOT / ".github/workflows/tests.yml").read_text("utf-8")
        recovery = (ROOT / "references/capability-detection.md").read_text("utf-8")
        self.assertNotIn("/home/acer", readme)
        self.assertNotIn("/mnt/c/Users/acer", readme)
        self.assertIn("docs/surfaces.md#mcp-server", readme)
        self.assertIn("CONTRIBUTING.md", readme)
        self.assertIn("resume", recovery)
        self.assertIn("17 `comic_*` tools", readme)
        for platform in ("ubuntu-latest", "macos-26-intel", "windows-latest"):
            self.assertIn(platform, workflow)
        self.assertNotIn("/tmp", workflow)
