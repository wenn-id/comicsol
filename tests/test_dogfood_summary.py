"""Contract coverage for deterministic, offline dogfood report aggregation."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core_primitives import canonical_artifact_bytes
from scripts.dogfood_report import canonical_report_bytes
from scripts.dogfood_summary import (
    COLLECTION_LIMITATIONS,
    MAX_REPORT_INPUTS,
    DogfoodSummaryError,
    aggregate_reports,
    canonical_summary_bytes,
    load_reports,
    main,
    render_markdown,
    write_summary,
)
from scripts.project_io import durable_atomic_write
from scripts.input_limits import MAX_JSON_BYTES


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests/fixtures/dogfood/summary-input"
FIXTURES = tuple(sorted(FIXTURE_ROOT.glob("report-*.json")))

EXPECTED_SUMMARY = {
    "evidence_planes": {
        "deterministic_mechanics": {
            "included": False,
            "label": "deterministic mechanics",
            "statement": (
                "Separate deterministic benchmark evidence; never combined with this "
                "dogfood aggregate."
            ),
        },
        "opt_in_creator_adoption_evidence": {
            "included": True,
            "label": "opt-in creator adoption evidence",
            "statement": (
                "Valid consented local reports are aggregated here; deterministic fixtures "
                "do not prove adoption."
            ),
        },
        "retained_live_visual_evidence": {
            "included": False,
            "label": "retained live visual evidence",
            "statement": (
                "Separate retained-render evidence; no visual-quality claim is derived from "
                "these reports."
            ),
        },
    },
    "kind": "comic-sol-dogfood-summary",
    "limitations": list(COLLECTION_LIMITATIONS),
    "metrics": {
        "blocked_category_counts": {
            "image-capability-unavailable": 1,
            "other": 1,
            "provider-refusal": 1,
        },
        "blocked_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
        "completion_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
        "durations": {
            "first_project": {"median_minutes": 30, "missing": 0, "observed": 3},
            "setup": {"median_minutes": 20, "missing": 0, "observed": 3},
            "verified_pdf": {"median_minutes": 60, "missing": 0, "observed": 3},
        },
        "friction_category_counts": {
            "blocked-recovery": 1,
            "filesystem": 1,
            "handoff": 1,
            "installation": 1,
            "setup": 1,
        },
        "handoff_completion_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
        "handoff_use_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
        "manual_filesystem_intervention_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 1,
            "value": 0.333333,
        },
        "resume_outcomes": {
            "committed_successful_resumes": 3,
            "self_reported_failed_resume_attempts": {
                "missing_reports": 1,
                "observed_reports": 2,
                "total": 3,
            },
            "success_rate": {
                "denominator": 6,
                "missing": 1,
                "numerator": 3,
                "value": 0.5,
            },
        },
        "retries_per_panel": {
            "denominator": 8,
            "missing": 0,
            "numerator": 3,
            "value": 0.375,
        },
        "retry_rate": {
            "denominator": 10,
            "missing": 0,
            "numerator": 3,
            "value": 0.3,
        },
        "verified_pdf_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
        "would_use_again_rate": {
            "denominator": 3,
            "missing": 0,
            "numerator": 2,
            "value": 0.666667,
        },
    },
    "report_schema_version": "1.0",
    "sample": {
        "collection_window": "below-target-window",
        "gate_status": "insufficient-evidence",
        "missing_cohort_aliases": 1,
        "observed_cohort_aliases": 2,
        "reports": 3,
        "target": {
            "maximum_reports": 50,
            "minimum_observed_cohort_aliases": 10,
            "minimum_reports": 20,
        },
        "verification_responsibility": (
            "Observed aliases are anonymous cohort labels; external creator identity "
            "verification remains a maintainer responsibility."
        ),
    },
    "schema_version": "1.0",
}

EXPECTED_MARKDOWN = """# Comic Sol dogfood summary

## Evidence planes

- **Deterministic mechanics:** Separate deterministic benchmark evidence; never combined with this dogfood aggregate.
- **Retained live visual evidence:** Separate retained-render evidence; no visual-quality claim is derived from these reports.
- **Opt-in creator adoption evidence:** Valid consented local reports are aggregated here; deterministic fixtures do not prove adoption.

## Sample and collection limitations

- Valid consented reports: **3**
- Observed anonymous cohort aliases: **2**
- Reports with no cohort alias: **1**
- Gate status: `insufficient-evidence`
- Collection window: `below-target-window`
- Target: at least 10 distinct observed aliases and 20–50 reports.
- Observed aliases do not verify external creator identity; verification remains a maintainer responsibility.

## Rates

