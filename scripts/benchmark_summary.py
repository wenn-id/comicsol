#!/usr/bin/env python3
"""Compact, version-tagged benchmark summaries and metric deltas.

``scripts/benchmark.py`` publishes one machine-readable record per benchmark case.
A record is complete evidence and an unreadable report: nobody qualifying a release
reads six metrics across every case and concludes anything from them. This module
folds a directory of those records into one compact, version-tagged summary suitable
for release notes or a CI artifact, folds in the character consistency benchmark's
own published planes, and compares two summaries so a review sees metric deltas
instead of two directories of JSON.

The summary is a separate tool on purpose. Result records are the interface, so a
summary can be produced from results that already exist -- an archived CI artifact,
or a baseline benchmarked from another worktree -- without rerunning the engine and
without touching the byte-reproducible run path.

Aggregates pool each metric's numerator and denominator across cases, which is the
only aggregation the per-case contract supports: every metric is a ratio with a
declared numerator and denominator, so a pooled ratio keeps the same meaning.
Summaries derive only from timestamp-free, path-free records, so summarizing two
byte-identical deterministic runs produces byte-identical summaries.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .benchmark import HARNESS_VERSION, METRIC_DIRECTIONS, METRIC_IDS, load_results

try:
    from tests.consistency_benchmark import (
        CONSISTENCY_DIMENSIONS as CANONICAL_CONSISTENCY_DIMENSIONS,
        MATRIX_BY_PANEL as CANONICAL_MATRIX_BY_PANEL,
        PANEL_IDS as CANONICAL_PANEL_IDS,
        definition_digest as canonical_definition_digest,
        structural_baseline as canonical_structural_baseline,
    )
    CANONICAL_DEFINITION_ERROR = None
except ImportError as error:  # pragma: no cover - packaged runtime has no test tree
    CANONICAL_CONSISTENCY_DIMENSIONS = ()
    CANONICAL_MATRIX_BY_PANEL = {}
    CANONICAL_PANEL_IDS = ()
    canonical_definition_digest = None
    canonical_structural_baseline = None
    CANONICAL_DEFINITION_ERROR = error

SUMMARY_SCHEMA_VERSION = "1.0"
SUMMARY_KIND = "benchmark-summary"
DELTA_SCHEMA_VERSION = "1.0"
DELTA_KIND = "benchmark-summary-delta"

# The metrics a revision has to hold at their target before a summary calls it good.
# ``repair_rate`` is deliberately absent: a case that declares a scripted repair is
# supposed to record one, so a non-zero repair rate is evidence, not a failure.
GATING_METRIC_IDS = (
    "dialogue_correctness",
    "export_success",
    "panel_acceptance",
    "pipeline_success",
    "resume_success",
)

# A ratio with no denominator measured nothing. Dialogue correctness is the one
# metric whose absence is success, matching ``scripts.benchmark._metric``.
EMPTY_METRIC_VALUES = {"dialogue_correctness": 1.0}

CONSISTENCY_BENCHMARK = "character-consistency"
CONSISTENCY_BASELINE_KIND = "character-consistency-baseline"
CONSISTENCY_SCORECARD_KIND = "character-consistency-scorecard"
CONSISTENCY_SCHEMA_VERSION = "1.0"
CONSISTENCY_METRIC_DIRECTIONS = {
    "consistency_invariant_pinning": "higher-is-better",
    "consistency_trait_restatement": "higher-is-better",
    "consistency_visual_coverage": "higher-is-better",
    "consistency_visual_score": "higher-is-better",
}
CONSISTENCY_METRIC_IDS = tuple(sorted(CONSISTENCY_METRIC_DIRECTIONS))

SUMMARY_LIMITATION = (
    "A summary pools the numerators and denominators of the records it summarizes; it "
    "inherits every limitation of those records and adds no evidence of its own."
)
CONSISTENCY_LIMITATION = (
    "Character consistency metrics are reported and never gated: the structural plane is "
    "asserted by its own suite, and the visual plane is a reviewer judgement."
)


def _require_canonical_definition() -> None:
    """Refuse consistency evidence when its canonical definition is unavailable."""
    if canonical_definition_digest is None or canonical_structural_baseline is None:
        detail = f": {CANONICAL_DEFINITION_ERROR}" if CANONICAL_DEFINITION_ERROR else ""
        raise ValueError(f"canonical character consistency definition is unavailable{detail}")


# --------------------------------------------------------------------------- #
# Pooled metrics
# --------------------------------------------------------------------------- #


def _ratio(
    metric_id: str, numerator: float, denominator: int, direction: str
) -> dict[str, Any]:
    """Return one pooled ratio metric carrying its comparison direction."""
    denominator = int(denominator)
    if denominator > 0:
        value = round(float(numerator) / denominator, 6)
    else:
        value = EMPTY_METRIC_VALUES.get(metric_id, 0.0)
    return {
        "denominator": denominator,
        "direction": direction,
        "numerator": numerator,
        "unit": "ratio",
        "value": value,
    }


def aggregate_metrics(
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Pool every registered pipeline metric across one revision's case records.

    Pooling is the aggregation the metric contract already implies: ``panel_acceptance``
    becomes accepted panels over all panels, ``repair_rate`` extra generation calls over
    all panels, ``dialogue_correctness`` passing dialogue checks over all of them, and
    ``export_success`` verified pages over all expected pages. An average of per-case
    ratios would silently weight a one-panel case like a twelve-panel case.
    """
    aggregates: dict[str, dict[str, Any]] = {}
    for metric_id in METRIC_IDS:
        numerator: float = 0
        denominator = 0
        for record in records.values():
            metric = cast(Mapping[str, Any], record["metrics"])[metric_id]
            numerator += cast(float, metric["numerator"])
            denominator += int(cast(int, metric["denominator"]))
        aggregate = _ratio(
            metric_id, numerator, denominator, METRIC_DIRECTIONS[metric_id]
        )
        aggregate["cases"] = len(records)
        aggregates[metric_id] = aggregate
    return aggregates


