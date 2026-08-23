"""Coverage for compact, version-tagged Comic Sol benchmark summaries."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from comic_sol_product import __version__ as RELEASE_VERSION

from comic_sol_product import __version__
from tests.consistency_benchmark import definition_digest

ROOT = Path(__file__).resolve().parents[1]

from scripts.benchmark import (  # noqa: E402
    HARNESS_VERSION,
    METRIC_DIRECTIONS,
    METRIC_IDS,
    write_result,
)
from scripts.benchmark_summary import (  # noqa: E402
    CONSISTENCY_METRIC_IDS,
    GATING_METRIC_IDS,
    SUMMARY_KIND,
    consistency_report,
    diff_summaries,
    load_consistency_baseline,
    load_consistency_scorecard,
    load_summary,
    main,
    render_summary_markdown,
    summarize_results,
    write_summary,
)

RELEASE_BASELINE = ROOT / f"benchmarks/consistency/baseline-v{__version__}.json"

# One three-panel, one-page, nine-dialogue-check case at every metric target.
PERFECT = {
    "dialogue_correctness": (9, 9),
    "export_success": (1, 1),
    "panel_acceptance": (3, 3),
    "pipeline_success": (1, 1),
    "repair_rate": (0, 3),
    "resume_success": (1, 1),
}


def _metric(metric_id, numerator, denominator):
    """Build one result metric the harness result validator accepts."""
    if denominator > 0:
        value = round(numerator / denominator, 6)
    else:
        value = 1.0 if metric_id == "dialogue_correctness" else 0.0
    return {
        "denominator": denominator,
        "direction": METRIC_DIRECTIONS[metric_id],
        "numerator": numerator,
        "unit": "ratio",
        "value": value,
    }


def _result(
    case_id,
    metrics=None,
    *,
    status="passed",
    engine=RELEASE_VERSION,
    git="a" * 40,
    harness=HARNESS_VERSION,
    proves_visual_quality=False,
    case_sha256="b" * 64,
):
    """Build one synthetic benchmark result record for summary coverage."""
    measured = {**PERFECT, **(metrics or {})}
    return {
        "case_id": case_id,
        "case_sha256": case_sha256,
        "evidence": {
            "mode": "deterministic",
            "panels": {},
            "proves_visual_quality": proves_visual_quality,
        },
        "harness_version": harness,
        "kind": "benchmark-result",
        "limitations": ["deterministic runs prove mechanics only"],
        "metrics": {
            metric_id: _metric(metric_id, *measured[metric_id]) for metric_id in METRIC_IDS
        },
        "revision": {
            "engine_version": engine,
            "git_revision": git,
            "harness_version": harness,
            "stage_versions": {},
        },
        "schema_version": "1.0",
        "seed": 1,
        "status": status,
    }


def _baseline(**overrides):
    """Build one character consistency baseline report."""
    from tests.consistency_benchmark import structural_baseline

    baseline = {
        "benchmark": "character-consistency",
        "definition_sha256": definition_digest(),
        "engine_version": __version__,
        "evidence_mode": "structural",
        "kind": "character-consistency-baseline",
        "project_validation": {"result": "pass", "stage": "storyboard"},
        "schema_version": "1.0",
        "structural": structural_baseline(),
        "visual": {"scored": False, "scored_dimensions": 0, "total_dimensions": 105},
    }
    baseline.update(overrides)
    return baseline


def _scorecard(scores, *, engine_version=RELEASE_VERSION):
    """Build one attributable character consistency scorecard."""
    from tests.consistency_benchmark import scorecard_template

    scorecard = scorecard_template()
    for dimension, score in scores.items():
        scorecard["panels"]["p01-01"]["characters"]["rani"][dimension] = score
    scorecard["review"] = {
        "engine_version": engine_version,
        "method": "bounded visual review",
        "model": "example-model",
        "provider": "example-provider",
        "reviewer": "reviewer",
    }
    return scorecard


class TemporaryRootTestCase(unittest.TestCase):
    """Give every summary test its own throwaway working directory."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def _json(self, payload, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return path