| Metric | Numerator | Denominator | Missing | Value |
| --- | ---: | ---: | ---: | ---: |
| Completion | 2 | 3 | 0 | 0.666667 |
| Verified PDF | 2 | 3 | 0 | 0.666667 |
| Blocked | 2 | 3 | 0 | 0.666667 |
| Resume success (committed / committed + self-reported failed) | 3 | 6 | 1 | 0.5 |
| Retry attempts | 3 | 10 | 0 | 0.3 |
| Retries per panel | 3 | 8 | 0 | 0.375 |
| Handoff use | 2 | 3 | 0 | 0.666667 |
| Handoff completion | 2 | 3 | 0 | 0.666667 |
| Manual filesystem intervention | 1 | 3 | 0 | 0.333333 |
| Would use again | 2 | 3 | 0 | 0.666667 |

## Resume evidence

- Committed successful resumes (engine-derived): **3**
- Self-reported failed resume attempts: **3** across **2** observed reports; **1** reports missing.

## Categories

### Blocked categories

- `image-capability-unavailable`: 1
- `other`: 1
- `provider-refusal`: 1

### Friction categories

- `blocked-recovery`: 1
- `filesystem`: 1
- `handoff`: 1
- `installation`: 1
- `setup`: 1

## Durations

| Duration | Observed | Missing | Median minutes |
| --- | ---: | ---: | ---: |
| Setup | 3 | 0 | 20 |
| First project | 3 | 0 | 30 |
| Verified PDF | 3 | 0 | 60 |

## Limitations