def version_tag(engine_version: str, git_revision: str) -> str:
    """Return the release-notes tag one summary is published under."""
    tag = f"v{engine_version}"
    if git_revision and git_revision != "unknown":
        return f"{tag}+{git_revision[:12]}"
    return tag


def _revision_field(record: Mapping[str, Any], field: str) -> str:
    """Return one revision field of a result record, or ``unknown``."""
    revision = record.get("revision")
    value = revision.get(field) if isinstance(revision, Mapping) else None
    return value if isinstance(value, str) and value.strip() else "unknown"


def _revision_identity(
    records: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Return the single engine revision a summary is accountable to.

    Records from two revisions do not summarize one revision, so mixed input is an
    exception rather than an average of two engines.
    """
    identities = sorted(
        {
            (
                _revision_field(record, "engine_version"),
                _revision_field(record, "git_revision"),
            )
            for record in records.values()
        }
    )
    exceptions: list[str] = []
    if len(identities) > 1:
        exceptions.append(
            "results span more than one engine revision, so they do not summarize one "
            "revision: " + ", ".join(f"{engine}@{git}" for engine, git in identities)
        )
    engine_version, git_revision = identities[0] if identities else ("unknown", "unknown")
    return (
        {
            "engine_version": engine_version,
            "git_revision": git_revision,
            "harness_version": HARNESS_VERSION,
        },
        exceptions,
    )


# --------------------------------------------------------------------------- #
# Character consistency planes
# --------------------------------------------------------------------------- #


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read one JSON object, reporting every ordinary failure as a value error."""
    path = Path(path)
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"{path.name}: cannot be read: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"{path.name}: is not valid UTF-8") from error
    try:
        record = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path.name}: is not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"{path.name}: must contain a JSON object")
    return cast(dict[str, Any], record)