class ConsistencyPlaneTests(TemporaryRootTestCase):
    def test_committed_release_baseline_folds_into_reported_metrics(self):
        self.assertTrue(RELEASE_BASELINE.is_file(), RELEASE_BASELINE)
        report = consistency_report(baseline=load_consistency_baseline(RELEASE_BASELINE))
        self.assertEqual(1.0, report["metrics"]["consistency_invariant_pinning"]["value"])
        self.assertEqual(1.0, report["metrics"]["consistency_trait_restatement"]["value"])
        self.assertEqual(0.0, report["metrics"]["consistency_visual_coverage"]["value"])
        self.assertNotIn(
            "consistency_visual_score",
            report["metrics"],
            "an unscored baseline must not publish a visual score",
        )
        self.assertFalse(report["scored"])
        self.assertFalse(report["proves_visual_quality"])
        self.assertEqual("pass", report["project_validation"])
        self.assertEqual(12, report["structural"]["panel_count"])

    def test_unscored_release_scorecard_matches_the_release_baseline(self):
        from tests.consistency_benchmark import scorecard_template

        scorecard = load_consistency_scorecard(self._json(scorecard_template(), "scorecard.json"))
        report = consistency_report(
            baseline=load_consistency_baseline(RELEASE_BASELINE), scorecard=scorecard
        )
        coverage = report["metrics"]["consistency_visual_coverage"]
        self.assertEqual(0, coverage["numerator"])
        self.assertEqual(105, coverage["denominator"])
        self.assertFalse(report["scored"])
        self.assertEqual(64, len(report["definition_sha256"]))

    def test_scored_scorecard_normalizes_the_mean_over_scored_entries_only(self):
        scorecard = load_consistency_scorecard(
            self._json(_scorecard({"face": 4, "hair": 2, "age": None}), "scored.json")
        )
        report = consistency_report(scorecard=scorecard)
        score = report["metrics"]["consistency_visual_score"]
        self.assertEqual(6, score["numerator"])
        self.assertEqual(8, score["denominator"])
        self.assertEqual(0.75, score["value"])
        coverage = report["metrics"]["consistency_visual_coverage"]
        self.assertEqual(2, coverage["numerator"])
        self.assertEqual(105, coverage["denominator"])
        self.assertTrue(report["scored"])
        self.assertTrue(report["proves_visual_quality"])
        self.assertEqual("reviewer", report["review"]["reviewer"])

    def test_scorecard_engine_version_must_match_result_revision(self):
        scorecard = load_consistency_scorecard(
            self._json(_scorecard({"face": 4}, engine_version="2.0.0rc3"), "scored.json")
        )
        report = consistency_report(scorecard=scorecard)
        self.assertEqual("2.0.0rc3", report["engine_version"])
        results = self.root / "results"
        write_result(_result("case-one"), results)
        summary = summarize_results(results, consistency_scorecard=self.root / "scored.json")
        self.assertEqual("failed", summary["status"])
        self.assertTrue(any("scorecard engine version" in item for item in summary["exceptions"]))

    def test_a_scorecard_from_another_definition_is_refused(self):
        foreign = _scorecard({"face": 4})
        foreign["definition_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "canonical benchmark definition"):
            load_consistency_scorecard(self._json(foreign, "short.json"))

    def test_unattributable_and_out_of_scale_scores_are_refused(self):
        unattributed = _scorecard({"face": 4})
        unattributed["review"] = {"method": None, "reviewer": None}
        with self.assertRaisesRegex(ValueError, "review.method and review.reviewer"):
            load_consistency_scorecard(self._json(unattributed, "unattributed.json"))
        with self.assertRaisesRegex(ValueError, "review.engine_version"):
            invalid_type = _scorecard({"face": 4})
            invalid_type["review"]["engine_version"] = [RELEASE_VERSION]
            load_consistency_scorecard(self._json(invalid_type, "invalid-type.json"))
        with self.assertRaisesRegex(ValueError, "outside the published scale"):
            load_consistency_scorecard(self._json(_scorecard({"face": 9}), "high.json"))
        with self.assertRaisesRegex(ValueError, "integer or null"):
            load_consistency_scorecard(self._json(_scorecard({"face": True}), "bool.json"))

    def test_foreign_and_malformed_baselines_are_refused(self):
        with self.assertRaisesRegex(ValueError, "kind must be"):
            load_consistency_baseline(self._json(_baseline(kind="something-else"), "foreign.json"))
        over_recorded = _baseline(
            structural={
                **_baseline()["structural"],
                "invariant_pins": {"expected": 60, "recorded": 61},
            }
        )
        with self.assertRaisesRegex(ValueError, "recorded.*expected"):
            load_consistency_baseline(self._json(over_recorded, "over-recorded.json"))
        broken = _baseline()
        broken["structural"] = {
            **broken["structural"],
            "invariant_pins": {"expected": 60},
        }
        with self.assertRaisesRegex(ValueError, "invariant_pins"):
            load_consistency_baseline(self._json(broken, "broken.json"))
        unreadable = self.root / "unreadable.json"
        unreadable.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            load_consistency_baseline(unreadable)

    def test_a_report_needs_at_least_one_published_plane(self):
        with self.assertRaises(ValueError):
            consistency_report()


class SummaryTests(TemporaryRootTestCase):
    def setUp(self):
        super().setUp()
        self.results = self.root / "results"

    def _publish(self, *records):
        for record in records:
            write_result(record, self.results)

    def test_metrics_pool_numerators_and_denominators_across_cases(self):
        self._publish(
            _result("case-one"),
            _result(
                "case-two",
                {
                    "dialogue_correctness": (0, 0),
                    "panel_acceptance": (1, 4),
                    "repair_rate": (2, 4),
                },
            ),
        )
        summary = summarize_results(self.results)
        self.assertEqual(2, summary["case_count"])
        acceptance = summary["metrics"]["panel_acceptance"]
        self.assertEqual((4, 7), (acceptance["numerator"], acceptance["denominator"]))
        self.assertEqual(round(4 / 7, 6), acceptance["value"])
        repair = summary["metrics"]["repair_rate"]
        self.assertEqual((2, 7), (repair["numerator"], repair["denominator"]))
        self.assertEqual("lower-is-better", repair["direction"])
        dialogue = summary["metrics"]["dialogue_correctness"]
        self.assertEqual(
            (9, 9),
            (dialogue["numerator"], dialogue["denominator"]),
            "a case without dialogue must not dilute the pooled dialogue metric",
        )
        self.assertEqual(
            "failed",
            summary["status"],
            "a pooled gating metric below target cannot report a passing revision",
        )

    def test_summary_is_version_tagged_and_byte_reproducible(self):
        self._publish(_result("case-one"), _result("case-two"))
        first = summarize_results(self.results)
        second = summarize_results(self.results)
        self.assertEqual("passed", first["status"])
        self.assertEqual(SUMMARY_KIND, first["kind"])
        self.assertEqual(f"v{RELEASE_VERSION}+{'a' * 12}", first["version_tag"])
        self.assertEqual(RELEASE_VERSION, first["revision"]["engine_version"])
        first_path, first_markdown = write_summary(first, self.root / "first/summary.json")
        second_path, _ = write_summary(second, self.root / "second/summary.json")
        self.assertEqual(
            first_path.read_bytes(),
            second_path.read_bytes(),
            "summarizing one result set twice must produce identical summaries",
        )
        self.assertEqual(self.root / "first/summary.md", first_markdown)
        self.assertTrue(first_markdown.is_file())

    def test_version_tag_falls_back_to_the_engine_version_outside_a_worktree(self):
        self._publish(_result("case-one", git="unknown"))
        self.assertEqual(f"v{RELEASE_VERSION}", summarize_results(self.results)["version_tag"])

    def test_mixed_revisions_fail_closed(self):
        self._publish(_result("case-one"), _result("case-two", git="c" * 40))
        summary = summarize_results(self.results)
        self.assertEqual("failed", summary["status"])
        self.assertTrue(
            any("more than one engine revision" in item for item in summary["exceptions"]),
            summary["exceptions"],
        )

    def test_a_foreign_harness_version_fails_closed(self):
        self._publish(_result("case-one", harness="99"))
        summary = summarize_results(self.results)
        self.assertEqual("failed", summary["status"])
        self.assertTrue(
            any("benchmark harness" in item for item in summary["exceptions"]),
            summary["exceptions"],
        )

    def test_consistency_baseline_engine_must_match_result_engine(self):
        self._publish(_result("case-one"))
        baseline_path = self.root / "foreign-engine-baseline.json"
        baseline_path.write_text(json.dumps(_baseline(engine_version="2.0.0rc3")), encoding="utf-8")
        summary = summarize_results(self.results, consistency_baseline=baseline_path)
        self.assertEqual("failed", summary["status"])
        self.assertTrue(
            any("baseline engine version" in item for item in summary["exceptions"]),
            summary["exceptions"],
        )

    def test_failed_cases_and_empty_result_sets_fail_closed(self):
        self._publish(_result("case-one", status="failed"))
        summary = summarize_results(self.results)
        self.assertEqual(["case-one"], summary["failed_cases"])
        self.assertEqual("failed", summary["status"])
        empty = self.root / "empty"
        empty.mkdir()
        blank = summarize_results(empty)
        self.assertEqual(0, blank["case_count"])
        self.assertEqual("failed", blank["status"])
        self.assertTrue(blank["exceptions"])

    def test_summary_repeats_record_limitations_and_never_claims_visual_quality(self):
        self._publish(_result("case-one"))
        summary = summarize_results(self.results)
        self.assertFalse(summary["proves_visual_quality"])
        self.assertIn("deterministic runs prove mechanics only", summary["limitations"])
        self.assertTrue(
            any("pools the numerators" in item for item in summary["limitations"]),
            summary["limitations"],
        )

    def test_visual_quality_is_claimed_only_when_every_record_claims_it(self):
        self._publish(
            _result("case-one", proves_visual_quality=True),
            _result("case-two", proves_visual_quality=True),
        )
        self.assertTrue(summarize_results(self.results)["proves_visual_quality"])
        write_result(_result("case-three"), self.results)
        self.assertFalse(summarize_results(self.results)["proves_visual_quality"])

    def test_consistency_planes_are_folded_in_but_never_gate(self):
        self._publish(_result("case-one"))
        summary = summarize_results(
            self.results, consistency_baseline=self._json(_baseline(), "baseline.json")
        )
        self.assertEqual("passed", summary["status"])
        self.assertEqual(
            1.0,
            summary["consistency"]["metrics"]["consistency_invariant_pinning"]["value"],
        )
        self.assertEqual(
            0.0,
            summary["consistency"]["metrics"]["consistency_visual_coverage"]["value"],
        )
        self.assertTrue(
            any("never gated" in item for item in summary["limitations"]),
            summary["limitations"],
        )

    def test_a_degraded_consistency_plane_still_reports_a_passing_pipeline(self):
        self._publish(_result("case-one"))
        summary = summarize_results(
            self.results,
            consistency_baseline=self._json(
                _baseline(
                    structural={
                        **_baseline()["structural"],
                        "invariant_pins": {"expected": 60, "recorded": 30},
                    }
                ),
                "degraded.json",
            ),
        )
        self.assertEqual("passed", summary["status"])
        self.assertEqual(
            0.5,
            summary["consistency"]["metrics"]["consistency_invariant_pinning"]["value"],
        )

    def test_a_broken_consistency_artifact_is_reported_not_swallowed(self):
        self._publish(_result("case-one"))
        summary = summarize_results(self.results, consistency_baseline=self.root / "missing.json")
        self.assertEqual("failed", summary["status"])
        self.assertIsNone(summary["consistency"])
        self.assertTrue(
            any("character consistency" in item for item in summary["exceptions"]),
            summary["exceptions"],
        )

    def test_markdown_names_the_version_tag_every_metric_and_every_case(self):
        self._publish(_result("case-one"))
        rendered = render_summary_markdown(
            summarize_results(
                self.results, consistency_baseline=self._json(_baseline(), "baseline.json")
            )
        )
        self.assertIn(f"v{RELEASE_VERSION}", rendered)
        for metric_id in METRIC_IDS:
            with self.subTest(metric=metric_id):
                self.assertIn(f"`{metric_id}`", rendered)
        self.assertIn("`case-one`", rendered)
        self.assertIn("never gated", rendered)
        self.assertIn("unscored", rendered)


class SummaryDeltaTests(TemporaryRootTestCase):
    def _summary(self, name, records, **options):
        results = self.root / f"{name}-results"
        for record in records:
            write_result(record, results)
        output = self.root / f"{name}-summary.json"
        write_summary(summarize_results(results, **options), output)
        return output

    def test_identical_summaries_report_no_regression(self):
        baseline = self._summary("baseline", [_result("case-one")])
        candidate = self._summary("candidate", [_result("case-one")])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("NO REGRESSION", delta["decision"])
        self.assertEqual("passed", delta["status"])
        self.assertEqual([], delta["regressions"])
        self.assertEqual("unchanged", delta["metrics"]["panel_acceptance"]["verdict"])
        self.assertEqual("passed", delta["cases"]["case-one"]["status"])
        self.assertIn("NO REGRESSION", (self.root / "delta.md").read_text(encoding="utf-8"))

    def test_case_metric_regression_is_not_hidden_by_equal_pooling(self):
        baseline = self._summary(
            "baseline",
            [
                _result("case-one", {"repair_rate": (0, 2)}),
                _result("case-two", {"repair_rate": (2, 2)}),
            ],
        )
        candidate = self._summary(
            "candidate",
            [
                _result("case-one", {"repair_rate": (2, 2)}),
                _result("case-two", {"repair_rate": (0, 2)}),
            ],
        )
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("REGRESSION", delta["decision"])
        self.assertIn("case-one/repair_rate", delta["regressions"])

    def test_pooled_regressions_are_highlighted_and_fail_closed(self):
        baseline = self._summary("baseline", [_result("case-one")])
        candidate = self._summary(
            "candidate",
            [
                _result(
                    "case-one",
                    {"panel_acceptance": (2, 3), "repair_rate": (3, 3)},
                    status="failed",
                )
            ],
        )
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("REGRESSION", delta["decision"])
        self.assertEqual("failed", delta["status"])
        self.assertIn("panel_acceptance", delta["regressions"])
        self.assertIn("repair_rate", delta["regressions"])
        self.assertIn("case-one/status", delta["regressions"])
        self.assertEqual(round(2 / 3 - 1, 6), delta["metrics"]["panel_acceptance"]["delta"])
        self.assertEqual("failed", delta["cases"]["case-one"]["status"])

    def test_improvements_are_reported_without_blocking(self):
        baseline = self._summary("baseline", [_result("case-one", {"repair_rate": (3, 3)})])
        candidate = self._summary("candidate", [_result("case-one")])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("passed", delta["status"])
        self.assertEqual(["case-one/repair_rate", "repair_rate"], delta["improvements"])

    def test_tolerance_absorbs_noise_but_not_real_regressions(self):
        baseline = self._summary("baseline", [_result("case-one", {"repair_rate": (0, 100)})])
        candidate = self._summary("candidate", [_result("case-one", {"repair_rate": (1, 100)})])
        absorbed = diff_summaries(baseline, candidate, self.root / "absorbed.json", tolerance=0.02)
        self.assertEqual("passed", absorbed["status"])
        self.assertEqual("unchanged", absorbed["metrics"]["dialogue_correctness"]["verdict"])
        strict = diff_summaries(baseline, candidate, self.root / "strict.json")
        self.assertEqual("failed", strict["status"])
        with self.assertRaises(ValueError):
            diff_summaries(baseline, candidate, self.root / "bad.json", tolerance=-1)
        with self.assertRaises(ValueError):
            diff_summaries(baseline, candidate, self.root / "nan.json", tolerance=float("nan"))

    def test_different_case_digests_are_not_comparable(self):
        baseline = self._summary("baseline", [_result("case-one", case_sha256="b" * 64)])
        candidate = self._summary("candidate", [_result("case-one", case_sha256="c" * 64)])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("failed", delta["status"])
        self.assertIn("case-one/case_sha256", delta["regressions"])

    def test_failed_summary_status_and_exceptions_block_delta(self):
        baseline = self._summary("baseline", [_result("case-one")])
        candidate = self._summary("candidate", [_result("case-one", harness="foreign-harness")])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("failed", delta["status"])
        self.assertTrue(any("candidate" in item for item in delta["exceptions"]))

    def test_different_engine_revisions_are_comparable(self):
        baseline = self._summary("baseline", [_result("case-one", engine="2.0.0rc3")])
        candidate = self._summary("candidate", [_result("case-one", engine=RELEASE_VERSION)])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("NO REGRESSION", delta["decision"])
        self.assertEqual("passed", delta["status"])

    def test_corrupt_archived_metric_is_rejected(self):
        summary_path = self._summary("baseline", [_result("case-one")])
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        payload["metrics"]["panel_acceptance"]["value"] = 0.25
        summary_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "panel_acceptance.value"):
            load_summary(summary_path)

    def test_differing_case_sets_are_not_comparable(self):
        baseline = self._summary("baseline", [_result("case-one"), _result("case-two")])
        candidate = self._summary("candidate", [_result("case-one"), _result("case-three")])
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("failed", delta["status"])
        self.assertEqual(["case-two"], delta["missing_cases"])
        self.assertEqual(["case-three"], delta["new_cases"])
        self.assertTrue(
            any("different benchmark cases" in item for item in delta["exceptions"]),
            delta["exceptions"],
        )

    def test_unreadable_and_foreign_summaries_are_reported_not_ignored(self):
        baseline = self._summary("baseline", [_result("case-one")])
        foreign = self.root / "foreign.json"
        foreign.write_text(json.dumps({"kind": "other"}), encoding="utf-8")
        delta = diff_summaries(baseline, foreign, self.root / "delta.json")
        self.assertEqual("failed", delta["status"])
        self.assertEqual({}, delta["metrics"])
        self.assertTrue(
            any(item.startswith("candidate:") for item in delta["exceptions"]),
            delta["exceptions"],
        )
        missing = diff_summaries(self.root / "absent.json", baseline, self.root / "missing.json")
        self.assertEqual("failed", missing["status"])
        self.assertTrue(
            any(item.startswith("baseline:") for item in missing["exceptions"]),
            missing["exceptions"],
        )

    def test_consistency_verdicts_are_advisory_and_never_gate(self):
        baseline = self._summary(
            "baseline",
            [_result("case-one")],
            consistency_scorecard=self._json(_scorecard({"face": 4, "hair": 4}), "good.json"),
        )
        candidate = self._summary(
            "candidate",
            [_result("case-one")],
            consistency_scorecard=self._json(_scorecard({"face": 1, "hair": 1}), "drifted.json"),
        )
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual(
            "passed",
            delta["status"],
            "a reviewer judgement never gates a deterministic benchmark delta",
        )
        self.assertEqual([], delta["regressions"])
        self.assertIn("consistency_visual_score/regressed", delta["advisory"])
        self.assertEqual(
            "regressed",
            delta["consistency"]["metrics"]["consistency_visual_score"]["verdict"],
        )
        self.assertIn("advisory", (self.root / "delta.md").read_text(encoding="utf-8").lower())

    def test_one_sided_consistency_metrics_are_reported_not_fatal(self):
        baseline = self._summary("baseline", [_result("case-one")])
        candidate = self._summary(
            "candidate",
            [_result("case-one")],
            consistency_baseline=self._json(_baseline(), "plane.json"),
        )
        delta = diff_summaries(baseline, candidate, self.root / "delta.json")
        self.assertEqual("passed", delta["status"])
        self.assertEqual([], delta["exceptions"])
        self.assertIn("consistency_invariant_pinning", delta["consistency"]["unavailable"])
        self.assertIn("consistency_invariant_pinning/unavailable", delta["advisory"])


