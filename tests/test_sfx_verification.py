"""SFX render-mode policy, provenance, verification flags, and repair path."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]

from scripts import page_quality  # noqa: E402

from scripts.comic_sol import (  # noqa: E402
    _manifest_artifact_problem,
    _resume_stage_material,
    read_json,
    sha256_file,
    stage_cache_key,
)
from scripts.letter_panels import (  # noqa: E402
    _letter_project_with_summaries,
    letter_panel,
    letter_project,
)
from scripts.schema import read_project_manifest  # noqa: E402
from scripts.sfx_repair import replace_generated_sfx  # noqa: E402
from scripts.sfx_verification import (  # noqa: E402
    DETERMINISTIC_LETTERING,
    DETERMINISTIC_SFX_NEGATIVE,
    GENERATED_VISUAL,
    ORIGIN_IMAGE_MODEL,
    ORIGIN_LETTERING,
    SFX_CONTENT_LENGTH_LIMIT,
    SFX_TOKEN_LENGTH_LIMIT,
    VERIFICATION_DETERMINISTIC,
    VERIFICATION_REVIEWER,
    evaluate_sfx_flags,
    is_deterministic_sfx,
    is_generated_sfx,
    negatives_prohibit_generated_sfx,
    render_mode_problem,
    sfx_provenance,
    sfx_render_mode,
)
from scripts.typography import LETTERING_GEOMETRY_SCHEMA_VERSION  # noqa: E402
from scripts.validate_project import (  # noqa: E402
    sfx_advisories,
    validate_lettering_provenance,
    validate_storyboard,
)

FIXTURES = ROOT / "tests/fixtures"


def sfx(content="KRAK!", priority=1, anchor="middle-right", render_mode=None):
    """Build one storyboard SFX item, generated unless a mode is declared."""
    item = {
        "id": f"sfx-{priority}",
        "kind": "sfx",
        "speaker": None,
        "content": content,
        "anchor": anchor,
        "priority": priority,
    }
    if render_mode is not None:
        item["render_mode"] = render_mode
    return item


def panel(*text_items, negative=("text", "watermark")):
    """Build the smallest storyboard panel the policy functions read."""
    return {"id": "p01-01", "negative": list(negative), "text": list(text_items)}


def flag_ids(panel_value):
    return [flag["id"] for flag in evaluate_sfx_flags(panel_value)]


class SfxRenderModePolicyTests(unittest.TestCase):
    def test_absent_render_mode_keeps_the_historical_generated_meaning(self):
        item = sfx()
        self.assertNotIn("render_mode", item)
        self.assertEqual(GENERATED_VISUAL, sfx_render_mode(item))
        self.assertTrue(is_generated_sfx(item))
        self.assertFalse(is_deterministic_sfx(item))
        self.assertIsNone(render_mode_problem(item))

    def test_declared_modes_are_recognized_and_mutually_exclusive(self):
        generated = sfx(render_mode=GENERATED_VISUAL)
        lettered = sfx(render_mode=DETERMINISTIC_LETTERING)

        self.assertTrue(is_generated_sfx(generated))
        self.assertFalse(is_deterministic_sfx(generated))
        self.assertTrue(is_deterministic_sfx(lettered))
        self.assertFalse(is_generated_sfx(lettered))
        self.assertIsNone(render_mode_problem(generated))
        self.assertIsNone(render_mode_problem(lettered))

    def test_render_mode_is_refused_on_unknown_values_and_on_other_kinds(self):
        self.assertIn(
            "must be one of",
            render_mode_problem(sfx(render_mode="hand-drawn")) or "",
        )
        self.assertIn(
            "must be one of", render_mode_problem(sfx(render_mode=True)) or ""
        )
        caption = {
            "id": "caption-1",
            "kind": "caption",
            "speaker": None,
            "content": "Below the city.",
            "anchor": "bottom-right",
            "priority": 1,
            "render_mode": DETERMINISTIC_LETTERING,
        }
        self.assertIn("must be omitted", render_mode_problem(caption) or "")

    def test_dialogue_and_captions_are_never_sfx_regardless_of_mode(self):
        dialogue = {"id": "d", "kind": "dialogue", "render_mode": GENERATED_VISUAL}
        self.assertFalse(is_generated_sfx(dialogue))
        self.assertFalse(is_deterministic_sfx(dialogue))


class SfxProvenanceTests(unittest.TestCase):
    def test_provenance_names_the_producer_of_every_effect(self):
        record = sfx_provenance(panel(
            sfx("KRAK!", priority=1),
            sfx("THUD", priority=2, render_mode=DETERMINISTIC_LETTERING),
        ))

        self.assertEqual(["sfx-1", "sfx-2"], [item["id"] for item in record["items"]])
        generated, lettered = record["items"]
        self.assertEqual(ORIGIN_IMAGE_MODEL, generated["origin"])
        self.assertEqual(VERIFICATION_REVIEWER, generated["verification"])
        self.assertIsNone(generated["box"])
        self.assertEqual(ORIGIN_LETTERING, lettered["origin"])
        self.assertEqual(VERIFICATION_DETERMINISTIC, lettered["verification"])

    def test_lettered_effects_bind_the_rectangle_they_occupy(self):
        box = {"x": 10, "y": 20, "width": 30, "height": 40}
        placements = [{"id": "sfx-1", "kind": "sfx", "box": box}]

        lettered = sfx_provenance(
            panel(sfx(render_mode=DETERMINISTIC_LETTERING)), placements
        )
        generated = sfx_provenance(panel(sfx()), placements)

        self.assertEqual(box, lettered["items"][0]["box"])
        # A generated effect has no rectangle even when a stale placement claims
        # one, because nothing this engine drew produced it.
        self.assertIsNone(generated["items"][0]["box"])

    def test_provenance_records_authored_content_and_orders_by_placement(self):
        record = sfx_provenance(panel(
            sfx("SECOND", priority=9),
            sfx("FIRST", priority=2),
        ))
        self.assertEqual(["FIRST", "SECOND"], [item["content"] for item in record["items"]])

    def test_panel_without_sfx_records_an_empty_block(self):
        record = sfx_provenance(panel())
        self.assertEqual([], record["items"])
        self.assertEqual([], record["flags"])


class SfxFlagTests(unittest.TestCase):
    def test_ordinary_generated_effect_raises_no_flag(self):
        self.assertEqual([], flag_ids(panel(sfx("KRAK!"))))

    def test_non_latin_generated_effect_is_flagged_as_glyph_risk(self):
        risky = panel(sfx("ドォン"))
        self.assertEqual(["sfx-glyph-risk"], flag_ids(risky))

        flag = evaluate_sfx_flags(risky)[0]
        self.assertEqual(["sfx-1"], flag["item_ids"])
        self.assertIn("U+30C9", flag["evidence"])
        self.assertIn(DETERMINISTIC_LETTERING, flag["remediation"])
        self.assertEqual("warning", flag["result"])
        self.assertEqual("sfx-policy-v1", flag["method"])

    def test_glyph_risk_does_not_fire_once_the_effect_is_lettered_here(self):
        # Deterministic lettering verifies coverage against the pinned font
        # policy, so the reason the flag exists no longer applies.
        self.assertNotIn(
            "sfx-glyph-risk",
            flag_ids(panel(
                sfx("ドォン", render_mode=DETERMINISTIC_LETTERING),
                negative=(DETERMINISTIC_SFX_NEGATIVE,),
            )),
        )

    def test_duplicate_generated_content_is_flagged_case_insensitively(self):
        duplicated = panel(sfx("KRAK!", priority=1), sfx("krak!", priority=2))
        self.assertEqual(["sfx-duplicate-content"], flag_ids(duplicated))
        self.assertEqual(
            ["sfx-1", "sfx-2"], evaluate_sfx_flags(duplicated)[0]["item_ids"]
        )

    def test_distinct_generated_content_is_not_flagged_as_duplicate(self):
        self.assertEqual(
            [],
            flag_ids(panel(sfx("KRAK!", priority=1), sfx("THUD", priority=2))),
        )

    def test_long_generated_effects_exceed_the_legibility_budget(self):
        token = "K" + "R" * SFX_TOKEN_LENGTH_LIMIT
        self.assertEqual(["sfx-legibility-budget"], flag_ids(panel(sfx(token))))

        # Three short words are inside the schema's word limit but still too much
        # ink for a model to letter faithfully inside artwork.
        long_total = "KRAKKAAA BLAMMOOO THUDDERR"
        self.assertGreater(len(long_total), SFX_CONTENT_LENGTH_LIMIT)
        self.assertEqual(["sfx-legibility-budget"], flag_ids(panel(sfx(long_total))))

    def test_lettered_panel_without_a_generated_sfx_negative_is_flagged(self):
        unprohibited = panel(
            sfx(render_mode=DETERMINISTIC_LETTERING), negative=("text", "watermark")
        )
        self.assertEqual(["sfx-unprohibited-generation"], flag_ids(unprohibited))
        self.assertIn(
            DETERMINISTIC_SFX_NEGATIVE, evaluate_sfx_flags(unprohibited)[0]["remediation"]
        )

        prohibited = panel(
            sfx(render_mode=DETERMINISTIC_LETTERING),
            negative=("text", "no generated SFX of any kind"),
        )
        self.assertEqual([], flag_ids(prohibited))

    def test_the_boilerplate_sfx_negative_does_not_satisfy_the_prohibition(self):
        """The negative every panel carries permits the effect now being lettered.

        `unauthorized sfx` and `sfx other than the exact authored text` forbid
        everything *except* the authored effect. A lettered effect is the authored
        one, so those entries license exactly the baked copy that would end up
        underneath it — and accepting them would make this flag dead on every
        project the skill produces.
        """
        for boilerplate in (
            "unauthorized sfx",
            "sfx other than the exact authored text",
            "unauthorized text/SFX",
        ):
            with self.subTest(negative=boilerplate):
                self.assertFalse(
                    negatives_prohibit_generated_sfx(
                        panel(negative=("text", boilerplate))
                    )
                )
                self.assertEqual(
                    ["sfx-unprohibited-generation"],
                    flag_ids(panel(
                        sfx(render_mode=DETERMINISTIC_LETTERING),
                        negative=("text", boilerplate),
                    )),
                )
        self.assertTrue(
            negatives_prohibit_generated_sfx(
                panel(negative=(DETERMINISTIC_SFX_NEGATIVE,))
            )
        )

    def test_flags_are_ordered_by_id_and_not_by_authoring_order(self):
        crowded = panel(
            sfx("ドォン", priority=1),
            sfx("KRAK!", priority=2),
            sfx("krak!", priority=3),
        )
        self.assertEqual(
            ["sfx-duplicate-content", "sfx-glyph-risk"], flag_ids(crowded)
        )


class SfxStoryboardValidationTests(unittest.TestCase):
    def setUp(self):
        self.story = read_json(FIXTURES / "valid-one-page/plan/story-plan.json")
        self.characters = read_json(
            FIXTURES / "valid-one-page/plan/character-bible.json"
        )
        self.storyboard = read_json(
            FIXTURES / "valid-one-page/plan/storyboard.json"
        )

    def _issues(self, item, panel_index=0):
        storyboard = json.loads(json.dumps(self.storyboard))
        target = storyboard["pages"][0]["panels"][panel_index]
        target["text"].append(item)
        return [
            issue.message
            for issue in validate_storyboard(storyboard, self.story, self.characters)
            if "render_mode" in issue.field
        ]

    def test_fixture_storyboard_is_valid_before_any_sfx_is_added(self):
        self.assertEqual(
            [], validate_storyboard(self.storyboard, self.story, self.characters)
        )

    def test_both_declared_render_modes_validate_on_sfx(self):
        for mode in (GENERATED_VISUAL, DETERMINISTIC_LETTERING):
            with self.subTest(mode=mode):
                item = sfx("KRAK!", priority=2, render_mode=mode)
                item["id"] = "p01-01-t02"
                self.assertEqual([], self._issues(item))

    def test_unknown_render_mode_is_reported(self):
        item = sfx("KRAK!", priority=2, render_mode="hand-drawn")
        item["id"] = "p01-01-t02"
        self.assertTrue(any("must be one of" in detail for detail in self._issues(item)))

    def test_render_mode_on_a_caption_is_reported(self):
        item = {
            "id": "p01-01-t02",
            "kind": "caption",
            "speaker": None,
            "content": "Below the city.",
            "anchor": "bottom-right",
            "priority": 2,
            "render_mode": DETERMINISTIC_LETTERING,
        }
        self.assertTrue(
            any("must be omitted" in detail for detail in self._issues(item))
        )


class SfxLetteringTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.panel = self.root / "p01-01.png"
        Image.new("RGB", (800, 1000), (28, 32, 40)).save(self.panel)
        self.characters = [{"id": "mira", "name": "Mira"}]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_deterministic_sfx_is_drawn_placed_and_counted(self):
        before = Image.open(self.panel).convert("RGB")

        result = letter_panel(
            str(self.panel),
            800,
            1000,
            [sfx(render_mode=DETERMINISTIC_LETTERING)],
            self.characters,
        )

        with Image.open(self.panel) as actual:
            self.assertIsNotNone(
                ImageChops.difference(before, actual.convert("RGB")).getbbox(),
                "deterministic SFX must change the raster",
            )
        self.assertEqual(1, result["sfx_count"])
        self.assertEqual(1, result["lettered_sfx_count"])
        self.assertEqual(1, result["rendered_text_count"])
        placement = result["placements"][0]
        self.assertEqual("sfx", placement["kind"])
        self.assertIsNone(placement["tail"])
        self.assertIsNone(placement["attribution"])
        self.assertEqual(
            ["ComicNeue-Bold.ttf"],
            [run["font_id"] for run in placement["font_runs"]],
        )

    def test_generated_sfx_leaves_every_pixel_of_the_artwork_alone(self):
        before = Image.open(self.panel).convert("RGB")

        result = letter_panel(
            str(self.panel),
            800,
            1000,
            [sfx(render_mode=GENERATED_VISUAL)],
            self.characters,
        )

        with Image.open(self.panel) as actual:
            self.assertIsNone(
                ImageChops.difference(before, actual.convert("RGB")).getbbox(),
                "generated SFX must not be overdrawn by the engine",
            )
        self.assertEqual(0, result["lettered_sfx_count"])
        self.assertEqual(1, result["sfx_count"])
        self.assertEqual([], result["placements"])

    def test_lettered_sfx_reserves_space_against_dialogue(self):
        dialogue = {
            "id": "dialogue-2",
            "kind": "dialogue",
            "speaker": "mira",
            "content": "Same anchor.",
            "anchor": "middle-right",
            "voice_source": "human",
            "speaker_anchor": [0.2, 0.2],
            "priority": 2,
        }

        result = letter_panel(
            str(self.panel),
            800,
            1000,
            [
                sfx(priority=1, anchor="middle-right",
                    render_mode=DETERMINISTIC_LETTERING),
                dialogue,
            ],
            self.characters,
        )

        boxes = {item["id"]: item["box"] for item in result["placements"]}
        self.assertEqual(2, len(boxes))
        first, second = boxes["sfx-1"], boxes["dialogue-2"]
        overlaps = (
            first["x"] < second["x"] + second["width"]
            and second["x"] < first["x"] + first["width"]
            and first["y"] < second["y"] + second["height"]
            and second["y"] < first["y"] + first["height"]
        )
        self.assertFalse(overlaps, (first, second))

    def test_unknown_render_mode_fails_lettering_without_touching_the_raster(self):
        before = self.panel.read_bytes()
        with self.assertRaisesRegex(ValueError, "render_mode"):
            letter_panel(
                str(self.panel),
                800,
                1000,
                [sfx(render_mode="hand-drawn")],
                self.characters,
            )
        self.assertEqual(before, self.panel.read_bytes())

    def test_lettered_sfx_that_cannot_fit_is_refused(self):
        tiny = self.root / "tiny.png"
        Image.new("RGB", (64, 64), (10, 10, 10)).save(tiny)
        with self.assertRaisesRegex(ValueError, "does not fit inside the panel"):
            letter_panel(
                str(tiny),
                64,
                64,
                [sfx("KRAKKA BOOM", render_mode=DETERMINISTIC_LETTERING)],
                self.characters,
            )


class SfxAdvisoryTests(unittest.TestCase):
    """Flags reach a surface, and reach it while they are still actionable."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "valid-one-page"
        shutil.copytree(FIXTURES / "valid-one-page", self.project)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _append(self, item):
        storyboard = read_json(self.project / "plan/storyboard.json")
        storyboard["pages"][0]["panels"][0]["text"].append(item)
        (self.project / "plan/storyboard.json").write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

    def test_clean_storyboard_raises_no_advisory(self):
        self.assertEqual([], sfx_advisories(self.project))

    def test_risky_generated_effect_is_reported_before_generation_runs(self):
        item = sfx("ドォン", priority=2)
        item["id"] = "p01-01-t02"
        self._append(item)

        lines = sfx_advisories(self.project)

        self.assertEqual(1, len(lines), lines)
        self.assertIn("SFX-WARNING p01-01:sfx-glyph-risk", lines[0])
        self.assertIn("Remediation:", lines[0])
        # The panel raster and its QA record are untouched, so this warning is
        # available while the effect can still be re-authored rather than repaired.
        self.assertTrue((self.project / "panels/p01-01/clean.png").is_file())

    def test_unreadable_storyboard_yields_no_advisory_instead_of_raising(self):
        (self.project / "plan/storyboard.json").write_text("{ not json", "utf-8")
        self.assertEqual([], sfx_advisories(self.project))

    def test_glyph_risk_warns_before_a_project_that_cannot_letter_at_all(self):
        """Kana SFX also fails typography preflight, so the early warning matters."""
        item = sfx("ドォン", priority=2)
        item["id"] = "p01-01-t02"
        self._append(item)

        self.assertEqual(1, len(sfx_advisories(self.project)))
        with self.assertRaisesRegex(Exception, "missing-glyph"):
            letter_project(self.project)

    def test_lettering_summary_reports_the_flags_it_recorded(self):
        long_effect = sfx("KRAKKAKRAKKAK", priority=2)
        long_effect["id"] = "p01-01-t02"
        self._append(long_effect)

        summaries = _letter_project_with_summaries(self.project)[1]

        flagged = [
            flag["id"]
            for summary in summaries
            for flag in summary.get("sfx_flags", [])
        ]
        self.assertEqual(["sfx-legibility-budget"], flagged)