def _is_count(value: object) -> bool:
    """Report whether a value is a non-negative integer count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_count_pair(value: object) -> bool:
    """Report whether a value records expected and recorded counts."""
    if not isinstance(value, Mapping):
        return False
    return all(_is_count(value.get(field)) for field in ("expected", "recorded"))


def _has_valid_provenance(review: object, fields: tuple[str, ...]) -> bool:
    """Require every provenance field to be a non-empty string."""
    if not isinstance(review, Mapping):
        return False
    if not all(isinstance(review.get(field), str) and review[field].strip() for field in fields):
        return False
    for field in ("model", "provider", "reviewed_at", "evidence_mode"):
        value = review.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return False
    return True


def _metric_contract_problems(
    metric_id: str, metric: object, *, location: str = ""
) -> list[str]:
    """Validate one archived ratio metric before it enters a comparison."""
    prefix = f"{location}{metric_id}"
    problems: list[str] = []
    if not isinstance(metric, Mapping):
        return [f"{prefix} must be a JSON object"]
    if metric.get("direction") != METRIC_DIRECTIONS[metric_id]:
        problems.append(f"{prefix}.direction is invalid")
    if metric.get("unit") != "ratio":
        problems.append(f"{prefix}.unit must be 'ratio'")
    numerator = metric.get("numerator")
    denominator = metric.get("denominator")
    value = metric.get("value")
    if (
        not isinstance(numerator, (int, float))
        or isinstance(numerator, bool)
        or not math.isfinite(float(numerator))
        or numerator < 0
    ):
        problems.append(f"{prefix}.numerator must be a finite non-negative number")
    if not _is_count(denominator):
        problems.append(f"{prefix}.denominator must be a non-negative integer")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
    ):
        problems.append(f"{prefix}.value must be a finite non-negative number")
    elif (
        isinstance(numerator, (int, float))
        and not isinstance(numerator, bool)
        and _is_count(denominator)
    ):
        denominator_value = cast(int, denominator)
        expected = (
            round(float(numerator) / denominator_value, 6)
            if denominator_value > 0
            else EMPTY_METRIC_VALUES.get(metric_id, 0.0)
        )
        if value != expected:
            problems.append(f"{prefix}.value does not match its numerator and denominator")
    return problems


def load_consistency_baseline(path: Path) -> dict[str, Any]:
    """Read and validate one character consistency baseline report."""
    _require_canonical_definition()
    baseline = _read_json_object(path)
    problems: list[str] = []
    if baseline.get("kind") != CONSISTENCY_BASELINE_KIND:
        problems.append(f"kind must be {CONSISTENCY_BASELINE_KIND!r}")
    if baseline.get("benchmark") != CONSISTENCY_BENCHMARK:
        problems.append(f"benchmark must be {CONSISTENCY_BENCHMARK!r}")
    if baseline.get("schema_version") != CONSISTENCY_SCHEMA_VERSION:
        problems.append(f"schema_version must be {CONSISTENCY_SCHEMA_VERSION!r}")
    digest = baseline.get("definition_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        problems.append("definition_sha256 must bind the baseline to one definition")
    elif canonical_definition_digest is not None and digest != canonical_definition_digest():
        problems.append("definition_sha256 does not match the canonical benchmark definition")
    if not isinstance(baseline.get("engine_version"), str) or not baseline["engine_version"]:
        problems.append("engine_version must name the revision the baseline measured")
    validation = baseline.get("project_validation")
    if not isinstance(validation, Mapping) or validation.get("result") not in {"pass", "fail"}:
        problems.append("project_validation.result must be 'pass' or 'fail'")
    structural = baseline.get("structural")
    if not isinstance(structural, Mapping):
        problems.append("structural must be a JSON object")
    else:
        canonical = cast(Mapping[str, Any], canonical_structural_baseline())
        for field in (
            "backgrounds", "characters", "dimensions", "expressions", "lighting_conditions",
            "layouts", "page_count", "panel_count", "panels", "text_item_count", "views",
            "views_per_character",
        ):
            if structural.get(field) != canonical.get(field):
                problems.append(f"structural.{field} does not match the canonical definition")
        for field in ("invariant_pins", "trait_restatements"):
            if not _is_count_pair(structural.get(field)):
                problems.append(
                    f"structural.{field} must record non-negative expected and recorded counts"
                )
            elif structural[field]["recorded"] > structural[field]["expected"]:
                problems.append(f"structural.{field}.recorded must not exceed expected")
        for field in ("character_count", "page_count", "panel_count"):
            if not _is_count(structural.get(field)):
                problems.append(f"structural.{field} must be a non-negative integer")
        for field in ("backgrounds", "expressions", "lighting_conditions"):
            if not isinstance(structural.get(field), list):
                problems.append(f"structural.{field} must be an array")
        if not isinstance(structural.get("views"), Mapping):
            problems.append("structural.views must be a JSON object")
    visual = baseline.get("visual")
    if not isinstance(visual, Mapping):
        problems.append("visual must be a JSON object")
    else:
        if not isinstance(visual.get("scored"), bool):
            problems.append("visual.scored must state whether any dimension was scored")
        visual_counts_valid = True
        for field in ("scored_dimensions", "total_dimensions"):
            if not _is_count(visual.get(field)):
                visual_counts_valid = False
                problems.append(f"visual.{field} must be a non-negative integer")
        if visual_counts_valid and int(visual["scored_dimensions"]) > int(visual["total_dimensions"]):
            problems.append("visual.scored_dimensions must not exceed total_dimensions")
        canonical_total = cast(Mapping[str, Any], canonical_structural_baseline())["trait_restatements"]["expected"]
        if visual.get("total_dimensions") != canonical_total:
            problems.append("visual.total_dimensions does not match the canonical definition")
    if problems:
        raise ValueError(
            f"{Path(path).name}: invalid character consistency baseline: "
            + "; ".join(problems)
        )
    return baseline


def _scorecard_entries(scorecard: Mapping[str, Any]) -> list[tuple[str, object]]:
    """Return one (location, score) entry per scoreable dimension, in stable order."""
    entries: list[tuple[str, object]] = []
    panels = scorecard.get("panels")
    if not isinstance(panels, Mapping):
        return entries
    for panel_id in sorted(panels):
        panel = panels[panel_id]
        characters = panel.get("characters") if isinstance(panel, Mapping) else None
        if not isinstance(characters, Mapping):
            continue
        for character_id in sorted(characters):
            scores = characters[character_id]
            if not isinstance(scores, Mapping):
                continue
            for dimension in sorted(scores):
                entries.append(
                    (f"{panel_id}/{character_id}/{dimension}", scores[dimension])
                )
    return entries


def load_consistency_scorecard(path: Path) -> dict[str, Any]:
    """Read and validate one character consistency scorecard.

    A scored scorecard without a named reviewer and method is refused: an
    unattributable score is not evidence, so it is not summarized either.
    """
    _require_canonical_definition()
    scorecard = _read_json_object(path)
    problems: list[str] = []
    if scorecard.get("kind") != CONSISTENCY_SCORECARD_KIND:
        problems.append(f"kind must be {CONSISTENCY_SCORECARD_KIND!r}")
    if scorecard.get("benchmark") != CONSISTENCY_BENCHMARK:
        problems.append(f"benchmark must be {CONSISTENCY_BENCHMARK!r}")
    if scorecard.get("schema_version") != CONSISTENCY_SCHEMA_VERSION:
        problems.append(f"schema_version must be {CONSISTENCY_SCHEMA_VERSION!r}")
    digest = scorecard.get("definition_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        problems.append("definition_sha256 must bind the score to one definition")
    elif canonical_definition_digest is not None and digest != canonical_definition_digest():
        problems.append("definition_sha256 does not match the canonical benchmark definition")
    dimensions = scorecard.get("dimensions")
    if canonical_definition_digest is not None and dimensions != list(
        CANONICAL_CONSISTENCY_DIMENSIONS
    ):
        problems.append("dimensions must match the canonical benchmark definition")
    scale = scorecard.get("scale")
    minimum = scale.get("min") if isinstance(scale, Mapping) else None
    maximum = scale.get("max") if isinstance(scale, Mapping) else None
    if not _is_count(minimum) or not _is_count(maximum) or cast(int, maximum) <= cast(int, minimum):
        problems.append("scale must publish an ordered, non-negative min and max")
        minimum, maximum = 0, 0
    panels = scorecard.get("panels")
    if not isinstance(panels, Mapping) or not panels:
        problems.append("panels must be a non-empty JSON object")
    else:
        if canonical_definition_digest is not None and set(panels) != set(CANONICAL_PANEL_IDS):
            problems.append("panels must contain exactly every canonical benchmark panel")
        for panel_id in sorted(panels):
            panel = panels[panel_id]
            characters = panel.get("characters") if isinstance(panel, Mapping) else None
            if not isinstance(characters, Mapping) or not characters:
                problems.append(f"{panel_id}: characters must be a non-empty JSON object")
                continue
            expected_characters = (
                set(CANONICAL_MATRIX_BY_PANEL[panel_id]["characters"])
                if canonical_definition_digest is not None and panel_id in CANONICAL_MATRIX_BY_PANEL
                else None
            )
            if expected_characters is not None and set(characters) != expected_characters:
                problems.append(f"{panel_id}: characters must match the canonical benchmark panel")
            for character_id in sorted(characters):
                scores = characters[character_id]
                if not isinstance(scores, Mapping) or not scores:
                    problems.append(
                        f"{panel_id}/{character_id}: scores must be a non-empty JSON object"
                    )
                    continue
                if canonical_definition_digest is not None and set(scores) != set(
                    CANONICAL_CONSISTENCY_DIMENSIONS
                ):
                    problems.append(
                        f"{panel_id}/{character_id}: dimensions must match the canonical benchmark"
                    )
    for location, score in _scorecard_entries(scorecard):
        if score is None:
            continue
        if isinstance(score, bool) or not isinstance(score, int):
            problems.append(f"{location}: score must be an integer or null")
        elif not cast(int, minimum) <= score <= cast(int, maximum):
            problems.append(f"{location}: score is outside the published scale")
    review = scorecard.get("review")
    scored = any(score is not None for _, score in _scorecard_entries(scorecard))
    if scored and not _has_valid_provenance(
        review, ("engine_version", "method", "reviewer")
    ):
        problems.append(
            "a scored scorecard must name review.engine_version, review.method and "
            "review.reviewer, because an unattributable score is not evidence"
        )
    if problems:
        raise ValueError(
            f"{Path(path).name}: invalid character consistency scorecard: "
            + "; ".join(problems)
        )
    return scorecard


def consistency_report(
    *,
    baseline: Mapping[str, Any] | None = None,
    scorecard: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold the character consistency planes into one reported, never gated block.

    The structural plane is restated from the baseline the consistency suite already
    asserts. The visual plane reports how much a reviewer actually scored and the mean
    of those scores normalized to the published scale, over scored entries only, so an
    unscored dimension is never averaged in as a zero.
    """
    if baseline is None and scorecard is None:
        raise ValueError("a character consistency report needs a baseline or a scorecard")
    _require_canonical_definition()
    metrics: dict[str, Any] = {}
    report: dict[str, Any] = {
        "benchmark": CONSISTENCY_BENCHMARK,
        "definition_sha256": None,
        "engine_version": None,
        "metrics": metrics,
        "project_validation": None,
        "proves_visual_quality": False,
        "review": None,
        "scored": False,
        "structural": None,
    }
    scored_dimensions = 0
    total_dimensions = 0
    if baseline is not None:
        structural = cast(Mapping[str, Any], baseline["structural"])
        visual = cast(Mapping[str, Any], baseline["visual"])
        for metric_id, field in (
            ("consistency_invariant_pinning", "invariant_pins"),
            ("consistency_trait_restatement", "trait_restatements"),
        ):
            counts = cast(Mapping[str, int], structural[field])
            metrics[metric_id] = _ratio(
                metric_id,
                counts["recorded"],
                counts["expected"],
                CONSISTENCY_METRIC_DIRECTIONS[metric_id],
            )
        report["engine_version"] = baseline["engine_version"]
        report["definition_sha256"] = (
            canonical_definition_digest() if canonical_definition_digest is not None else baseline.get("definition_sha256")
        )
        report["project_validation"] = cast(
            Mapping[str, Any], baseline["project_validation"]
        )["result"]
        report["structural"] = {
            "background_count": len(cast(list, structural["backgrounds"])),
            "character_count": structural["character_count"],
            "expression_count": len(cast(list, structural["expressions"])),
            "lighting_condition_count": len(cast(list, structural["lighting_conditions"])),
            "page_count": structural["page_count"],
            "panel_count": structural["panel_count"],
            "view_count": len(cast(Mapping[str, Any], structural["views"])),
        }
        report["scored"] = bool(visual["scored"])
        scored_dimensions = int(cast(int, visual["scored_dimensions"]))
        total_dimensions = int(cast(int, visual["total_dimensions"]))
    if scorecard is not None:
        entries = _scorecard_entries(scorecard)
        scores = [cast(int, score) for _, score in entries if score is not None]
        if baseline is not None and len(entries) != total_dimensions:
            raise ValueError(
                "the scorecard and the baseline describe a different number of scoreable "
                "dimensions, so they do not describe one benchmark definition"
            )
        if baseline is not None and scorecard["definition_sha256"] != baseline.get(
            "definition_sha256"
        ):
            raise ValueError(
                "the scorecard and the baseline have different definition_sha256 values, "
                "so they do not describe one benchmark definition"
            )
        scored_dimensions, total_dimensions = len(scores), len(entries)
        review = scorecard.get("review")
        report["definition_sha256"] = scorecard["definition_sha256"]
        report["review"] = {
            field: (review.get(field) if isinstance(review, Mapping) else None)
            for field in ("engine_version", "method", "model", "provider", "reviewer")
        }
        report["engine_version"] = (
            review.get("engine_version") if isinstance(review, Mapping) else None
        )
        report["scored"] = bool(scores)
        report["proves_visual_quality"] = bool(scores)
        if scores:
            maximum = int(cast(int, cast(Mapping[str, Any], scorecard["scale"])["max"]))
            metrics["consistency_visual_score"] = _ratio(
                "consistency_visual_score",
                sum(scores),
                len(scores) * maximum,
                CONSISTENCY_METRIC_DIRECTIONS["consistency_visual_score"],
            )
    metrics["consistency_visual_coverage"] = _ratio(
        "consistency_visual_coverage",
        scored_dimensions,
        total_dimensions,
        CONSISTENCY_METRIC_DIRECTIONS["consistency_visual_coverage"],
    )
    return report


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