class SummaryCommandTests(TemporaryRootTestCase):
    def test_summarize_and_compare_report_status_through_the_exit_code(self):
        results = self.root / "results"
        write_result(_result("case-one"), results)
        summary_path = self.root / "summary.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(["--results", str(results), "--output", str(summary_path)]))
        self.assertEqual("passed", load_summary(summary_path)["status"])
        self.assertTrue((self.root / "summary.md").is_file())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                main(
                    [
                        "--baseline",
                        str(summary_path),
                        "--candidate",
                        str(summary_path),
                        "--output",
                        str(self.root / "delta.json"),
                    ]
                ),
            )

    def test_a_failed_summary_still_publishes_machine_readable_evidence(self):
        results = self.root / "results"
        write_result(_result("case-one", status="failed"), results)
        output = self.root / "summary.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, main(["--results", str(results), "--output", str(output)]))
        self.assertEqual("failed", load_summary(output)["status"])
        self.assertIn("case-one", output.read_text(encoding="utf-8"))

    def test_command_refuses_incomplete_and_conflicting_invocations(self):
        for argv in (
            ["--output", str(self.root / "summary.json")],
            ["--results", str(self.root)],
            [
                "--baseline",
                str(self.root / "a.json"),
                "--output",
                str(self.root / "delta.json"),
            ],
            [
                "--results",
                str(self.root),
                "--baseline",
                str(self.root / "a.json"),
                "--candidate",
                str(self.root / "b.json"),
                "--output",
                str(self.root / "delta.json"),
            ],
        ):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(argv)