- Aggregates only valid, explicitly consented comic-sol-dogfood-report schema 1.0 files supplied locally; no report is uploaded.
- Observed cohort aliases are anonymous labels, not verified external creator identities.
- Selection, recruitment, self-reporting, and missing-data bias are not corrected by this deterministic aggregate.
- Dogfood evidence does not prove adoption or visual quality and is never combined with deterministic benchmark results.
"""


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class DogfoodSummaryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def _write(self, report: dict[str, object], name: str) -> Path:
        path = self.root / name
        path.write_bytes(canonical_artifact_bytes(report))
        return path

    def _reports(self) -> list[dict[str, object]]:
        return [_read(path) for path in FIXTURES]

    def _cohort(self, count: int, distinct_aliases: int) -> list[Path]:
        template = self._reports()[2]
        paths = []
        for index in range(count):
            report = deepcopy(template)
            report["creator"]["setup_minutes"] = index
            report["creator"]["cohort_alias"] = (
                f"cohort-{index:02d}" if index < distinct_aliases else None
            )
            paths.append(self._write(report, f"cohort-{index:03d}.json"))
        return paths

    def test_exact_json_and_markdown_output(self) -> None:
        summary = aggregate_reports(FIXTURES)
        self.assertEqual(EXPECTED_SUMMARY, summary)
        self.assertEqual(
            canonical_artifact_bytes(EXPECTED_SUMMARY), canonical_summary_bytes(summary)
        )
        self.assertEqual(EXPECTED_MARKDOWN, render_markdown(summary))

        json_output = self.root / "dogfood-summary.json"
        markdown_output = self.root / "dogfood-summary.md"
        write_summary(FIXTURES, json_output=json_output, markdown_output=markdown_output)
        self.assertEqual(canonical_artifact_bytes(EXPECTED_SUMMARY), json_output.read_bytes())
        self.assertEqual(EXPECTED_MARKDOWN.encode("utf-8"), markdown_output.read_bytes())

    def test_output_publication_restores_both_originals_when_second_write_fails(self) -> None:
        json_output = self.root / "dogfood-summary.json"
        markdown_output = self.root / "dogfood-summary.md"
        json_output.write_bytes(b"original json\n")
        markdown_output.write_bytes(b"original markdown\n")
        calls = 0

        def fail_second_write(path: Path, payload: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second-output failure")
            durable_atomic_write(path, payload)

        with (
            patch("scripts.dogfood_summary.durable_atomic_write", side_effect=fail_second_write),
            self.assertRaisesRegex(DogfoodSummaryError, "cannot be written safely"),
        ):
            write_summary(
                FIXTURES,
                json_output=json_output,
                markdown_output=markdown_output,
            )

        self.assertEqual(b"original json\n", json_output.read_bytes())
        self.assertEqual(b"original markdown\n", markdown_output.read_bytes())

    def test_output_publication_removes_new_first_output_when_second_write_fails(self) -> None:
        json_output = self.root / "dogfood-summary.json"
        markdown_output = self.root / "dogfood-summary.md"
        calls = 0

        def fail_second_write(path: Path, payload: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second-output failure")
            durable_atomic_write(path, payload)

        with (
            patch("scripts.dogfood_summary.durable_atomic_write", side_effect=fail_second_write),
            self.assertRaisesRegex(DogfoodSummaryError, "cannot be written safely"),
        ):
            write_summary(
                FIXTURES,
                json_output=json_output,
                markdown_output=markdown_output,
            )

        self.assertFalse(json_output.exists())
        self.assertFalse(markdown_output.exists())

    def test_every_rate_exposes_numerator_denominator_missing_and_value(self) -> None:
        metrics = aggregate_reports(FIXTURES)["metrics"]
        rates = [
            metrics[name]
            for name in (
                "completion_rate",
                "verified_pdf_rate",
                "blocked_rate",
                "retry_rate",
                "retries_per_panel",
                "handoff_use_rate",
                "handoff_completion_rate",
                "manual_filesystem_intervention_rate",
                "would_use_again_rate",
            )
        ]
        rates.append(metrics["resume_outcomes"]["success_rate"])
        for rate in rates:
            self.assertEqual({"numerator", "denominator", "missing", "value"}, set(rate))
        for duration in metrics["durations"].values():
            self.assertEqual({"observed", "missing", "median_minutes"}, set(duration))

    def test_zero_denominators_are_null_and_missing_optional_values_are_explicit(self) -> None:
        report = self._reports()[2]
        report["derived"]["generation_attempts"] = {"initial": 0, "retries": 0, "total": 0}
        report["derived"]["retry_rate"] = {"numerator": 0, "denominator": 0}
        report["derived"]["panel_count"] = 0
        report["derived"]["handoff_count"] = 0
        report["derived"]["handoff_completions"] = 0
        report["derived"]["successful_resumes"] = 0
        report["creator"]["failed_resume_attempts"] = None
        summary = aggregate_reports([self._write(report, "zero.json")])
        metrics = summary["metrics"]
        for name in ("retry_rate", "retries_per_panel", "handoff_completion_rate"):
            self.assertEqual(0, metrics[name]["denominator"])
            self.assertIsNone(metrics[name]["value"])
        resume = metrics["resume_outcomes"]["success_rate"]
        self.assertEqual({"numerator": 0, "denominator": 0, "missing": 1, "value": None}, resume)

    def test_statistics_median_handles_odd_and_even_samples(self) -> None:
        odd = aggregate_reports(FIXTURES)["metrics"]["durations"]
        self.assertEqual(20, odd["setup"]["median_minutes"])
        even = aggregate_reports(FIXTURES[:2])["metrics"]["durations"]
        self.assertEqual(16, even["setup"]["median_minutes"])
        self.assertEqual(24.5, even["first_project"]["median_minutes"])
        self.assertEqual(53.5, even["verified_pdf"]["median_minutes"])

    def test_digest_sorting_input_order_and_canonical_serialization_are_stable(self) -> None:
        forward = aggregate_reports(FIXTURES)
        reverse = aggregate_reports(tuple(reversed(FIXTURES)))
        self.assertEqual(forward, reverse)
        self.assertEqual(canonical_summary_bytes(forward), canonical_summary_bytes(reverse))
        loaded = load_reports(tuple(reversed(FIXTURES)))
        self.assertEqual(sorted(item.digest for item in loaded), [item.digest for item in loaded])
        expected_digests = sorted(
            hashlib.sha256(canonical_report_bytes(_read(path))).hexdigest() for path in FIXTURES
        )
        self.assertEqual(expected_digests, [item.digest for item in loaded])
        serialized = canonical_summary_bytes(forward)
        self.assertTrue(serialized.endswith(b"\n"))
        self.assertEqual(forward, json.loads(serialized))

    def test_duplicate_canonical_reports_are_rejected_without_public_digests(self) -> None:
        duplicate = self.root / "different-filename.json"
        duplicate.write_text(FIXTURES[0].read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(DogfoodSummaryError, "duplicate"):
            aggregate_reports([FIXTURES[0], duplicate])
        rendered = canonical_summary_bytes(aggregate_reports(FIXTURES)).decode("utf-8")
        for path in FIXTURES:
            digest = hashlib.sha256(canonical_report_bytes(_read(path))).hexdigest()
            self.assertNotIn(digest, rendered)
            self.assertNotIn(path.name, rendered)

    def test_invalid_consent_mixed_unknown_and_malformed_reports_are_rejected(self) -> None:
        report = self._reports()[0]
        no_consent = deepcopy(report)
        no_consent["consent"]["share_report"] = False
        unknown_schema = deepcopy(report)
        unknown_schema["schema_version"] = "2.0"
        unknown_kind = deepcopy(report)
        unknown_kind["kind"] = "other-report"
        malformed = self.root / "malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        cases = (
            ("consent", [self._write(no_consent, "no-consent.json")]),
            ("schema", [FIXTURES[0], self._write(unknown_schema, "mixed.json")]),
            ("schema", [self._write(unknown_schema, "unknown-schema.json")]),
            ("schema", [self._write(unknown_kind, "unknown-kind.json")]),
            ("invalid", [malformed]),
        )
        for message, paths in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(DogfoodSummaryError, message),
            ):
                aggregate_reports(paths)

    def test_oversized_and_excessive_input_collections_are_rejected(self) -> None:
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * (MAX_JSON_BYTES + 1))
        with self.assertRaisesRegex(DogfoodSummaryError, "invalid"):
            aggregate_reports([oversized])
        with self.assertRaisesRegex(DogfoodSummaryError, str(MAX_REPORT_INPUTS)):
            aggregate_reports([FIXTURES[0]] * (MAX_REPORT_INPUTS + 1))

    def test_privacy_canaries_paths_versions_aliases_and_digests_never_escape(self) -> None:
        report = self._reports()[0]
        report["comic_sol_version"] = "private-model-canary"
        report["creator"]["cohort_alias"] = "private-title-canary"
        private_path = self._write(report, "home-alice-private-project.json")
        with (
            patch("socket.socket", side_effect=AssertionError("network must not be used")),
            patch("urllib.request.urlopen", side_effect=AssertionError("network must not be used")),
            patch("time.time", side_effect=AssertionError("clock must not be used")),
        ):
            summary = aggregate_reports([private_path])
            rendered = canonical_summary_bytes(summary).decode("utf-8") + render_markdown(summary)
        for canary in (
            "private-model-canary",
            "private-title-canary",
            "home-alice-private-project.json",
            str(private_path),
            hashlib.sha256(canonical_report_bytes(report)).hexdigest(),
            "provider",
            "endpoint",
            "credential",
            "prompt",
            "character",
            "sha256",
        ):
            with self.subTest(canary=canary):
                self.assertNotIn(canary, rendered.lower())

    def test_distinct_and_missing_aliases_are_counts_not_identity_claims(self) -> None:
        reports = self._reports()
        reports[1]["creator"]["cohort_alias"] = reports[0]["creator"]["cohort_alias"]
        paths = [self._write(report, f"alias-{index}.json") for index, report in enumerate(reports)]
        sample = aggregate_reports(paths)["sample"]
        self.assertEqual(1, sample["observed_cohort_aliases"])
        self.assertEqual(1, sample["missing_cohort_aliases"])
        self.assertIn("anonymous cohort labels", sample["verification_responsibility"])
        self.assertIn("maintainer responsibility", sample["verification_responsibility"])

    def test_gate_statuses_cover_insufficient_target_and_beyond_target_windows(self) -> None:
        insufficient = aggregate_reports(self._cohort(20, 9))["sample"]
        self.assertEqual("insufficient-evidence", insufficient["gate_status"])
        self.assertEqual("target-window", insufficient["collection_window"])

        target = aggregate_reports(self._cohort(20, 10))["sample"]
        self.assertEqual("target-window", target["gate_status"])
        self.assertEqual("target-window", target["collection_window"])

        beyond = aggregate_reports(self._cohort(51, 10))["sample"]
        self.assertEqual("beyond-target-window", beyond["gate_status"])
        self.assertEqual("beyond-target-window", beyond["collection_window"])
        self.assertEqual(51, beyond["reports"])

    def test_cli_writes_both_outputs_and_never_uses_input_identity(self) -> None:
        json_output = self.root / "summary.json"
        markdown_output = self.root / "summary.md"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    *(str(path) for path in reversed(FIXTURES)),
                    "--json-output",
                    str(json_output),
                    "--markdown-output",
                    str(markdown_output),
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(canonical_summary_bytes(EXPECTED_SUMMARY), json_output.read_bytes())
        self.assertEqual(EXPECTED_MARKDOWN, markdown_output.read_text(encoding="utf-8"))
        self.assertEqual("dogfood-summary-ok: 3 valid reports\n", stdout.getvalue())

    def test_build_only_registration_excludes_runtime_and_skill_payloads(self) -> None:
        setup = (ROOT / "setup.py").read_text(encoding="utf-8")
        runtime_contract = (ROOT / "runtime_contract.py").read_text(encoding="utf-8")
        plugin_bundle = (ROOT / "scripts/sync_plugin_bundle.py").read_text(encoding="utf-8")
        self.assertIn('"dogfood_summary.py"', setup)
        self.assertNotIn("dogfood_summary.py", runtime_contract)
        self.assertNotIn("dogfood_summary.py", plugin_bundle)
        self.assertFalse((ROOT / "skills/comic-sol/scripts/dogfood_summary.py").exists())


if __name__ == "__main__":
    unittest.main()