def summarize_results(
    results: Path,
    *,
    consistency_baseline: Path | None = None,
    consistency_scorecard: Path | None = None,
) -> dict[str, Any]:
    """Fold one revision's benchmark result records into one comparable summary."""
    records, exceptions = load_results(Path(results))
    for case_id in sorted(records):
        harness = records[case_id].get("harness_version")
        if harness != HARNESS_VERSION:
            exceptions.append(
                f"{case_id}: result was produced by benchmark harness {harness!r}, not "
                f"{HARNESS_VERSION!r}, so it is not comparable"
            )
    revision, revision_exceptions = _revision_identity(records)
    exceptions.extend(revision_exceptions)

    consistency: dict[str, Any] | None = None
    if consistency_baseline is not None or consistency_scorecard is not None:
        try:
            consistency = consistency_report(
                baseline=(
                    load_consistency_baseline(consistency_baseline)
                    if consistency_baseline is not None
                    else None
                ),
                scorecard=(
                    load_consistency_scorecard(consistency_scorecard)
                    if consistency_scorecard is not None
                    else None
                ),
            )
        except (OSError, ValueError) as error:
            exceptions.append(f"character consistency: {error}")
        else:
            consistency_engine = consistency.get("engine_version")
            result_engine = revision.get("engine_version")
            if (
                isinstance(consistency_engine, str)
                and isinstance(result_engine, str)
                and consistency_engine != result_engine
            ):
                source_label = (
                    "scorecard" if consistency_scorecard is not None and consistency_baseline is None else "baseline"
                )
                exceptions.append(
                    f"character consistency {source_label} engine version "
                    f"{consistency_engine!r} does not match result engine version "
                    f"{result_engine!r}"
                )
            elif result_engine == "unknown":
                exceptions.append("result engine version is unknown; consistency evidence is not comparable")

    metrics = aggregate_metrics(records)
    cases = {
        case_id: {
            "case_sha256": records[case_id].get("case_sha256"),
            "metrics": {
                metric_id: cast(Mapping[str, Any], records[case_id]["metrics"])[metric_id][
                    "value"
                ]
                for metric_id in METRIC_IDS
            },
            "status": records[case_id].get("status"),
        }
        for case_id in sorted(records)
    }
    failed_cases = sorted(
        case_id for case_id, case in cases.items() if case["status"] != "passed"
    )
    limitations = {SUMMARY_LIMITATION}
    if consistency is not None:
        limitations.add(CONSISTENCY_LIMITATION)
    for record in records.values():
        recorded = record.get("limitations")
        for limitation in recorded if isinstance(recorded, list) else []:
            if isinstance(limitation, str) and limitation.strip():
                limitations.add(limitation)
    proves_visual_quality = bool(records) and all(
        isinstance(record.get("evidence"), Mapping)
        and cast(Mapping[str, Any], record["evidence"]).get("proves_visual_quality") is True
        for record in records.values()
    )
    at_target = bool(records) and all(
        metrics[metric_id]["value"] >= 1.0 for metric_id in GATING_METRIC_IDS
    )
    return {
        "case_count": len(records),
        "cases": cases,
        "consistency": consistency,
        "exceptions": exceptions,
        "failed_cases": failed_cases,
        "harness_version": HARNESS_VERSION,
        "kind": SUMMARY_KIND,
        "limitations": sorted(limitations),
        "metrics": metrics,
        "proves_visual_quality": proves_visual_quality,
        "revision": revision,
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "status": "passed" if at_target and not failed_cases and not exceptions else "failed",
        "version_tag": version_tag(revision["engine_version"], revision["git_revision"]),
    }


