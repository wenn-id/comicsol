from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts import check_ruff_baseline


def baseline_payload(findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ruff_version": "0.17.2",
        "rules": ["B", "C90"],
        "findings": findings,
    }


def write_baseline(root: Path, findings: list[dict[str, object]]) -> Path:
    path = root / "ruff-baseline.json"
    path.write_text(json.dumps(baseline_payload(findings)), encoding="utf-8")
    return path


def ruff_finding(root: Path, file_path: str, code: str) -> dict[str, object]:
    return {"code": code, "filename": str(root / file_path)}


class BaselineParsingTests(unittest.TestCase):
    def test_loads_valid_per_file_rule_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_baseline(root, [{"file": "scripts/a.py", "code": "B023", "count": 2}])

            self.assertEqual({("scripts/a.py", "B023"): 2}, check_ruff_baseline.load_baseline(path))

    def test_rejects_invalid_baseline_headers(self):
        invalid_payloads = (
            [],
            {**baseline_payload([]), "schema_version": 2},
            {**baseline_payload([]), "ruff_version": "0.17.1"},
            {**baseline_payload([]), "rules": ["B"]},
            {**baseline_payload([]), "findings": {}},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ruff-baseline.json"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        check_ruff_baseline.load_baseline(path)

    def test_rejects_invalid_or_duplicate_entries(self):
        invalid_entries = (
            ["not an object"],
            [{"file": "../outside.py", "code": "B023", "count": 1}],
            [{"file": "/outside.py", "code": "B023", "count": 1}],
            [{"file": "scripts/a.py", "code": "E501", "count": 1}],
            [{"file": "scripts/a.py", "code": "B023", "count": True}],
            [{"file": "scripts/a.py", "code": "B023", "count": 0}],
            [
                {"file": "scripts/a.py", "code": "B023", "count": 1},
                {"file": "scripts/a.py", "code": "B023", "count": 2},
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ruff-baseline.json"
            for entries in invalid_entries:
                with self.subTest(entries=entries):
                    path.write_text(json.dumps(baseline_payload(entries)), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        check_ruff_baseline.load_baseline(path)

    def test_finding_counts_normalize_absolute_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = [
                ruff_finding(root, "tests/test_a.py", "B023"),
                {"code": "B023", "filename": "tests/test_a.py"},
            ]

            self.assertEqual(
                Counter({("tests/test_a.py", "B023"): 2}),
                check_ruff_baseline.finding_counts(payload, root),
            )

    def test_finding_counts_reject_malformed_records_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_payloads: tuple[object, ...] = (
                {},
                ["not an object"],
                [{"filename": "scripts/a.py"}],
                [{"code": "B023"}],
                [ruff_finding(root, "../outside.py", "B023")],
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        check_ruff_baseline.finding_counts(payload, root)


class BaselineComparisonTests(unittest.TestCase):
    def test_compare_accepts_existing_counts_and_reports_reductions(self):
        actual = Counter({("scripts/a.py", "B023"): 1})
        baseline = {("scripts/a.py", "B023"): 2, ("tests/b.py", "C901"): 1}

        exceeded, reduced = check_ruff_baseline.compare_counts(actual, baseline)

        self.assertEqual([], exceeded)
        self.assertEqual(
            [
                "scripts/a.py B023: 2 allowed, 1 found",
                "tests/b.py C901: 1 allowed, 0 found",
            ],
            reduced,
        )

    def test_compare_rejects_new_file_and_rule_findings(self):
        actual = Counter({("scripts/new.py", "B023"): 1, ("tests/a.py", "C901"): 2})

        exceeded, reduced = check_ruff_baseline.compare_counts(actual, {})

        self.assertEqual(
            [
                "scripts/new.py B023: 1 found, 0 allowed",
                "tests/a.py C901: 2 found, 0 allowed",
            ],
            exceeded,
        )
        self.assertEqual([], reduced)


class BaselineCommandTests(unittest.TestCase):
    def _invoke(
        self,
        root: Path,
        baseline: Path,
        result: CompletedProcess[str] | None = None,
        error: OSError | None = None,
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(check_ruff_baseline, "ROOT", root),
            patch.object(check_ruff_baseline, "BASELINE_PATH", baseline),
            patch.object(
                check_ruff_baseline,
                "run_ruff",
                side_effect=error if error is not None else None,
                return_value=result,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = check_ruff_baseline.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_main_accepts_baseline_counts_even_when_ruff_reports_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = write_baseline(root, [{"file": "scripts/a.py", "code": "B023", "count": 1}])
            result = CompletedProcess(
                args=["ruff"],
                returncode=1,
                stdout=json.dumps([ruff_finding(root, "scripts/a.py", "B023")]),
                stderr="",
            )

            code, stdout, stderr = self._invoke(root, baseline, result)

        self.assertEqual(0, code)
        self.assertIn("Ruff baseline passed", stdout)
        self.assertEqual("", stderr)

    def test_main_fails_when_a_file_rule_count_increases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = write_baseline(root, [{"file": "scripts/a.py", "code": "B023", "count": 1}])
            findings = [ruff_finding(root, "scripts/a.py", "B023") for _ in range(2)]
            result = CompletedProcess(
                args=["ruff"], returncode=1, stdout=json.dumps(findings), stderr=""
            )

            code, stdout, stderr = self._invoke(root, baseline, result)

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("Ruff baseline exceeded", stderr)
        self.assertIn("2 found, 1 allowed", stderr)

    def test_main_reports_a_reduction_without_rewriting_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = [{"file": "scripts/a.py", "code": "B023", "count": 2}]
            baseline = write_baseline(root, entries)
            before = baseline.read_bytes()
            result = CompletedProcess(
                args=["ruff"],
                returncode=1,
                stdout=json.dumps([ruff_finding(root, "scripts/a.py", "B023")]),
                stderr="",
            )

            code, stdout, stderr = self._invoke(root, baseline, result)

            self.assertEqual(before, baseline.read_bytes())

        self.assertEqual(0, code)
        self.assertIn("Baseline reductions available", stdout)
        self.assertIn("2 allowed, 1 found", stdout)
        self.assertEqual("", stderr)

    def test_main_fails_on_invalid_ruff_output_or_process_status(self):
        results = (
            CompletedProcess(args=["ruff"], returncode=2, stdout="", stderr="bad config"),
            CompletedProcess(args=["ruff"], returncode=1, stdout="not json", stderr=""),
            CompletedProcess(args=["ruff"], returncode=1, stdout="[]", stderr=""),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = write_baseline(root, [])
            for result in results:
                with self.subTest(returncode=result.returncode, stdout=result.stdout):
                    code, _, stderr = self._invoke(root, baseline, result)
                    self.assertEqual(1, code)
                    self.assertIn("Ruff baseline check failed", stderr)

    def test_main_reports_missing_ruff_and_invalid_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = write_baseline(root, [])
            code, _, stderr = self._invoke(root, baseline, error=FileNotFoundError("ruff"))
            self.assertEqual(1, code)
            self.assertIn("Ruff baseline check failed", stderr)

            baseline.write_text("not json", encoding="utf-8")
            code, _, stderr = self._invoke(root, baseline, result=CompletedProcess([], 0, "[]", ""))

        self.assertEqual(1, code)
        self.assertIn("cannot read Ruff baseline", stderr)


if __name__ == "__main__":
    unittest.main()