class SummaryIntegrationTests(unittest.TestCase):
    def test_summary_tool_is_a_repository_gate_not_a_shipped_script(self):
        from comic_sol_product.release import FORBIDDEN_WHEEL_MEMBERS
        from scripts.sync_plugin_bundle import BUNDLED_SCRIPTS

        self.assertNotIn("benchmark_summary.py", BUNDLED_SCRIPTS)
        self.assertFalse((ROOT / "skills/comic-sol/scripts/benchmark_summary.py").exists())
        self.assertIn('"benchmark_summary.py"', (ROOT / "setup.py").read_text(encoding="utf-8"))
        self.assertIn("comic_sol_product/engine/benchmark_summary.py", FORBIDDEN_WHEEL_MEMBERS)

    def test_gating_metrics_are_every_metric_except_the_repair_rate(self):
        self.assertEqual(set(METRIC_IDS) - {"repair_rate"}, set(GATING_METRIC_IDS))

    def test_docs_describe_the_summary_the_delta_and_the_consistency_planes(self):
        documentation = (ROOT / "docs/benchmark.md").read_text(encoding="utf-8")
        for token in (
            "scripts/benchmark_summary.py",
            "--results",
            "--consistency-baseline",
            "--consistency-scorecard",
            "version_tag",
            "NO REGRESSION",
            *CONSISTENCY_METRIC_IDS,
        ):
            with self.subTest(token=token):
                self.assertIn(token, documentation)

    def test_docs_publish_the_ci_wiring_for_both_summaries_and_the_delta(self):
        documentation = (ROOT / "docs/benchmark.md").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/benchmark.yml").read_text(encoding="utf-8")
        for token in (
            ".github/workflows/benchmark.yml",
            "benchmark/candidate-summary.json",
            "benchmark/baseline-summary.json",
            "benchmark/summary-delta.json",
            "GITHUB_STEP_SUMMARY",
        ):
            with self.subTest(token=token, source="documentation"):
                self.assertIn(token, documentation)
        for token in (
            "name: Summarize the candidate revision",
            "name: Summarize the baseline revision",
            "python scripts/benchmark_summary.py",
            "--results benchmark/candidate",
            "--results benchmark/baseline",
            "--output benchmark/candidate-summary.json",
            "--output benchmark/baseline-summary.json",
            "--baseline benchmark/baseline-summary.json",
            "--candidate benchmark/candidate-summary.json",
            "--output benchmark/summary-delta.json",
            "benchmark/candidate-summary.md",
            "benchmark/baseline-summary.md",
            "benchmark/summary-delta.md",
            'cat "${report}" >> "$GITHUB_STEP_SUMMARY"',
        ):
            with self.subTest(token=token, source="workflow"):
                self.assertIn(token, workflow)


if __name__ == "__main__":
    unittest.main()