def load_summary(path: Path) -> dict[str, Any]:
    """Read and validate one benchmark summary before it enters a comparison."""
    summary = _read_json_object(path)
    problems: list[str] = []
    if summary.get("kind") != SUMMARY_KIND:
        problems.append("wrong summary kind")
    if summary.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        problems.append("unsupported summary schema")
    if not isinstance(summary.get("version_tag"), str) or not summary["version_tag"]:
        problems.append("summary has no version tag")
    if summary.get("status") not in {"passed", "failed"}:
        problems.append("invalid summary status")
    metrics = summary.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != set(METRIC_IDS):
        problems.append("summary metrics do not match the registered metric IDs")
    elif isinstance(metrics, Mapping):
        for metric_id in METRIC_IDS:
            problems.extend(_metric_contract_problems(metric_id, metrics[metric_id]))
    if not isinstance(summary.get("cases"), Mapping):
        problems.append("summary cases must be a JSON object")
    else:
        for case_id, case in summary["cases"].items():
            if not isinstance(case, Mapping):
                problems.append(f"case {case_id!r} must be a JSON object")
                continue
            digest = case.get("case_sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                problems.append(f"case {case_id!r} has no valid case_sha256")
            if case.get("status") not in {"passed", "failed"}:
                problems.append(f"case {case_id!r} has no valid status")
            case_metrics = case.get("metrics")
            if not isinstance(case_metrics, Mapping) or set(case_metrics) != set(METRIC_IDS):
                problems.append(f"case {case_id!r} metrics do not match the registered metric IDs")
            else:
                for metric_id in METRIC_IDS:
                    value = case_metrics[metric_id]
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(float(value))
                        or not 0 <= float(value) <= 1
                    ):
                        problems.append(f"case {case_id!r} {metric_id} must be a ratio scalar")
    consistency = summary.get("consistency")
    if consistency is not None and not isinstance(consistency, Mapping):
        problems.append("summary consistency must be a JSON object or null")
    if problems:
        raise ValueError(
            f"{Path(path).name}: invalid benchmark summary: " + "; ".join(problems)
        )
    return summary


