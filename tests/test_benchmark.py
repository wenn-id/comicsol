"""Coverage for the versioned Comic Sol benchmark harness."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.benchmark import (  # noqa: E402
    CASES_ROOT,
    METRIC_DIRECTIONS,
    METRIC_IDS,
    diff_results,
    discover_cases,
    load_case,
    load_results,
    main,
    _attempt_payload,
    _compare_metric,
    _export_verified,
    _metric,
    _reference_raster,
    _storyboard_panels,
    panel_raster_size,
    run_case,
    synthesize_panel_raster,
    tail_direction_result,
    validate_case,
    write_result,
)

MINI_COMIC = CASES_ROOT / "mini-comic.json"


def _minimal_result(case_id, values, *, status="passed", revision="a" * 40):
    """Build a synthetic benchmark result record for diff coverage."""
    return {
        "case_id": case_id,
        "case_sha256": "b" * 64,
        "harness_version": "1",
        "kind": "benchmark-result",
        "metrics": {
            metric_id: {
                "denominator": 1,
                "direction": METRIC_DIRECTIONS[metric_id],
                "numerator": values[metric_id],
                "unit": "ratio",
                "value": values[metric_id],
            }
            for metric_id in METRIC_IDS
        },
        "revision": {"engine_version": "0.0.0", "git_revision": revision},
        "schema_version": "1.0",
        "status": status,
    }


PERFECT = {metric_id: 0.0 if metric_id == "repair_rate" else 1.0 for metric_id in METRIC_IDS}


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_registered_cases_satisfy_the_benchmark_project_contract(self):
        cases = discover_cases()
        self.assertTrue(cases, "at least one benchmark case must be registered")
        for path in cases:
            with self.subTest(case=path.name):
                case = load_case(path)
                self.assertEqual((), validate_case(case))
                self.assertEqual(path.stem, case["case_id"])

    def test_contract_rejects_incomplete_and_inconsistent_cases(self):
        case = load_case(MINI_COMIC)
        self.assertIn("case-missing-field", validate_case(
            {key: value for key, value in case.items() if key != "seed"}
        ))
        self.assertIn("case-unknown-field", validate_case({**case, "extra": 1}))
        self.assertIn("case-schema-version", validate_case({**case, "schema_version": "9.9"}))
        self.assertIn("case-kind", validate_case({**case, "kind": "something-else"}))
        self.assertIn("case-evidence-mode", validate_case({**case, "evidence_mode": "vibes"}))
        self.assertIn("case-resume-stage", validate_case({**case, "resume_stage": "planning"}))
        self.assertIn("case-panels", validate_case({**case, "panels": ["nope"]}))
        self.assertIn(
            "case-repair-panels",
            validate_case({**case, "repair_panels": ["p09-09"]}),
        )
        self.assertIn("case-seed", validate_case({**case, "seed": -1}))
        self.assertIn("case-structure", validate_case(["not", "an", "object"]))

    def test_contract_requires_a_complete_local_fixture(self):
        self.assertIn(
            "case-fixture-missing",
            validate_case({**load_case(MINI_COMIC), "fixture": "does/not/exist"}),
        )
        fixture = self.root / "partial"
        (fixture / "source").mkdir(parents=True)
        (fixture / "source/input.txt").write_text("story\n", encoding="utf-8")
        self.assertIn(
            "case-fixture-incomplete",
            validate_case(
                {**load_case(MINI_COMIC), "fixture": "partial"}, fixture_root=self.root
            ),
        )

    def test_contract_requires_fixture_page_count_to_match(self):
        case = load_case(MINI_COMIC)
        self.assertIn("case-page-count", validate_case({**case, "page_count": 2}))

    def test_contract_refuses_a_fixture_outside_the_fixture_root(self):
        nested = self.root / "nested"
        nested.mkdir()
        self.assertIn(
            "case-fixture-missing",
            validate_case({**load_case(MINI_COMIC), "fixture": ".."}, fixture_root=nested),
        )

    def test_load_case_fails_closed_on_an_invalid_contract(self):
        path = self.root / "broken.json"
        path.write_text(json.dumps({"kind": "benchmark-case"}), encoding="utf-8")
        with self.assertRaises(ValueError) as context:
            load_case(path)
        self.assertIn("case-missing-field", str(context.exception))


class BenchmarkPrimitiveTests(unittest.TestCase):
    def test_live_retry_requires_revision_specific_raster(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "p01-01.png").write_bytes(
                synthesize_panel_raster(1, "p01-01", 0, (512, 512))
            )
            case = {"evidence_mode": "live-visual"}
            with self.assertRaises(FileNotFoundError):
                _attempt_payload(case, "p01-01", 1, (512, 512), attempt_root=root)

    def test_dialogue_correctness_is_vacuously_successful_without_dialogue(self):
        metric = _metric("dialogue_correctness", 0, 0)
        self.assertEqual(1.0, metric["value"])

    def test_reference_rasters_are_reproducible_across_processes(self):
        code = (
            "from scripts.benchmark import _reference_raster; "
            "import hashlib; "
            "print(hashlib.sha256(_reference_raster(1, 'hero')).hexdigest())"
        )
        environment = {**os.environ, "PYTHONHASHSEED": "random"}
        first = subprocess.check_output([sys.executable, "-c", code], env=environment, text=True)
        second = subprocess.check_output([sys.executable, "-c", code], env=environment, text=True)
        self.assertEqual(first, second)
        self.assertEqual(
            first.strip(), hashlib.sha256(_reference_raster(1, "hero")).hexdigest()
        )

    def test_metric_comparison_rejects_non_finite_and_boolean_values(self):
        for invalid in (True, float("nan"), float("inf"), -0.1):
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    _compare_metric(
                        {"x": {"value": invalid}},
                        {"x": {"value": 1.0}},
                        "pipeline_success",
                        0.0,
                    )
                )

    def test_synthesized_rasters_are_seeded_and_reproducible(self):
        size = (736, 1136)
        first = synthesize_panel_raster(1, "p01-01", 0, size)
        self.assertEqual(first, synthesize_panel_raster(1, "p01-01", 0, size))
        self.assertNotEqual(first, synthesize_panel_raster(2, "p01-01", 0, size))
        self.assertNotEqual(first, synthesize_panel_raster(1, "p01-01", 1, size))
        self.assertNotEqual(first, synthesize_panel_raster(1, "p01-02", 0, size))

    def test_synthesized_rasters_refuse_sizes_the_engine_rejects(self):
        with self.assertRaises(ValueError):
            synthesize_panel_raster(1, "p01-01", 0, (511, 1136))

    def test_raster_size_preserves_the_storyboard_aspect_ratio(self):
        for rect, expected in (
            ({"width": 1472, "height": 2272}, (736, 1136)),
            ({"width": 1472, "height": 1120}, (736, 560)),
            ({"width": 1000, "height": 1000}, (1000, 1000)),
        ):
            with self.subTest(rect=rect):
                width, height = panel_raster_size(rect)
                self.assertEqual(expected, (width, height))
                self.assertAlmostEqual(
                    rect["width"] / rect["height"], width / height, places=6
                )
                self.assertGreaterEqual(min(width, height), 512)

    def test_raster_size_rejects_an_invalid_rectangle(self):
        with self.assertRaises(ValueError):
            panel_raster_size({"width": 0, "height": 10})

    def test_tail_verdict_requires_an_aligned_contained_tail(self):
        tail = {
            "attachment": [100.0, 100.0],
            "tip": [140.0, 100.0],
            "source_gap": 20.0,
        }
        self.assertEqual("pass", tail_direction_result(tail, [0.5, 0.1], 1000, 1000))
        self.assertEqual(
            "fail", tail_direction_result(tail, [0.02, 0.1], 1000, 1000),
            "a tail pointing away from the speaker must fail",
        )
        self.assertEqual(
            "fail",
            tail_direction_result({**tail, "source_gap": 0.0}, [0.5, 0.1], 1000, 1000),
            "a tail that reaches its speaker leaves no source gap",
        )
        self.assertEqual(
            "fail",
            tail_direction_result({**tail, "tip": [1400.0, 100.0]}, [1.0, 0.1], 1000, 1000),
            "a tail tip outside the panel must fail",
        )
        self.assertEqual("fail", tail_direction_result(tail, "not-an-anchor", 1000, 1000))


class BenchmarkDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.baseline = self.root / "baseline"
        self.candidate = self.root / "candidate"
        self.addCleanup(self.temporary_directory.cleanup)

    def _publish(self, directory, record):
        write_result(record, directory)

    def test_identical_revisions_report_no_regression(self):
        for directory in (self.baseline, self.candidate):
            self._publish(directory, _minimal_result("case-one", PERFECT))
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual("NO REGRESSION", result["decision"])
        self.assertEqual("passed", result["status"])
        self.assertEqual([], result["regressions"])
        self.assertEqual("unchanged", result["cases"]["case-one"]["metrics"]["export_success"]["verdict"])
        self.assertTrue((self.root / "diff.md").is_file())
        self.assertIn("NO REGRESSION", (self.root / "diff.md").read_text(encoding="utf-8"))

    def test_lower_quality_metrics_regress_and_fail_closed(self):
        self._publish(self.baseline, _minimal_result("case-one", PERFECT))
        degraded = {**PERFECT, "panel_acceptance": 0.5, "repair_rate": 0.75}
        self._publish(
            self.candidate,
            _minimal_result("case-one", degraded, status="failed", revision="c" * 40),
        )
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual("REGRESSION", result["decision"])
        self.assertEqual("failed", result["status"])
        metrics = result["cases"]["case-one"]["metrics"]
        self.assertEqual("regressed", metrics["panel_acceptance"]["verdict"])
        self.assertEqual(-0.5, metrics["panel_acceptance"]["delta"])
        self.assertEqual("regressed", metrics["repair_rate"]["verdict"])
        self.assertIn("case-one/status", result["regressions"])
        self.assertEqual("failed", result["cases"]["case-one"]["status"])
        self.assertNotEqual(
            result["baseline_revisions"], result["candidate_revisions"],
            "a two-revision diff must record both engine revisions",
        )

    def test_improvements_are_reported_without_blocking(self):
        self._publish(
            self.baseline, _minimal_result("case-one", {**PERFECT, "repair_rate": 0.5})
        )
        self._publish(self.candidate, _minimal_result("case-one", PERFECT))
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual("passed", result["status"])
        self.assertEqual(["case-one/repair_rate"], result["improvements"])

    def test_tolerance_absorbs_noise_but_not_real_regressions(self):
        self._publish(self.baseline, _minimal_result("case-one", PERFECT))
        self._publish(
            self.candidate, _minimal_result("case-one", {**PERFECT, "dialogue_correctness": 0.99})
        )
        absorbed = diff_results(
            self.baseline, self.candidate, self.root / "absorbed.json", tolerance=0.02
        )
        self.assertEqual("passed", absorbed["status"])
        self.assertEqual(
            "unchanged", absorbed["cases"]["case-one"]["metrics"]["dialogue_correctness"]["verdict"]
        )
        strict = diff_results(self.baseline, self.candidate, self.root / "strict.json")
        self.assertEqual("failed", strict["status"])
        with self.assertRaises(ValueError):
            diff_results(self.baseline, self.candidate, self.root / "bad.json", tolerance=-1)

    def test_missing_cases_and_changed_contracts_block_the_diff(self):
        self._publish(self.baseline, _minimal_result("case-one", PERFECT))
        self._publish(self.baseline, _minimal_result("case-two", PERFECT))
        changed = _minimal_result("case-one", PERFECT)
        changed["case_sha256"] = "d" * 64
        self._publish(self.candidate, changed)
        self._publish(self.candidate, _minimal_result("case-three", PERFECT))
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual("failed", result["status"])
        self.assertEqual(["case-two"], result["missing_cases"])
        self.assertEqual(["case-three"], result["new_cases"])
        self.assertTrue(
            any("contract changed" in item for item in result["exceptions"]),
            result["exceptions"],
        )

    def test_candidate_only_cases_block_the_diff(self):
        self._publish(self.baseline, _minimal_result("case-one", PERFECT))
        self._publish(self.candidate, _minimal_result("case-one", PERFECT))
        self._publish(self.candidate, _minimal_result("case-two", PERFECT))
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual(["case-two"], result["new_cases"])
        self.assertEqual("failed", result["status"])

    def test_malformed_result_metrics_are_rejected_fail_closed(self):
        self._publish(self.baseline, _minimal_result("case-one", PERFECT))
        malformed = _minimal_result("case-one", PERFECT)
        malformed["metrics"]["pipeline_success"]["value"] = float("nan")
        self._publish(self.candidate, malformed)
        result = diff_results(self.baseline, self.candidate, self.root / "diff.json")
        self.assertEqual("failed", result["status"])
        self.assertTrue(any("invalid benchmark result" in item for item in result["exceptions"]))

    def test_unreadable_and_foreign_results_are_reported_not_ignored(self):
        self.baseline.mkdir(parents=True)
        (self.baseline / "result-broken.json").write_text("{not json", encoding="utf-8")
        (self.baseline / "result-foreign.json").write_text('{"kind":"other"}', encoding="utf-8")
        records, exceptions = load_results(self.baseline)
        self.assertEqual({}, records)
        self.assertEqual(2, len(exceptions))
        self.candidate.mkdir(parents=True)
        _, empty = load_results(self.candidate)
        self.assertEqual(1, len(empty))


class BenchmarkRunTests(unittest.TestCase):
    """End-to-end deterministic benchmark execution against the real engine."""

    @classmethod
    def setUpClass(cls):
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary_directory.name)
        cls.case = load_case(MINI_COMIC)
        cls.first = run_case(cls.case, output_root=cls.root / "first")

    @classmethod
    def tearDownClass(cls):
        cls.temporary_directory.cleanup()

    def test_deterministic_run_reports_every_required_metric(self):
        self.assertEqual(set(METRIC_IDS), set(self.first["metrics"]))
        for metric_id, metric in self.first["metrics"].items():
            with self.subTest(metric=metric_id):
                self.assertEqual(METRIC_DIRECTIONS[metric_id], metric["direction"])
                self.assertEqual("ratio", metric["unit"])
                self.assertIsInstance(metric["value"], float)

    def test_deterministic_run_passes_the_whole_pipeline_contract(self):
        self.assertEqual("passed", self.first["status"])
        self.assertEqual([], self.first["observations"]["final_validation_issues"])
        self.assertEqual(
            "COMPLETE_WITH_WARNINGS", self.first["observations"]["terminal_status"]
        )
        for metric_id in (
            "pipeline_success", "resume_success", "panel_acceptance",
            "dialogue_correctness", "export_success",
        ):
            with self.subTest(metric=metric_id):
                self.assertEqual(1.0, self.first["metrics"][metric_id]["value"])
        self.assertEqual(0.0, self.first["metrics"]["repair_rate"]["value"])
        self.assertGreater(self.first["metrics"]["dialogue_correctness"]["denominator"], 0)

    def test_resume_drill_preserves_upstream_stages_and_refinalizes(self):
        resume = self.first["observations"]["resume"]
        self.assertEqual("composition", resume["stage"])
        self.assertEqual(
            ["planning", "storyboard", "generation", "lettering"], resume["preserved"]
        )
        self.assertEqual(["composition", "export"], resume["invalidated"])
        self.assertEqual("COMPLETE_WITH_WARNINGS", resume["refinalized_status"])
        self.assertTrue(resume["succeeded"])

    def test_deterministic_run_never_claims_visual_quality(self):
        self.assertEqual("deterministic", self.first["evidence"]["mode"])
        self.assertFalse(self.first["evidence"]["proves_visual_quality"])
        for panel_id, evidence in self.first["evidence"]["panels"].items():
            with self.subTest(panel=panel_id):
                self.assertFalse(evidence["proves_visual_quality"])
                self.assertEqual("mechanics-only", evidence["scope"])
        self.assertTrue(self.first["limitations"])

    def test_results_are_versioned_and_bound_to_their_contract(self):
        self.assertEqual("benchmark-result", self.first["kind"])
        self.assertEqual("1.0", self.first["schema_version"])
        self.assertEqual("mini-comic", self.first["case_id"])
        self.assertEqual(64, len(self.first["case_sha256"]))
        self.assertEqual(self.case["seed"], self.first["seed"])
        revision = self.first["revision"]
        self.assertIn("engine_version", revision)
        self.assertIn("git_revision", revision)
        self.assertEqual("1", revision["harness_version"])
        self.assertEqual(
            {"composition", "export", "generation", "lettering", "planning", "storyboard"},
            set(revision["stage_versions"]),
        )

    def test_repeated_deterministic_runs_are_byte_comparable(self):
        second = run_case(self.case, output_root=self.root / "second")
        first_path = write_result(self.first, self.root / "results-first")
        second_path = write_result(second, self.root / "results-second")
        self.assertEqual(
            first_path.read_bytes(), second_path.read_bytes(),
            "repeated deterministic benchmark runs must produce identical records",
        )
        result = diff_results(
            self.root / "results-first",
            self.root / "results-second",
            self.root / "diff.json",
        )
        self.assertEqual("NO REGRESSION", result["decision"])

    def test_live_visual_runs_require_explicit_provenance(self):
        with self.assertRaises(ValueError) as context:
            run_case(
                {**self.case, "evidence_mode": "live-visual"},
                output_root=self.root / "live",
            )
        self.assertIn("live-visual", str(context.exception))

    def test_live_evidence_binds_to_the_promoted_repair_attempt(self):
        case = load_case(CASES_ROOT / "sunlight-courier.json")
        fixture = ROOT / case["fixture"]
        storyboard = json.loads(
            (fixture / "plan/storyboard.json").read_text(encoding="utf-8")
        )
        panels = _storyboard_panels(storyboard)
        live_case = {**case, "evidence_mode": "live-visual"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            attempt_root = Path(temporary_directory) / "attempts"
            attempt_root.mkdir()
            for panel_id in case["panels"]:
                size = panel_raster_size(panels[panel_id]["rect"])
                (attempt_root / f"{panel_id}-0.png").write_bytes(
                    synthesize_panel_raster(7, panel_id, 0, size)
                )
                if panel_id in case["repair_panels"]:
                    (attempt_root / f"{panel_id}-1.png").write_bytes(
                        synthesize_panel_raster(7, panel_id, 1, size)
                    )
            result = run_case(
                live_case,
                output_root=Path(temporary_directory) / "project",
                attempt_root=attempt_root,
                provider="provider",
                model="model",
                reviewer_method="bounded review",
            )
        evidence = result["evidence"]["panels"]["p01-02"]
        self.assertEqual(
            "panels/attempts/p01-02/visual_retry-1.png",
            evidence["retained_attempt"],
        )
        self.assertNotIn("\\", evidence["retained_attempt"])

    def test_export_verification_rejects_malformed_page_records(self):
        project = self.root / "first/mini-comic-benchmark"
        verification_path = project / "exports/pdf-verification.json"
        original = verification_path.read_bytes()
        try:
            verification = json.loads(original)
            verification["pages"][0]["page_number"] = 2
            verification_path.write_text(json.dumps(verification), encoding="utf-8")
            self.assertEqual((0, 1), _export_verified(project, self.case))
        finally:
            verification_path.write_bytes(original)


class BenchmarkCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_list_reports_registered_cases_and_metrics(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(0, main(["--list"]))
        listed = json.loads(stream.getvalue())
        self.assertIn("mini-comic.json", listed["cases"])
        self.assertEqual(list(METRIC_IDS), listed["metrics"])

    def test_failed_case_still_publishes_machine_readable_evidence(self):
        case_path = self.root / "cases/unusable.json"
        case_path.parent.mkdir(parents=True)
        case = load_case(MINI_COMIC)
        case_path.write_text(
            json.dumps({**case, "fixture": "does/not/exist"}), encoding="utf-8"
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                1,
                main([
                    "--case", str(case_path),
                    "--output-root", str(self.root / "projects"),
                    "--results", str(self.root / "results"),
                ]),
            )
        published = self.root / "results/result-unusable.json"
        self.assertTrue(published.is_file())
        record = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual("failed", record["status"])
        self.assertEqual("benchmark-result", record["kind"])
        self.assertTrue(record["exceptions"])

    def test_diff_arguments_must_be_supplied_together(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--baseline", str(self.root)])

    def test_run_requires_an_output_root_and_results_directory(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--case", str(MINI_COMIC)])


class BenchmarkIntegrationTests(unittest.TestCase):
    def test_benchmark_harness_exposes_the_documented_interfaces(self):
        source = (ROOT / "scripts/benchmark.py").read_text(encoding="utf-8")
        for token in (
            "def run_case(",
            "def diff_results(",
            "def validate_case(",
            "def write_result(",
            "--baseline",
            "--candidate",
            "--diff-output",
            "--tolerance",
            "--attempt-root",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_docs_describe_the_contract_metrics_and_revision_diff(self):
        documentation = (ROOT / "docs/benchmark.md").read_text(encoding="utf-8")
        for token in (
            "benchmarks/cases",
            "scripts/benchmark.py",
            "--diff-output",
            "proves_visual_quality",
            *METRIC_IDS,
        ):
            with self.subTest(token=token):
                self.assertIn(token, documentation)

    def test_workflow_runs_two_revisions_and_gates_on_the_diff(self):
        workflow = (ROOT / ".github/workflows/benchmark.yml").read_text(encoding="utf-8")
        for token in (
            "scripts/benchmark.py",
            "--all",
            "--baseline",
            "--candidate",
            "--diff-output",
            "if-no-files-found: error",
            "NO REGRESSION",
        ):
            with self.subTest(token=token):
                self.assertIn(token, workflow)

    def test_benchmark_harness_is_not_shipped_in_the_plugin_bundle(self):
        from scripts.sync_plugin_bundle import BUNDLED_SCRIPTS

        self.assertNotIn(
            "benchmark.py", BUNDLED_SCRIPTS,
            "the benchmark harness is a repository gate, not a bundled runtime script",
        )
        self.assertFalse((ROOT / "skills/comic-sol/scripts/benchmark.py").exists())

    def test_case_fixtures_stay_inside_the_repository(self):
        for path in discover_cases():
            case = load_case(path)
            fixture = (ROOT / case["fixture"]).resolve()
            with self.subTest(case=case["case_id"]):
                self.assertTrue(fixture.is_dir())
                self.assertIn(ROOT, fixture.parents)


if __name__ == "__main__":
    unittest.main()
