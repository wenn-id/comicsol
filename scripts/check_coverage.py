#!/usr/bin/env python3
"""Enforce independent application line and branch coverage floors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping


LINE_COVERAGE_FLOOR = 82.0
BRANCH_COVERAGE_FLOOR = 72.0


def _percentage(covered: object, total: object, label: str) -> float:
    """Return a validated percentage from Coverage.py total counters."""
    if not isinstance(covered, int) or not isinstance(total, int) or isinstance(covered, bool):
        raise ValueError(f"coverage {label} counters must be integers")
    if isinstance(total, bool) or total <= 0 or covered < 0 or covered > total:
        raise ValueError(f"coverage {label} counters are invalid")
    return covered * 100.0 / total


def coverage_percentages(report: Mapping[str, object]) -> tuple[float, float]:
    """Extract independent line and branch percentages from Coverage.py JSON."""
    totals = report.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("coverage report is missing totals")
    line = _percentage(totals.get("covered_lines"), totals.get("num_statements"), "line")
    branch = _percentage(totals.get("covered_branches"), totals.get("num_branches"), "branch")
    return line, branch


def check_coverage(report: Mapping[str, object]) -> tuple[float, float]:
    """Raise when the report falls below either immutable quality floor."""
    line, branch = coverage_percentages(report)
    failures = []
    if line < LINE_COVERAGE_FLOOR:
        failures.append(f"line {line:.2f}% < {LINE_COVERAGE_FLOOR:.2f}%")
    if branch < BRANCH_COVERAGE_FLOOR:
        failures.append(f"branch {branch:.2f}% < {BRANCH_COVERAGE_FLOOR:.2f}%")
    if failures:
        raise ValueError("coverage gate failed: " + "; ".join(failures))
    return line, branch


def main(argv: list[str] | None = None) -> int:
    """Check a Coverage.py JSON report and print the enforced application totals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path, default=Path("coverage.json"))
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.report.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("coverage report root must be an object")
        line, branch = check_coverage(payload)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(
        f"application coverage passed: line {line:.2f}% "
        f"(floor {LINE_COVERAGE_FLOOR:.2f}%), branch {branch:.2f}% "
        f"(floor {BRANCH_COVERAGE_FLOOR:.2f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