def write_summary(
    summary: Mapping[str, Any], output: Path, *, markdown: Path | None = None
) -> tuple[Path, Path]:
    """Write one summary as canonical JSON and as a reviewable Markdown report."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    report = Path(markdown) if markdown is not None else output.with_suffix(".md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_summary_markdown(summary), encoding="utf-8", newline="\n")
    return output, report


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    """Render one release-notes-ready Markdown summary of a benchmark revision."""
    metrics = cast(Mapping[str, Any], summary["metrics"])
    revision = cast(Mapping[str, Any], summary["revision"])
    lines = [
        f"# Comic Sol benchmark summary - {summary['version_tag']}",
        "",
        f"Status: `{summary['status']}` | {summary['case_count']} case(s), "
        f"{len(summary['failed_cases'])} failed | engine "
        f"`{revision['engine_version']}` | harness `{summary['harness_version']}` | "
        f"proves visual quality: {'yes' if summary['proves_visual_quality'] else 'no'}",
        "",
        "| metric | value | direction | pooled |",
        "| --- | --- | --- | --- |",
    ]
    for metric_id in METRIC_IDS:
        metric = cast(Mapping[str, Any], metrics[metric_id])
        lines.append(
            f"| `{metric_id}` | {metric['value']} | {metric['direction']} "
            f"| {metric['numerator']}/{metric['denominator']} |"
        )
    lines.extend(["", "## Cases", ""])
    lines.append(
        "| case | status | " + " | ".join(f"`{metric_id}`" for metric_id in METRIC_IDS) + " |"
    )
    lines.append("| --- | --- | " + " | ".join("---" for _ in METRIC_IDS) + " |")
    for case_id, case in cast(Mapping[str, Any], summary["cases"]).items():
        values = " | ".join(str(case["metrics"][metric_id]) for metric_id in METRIC_IDS)
        lines.append(f"| `{case_id}` | {case['status']} | {values} |")
    consistency = summary.get("consistency")
    if isinstance(consistency, Mapping):
        lines.extend(["", "## Character consistency (reported, never gated)", ""])
        structural = consistency.get("structural")
        if isinstance(structural, Mapping):
            lines.extend(
                [
                    f"Structural: {structural['panel_count']} panel(s), "
                    f"{structural['page_count']} page(s), "
                    f"{structural['character_count']} character(s), "
                    f"{structural['view_count']} view(s); project validation "
                    f"`{consistency['project_validation']}`.",
                    "",
                ]
            )
        lines.extend(["| metric | value | pooled |", "| --- | --- | --- |"])
        consistency_metrics = cast(Mapping[str, Any], consistency["metrics"])
        for metric_id in CONSISTENCY_METRIC_IDS:
            metric = consistency_metrics.get(metric_id)
            if not isinstance(metric, Mapping):
                continue
            lines.append(
                f"| `{metric_id}` | {metric['value']} "
                f"| {metric['numerator']}/{metric['denominator']} |"
            )
        review = consistency.get("review")
        attribution = (
            f"reviewer `{review.get('reviewer')}`, method `{review.get('method')}`"
            if isinstance(review, Mapping) and review.get("reviewer")
            else "no reviewer"
        )
        lines.extend(
            [
                "",
                f"Visual plane: {'scored' if consistency['scored'] else 'unscored'} "
                f"({attribution}).",
            ]
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in summary["limitations"])
    if summary["exceptions"]:
        lines.extend(["", "## Summary errors", ""])
        lines.extend(f"- {item}" for item in summary["exceptions"])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Summary deltas
# --------------------------------------------------------------------------- #


def _metric_value(metrics: object, metric_id: str) -> object:
    """Return one metric's reported value, or ``None`` when it is unusable."""
    if not isinstance(metrics, Mapping):
        return None
    metric = metrics.get(metric_id)
    return metric.get("value") if isinstance(metric, Mapping) else None


def _case_metric_value(metrics: object, metric_id: str) -> object:
    """Return one case metric value, whose summary representation is scalar."""
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(metric_id)
    if isinstance(value, Mapping):
        return value.get("value")
    return value


def _consistency_values(summary: Mapping[str, Any]) -> dict[str, object]:
    """Return the consistency metric values one summary actually reported."""
    consistency = summary.get("consistency")
    metrics = consistency.get("metrics") if isinstance(consistency, Mapping) else None
    if not isinstance(metrics, Mapping):
        return {}
    return {
        metric_id: _metric_value(metrics, metric_id)
        for metric_id in CONSISTENCY_METRIC_IDS
        if isinstance(metrics.get(metric_id), Mapping)
    }


def _compare_value(
    baseline: object, candidate: object, direction: str, tolerance: float
) -> dict[str, Any] | None:
    """Compare one metric value across revisions and classify the change."""
    for value in (baseline, candidate):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            return None
    delta = round(float(cast(float, candidate)) - float(cast(float, baseline)), 6)
    if direction == "higher-is-better":
        verdict = (
            "regressed" if delta < -tolerance else "improved" if delta > tolerance else "unchanged"
        )
    else:
        verdict = (
            "regressed" if delta > tolerance else "improved" if delta < -tolerance else "unchanged"
        )
    return {
        "baseline": float(cast(float, baseline)),
        "candidate": float(cast(float, candidate)),
        "delta": delta,
        "direction": direction,
        "verdict": verdict,
    }


