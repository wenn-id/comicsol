#!/usr/bin/env python3
"""Deterministic, offline aggregation for consented dogfood report schema 1.0 files."""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.core_primitives import canonical_artifact_bytes
from scripts.dogfood_report import (
    REPORT_SCHEMA_VERSION,
    DogfoodReportError,
    canonical_report_bytes,
    validate_report_file,
)
from scripts.project_io import durable_atomic_write, fsync_directory


SUMMARY_KIND = "comic-sol-dogfood-summary"
SUMMARY_SCHEMA_VERSION = "1.0"
MAX_REPORT_INPUTS = 1000
MIN_TARGET_REPORTS = 20
MAX_TARGET_REPORTS = 50
MIN_TARGET_ALIASES = 10

COLLECTION_LIMITATIONS = (
    "Aggregates only valid, explicitly consented comic-sol-dogfood-report schema 1.0 "
    "files supplied locally; no report is uploaded.",
    "Observed cohort aliases are anonymous labels, not verified external creator identities.",
    "Selection, recruitment, self-reporting, and missing-data bias are not corrected by this "
    "deterministic aggregate.",
    "Dogfood evidence does not prove adoption or visual quality and is never combined with "
    "deterministic benchmark results.",
)

EVIDENCE_PLANES = {
    "deterministic_mechanics": {
        "included": False,
        "label": "deterministic mechanics",
        "statement": (
            "Separate deterministic benchmark evidence; never combined with this dogfood aggregate."
        ),
    },
    "opt_in_creator_adoption_evidence": {
        "included": True,
        "label": "opt-in creator adoption evidence",
        "statement": (
            "Valid consented local reports are aggregated here; deterministic fixtures do not "
            "prove adoption."
        ),
    },
    "retained_live_visual_evidence": {
        "included": False,
        "label": "retained live visual evidence",
        "statement": (
            "Separate retained-render evidence; no visual-quality claim is derived from these "
            "reports."
        ),
    },
}

COMPLETION_STATUSES = frozenset({"COMPLETE", "COMPLETE_WITH_WARNINGS"})


class DogfoodSummaryError(ValueError):
    """Raised when report inputs cannot produce a truthful bounded summary."""


@dataclass(frozen=True)
class LoadedReport:
    """One validated report plus its private canonical digest used only for ordering."""

    digest: str
    report: dict[str, object]


def _rate(numerator: int, denominator: int, *, missing: int = 0) -> dict[str, object]:
    if min(numerator, denominator, missing) < 0 or numerator > denominator:
        raise DogfoodSummaryError("aggregate rate counts are inconsistent")
    value = None if denominator == 0 else round(numerator / denominator, 6)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "missing": missing,
        "value": value,
    }


def _ratio(numerator: int, denominator: int, *, missing: int = 0) -> dict[str, object]:
    if min(numerator, denominator, missing) < 0:
        raise DogfoodSummaryError("aggregate ratio counts are inconsistent")
    value = None if denominator == 0 else round(numerator / denominator, 6)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "missing": missing,
        "value": value,
    }


def _duration(values: Sequence[int], sample_size: int) -> dict[str, object]:
    observed = len(values)
    return {
        "observed": observed,
        "missing": sample_size - observed,
        "median_minutes": statistics.median(values) if values else None,
    }


def load_reports(paths: Sequence[Path]) -> list[LoadedReport]:
    """Validate bounded inputs and return them sorted by private canonical digest."""
    supplied = tuple(Path(path) for path in paths)
    if not supplied:
        raise DogfoodSummaryError("at least one dogfood report is required")
    if len(supplied) > MAX_REPORT_INPUTS:
        raise DogfoodSummaryError(
            f"report input count exceeds MAX_REPORT_INPUTS={MAX_REPORT_INPUTS}"
        )

    loaded: list[LoadedReport] = []
    seen: set[str] = set()
    for path in supplied:
        try:
            report = validate_report_file(path, require_consent=True)
            payload = canonical_report_bytes(report)
        except DogfoodReportError as error:
            raise DogfoodSummaryError(f"report input is invalid: {error}") from error
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            raise DogfoodSummaryError("duplicate canonical dogfood report input")
        seen.add(digest)
        loaded.append(LoadedReport(digest=digest, report=report))
    return sorted(loaded, key=lambda item: item.digest)


def _collection_window(report_count: int) -> str:
    if report_count < MIN_TARGET_REPORTS:
        return "below-target-window"
    if report_count <= MAX_TARGET_REPORTS:
        return "target-window"
    return "beyond-target-window"