class SfxPageQualityTests(unittest.TestCase):
    """The balloon placement rules measure balloons, not sound effects.

    The end-to-end verdict is covered by
    `test_page_quality.BalloonPlacementQualityTests
    .test_lettered_sfx_is_not_judged_by_the_balloon_placement_rules`; these pin the
    two region builders that would otherwise fail a correct page.
    """

    def test_a_box_that_obstructs_an_anchor_fails_only_as_a_balloon(self):
        anchors = [("p01-01-t01", 400, 500)]
        box = {"x": 380, "y": 480, "width": 120, "height": 60}

        balloon_regions = page_quality._subject_obstruction_regions(
            "p01-01", anchors, [("dialogue-1", box, "dialogue")], 800, 1000
        )
        # Excluding SFX at the call site is what keeps the identical geometry from
        # being reported: a sound effect is placed over the action on purpose.
        sfx_regions = page_quality._subject_obstruction_regions(
            "p01-01", anchors, [], 800, 1000
        )

        self.assertEqual(1, len(balloon_regions))
        self.assertEqual([], sfx_regions)

    def test_sfx_area_would_inflate_reported_balloon_crowding(self):
        effect = {"x": 40, "y": 40, "width": 720, "height": 340}
        balloon = {"x": 40, "y": 400, "width": 200, "height": 100}

        with_sfx = page_quality._crowding_regions(
            "p01-01",
            [("dialogue-1", balloon, "dialogue"), ("sfx-1", effect, "sfx")],
            800,
            1000,
        )
        balloons_only = page_quality._crowding_regions(
            "p01-01", [("dialogue-1", balloon, "dialogue")], 800, 1000
        )

        # A 120pt effect is the largest rectangle on a panel, so counting it would
        # raise a crowding warning whose evidence tells the reviewer to shorten
        # dialogue that is not the cause. One balloon alone is comfortable.
        self.assertEqual(1, len(with_sfx))
        self.assertGreater(
            with_sfx[0]["coverage_ratio"], page_quality.BALLOON_COVERAGE_WARNING_RATIO
        )
        self.assertEqual([], balloons_only)


