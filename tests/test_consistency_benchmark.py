"""Character consistency benchmark coverage: structure is proven, opinion is not.

These tests assert the deterministic plane of the benchmark only. A subjective
identity score never appears here: the scorecard arithmetic is exercised with
synthetic scores, and the committed baseline is required to declare its visual
plane unscored.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import tests.consistency_benchmark as consistency_benchmark
from scripts.character_identity import derive_identity_pack
from scripts.character_quality import (
    CHARACTER_TRAITS,
    build_character_identity_check,
    character_consistency_context,
)
from scripts.comic_sol import read_json, sha256_file
from scripts.reference_strategy import project_reference_plan
from scripts.validate_project import ProjectValidationError, require_valid_project
from tests.consistency_benchmark import (
    BASELINE_KIND,
    CHARACTERS,
    CONSISTENCY_DIMENSIONS,
    DIMENSION_SOURCES,
    MATRIX,
    MATRIX_BY_PANEL,
    PANEL_IDS,
    SCENARIO,
    SCENES,
    SCORE_SCALE,
    SCORECARD_KIND,
    STORYBOARD,
    TITLE,
    VIEWS,
    ScorecardError,
    build_baseline_report,
    build_consistency_project,
    conditions_per_view,
    consistency_metadata,
    definition_digest,
    immutable_traits,
    invariant_pins,
    load_scorecard,
    main,
    panel_prompt,
    resolved_panels,
    scorecard_template,
    structural_baseline,
    summarize_scorecard,
    trait_restatements,
    view_counts,
    views_per_character,
)


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION = ROOT / "docs/character-consistency-benchmark.md"
BASELINE_DIRECTORY = ROOT / "benchmarks/consistency"

# The seven dimensions the benchmark has to record for every scored panel.
REQUIRED_DIMENSIONS = {
    "face",
    "hair",
    "age",
    "clothing",
    "accessories",
    "proportions",
    "signature-traits",
}
# Twelve panels hold fifteen character appearances, so one dimension is scored
# fifteen times and the whole scorecard holds 105 scores.
CHARACTER_APPEARANCES = 15
TOTAL_SCORES = CHARACTER_APPEARANCES * len(REQUIRED_DIMENSIONS)
# Fixtures stay text-only: a raster would immediately break these budgets.
MAX_PROJECT_FILE_BYTES = 32 * 1024
MAX_PROJECT_BYTES = 256 * 1024


def _scored_template(score=4, unscored=()):
    """Return a scorecard filled with one synthetic score plus review provenance."""
    scorecard = scorecard_template()
    skipped = set(unscored)
    for panel_id, panel in scorecard["panels"].items():
        for character_id, scores in panel["characters"].items():
            for dimension in scores:
                if (panel_id, character_id, dimension) in skipped:
                    continue
                scores[dimension] = score
    scorecard["review"].update({
        "engine_version": "test",
        "evidence_mode": "model-assisted",
        "method": "synthetic scores for arithmetic coverage",
        "reviewer": "test suite",
    })
    return scorecard


def _run_command(*argv):
    """Run the benchmark CLI, returning its exit code, stdout, and stderr."""
    out, reported = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(reported):
        try:
            code = main(list(argv))
        except SystemExit as error:
            code = int(error.code)
    return code, out.getvalue(), reported.getvalue()


class ConsistencyDefinitionTests(unittest.TestCase):
    def test_records_every_required_consistency_dimension(self):
        self.assertEqual(REQUIRED_DIMENSIONS, set(CONSISTENCY_DIMENSIONS))
        self.assertEqual(REQUIRED_DIMENSIONS, set(DIMENSION_SOURCES))
        self.assertEqual(len(CONSISTENCY_DIMENSIONS), len(set(CONSISTENCY_DIMENSIONS)))

    def test_immutable_traits_are_owned_by_the_character_bible(self):
        for character_id in sorted(CHARACTERS):
            traits = immutable_traits(character_id)
            self.assertEqual(set(CONSISTENCY_DIMENSIONS), set(traits))
            for dimension, text in traits.items():
                value = CHARACTERS[character_id]
                for key in DIMENSION_SOURCES[dimension]:
                    value = value[key]
                expected = "; ".join(value) if isinstance(value, (list, tuple)) else value
                with self.subTest(character=character_id, dimension=dimension):
                    self.assertEqual(expected, text)
                    self.assertTrue(text.strip())

    def test_every_canonical_character_is_exercised_in_all_five_views(self):
        coverage = views_per_character()
        self.assertEqual(set(CHARACTERS), set(coverage))
        for character_id, views in coverage.items():
            with self.subTest(character=character_id):
                self.assertEqual(sorted(VIEWS), views)

    def test_every_view_is_exercised_under_two_lighting_conditions_and_backgrounds(self):
        lighting = conditions_per_view("lighting_condition")
        backgrounds = conditions_per_view("scene")
        for view in VIEWS:
            with self.subTest(view=view):
                # One view under one light proves nothing about identity drift; the
                # claim is that the same view survives a change of light.
                self.assertGreaterEqual(len(lighting[view]), 2, lighting[view])
                self.assertGreaterEqual(len(backgrounds[view]), 2, backgrounds[view])

    def test_matrix_varies_expression_pose_lighting_and_background(self):
        self.assertEqual(12, len(MATRIX))
        self.assertGreaterEqual(len({row["expression"] for row in MATRIX}), 6)
        self.assertEqual(len(MATRIX), len({row["pose"] for row in MATRIX}))
        self.assertEqual(len(MATRIX), len({row["beat"] for row in MATRIX}))
        self.assertEqual(4, len({row["lighting_condition"] for row in MATRIX}))
        self.assertEqual(set(SCENES), {row["scene"] for row in MATRIX})
        self.assertEqual(12, sum(view_counts().values()))

    def test_the_reference_scene_is_a_single_condition_control(self):
        control = [row for row in MATRIX if row["scene"] == "reference-studio"]
        self.assertEqual(4, len(control))
        # A control changes exactly one variable, so light and expression are held
        # while the camera moves around the character.
        self.assertEqual(1, len({row["lighting"] for row in control}))
        self.assertEqual({"neutral"}, {row["expression"] for row in control})
        self.assertEqual(4, len({row["view"] for row in control}))

    def test_matrix_rows_resolve_to_the_storyboard_panels_in_order(self):
        panels = resolved_panels()
        self.assertEqual(list(PANEL_IDS), [panel["id"] for panel in panels])
        self.assertEqual(3, len(STORYBOARD["pages"]))
        self.assertEqual([1, 2, 3], [page["number"] for page in STORYBOARD["pages"]])
        for panel in panels:
            row = MATRIX_BY_PANEL[panel["id"]]
            with self.subTest(panel=panel["id"]):
                self.assertIn(row["view"], panel["shot"])
                self.assertEqual(row["scene"], panel["scene_id"])
                self.assertEqual(list(row["characters"]), panel["characters"])
                self.assertEqual(row["lighting"], panel["lighting"])

    def test_every_panel_pins_every_invariant_of_every_character_present(self):
        pins = invariant_pins()
        self.assertEqual(pins["expected"], pins["recorded"])
        self.assertEqual(60, pins["expected"])
        for panel in resolved_panels():
            anchor = SCENES[panel["scene_id"]]["continuity_anchor"]
            with self.subTest(panel=panel["id"]):
                self.assertIn(f"{panel['scene_id']}:{anchor}", panel["continuity"])

    def test_every_panel_prompt_restates_every_immutable_trait_verbatim(self):
        restatements = trait_restatements()
        self.assertEqual(restatements["expected"], restatements["recorded"])
        self.assertEqual(TOTAL_SCORES, restatements["expected"])
        for panel_id in PANEL_IDS:
            prompt = panel_prompt(panel_id)
            row = MATRIX_BY_PANEL[panel_id]
            with self.subTest(panel=panel_id):
                self.assertIn(row["lighting"], prompt)
                self.assertIn(row["expression_detail"], prompt)
                self.assertIn(SCENES[row["scene"]]["continuity_anchor"], prompt)
                for character_id in row["characters"]:
                    for trait in immutable_traits(character_id).values():
                        self.assertIn(trait, prompt)

    def test_no_panel_authors_text_that_could_occlude_identity(self):
        for panel in resolved_panels():
            with self.subTest(panel=panel["id"]):
                self.assertEqual([], panel["text"])
                for prohibited in ("dialogue", "captions", "sound effects"):
                    self.assertIn(prohibited, panel["negative"])

    def test_metadata_states_the_measured_capability(self):
        metadata = consistency_metadata()
        self.assertEqual(SCENARIO, metadata["scenario"])
        self.assertEqual(TITLE, metadata["title"])
        self.assertTrue(metadata["local_only"])
        self.assertEqual("structural", metadata["evidence_mode"])
        self.assertGreaterEqual(len(metadata["capability"].split()), 8)
        self.assertTrue(metadata["capability"].endswith("."))
        self.assertFalse(metadata["scoring"]["ci_asserted"])
        for tag in metadata["stresses"]:
            self.assertIn(":", tag, tag)
        self.assertEqual(list(PANEL_IDS), metadata["expected"]["panels"])
        self.assertEqual(0, metadata["expected"]["text_item_count"])

    def test_documentation_describes_the_matrix_dimensions_and_tags(self):
        documentation = DOCUMENTATION.read_text(encoding="utf-8")
        for panel_id in PANEL_IDS:
            self.assertIn(f"`{panel_id}`", documentation)
        for view in VIEWS:
            self.assertIn(f"`{view}`", documentation)
        for dimension in CONSISTENCY_DIMENSIONS:
            self.assertIn(f"`{dimension}`", documentation)
        for scene_id in SCENES:
            self.assertIn(f"`{scene_id}`", documentation)
        for tag in consistency_metadata()["stresses"]:
            self.assertIn(tag, documentation)
        self.assertIn("python -m tests.consistency_benchmark", documentation)


class ConsistencyScorecardTests(unittest.TestCase):
    def test_template_is_unscored_and_covers_every_panel_character_dimension(self):
        template = scorecard_template()
        self.assertEqual(SCORECARD_KIND, template["kind"])
        self.assertEqual(set(PANEL_IDS), set(template["panels"]))
        self.assertEqual(definition_digest(), template["definition_sha256"])
        recorded = 0
        for panel_id, panel in template["panels"].items():
            row = MATRIX_BY_PANEL[panel_id]
            self.assertEqual(row["view"], panel["view"])
            self.assertEqual(row["scene"], panel["background"])
            self.assertEqual(row["lighting_condition"], panel["lighting_condition"])
            self.assertEqual(set(row["characters"]), set(panel["characters"]))
            for scores in panel["characters"].values():
                self.assertEqual(set(CONSISTENCY_DIMENSIONS), set(scores))
                self.assertEqual([None] * len(scores), list(scores.values()))
                recorded += len(scores)
        self.assertEqual(TOTAL_SCORES, recorded)

    def test_unscored_template_summarizes_as_incomplete_without_a_mean(self):
        summary = summarize_scorecard(scorecard_template())
        self.assertFalse(summary["complete"])
        self.assertIsNone(summary["overall"]["mean"])
        self.assertEqual(0, summary["overall"]["scored"])
        self.assertEqual(TOTAL_SCORES, summary["overall"]["total"])

    def test_summary_aggregates_scored_entries_by_dimension_view_and_character(self):
        summary = summarize_scorecard(_scored_template(3))
        self.assertTrue(summary["complete"])
        self.assertEqual(3.0, summary["overall"]["mean"])
        self.assertEqual(set(CONSISTENCY_DIMENSIONS), set(summary["by_dimension"]))
        self.assertEqual(set(VIEWS), set(summary["by_view"]))
        self.assertEqual(set(CHARACTERS), set(summary["by_character"]))
        for dimension, group in summary["by_dimension"].items():
            with self.subTest(dimension=dimension):
                self.assertEqual(3, group["min"])
                self.assertEqual(3, group["max"])
                self.assertEqual(CHARACTER_APPEARANCES, group["total"])
                self.assertEqual(group["total"], group["scored"])

    def test_summary_averages_only_the_dimensions_a_reviewer_scored(self):
        skipped = (("p01-01", "rani", "face"), ("p01-01", "rani", "hair"))
        summary = summarize_scorecard(_scored_template(2, unscored=skipped))
        self.assertFalse(summary["complete"])
        self.assertEqual(TOTAL_SCORES - len(skipped), summary["overall"]["scored"])
        self.assertEqual(TOTAL_SCORES, summary["overall"]["total"])
        # An unscored dimension is missing evidence, not a zero.
        self.assertEqual(2.0, summary["overall"]["mean"])
        self.assertEqual(CHARACTER_APPEARANCES, summary["by_dimension"]["face"]["total"])
        self.assertEqual(CHARACTER_APPEARANCES - 1, summary["by_dimension"]["face"]["scored"])

    def test_scorecard_scored_against_another_definition_is_rejected(self):
        scorecard = _scored_template()
        scorecard["definition_sha256"] = "0" * 64
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(scorecard)
        self.assertIn("definition_sha256", str(raised.exception))

    def test_scorecard_rejects_unknown_panels_and_characters(self):
        missing_panel = scorecard_template()
        missing_panel["panels"].pop("p02-01")
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(missing_panel)
        self.assertIn("every benchmark panel", str(raised.exception))

        foreign_character = scorecard_template()
        foreign_character["panels"]["p01-01"]["characters"]["bayu"] = {
            dimension: None for dimension in CONSISTENCY_DIMENSIONS
        }
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(foreign_character)
        self.assertIn("p01-01", str(raised.exception))

    def test_scorecard_rejects_missing_dimensions_and_out_of_scale_scores(self):
        missing_dimension = scorecard_template()
        missing_dimension["panels"]["p01-02"]["characters"]["rani"].pop("hair")
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(missing_dimension)
        self.assertIn("consistency dimension", str(raised.exception))

        out_of_scale = _scored_template()
        out_of_scale["panels"]["p01-03"]["characters"]["rani"]["face"] = SCORE_SCALE["max"] + 1
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(out_of_scale)
        self.assertIn("between", str(raised.exception))

        wrong_type = _scored_template()
        wrong_type["panels"]["p01-04"]["characters"]["rani"]["face"] = "good"
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(wrong_type)
        self.assertIn("integer or null", str(raised.exception))

    def test_a_score_without_review_provenance_is_rejected(self):
        unattributed = _scored_template()
        unattributed["review"]["reviewer"] = None
        with self.assertRaises(ScorecardError) as raised:
            summarize_scorecard(unattributed)
        self.assertIn("review.reviewer", str(raised.exception))

    def test_scored_panel_produces_actionable_results_for_all_qa_traits(self):
        scorecard = _scored_template(4)
        scores = scorecard["panels"]["p01-01"]["characters"]["rani"]
        scores["hair"] = 3
        scores["face"] = 2
        converter = getattr(consistency_benchmark, "panel_qa_assessments", None)
        self.assertIsNotNone(converter, "CS-013 per-trait QA output is not implemented")

        assessments = converter(scorecard, "p01-01")
        pack = derive_identity_pack(consistency_benchmark.CHARACTER_BIBLE)
        context = character_consistency_context(
            pack,
            consistency_benchmark.CHARACTER_BIBLE,
            project_reference_plan(pack, consistency_benchmark.STORYBOARD),
            "p01-01",
            storyboard=consistency_benchmark.STORYBOARD,
        )
        check = build_character_identity_check(
            context,
            assessments,
            method=scorecard["review"]["method"],
            reviewer=scorecard["review"]["reviewer"],
        )

        self.assertEqual(list(CHARACTER_TRAITS), [item["trait"] for item in check["regions"]])
        self.assertEqual(
            ["fail", "warning", "pass", "pass", "pass", "pass", "pass"],
            [item["result"] for item in check["regions"]],
        )
        self.assertEqual(("fail", "error"), (check["result"], check["severity"]))
        self.assertTrue(check["regions"][0]["repair_guidance"])
        self.assertTrue(check["regions"][1]["repair_guidance"])


class ConsistencyCommandTests(unittest.TestCase):
    def test_summarize_fails_closed_on_missing_unreadable_or_malformed_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            malformed = root / "malformed.json"
            malformed.write_text('{"panels": ', encoding="utf-8")
            not_utf8 = root / "not-utf8.json"
            not_utf8.write_bytes(b"\xff\xfe{}")
            foreign = root / "foreign.json"
            foreign.write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")
            for path in (root / "absent.json", malformed, not_utf8, foreign):
                with self.subTest(scorecard=path.name):
                    code, _, reported = _run_command("summarize", str(path))
                    # A traceback is not a diagnostic: the command owns its failure.
                    self.assertEqual(1, code)
                    self.assertIn("invalid scorecard", reported)

    def test_load_scorecard_names_the_path_it_could_not_use(self):
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "absent.json"
            with self.assertRaises(ScorecardError) as raised:
                load_scorecard(missing)
            self.assertIn(missing.name, str(raised.exception))

    def test_emitted_scorecard_summarizes_as_unscored(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "scorecard.json"
            self.assertEqual(0, _run_command("scorecard", str(path))[0])
            code, printed, _ = _run_command("summarize", str(path))
            self.assertEqual(0, code)
            summary = json.loads(printed)
            self.assertFalse(summary["complete"])
            self.assertEqual(TOTAL_SCORES, summary["overall"]["total"])

    def test_qa_results_command_emits_one_result_per_panel_trait(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "scorecard.json"
            path.write_text(json.dumps(_scored_template(4)), encoding="utf-8")

            code, output, reported = _run_command("qa-results", str(path), "p01-01")

        self.assertEqual(0, code, reported)
        results = json.loads(output)
        self.assertEqual(list(CHARACTER_TRAITS), [item["trait"] for item in results])
        self.assertEqual(["pass"] * len(CHARACTER_TRAITS), [item["result"] for item in results])


class ConsistencyProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary_directory.name)
        cls.project = build_consistency_project(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._temporary_directory.cleanup()

    def test_benchmark_project_validates_under_the_current_schema(self):
        try:
            require_valid_project(self.project, "storyboard")
        except ProjectValidationError as error:
            self.fail(
                "consistency benchmark failed storyboard validation:\n"
                + "\n".join(
                    f"{issue.path}:{issue.field}: {issue.message}" for issue in error.issues
                )
            )

    def test_benchmark_project_is_plan_complete_and_self_consistent(self):
        manifest = read_json(self.project / "project.json")
        self.assertEqual("STORYBOARDED", manifest["status"])
        self.assertEqual(SCENARIO, manifest["project_id"])
        self.assertEqual(TITLE, manifest["title"])
        self.assertEqual(list(PANEL_IDS), manifest["panels"])
        self.assertEqual(3, manifest["settings"]["page_count"])
        self.assertEqual(12, manifest["settings"]["panel_count"])
        for name in ("character_bible", "story_plan", "storyboard"):
            self.assertIn(name, manifest["artifacts"])

    def test_every_panel_carries_a_renderable_prompt(self):
        for panel_id in PANEL_IDS:
            prompt = self.project / f"prompts/panels/{panel_id}.txt"
            with self.subTest(panel=panel_id):
                self.assertTrue(prompt.is_file(), prompt)
                self.assertEqual(
                    panel_prompt(panel_id), prompt.read_text(encoding="utf-8")
                )

    def test_sidecars_carry_the_metadata_and_an_unscored_scorecard(self):
        metadata = self.root / f"{SCENARIO}.benchmark.json"
        scorecard = self.root / f"{SCENARIO}.scorecard.json"
        self.assertEqual(
            consistency_metadata(), json.loads(metadata.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            scorecard_template(), json.loads(scorecard.read_text(encoding="utf-8"))
        )

    def test_rerunning_the_benchmark_reuses_the_same_input_definition(self):
        with tempfile.TemporaryDirectory() as raw:
            rerun = build_consistency_project(Path(raw))
            for relative in (
                "source/input.txt",
                "plan/character-bible.json",
                "plan/story-plan.json",
                "plan/storyboard.json",
            ):
                with self.subTest(artifact=relative):
                    self.assertEqual(
                        sha256_file(self.project / relative),
                        sha256_file(rerun / relative),
                    )
            for panel_id in PANEL_IDS:
                relative = f"prompts/panels/{panel_id}.txt"
                with self.subTest(artifact=relative):
                    self.assertEqual(
                        sha256_file(self.project / relative),
                        sha256_file(rerun / relative),
                    )

    def test_benchmark_stays_small_enough_for_practical_evaluation(self):
        total = 0
        for path in self.project.rglob("*"):
            if not path.is_file():
                continue
            size = path.stat().st_size
            total += size
            with self.subTest(artifact=path.name):
                self.assertLessEqual(size, MAX_PROJECT_FILE_BYTES, path)
        self.assertLessEqual(total, MAX_PROJECT_BYTES)
        self.assertGreater(total, 0)


class ConsistencyBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_baseline_report()
        cls.baselines = sorted(BASELINE_DIRECTORY.glob("baseline-v*.json"))
        cls.current = BASELINE_DIRECTORY / f"baseline-v{cls.report['engine_version']}.json"

    def test_the_current_engine_revision_carries_its_own_baseline(self):
        # A baseline for an older engine is history, not coverage. Without this the
        # next version bump would inherit a stale baseline and claim the release was
        # measured when it never was.
        self.assertTrue(self.baselines, BASELINE_DIRECTORY)
        self.assertIn(self.current, self.baselines, sorted(path.name for path in self.baselines))

    def test_committed_baselines_are_named_for_the_engine_they_measured(self):
        for path in self.baselines:
            recorded = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(baseline=path.name):
                self.assertEqual(BASELINE_KIND, recorded["kind"])
                self.assertEqual(SCENARIO, recorded["benchmark"])
                self.assertTrue(str(recorded["engine_version"]).strip())
                self.assertEqual(f"baseline-v{recorded['engine_version']}.json", path.name)
                # Every baseline states its own evidence boundary, including older
                # ones kept as history.
                self.assertFalse(recorded["visual"]["scored"])

    def test_the_current_baseline_describes_the_current_definition(self):
        recorded = json.loads(self.current.read_text(encoding="utf-8"))
        self.assertEqual(self.report["engine_version"], recorded["engine_version"])
        for key in (
            "benchmark",
            "evidence_mode",
            "kind",
            "project_validation",
            "schema_version",
            "structural",
            "visual",
        ):
            self.assertEqual(self.report[key], recorded[key], key)

    def test_baseline_reports_a_measured_structural_plane(self):
        structural = self.report["structural"]
        self.assertEqual(BASELINE_KIND, self.report["kind"])
        self.assertEqual(structural_baseline(), structural)
        self.assertEqual(
            {"result": "pass", "stage": "storyboard"}, self.report["project_validation"]
        )
        self.assertEqual(12, structural["panel_count"])
        self.assertEqual(3, structural["page_count"])
        self.assertEqual(sorted(CHARACTERS), structural["characters"])
        self.assertEqual(
            structural["invariant_pins"]["expected"], structural["invariant_pins"]["recorded"]
        )
        self.assertEqual(
            structural["trait_restatements"]["expected"],
            structural["trait_restatements"]["recorded"],
        )

    def test_baseline_keeps_the_visual_plane_unscored_and_names_its_limits(self):
        visual = self.report["visual"]
        # CI states the boundary instead of publishing an opinion as a number.
        self.assertFalse(visual["scored"])
        self.assertEqual(0, visual["scored_dimensions"])
        self.assertEqual(TOTAL_SCORES, visual["total_dimensions"])
        self.assertGreaterEqual(len(visual["limitations"]), 3)
        self.assertEqual(SCORE_SCALE, visual["scale"])
        self.assertIn("summarize", visual["how_to_score"])


if __name__ == "__main__":
    unittest.main()