def _gate_status(report_count: int, alias_count: int) -> str:
    if report_count < MIN_TARGET_REPORTS or alias_count < MIN_TARGET_ALIASES:
        return "insufficient-evidence"
    if report_count <= MAX_TARGET_REPORTS:
        return "target-window"
    return "beyond-target-window"


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DogfoodSummaryError(f"validated report {name} is not an object")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DogfoodSummaryError(f"validated report {name} is not a non-negative integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise DogfoodSummaryError(f"validated report {name} is not a boolean")
    return value


def aggregate_reports(paths: Sequence[Path]) -> dict[str, object]:
    """Aggregate only allowlisted scalar and category evidence from valid local reports."""
    loaded = load_reports(paths)
    reports = [item.report for item in loaded]
    sample_size = len(reports)

    completion_count = 0
    verified_pdf_count = 0
    blocked_count = 0
    blocked_categories: Counter[str] = Counter()
    committed_resumes = 0
    failed_resume_total = 0
    failed_resume_observed = 0
    retry_numerator = 0
    retry_denominator = 0
    panel_count = 0
    handoff_reports = 0
    handoff_count = 0
    handoff_completions = 0
    manual_interventions = 0
    would_use_again = 0
    friction_categories: Counter[str] = Counter()
    setup_minutes: list[int] = []
    first_project_minutes: list[int] = []
    pdf_minutes: list[int] = []
    aliases: set[str] = set()
    missing_aliases = 0

    for report in reports:
        derived = _object(report["derived"], "derived")
        creator = _object(report["creator"], "creator")

        if derived["terminal_status"] in COMPLETION_STATUSES:
            completion_count += 1
        if _boolean(derived["final_pdf_verified"], "final_pdf_verified"):
            verified_pdf_count += 1

        report_blocked = derived["blocked_categories"]
        if not isinstance(report_blocked, list):
            raise DogfoodSummaryError("validated report blocked_categories is not an array")
        if report_blocked:
            blocked_count += 1
        blocked_categories.update(str(category) for category in report_blocked)

        committed_resumes += _integer(derived["successful_resumes"], "successful_resumes")
        failed_resumes = creator["failed_resume_attempts"]
        if failed_resumes is not None:
            failed_resume_total += _integer(failed_resumes, "failed_resume_attempts")
            failed_resume_observed += 1

        attempts = _object(derived["generation_attempts"], "generation_attempts")
        retry_numerator += _integer(attempts["retries"], "generation_attempts.retries")
        retry_denominator += _integer(attempts["total"], "generation_attempts.total")
        panel_count += _integer(derived["panel_count"], "panel_count")

        report_handoffs = _integer(derived["handoff_count"], "handoff_count")
        if report_handoffs > 0:
            handoff_reports += 1
        handoff_count += report_handoffs
        handoff_completions += _integer(derived["handoff_completions"], "handoff_completions")

        manual_interventions += int(_boolean(creator["manual_intervention"], "manual_intervention"))
        would_use_again += int(_boolean(creator["would_use_again"], "would_use_again"))

        report_friction = creator["friction_categories"]
        if not isinstance(report_friction, list):
            raise DogfoodSummaryError("validated report friction_categories is not an array")
        friction_categories.update(str(category) for category in report_friction)

        setup_minutes.append(_integer(creator["setup_minutes"], "setup_minutes"))
        first_project_minutes.append(
            _integer(creator["first_project_minutes"], "first_project_minutes")
        )
        pdf_minutes.append(_integer(creator["pdf_minutes"], "pdf_minutes"))

        alias = creator["cohort_alias"]
        if alias is None:
            missing_aliases += 1
        elif isinstance(alias, str) and alias:
            aliases.add(alias)
        else:
            raise DogfoodSummaryError("validated report cohort_alias is invalid")

    alias_count = len(aliases)
    resume_missing = sample_size - failed_resume_observed
    summary: dict[str, object] = {
        "evidence_planes": {key: dict(value) for key, value in sorted(EVIDENCE_PLANES.items())},
        "kind": SUMMARY_KIND,
        "limitations": list(COLLECTION_LIMITATIONS),
        "metrics": {
            "blocked_category_counts": dict(sorted(blocked_categories.items())),
            "blocked_rate": _rate(blocked_count, sample_size),
            "completion_rate": _rate(completion_count, sample_size),
            "durations": {
                "first_project": _duration(first_project_minutes, sample_size),
                "setup": _duration(setup_minutes, sample_size),
                "verified_pdf": _duration(pdf_minutes, sample_size),
            },
            "friction_category_counts": dict(sorted(friction_categories.items())),
            "handoff_completion_rate": _rate(handoff_completions, handoff_count),
            "handoff_use_rate": _rate(handoff_reports, sample_size),
            "manual_filesystem_intervention_rate": _rate(manual_interventions, sample_size),
            "resume_outcomes": {
                "committed_successful_resumes": committed_resumes,
                "self_reported_failed_resume_attempts": {
                    "missing_reports": resume_missing,
                    "observed_reports": failed_resume_observed,
                    "total": failed_resume_total,
                },
                "success_rate": _rate(
                    committed_resumes,
                    committed_resumes + failed_resume_total,
                    missing=resume_missing,
                ),
            },
            "retries_per_panel": _ratio(retry_numerator, panel_count),
            "retry_rate": _rate(retry_numerator, retry_denominator),
            "verified_pdf_rate": _rate(verified_pdf_count, sample_size),
            "would_use_again_rate": _rate(would_use_again, sample_size),
        },
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "sample": {
            "collection_window": _collection_window(sample_size),
            "gate_status": _gate_status(sample_size, alias_count),
            "missing_cohort_aliases": missing_aliases,
            "observed_cohort_aliases": alias_count,
            "reports": sample_size,
            "target": {
                "maximum_reports": MAX_TARGET_REPORTS,
                "minimum_observed_cohort_aliases": MIN_TARGET_ALIASES,
                "minimum_reports": MIN_TARGET_REPORTS,
            },
            "verification_responsibility": (
                "Observed aliases are anonymous cohort labels; external creator identity "
                "verification remains a maintainer responsibility."
            ),
        },
        "schema_version": SUMMARY_SCHEMA_VERSION,
    }
    return summary


def canonical_summary_bytes(summary: Mapping[str, object]) -> bytes:
    """Return stable, sorted JSON bytes for one aggregate without adding identifiers."""
    if (
        summary.get("kind") != SUMMARY_KIND
        or summary.get("schema_version") != SUMMARY_SCHEMA_VERSION
    ):
        raise DogfoodSummaryError("dogfood summary kind/schema is invalid")
    return canonical_artifact_bytes(summary)


def _display(value: object) -> str:
    return "null" if value is None else str(value)


def render_markdown(summary: Mapping[str, object]) -> str:
    """Render a deterministic review summary with evidence planes kept visibly separate."""
    canonical_summary_bytes(summary)
    sample = _object(summary["sample"], "sample")
    metrics = _object(summary["metrics"], "metrics")
    planes = _object(summary["evidence_planes"], "evidence_planes")

    plane_order = (
        "deterministic_mechanics",
        "retained_live_visual_evidence",
        "opt_in_creator_adoption_evidence",
    )
    lines = ["# Comic Sol dogfood summary", "", "## Evidence planes", ""]
    for key in plane_order:
        plane = _object(planes[key], key)
        label = str(plane["label"]).capitalize()
        lines.append(f"- **{label}:** {plane['statement']}")

    target = _object(sample["target"], "sample.target")
    lines.extend(
        [
            "",
            "## Sample and collection limitations",
            "",
            f"- Valid consented reports: **{sample['reports']}**",
            f"- Observed anonymous cohort aliases: **{sample['observed_cohort_aliases']}**",
            f"- Reports with no cohort alias: **{sample['missing_cohort_aliases']}**",
            f"- Gate status: `{sample['gate_status']}`",
            f"- Collection window: `{sample['collection_window']}`",
            "- Target: at least "
            f"{target['minimum_observed_cohort_aliases']} distinct observed aliases and "
            f"{target['minimum_reports']}–{target['maximum_reports']} reports.",
            "- Observed aliases do not verify external creator identity; verification remains "
            "a maintainer responsibility.",
            "",
            "## Rates",
            "",
            "| Metric | Numerator | Denominator | Missing | Value |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    rate_rows = (
        ("Completion", "completion_rate"),
        ("Verified PDF", "verified_pdf_rate"),
        ("Blocked", "blocked_rate"),
        (
            "Resume success (committed / committed + self-reported failed)",
            "resume_outcomes.success_rate",
        ),
        ("Retry attempts", "retry_rate"),
        ("Retries per panel", "retries_per_panel"),
        ("Handoff use", "handoff_use_rate"),
        ("Handoff completion", "handoff_completion_rate"),
        ("Manual filesystem intervention", "manual_filesystem_intervention_rate"),
        ("Would use again", "would_use_again_rate"),
    )
    for label, key in rate_rows:
        if key == "resume_outcomes.success_rate":
            outcomes = _object(metrics["resume_outcomes"], "resume_outcomes")
            rate = _object(outcomes["success_rate"], key)
        else:
            rate = _object(metrics[key], key)
        lines.append(
            f"| {label} | {rate['numerator']} | {rate['denominator']} | "
            f"{rate['missing']} | {_display(rate['value'])} |"
        )

    outcomes = _object(metrics["resume_outcomes"], "resume_outcomes")
    failed = _object(
        outcomes["self_reported_failed_resume_attempts"],
        "self_reported_failed_resume_attempts",
    )
    lines.extend(
        [
            "",
            "## Resume evidence",
            "",
            "- Committed successful resumes (engine-derived): "
            f"**{outcomes['committed_successful_resumes']}**",
            "- Self-reported failed resume attempts: "
            f"**{failed['total']}** across **{failed['observed_reports']}** observed reports; "
            f"**{failed['missing_reports']}** reports missing.",
            "",
            "## Categories",
            "",
            "### Blocked categories",
            "",
        ]
    )
    blocked_counts = _object(metrics["blocked_category_counts"], "blocked_category_counts")
    if blocked_counts:
        lines.extend(f"- `{name}`: {blocked_counts[name]}" for name in sorted(blocked_counts))
    else:
        lines.append("- None observed.")
    lines.extend(["", "### Friction categories", ""])
    friction_counts = _object(metrics["friction_category_counts"], "friction_category_counts")
    if friction_counts:
        lines.extend(f"- `{name}`: {friction_counts[name]}" for name in sorted(friction_counts))
    else:
        lines.append("- None observed.")

    durations = _object(metrics["durations"], "durations")
    lines.extend(
        [
            "",
            "## Durations",
            "",
            "| Duration | Observed | Missing | Median minutes |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for label, key in (
        ("Setup", "setup"),
        ("First project", "first_project"),
        ("Verified PDF", "verified_pdf"),
    ):
        duration = _object(durations[key], key)
        lines.append(
            f"| {label} | {duration['observed']} | {duration['missing']} | "
            f"{_display(duration['median_minutes'])} |"
        )

    limitations = summary["limitations"]
    if not isinstance(limitations, list):
        raise DogfoodSummaryError("summary limitations are invalid")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in limitations)
    return "\n".join(lines) + "\n"


def _restore_output(path: Path, original: bytes | None) -> None:
    """Durably restore one output to its state before paired publication."""
    if original is not None:
        durable_atomic_write(path, original)
        return
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def _publish_outputs(outputs: Sequence[tuple[Path, bytes]]) -> None:
    """Publish a batch of outputs, rolling back every destination on failure."""
    originals: list[bytes | None] = []
    published = 0
    for path, _payload in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        originals.append(path.read_bytes() if path.is_file() else None)
    try:
        for path, payload in outputs:
            # Count the destination before entering the helper: a directory fsync
            # can fail after the replacement itself has already happened.
            published += 1
            durable_atomic_write(path, payload)
    except BaseException:
        for index in range(published - 1, -1, -1):
            _restore_output(outputs[index][0], originals[index])
        raise


def write_summary(
    report_paths: Sequence[Path], *, json_output: Path, markdown_output: Path
) -> dict[str, object]:
    """Write canonical JSON and Markdown from the same validated offline aggregate."""
    json_path = Path(json_output)
    markdown_path = Path(markdown_output)
    if json_path.resolve() == markdown_path.resolve():
        raise DogfoodSummaryError("JSON and Markdown outputs must be different files")
    summary = aggregate_reports(report_paths)
    try:
        _publish_outputs(
            (
                (json_path, canonical_summary_bytes(summary)),
                (markdown_path, render_markdown(summary).encode("utf-8")),
            )
        )
    except OSError as error:
        raise DogfoodSummaryError("dogfood summary output cannot be written safely") from error
    return summary


def main(argv: list[str] | None = None) -> int:
    """Run the build-only offline aggregation command."""
    parser = argparse.ArgumentParser(
        prog="python scripts/dogfood_summary.py",
        description="Aggregate valid, consented dogfood report schema 1.0 files offline.",
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        summary = write_summary(
            arguments.reports,
            json_output=arguments.json_output,
            markdown_output=arguments.markdown_output,
        )
    except DogfoodSummaryError as error:
        print(f"dogfood-summary-error: {error}", file=sys.stderr)
        return 2
    sample = _object(summary["sample"], "sample")
    print(f"dogfood-summary-ok: {sample['reports']} valid reports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