def diff_summaries(
    baseline: Path,
    candidate: Path,
    output: Path,
    *,
    tolerance: float = 0.0,
    markdown: Path | None = None,
) -> dict[str, Any]:
    """Compare two benchmark summaries and report their metric deltas, fail-closed.

    Pooled aggregates over different case sets are not comparable, so a differing case
    set is an exception rather than a delta. Character consistency verdicts are reported
    as advisory and never change the decision.
    """
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("benchmark tolerance must be finite and non-negative")
    exceptions: list[str] = []
    loaded: dict[str, dict[str, Any] | None] = {"baseline": None, "candidate": None}
    for label, path in (("baseline", baseline), ("candidate", candidate)):
        try:
            loaded[label] = load_summary(path)
        except (OSError, ValueError) as error:
            exceptions.append(f"{label}: {error}")
    before, after = loaded["baseline"], loaded["candidate"]

    metrics: dict[str, Any] = {}
    consistency_metrics: dict[str, Any] = {}
    unavailable: list[str] = []
    cases: dict[str, Any] = {}
    missing_cases: list[str] = []
    new_cases: list[str] = []
    regressions: list[str] = []
    improvements: list[str] = []
    advisory: list[str] = []

    if before is not None and after is not None:
        before_revision = before.get("revision")
        after_revision = after.get("revision")
        if not isinstance(before_revision, Mapping) or not isinstance(after_revision, Mapping):
            exceptions.append("summaries must publish revision provenance")
        if before.get("harness_version") != after.get("harness_version"):
            exceptions.append("summary harness_version values differ")
        for label, summary in (("baseline", before), ("candidate", after)):
            if summary["status"] != "passed":
                exceptions.append(f"{label}: summary status is {summary['status']!r}")
            summary_exceptions = summary.get("exceptions")
            if isinstance(summary_exceptions, list) and summary_exceptions:
                exceptions.append(f"{label}: summary contains exceptions")
        for metric_id in METRIC_IDS:
            comparison = _compare_value(
                _metric_value(before["metrics"], metric_id),
                _metric_value(after["metrics"], metric_id),
                METRIC_DIRECTIONS[metric_id],
                tolerance,
            )
            if comparison is None:
                exceptions.append(f"metric is invalid in a summary: {metric_id}")
                continue
            metrics[metric_id] = comparison
            if comparison["verdict"] == "regressed":
                regressions.append(metric_id)
            elif comparison["verdict"] == "improved":
                improvements.append(metric_id)

        baseline_consistency = _consistency_values(before)
        candidate_consistency = _consistency_values(after)
        for metric_id in CONSISTENCY_METRIC_IDS:
            first = baseline_consistency.get(metric_id)
            second = candidate_consistency.get(metric_id)
            if metric_id not in baseline_consistency or metric_id not in candidate_consistency:
                if metric_id in baseline_consistency or metric_id in candidate_consistency:
                    unavailable.append(metric_id)
                    advisory.append(f"{metric_id}/unavailable")
                continue
            comparison = _compare_value(
                first, second, CONSISTENCY_METRIC_DIRECTIONS[metric_id], tolerance
            )
            if comparison is None:
                exceptions.append(
                    f"character consistency metric is invalid in a summary: {metric_id}"
                )
                continue
            consistency_metrics[metric_id] = comparison
            if comparison["verdict"] != "unchanged":
                advisory.append(f"{metric_id}/{comparison['verdict']}")

        baseline_cases = cast(Mapping[str, Any], before["cases"])
        candidate_cases = cast(Mapping[str, Any], after["cases"])
        missing_cases = sorted(set(baseline_cases) - set(candidate_cases))
        new_cases = sorted(set(candidate_cases) - set(baseline_cases))
        for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
            baseline_case = baseline_cases[case_id]
            candidate_case = candidate_cases[case_id]
            candidate_status = (
                candidate_case.get("status") if isinstance(candidate_case, Mapping) else None
            )
            cases[case_id] = {
                "baseline_case_sha256": (
                    baseline_case.get("case_sha256")
                    if isinstance(baseline_case, Mapping)
                    else None
                ),
                "candidate_case_sha256": (
                    candidate_case.get("case_sha256")
                    if isinstance(candidate_case, Mapping)
                    else None
                ),
                "baseline_status": (
                    baseline_case.get("status") if isinstance(baseline_case, Mapping) else None
                ),
                "candidate_status": candidate_status,
                "status": "passed" if candidate_status == "passed" else "failed",
            }
            if candidate_status != "passed":
                regressions.append(f"{case_id}/status")
            if cases[case_id]["baseline_case_sha256"] != cases[case_id]["candidate_case_sha256"]:
                regressions.append(f"{case_id}/case_sha256")
            baseline_metrics = (
                baseline_case.get("metrics") if isinstance(baseline_case, Mapping) else None
            )
            candidate_metrics = (
                candidate_case.get("metrics") if isinstance(candidate_case, Mapping) else None
            )
            for metric_id in METRIC_IDS:
                comparison = _compare_value(
                    _case_metric_value(baseline_metrics, metric_id),
                    _case_metric_value(candidate_metrics, metric_id),
                    METRIC_DIRECTIONS[metric_id],
                    tolerance,
                )
                if comparison is None:
                    exceptions.append(f"{case_id}: invalid {metric_id} metric")
                elif comparison["verdict"] == "regressed":
                    regressions.append(f"{case_id}/{metric_id}")
                elif comparison["verdict"] == "improved":
                    improvements.append(f"{case_id}/{metric_id}")
                cases[case_id].setdefault("metrics", {})[metric_id] = comparison
        if missing_cases or new_cases:
            exceptions.append(
                "the two summaries cover different benchmark cases, so their pooled "
                "aggregates are not comparable"
            )

    clean = not regressions and not exceptions
    result: dict[str, Any] = {
        "advisory": sorted(set(advisory)),
        "baseline_status": before["status"] if before is not None else None,
        "baseline_version_tag": before["version_tag"] if before is not None else None,
        "candidate_status": after["status"] if after is not None else None,
        "candidate_version_tag": after["version_tag"] if after is not None else None,
        "cases": cases,
        "consistency": {
            "metrics": consistency_metrics,
            "unavailable": sorted(set(unavailable)),
        },
        "decision": "NO REGRESSION" if clean else "REGRESSION",
        "exceptions": exceptions,
        "harness_version": HARNESS_VERSION,
        "improvements": sorted(set(improvements)),
        "kind": DELTA_KIND,
        "metrics": metrics,
        "missing_cases": missing_cases,
        "new_cases": new_cases,
        "regressions": sorted(set(regressions)),
        "schema_version": DELTA_SCHEMA_VERSION,
        "status": "passed" if clean else "failed",
        "tolerance": tolerance,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    report = Path(markdown) if markdown is not None else output.with_suffix(".md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_delta_markdown(result), encoding="utf-8", newline="\n")
    return result


def render_delta_markdown(delta: Mapping[str, Any]) -> str:
    """Render a reviewable Markdown report of one benchmark summary delta."""
    lines = [
        f"# Comic Sol benchmark summary {delta['decision']}",
        "",
        f"Status: `{delta['status']}` (tolerance `{delta['tolerance']}`)",
        "",
        f"Baseline `{delta['baseline_version_tag']}` (`{delta['baseline_status']}`) to "
        f"candidate `{delta['candidate_version_tag']}` (`{delta['candidate_status']}`)",
        "",
    ]
    if delta["metrics"]:
        lines.extend(
            [
                "| metric | baseline | candidate | delta | verdict |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for metric_id, comparison in cast(Mapping[str, Any], delta["metrics"]).items():
            lines.append(
                f"| `{metric_id}` | {comparison['baseline']} | {comparison['candidate']} "
                f"| {comparison['delta']:+} | {comparison['verdict']} |"
            )
        lines.append("")
    consistency = cast(Mapping[str, Any], delta["consistency"])
    if consistency["metrics"]:
        lines.extend(
            [
                "## Character consistency (advisory, never gating)",
                "",
                "| metric | baseline | candidate | delta | verdict |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for metric_id, comparison in cast(Mapping[str, Any], consistency["metrics"]).items():
            lines.append(
                f"| `{metric_id}` | {comparison['baseline']} | {comparison['candidate']} "
                f"| {comparison['delta']:+} | {comparison['verdict']} |"
            )
        lines.append("")
    if consistency["unavailable"]:
        lines.extend(
            [
                "Consistency metrics reported by only one summary: "
                + ", ".join(f"`{item}`" for item in consistency["unavailable"]),
                "",
            ]
        )
    if delta["cases"]:
        lines.extend(
            ["## Cases", "", "| case | baseline | candidate |", "| --- | --- | --- |"]
        )
        for case_id, case in cast(Mapping[str, Any], delta["cases"]).items():
            lines.append(
                f"| `{case_id}` | {case['baseline_status']} | {case['candidate_status']} |"
            )
        lines.append("")
    for label, key in (
        ("Regressions", "regressions"),
        ("Improvements", "improvements"),
        ("Advisory", "advisory"),
        ("Missing cases", "missing_cases"),
        ("New cases", "new_cases"),
    ):
        if delta[key]:
            lines.extend([f"{label}:", *[f"- `{item}`" for item in delta[key]], ""])
    if delta["exceptions"]:
        lines.extend(["Delta errors:", *[f"- {item}" for item in delta["exceptions"]], ""])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    """Build the benchmark summary command-line parser."""
    parser = argparse.ArgumentParser(
        prog="benchmark_summary.py",
        description=(
            "Summarize Comic Sol benchmark result records, or compare two summaries and "
            "report their metric deltas"
        ),
    )
    parser.add_argument("--results", type=Path)
    parser.add_argument("--consistency-baseline", type=Path)
    parser.add_argument("--consistency-scorecard", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Publish one benchmark summary or one summary delta, always leaving evidence."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    delta_requested = arguments.baseline is not None or arguments.candidate is not None
    if delta_requested and arguments.results is not None:
        parser.error("summarize a results directory or compare two summaries, not both")
    if arguments.output is None:
        parser.error("--output is required")

    if delta_requested:
        if arguments.baseline is None or arguments.candidate is None:
            parser.error("--baseline and --candidate must be supplied together")
        try:
            delta = diff_summaries(
                arguments.baseline,
                arguments.candidate,
                arguments.output,
                tolerance=arguments.tolerance,
                markdown=arguments.markdown,
            )
        except (OSError, ValueError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(json.dumps(delta, ensure_ascii=False, sort_keys=True))
        return 0 if delta["status"] == "passed" else 1

    if arguments.results is None:
        parser.error(
            "supply --results to summarize, or --baseline and --candidate to compare"
        )
    try:
        summary = summarize_results(
            arguments.results,
            consistency_baseline=arguments.consistency_baseline,
            consistency_scorecard=arguments.consistency_scorecard,
        )
        write_summary(summary, arguments.output, markdown=arguments.markdown)
    except (OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
