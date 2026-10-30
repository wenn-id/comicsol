#!/usr/bin/env python3
"""Run Ruff and enforce the checked-in per-file baseline for existing findings."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeAlias


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "ruff-baseline.json"
TARGETS = ("scripts", "comic_sol_product", "tests", "integrations")
BASELINE_RULES = ["B", "C90"]
BASELINE_SCHEMA_VERSION = 1
FindingKey: TypeAlias = tuple[str, str]


def load_baseline(path: Path) -> dict[FindingKey, int]:
    """Load and validate the per-file/per-rule Ruff allowances."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Ruff baseline: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Ruff baseline root must be an object")
    if payload.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ValueError("Ruff baseline schema_version is unsupported")
    if payload.get("ruff_version") != "0.17.2":
        raise ValueError("Ruff baseline must record the locked Ruff 0.17.2 version")
    if payload.get("rules") != BASELINE_RULES:
        raise ValueError("Ruff baseline rules must be B and C90")
    entries = payload.get("findings")
    if not isinstance(entries, list):
        raise ValueError("Ruff baseline findings must be an array")

    baseline: dict[FindingKey, int] = {}
    for entry in entries:
        key, count = _baseline_entry(entry)
        if key in baseline:
            raise ValueError(f"Ruff baseline repeats {key[0]} {key[1]}")
        baseline[key] = count
    return baseline


def _baseline_entry(entry: object) -> tuple[FindingKey, int]:
    if not isinstance(entry, dict):
        raise ValueError("Ruff baseline entry must be an object")
    file_path = entry.get("file")
    code = entry.get("code")
    count = entry.get("count")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("Ruff baseline entry must name a file")
    relative_path = PurePosixPath(file_path)
    windows_path = PureWindowsPath(file_path)
    if (
        relative_path.is_absolute()
        or windows_path.anchor
        or "\\" in file_path
        or ".." in relative_path.parts
        or relative_path.as_posix() != file_path
    ):
        raise ValueError(f"Ruff baseline file path is not repository-relative: {file_path}")
    if not isinstance(code, str) or not code.startswith(("B", "C9")):
        raise ValueError(f"Ruff baseline rule is not Bugbear or Complexity: {code}")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"Ruff baseline count must be a positive integer: {count}")
    return (file_path, code), count


def _relative_filename(filename: object, root: Path) -> str:
    if not isinstance(filename, str) or not filename:
        raise ValueError("Ruff finding is missing its filename")
    path = Path(filename)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"Ruff reported a file outside the repository: {filename}") from error


def finding_counts(payload: object, root: Path) -> Counter[FindingKey]:
    """Count Ruff diagnostics by normalized repository path and rule code."""
    if not isinstance(payload, list):
        raise ValueError("Ruff JSON output must be an array")
    counts: Counter[FindingKey] = Counter()
    for finding in payload:
        if not isinstance(finding, dict):
            raise ValueError("Ruff JSON finding must be an object")
        code = finding.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("Ruff finding is missing its rule code")
        file_path = _relative_filename(finding.get("filename"), root)
        counts[(file_path, code)] += 1
    return counts


def compare_counts(
    actual: Counter[FindingKey], baseline: dict[FindingKey, int]
) -> tuple[list[str], list[str]]:
    """Return new/excess findings and baseline reductions without editing files."""
    exceeded = [
        f"{file_path} {code}: {count} found, {baseline.get((file_path, code), 0)} allowed"
        for (file_path, code), count in sorted(actual.items())
        if count > baseline.get((file_path, code), 0)
    ]
    reduced = [
        f"{file_path} {code}: {baseline_count} allowed, {actual.get((file_path, code), 0)} found"
        for (file_path, code), baseline_count in sorted(baseline.items())
        if actual.get((file_path, code), 0) < baseline_count
    ]
    return exceeded, reduced


def run_ruff() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *TARGETS, "--output-format=json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    """Run configured Ruff rules and apply the committed count baseline."""
    try:
        baseline = load_baseline(BASELINE_PATH)
        result = run_ruff()
        if result.returncode not in {0, 1}:
            detail = result.stderr.strip() or f"Ruff exited with status {result.returncode}"
            raise ValueError(detail)
        findings = json.loads(result.stdout)
        actual = finding_counts(findings, ROOT)
        if result.returncode == 1 and not findings:
            raise ValueError("Ruff exited with violations but produced no JSON findings")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Ruff baseline check failed: {error}", file=sys.stderr)
        return 1

    exceeded, reduced = compare_counts(actual, baseline)
    if exceeded:
        print("Ruff baseline exceeded:", file=sys.stderr)
        for finding in exceeded:
            print(f"  {finding}", file=sys.stderr)
        return 1

    print(f"Ruff baseline passed: {sum(baseline.values())} existing findings are capped.")
    if reduced:
        print(f"Baseline reductions available for {len(reduced)} file/rule entries:")
        for finding in reduced:
            print(f"  {finding}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