class SfxRepairTests(unittest.TestCase):
    """The supported path that replaces one generated effect with lettering."""

    CACHED_STAGES = ("planning", "storyboard", "generation", "lettering")

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "valid-one-page"
        shutil.copytree(FIXTURES / "valid-one-page", self.project)
        self._author_generated_sfx()
        letter_project(self.project)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_json(self, relative, value):
        (self.project / relative).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

    def _storyboard(self):
        return read_json(self.project / "plan/storyboard.json")

    def _panel(self, storyboard=None, panel_id="p01-01"):
        storyboard = storyboard or self._storyboard()
        return next(
            candidate
            for page in storyboard["pages"]
            for candidate in page["panels"]
            if candidate["id"] == panel_id
        )

    def _author_generated_sfx(self):
        """Give the fixture one generated effect and re-bind the manifest to it."""
        storyboard = self._storyboard()
        self._panel(storyboard)["text"].append({
            "anchor": "middle-right",
            "content": "KRAK!",
            "id": "p01-01-t02",
            "kind": "sfx",
            "priority": 2,
            "speaker": None,
        })
        self._write_json("plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"]["sha256"] = sha256_file(
            self.project / "plan/storyboard.json"
        )
        self._write_json("project.json", manifest)

    def _stage_keys(self):
        manifest = read_project_manifest(self.project / "project.json")
        versions = manifest["stage_versions"]
        keys = {}
        for stage in self.CACHED_STAGES:
            canonical_inputs, files = _resume_stage_material(
                self.project, stage, manifest
            )
            keys[stage] = stage_cache_key(
                stage, canonical_inputs, files, versions[stage]
            )
        return keys

    def _replace(self, text_id="p01-01-t02", reason="Generated effect read as KRRAK."):
        return replace_generated_sfx(
            self.project, "p01-01", text_id, reason=reason
        )

    def test_generated_effect_is_recorded_as_the_image_model_before_repair(self):
        geometry = read_json(self.project / "panels/p01-01/lettering.json")

        self.assertEqual(
            LETTERING_GEOMETRY_SCHEMA_VERSION, geometry["schema_version"]
        )
        entry = geometry["sfx"]["items"][0]
        self.assertEqual("p01-01-t02", entry["id"])
        self.assertEqual(ORIGIN_IMAGE_MODEL, entry["origin"])
        self.assertEqual(GENERATED_VISUAL, entry["render_mode"])
        self.assertEqual(VERIFICATION_REVIEWER, entry["verification"])
        self.assertIsNone(entry["box"])
        self.assertNotIn(
            "p01-01-t02",
            [item["id"] for item in geometry["items"]],
            "a generated effect must not claim a drawn placement",
        )

    def test_replacement_routes_the_effect_to_lettering_and_prohibits_generation(self):
        result = self._replace()

        item = next(
            text for text in self._panel()["text"] if text["id"] == "p01-01-t02"
        )
        self.assertEqual(DETERMINISTIC_LETTERING, item["render_mode"])
        self.assertTrue(result["negative_added"])
        self.assertIn(DETERMINISTIC_SFX_NEGATIVE, self._panel()["negative"])
        # Every other authored field survives the edit untouched.
        self.assertEqual("KRAK!", item["content"])
        self.assertEqual("middle-right", item["anchor"])

    def test_replacement_preserves_the_rejected_rasters_and_binds_them_by_hash(self):
        before = {
            kind: (self.project / relative).read_bytes()
            for kind, relative in (
                ("raw", "panels/raw/p01-01.png"),
                ("clean", "panels/p01-01/clean.png"),
                ("lettered", "panels/p01-01/lettered.png"),
            )
        }

        result = self._replace()

        archived = {entry["kind"]: entry for entry in result["archived"]}
        # The raw attempt is copied, not merely referenced: the regeneration this
        # repair asks for overwrites panels/raw/{id}.png, so a recorded hash with
        # no archived bytes would name a raster no path resolves to.
        self.assertEqual({"raw", "clean", "lettered"}, set(archived))
        for kind, payload in before.items():
            entry = archived[kind]
            self.assertEqual(
                payload, (self.project / entry["path"]).read_bytes(), kind
            )
            self.assertEqual(
                entry["sha256"], sha256_file(self.project / entry["path"]), kind
            )
        # The rejected artwork itself is untouched: repair preserves evidence
        # rather than deleting the artifact it was diagnosing.
        self.assertEqual(
            before["clean"], (self.project / "panels/p01-01/clean.png").read_bytes()
        )
        self.assertEqual(
            before["raw"], (self.project / "panels/raw/p01-01.png").read_bytes()
        )

    def test_replacement_states_the_conditional_next_action(self):
        """Promotion refuses an accepted panel, so the prerequisite must be stated."""
        result = self._replace()

        self.assertIn("Re-letter", result["next_action"])
        self.assertIn("qa/panels/p01-01.json", result["next_action"])
        self.assertIn("regenerate", result["next_action"])

    def test_replacement_refuses_a_manifest_that_does_not_bind_the_storyboard(self):
        manifest = read_json(self.project / "project.json")
        del manifest["artifacts"]["storyboard"]
        self._write_json("project.json", manifest)
        storyboard_before = (self.project / "plan/storyboard.json").read_bytes()

        with self.assertRaisesRegex(ValueError, "no storyboard artifact descriptor"):
            self._replace()

        # Refusing beats succeeding with a manifest hash that disagrees with the
        # plan, which every later validation would report as a broken project.
        self.assertEqual(
            storyboard_before, (self.project / "plan/storyboard.json").read_bytes()
        )
        self.assertFalse((self.project / "panels/p01-01/sfx-audit").exists())

    def test_audit_record_states_the_transition_and_the_reason(self):
        self._replace(reason="Generated effect read as KRRAK.")

        record = read_json(self.project / "panels/p01-01/sfx-audit.json")
        self.assertEqual("sfx-audit", record["kind"])
        self.assertEqual("p01-01", record["panel_id"])
        entry, = record["replacements"]
        self.assertEqual("p01-01-t02", entry["text_id"])
        self.assertEqual("KRAK!", entry["content"])
        self.assertEqual(GENERATED_VISUAL, entry["from_render_mode"])
        self.assertEqual(DETERMINISTIC_LETTERING, entry["to_render_mode"])
        self.assertEqual("Generated effect read as KRRAK.", entry["reason"])
        self.assertEqual(1, entry["sequence"])
        self.assertEqual(DETERMINISTIC_SFX_NEGATIVE, entry["negative_added"])
        self.assertEqual(
            ["raw", "clean", "lettered"],
            [archive["kind"] for archive in entry["archived"]],
        )

    def test_replacement_appends_an_auditable_event(self):
        self._replace()

        events = [
            json.loads(line)
            for line in (self.project / "logs/events.jsonl")
            .read_text("utf-8")
            .splitlines()
            if line.strip()
        ]
        recorded = [
            event for event in events if event["event"] == "sfx.replacement-recorded"
        ]
        self.assertEqual(1, len(recorded))
        self.assertEqual(
            {
                "attempt": 1,
                "from": GENERATED_VISUAL,
                "panel_id": "p01-01",
                "text_id": "p01-01-t02",
                "to": DETERMINISTIC_LETTERING,
            },
            recorded[0]["details"],
        )

    def test_replacement_leaves_unrelated_stages_cached(self):
        before = self._stage_keys()

        self._replace()

        after = self._stage_keys()
        self.assertEqual(before["planning"], after["planning"])
        self.assertEqual(before["storyboard"], after["storyboard"])
        self.assertNotEqual(before["generation"], after["generation"])
        self.assertNotEqual(before["lettering"], after["lettering"])
        # The storyboard artifact is re-bound, so the storyboard stage is not
        # faulted for an artifact hash mismatch it did not cause.
        manifest = read_project_manifest(self.project / "project.json")
        self.assertNotIn(
            "storyboard", _manifest_artifact_problem(self.project, manifest)
        )

    def test_relettering_after_replacement_draws_and_attributes_the_effect(self):
        clean_before = (self.project / "panels/p01-01/clean.png").read_bytes()

        self._replace()
        letter_project(self.project)

        geometry = read_json(self.project / "panels/p01-01/lettering.json")
        entry, = geometry["sfx"]["items"]
        self.assertEqual(ORIGIN_LETTERING, entry["origin"])
        self.assertEqual(DETERMINISTIC_LETTERING, entry["render_mode"])
        self.assertIsNotNone(entry["box"])
        self.assertIn("p01-01-t02", [item["id"] for item in geometry["items"]])
        self.assertEqual([], geometry["sfx"]["flags"])
        # Lettering never edits its own input.
        self.assertEqual(
            clean_before, (self.project / "panels/p01-01/clean.png").read_bytes()
        )

    def test_relettered_provenance_passes_validation_and_stale_claims_do_not(self):
        self._replace()
        letter_project(self.project)

        self.assertEqual(
            (), validate_lettering_provenance(self.project, "p01-01")
        )

        # A record claiming this engine lettered an effect the storyboard still
        # hands to the image model must not pass as provenance.
        storyboard = self._storyboard()
        item = next(
            text for text in self._panel(storyboard)["text"]
            if text["id"] == "p01-01-t02"
        )
        item["render_mode"] = GENERATED_VISUAL
        self._write_json("plan/storyboard.json", storyboard)

        details = [
            issue.message
            for issue in validate_lettering_provenance(self.project, "p01-01")
        ]
        self.assertTrue(
            any("recorded SFX provenance does not match" in detail for detail in details),
            details,
        )

    def test_repeated_replacement_of_the_same_effect_is_refused(self):
        self._replace()
        with self.assertRaisesRegex(ValueError, "already deterministic-lettering"):
            self._replace()

    def test_replacement_refuses_unknown_panels_items_and_kinds(self):
        with self.assertRaisesRegex(ValueError, "no text item"):
            self._replace(text_id="p01-01-t99")
        with self.assertRaisesRegex(ValueError, "is not SFX"):
            self._replace(text_id="p01-01-t01")
        with self.assertRaisesRegex(ValueError, "invalid panel ID"):
            replace_generated_sfx(self.project, "p1-1", "p01-01-t02", reason="x")
        with self.assertRaisesRegex(ValueError, "invalid text ID"):
            replace_generated_sfx(
                self.project, "p01-01", "../escape", reason="x"
            )
        with self.assertRaisesRegex(ValueError, "reason must not be empty"):
            self._replace(reason="   ")

    def test_refused_replacement_leaves_the_project_untouched(self):
        storyboard_before = (self.project / "plan/storyboard.json").read_bytes()
        manifest_before = (self.project / "project.json").read_bytes()

        with self.assertRaises(ValueError):
            self._replace(text_id="p01-01-t01")

        self.assertEqual(
            storyboard_before, (self.project / "plan/storyboard.json").read_bytes()
        )
        self.assertEqual(
            manifest_before, (self.project / "project.json").read_bytes()
        )
        self.assertFalse((self.project / "panels/p01-01/sfx-audit.json").exists())

    def test_second_effect_in_the_same_panel_archives_its_own_evidence(self):
        storyboard = self._storyboard()
        self._panel(storyboard)["text"].append({
            "anchor": "bottom-left",
            "content": "THUD",
            "id": "p01-01-t03",
            "kind": "sfx",
            "priority": 3,
            "speaker": None,
        })
        self._write_json("plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"]["sha256"] = sha256_file(
            self.project / "plan/storyboard.json"
        )
        self._write_json("project.json", manifest)

        self._replace(text_id="p01-01-t02")
        self._replace(text_id="p01-01-t03")

        record = read_json(self.project / "panels/p01-01/sfx-audit.json")
        self.assertEqual(
            ["p01-01-t02", "p01-01-t03"],
            [entry["text_id"] for entry in record["replacements"]],
        )
        archives = sorted(
            path.name
            for path in (self.project / "panels/p01-01/sfx-audit").iterdir()
        )
        self.assertIn("p01-01-t02.attempt-1.clean.png", archives)
        self.assertIn("p01-01-t03.attempt-1.clean.png", archives)
        # The prohibition is added once and not duplicated.
        self.assertEqual(
            1, self._panel()["negative"].count(DETERMINISTIC_SFX_NEGATIVE)
        )

    def test_archive_sequence_advances_rather_than_overwriting_evidence(self):
        """A slot already holding evidence of any kind is skipped whole."""
        self._replace()
        # Only a manual edit back to generated-visual can reach a second repair of
        # one item, but the archive must survive that path rather than trust it.
        storyboard = self._storyboard()
        item = next(
            text for text in self._panel(storyboard)["text"]
            if text["id"] == "p01-01-t02"
        )
        item["render_mode"] = GENERATED_VISUAL
        self._write_json("plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"]["sha256"] = sha256_file(
            self.project / "plan/storyboard.json"
        )
        self._write_json("project.json", manifest)
        first = sorted(
            path.read_bytes()
            for path in (self.project / "panels/p01-01/sfx-audit").iterdir()
        )

        result = self._replace()

        self.assertEqual(2, result["sequence"])
        for entry in result["archived"]:
            self.assertIn(".attempt-2.", entry["path"])
        # Every byte archived by the first repair is still exactly where it was.
        surviving = sorted(
            path.read_bytes()
            for path in (self.project / "panels/p01-01/sfx-audit").iterdir()
        )
        for payload in first:
            self.assertIn(payload, surviving)


if __name__ == "__main__":
    unittest.main()
