#!/usr/bin/env python3
"""Validate and summarize candidate-bound live visual release evidence.

The deterministic benchmark cannot judge artwork. This tool consumes a curated,
local evidence bundle produced by an authorized agent session after a real image
provider run. It never calls a provider, reads credentials, or persists prompts.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.input_limits import looks_like_secret, loads_bounded_json
from tests.consistency_benchmark import (
    CONSISTENCY_DIMENSIONS,
    MATRIX_BY_PANEL,
    ScorecardError,
    validate_scorecard,
)

SCHEMA_VERSION = "1.0"
KIND = "milestone-live-visual-evidence"
MILESTONE = "v2.2"
EXPECTED_SCORE_COUNT = 105
MIN_OVERALL_MEAN = 3.5
MIN_GROUP_MEAN = 3.0
MIN_INDIVIDUAL_SCORE = 3
REQUIRED_MATERIAL_CHANGES = {
    *(f"CS-{number:03d}" for number in range(19, 27)),
    "CS-034",
    "CS-035",
}
CANONICAL_PANELS = set(MATRIX_BY_PANEL)

EXPECTED_GROUPS = {
    "by_character": {"bayu", "rani"},
    "by_dimension": {
        "accessories",
        "age",
        "clothing",
        "face",
        "hair",
        "proportions",
        "signature-traits",
    },
    "by_expression": {
        "alarmed",
        "braced",
        "delighted",
        "exhausted",
        "focused",
        "furious",
        "neutral",
        "relieved",
        "wry",
    },
    "by_lighting": {
        "cold-rim-and-red-glow",
        "even-neutral-daylight",
        "hard-noon-sun",
        "single-lamp-low-light",
    },
    "by_scene": {"engine-shed", "harbor-noon", "rain-night-market", "reference-studio"},
    "by_view": {"close-up", "front", "full-body", "profile", "three-quarter"},
}

REQUIRED_QUALITY_REVIEWS = {
    "action",
    "anatomy",
    "character-identity",
    "composition",
    "continuity",
    "lettering",
    "sfx",
    "technical-raster",
    "text-free-generation",
}


class EvidenceError(ValueError):
    """Raised when a live visual evidence bundle is incomplete or unsafe."""


def _is_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object, label: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvidenceError(f"{label} must be a non-empty string of at most {maximum} characters")
    if looks_like_secret(value):
        raise EvidenceError(f"{label} must not contain credentials or secrets")
    if any(character in value for character in ("\r", "\n", "|", "`")):
        raise EvidenceError(f"{label} must not contain Markdown control characters")
    return value.strip()


def _exact(record: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = set(record) - fields
    missing = fields - set(record)
    if unknown or missing:
        raise EvidenceError(
            f"{label} must contain exactly {sorted(fields)}; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be a JSON array")
    return value


def _bundle_path(root: Path, relative: object, label: str) -> Path:
    text = _text(relative, f"{label}.path", maximum=240)
    pure = PurePosixPath(text)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise EvidenceError(f"{label}.path must be a contained POSIX relative path")
    path = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceError(f"{label}.path must not traverse a symbolic link")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise EvidenceError(f"{label}.path escapes the evidence bundle") from error
    if not path.is_file():
        raise EvidenceError(f"{label}.path does not name a retained file")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(root: Path, value: object, label: str) -> dict[str, str]:
    record = _mapping(value, label)
    if set(record) != {"path", "sha256"}:
        raise EvidenceError(f"{label} must contain exactly path and sha256")
    path = _bundle_path(root, record.get("path"), label)
    expected = record.get("sha256")
    if not _is_digest(expected, 64):
        raise EvidenceError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    actual = _file_sha256(path)
    if actual != expected:
        raise EvidenceError(f"{label}.sha256 does not match the retained file")
    return {"path": path.relative_to(root).as_posix(), "sha256": actual}


def _raster_artifact(root: Path, value: object, label: str) -> dict[str, Any]:
    artifact = _artifact(root, value, label)
    path = root / artifact["path"]
    if path.suffix.lower() != ".png":
        raise EvidenceError(f"{label}.path must name a PNG raster")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError, DecompressionBombError) as error:
        raise EvidenceError(f"{label}.path must be a decodable PNG raster") from error
    if width < 64 or height < 64:
        raise EvidenceError(f"{label}.path must be at least 64x64 pixels")
    return {**artifact, "width": width, "height": height}


def _validate_candidate(
    value: object, *, expected_commit: str, expected_engine_version: str
) -> dict[str, str]:
    record = _mapping(value, "candidate")
    _exact(record, {"engine_version", "commit_sha", "milestone"}, "candidate")
    engine_version = _text(record.get("engine_version"), "candidate.engine_version", maximum=80)
    commit_sha = record.get("commit_sha")
    if not _is_digest(expected_commit, 40):
        raise EvidenceError("trusted expected commit must be a lowercase 40-character Git commit")
    if commit_sha != expected_commit:
        raise EvidenceError("candidate.commit_sha does not match the trusted expected commit")
    if engine_version != expected_engine_version:
        raise EvidenceError("candidate.engine_version does not match the trusted expected version")
    if record.get("milestone") != MILESTONE:
        raise EvidenceError(f"candidate.milestone must be {MILESTONE!r}")
    return {"engine_version": engine_version, "commit_sha": str(commit_sha), "milestone": MILESTONE}


def _validate_provenance(value: object) -> dict[str, str]:
    record = _mapping(value, "provenance")
    _exact(
        record,
        {"evidence_mode", "provider", "model", "reviewer", "method", "reviewed_at", "approval"},
        "provenance",
    )
    if record.get("evidence_mode") != "live-visual":
        raise EvidenceError("provenance.evidence_mode must be 'live-visual'")
    reviewed_at = _text(record.get("reviewed_at"), "provenance.reviewed_at", maximum=40)
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("provenance.reviewed_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError("provenance.reviewed_at must include a timezone")
    if parsed > datetime.now(timezone.utc):
        raise EvidenceError("provenance.reviewed_at must not be in the future")
    if record.get("approval") != "approved":
        raise EvidenceError("provenance.approval must be 'approved' for promotion")
    return {
        "evidence_mode": "live-visual",
        "provider": _text(record.get("provider"), "provenance.provider", maximum=120),
        "model": _text(record.get("model"), "provenance.model", maximum=120),
        "reviewer": _text(record.get("reviewer"), "provenance.reviewer", maximum=120),
        "method": _text(record.get("method"), "provenance.method", maximum=240),
        "reviewed_at": reviewed_at,
        "approval": "approved",
    }


def _validate_material_changes(root: Path, value: object) -> list[dict[str, Any]]:
    changes = _list(value, "material_changes")
    if not changes:
        raise EvidenceError("material_changes must publish at least one reviewed change")
    result = []
    seen = set()
    for index, item in enumerate(changes):
        label = f"material_changes[{index}]"
        record = _mapping(item, label)
        _exact(record, {"id", "summary", "before", "after", "decision"}, label)
        change_id = _text(record.get("id"), f"{label}.id", maximum=80)
        if change_id in seen:
            raise EvidenceError("material_changes ids must be unique")
        seen.add(change_id)
        before = _raster_artifact(root, record.get("before"), f"{label}.before")
        after = _raster_artifact(root, record.get("after"), f"{label}.after")
        if before["sha256"] == after["sha256"]:
            raise EvidenceError(f"{label} before and after renders must differ")
        decision = record.get("decision")
        if decision not in {"improved", "accepted-no-regression"}:
            raise EvidenceError(f"{label}.decision must record improved or accepted-no-regression")
        result.append(
            {
                "id": change_id,
                "summary": _text(record.get("summary"), f"{label}.summary"),
                "before": before,
                "after": after,
                "decision": decision,
            }
        )
    if seen != REQUIRED_MATERIAL_CHANGES:
        raise EvidenceError(
            "material_changes must contain exactly every v2.2 material change: "
            f"{sorted(REQUIRED_MATERIAL_CHANGES)}"
        )
    render_hashes = [change[side]["sha256"] for change in result for side in ("before", "after")]
    if len(render_hashes) != len(set(render_hashes)):
        raise EvidenceError("every material change must publish its own before/after renders")
    return result


def _scorecard_metrics(path: Path) -> dict[str, Any]:
    """Derive every promotion metric from the retained canonical scorecard."""
    try:
        scorecard = loads_bounded_json(path.read_bytes(), source=path.name)
        validate_scorecard(scorecard)
        scorecard_record = _mapping(scorecard, "scorecard")
        _exact(
            scorecard_record,
            {
                "benchmark",
                "definition_sha256",
                "dimensions",
                "kind",
                "panels",
                "review",
                "scale",
                "schema_version",
            },
            "scorecard",
        )
        review = _mapping(scorecard_record.get("review"), "scorecard.review")
        _exact(
            review,
            {
                "engine_version",
                "evidence_mode",
                "limitations",
                "method",
                "model",
                "provider",
                "reviewed_at",
                "reviewer",
            },
            "scorecard.review",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ScorecardError) as error:
        raise EvidenceError(f"character_consistency.scorecard is invalid: {error}") from error
    entries = []
    panels = _mapping(scorecard.get("panels"), "scorecard.panels")
    for panel_id in sorted(panels):
        panel = _mapping(panels[panel_id], f"scorecard.panels.{panel_id}")
        _exact(
            panel,
            {"background", "characters", "expression", "lighting_condition", "view"},
            f"scorecard.panels.{panel_id}",
        )
        characters = _mapping(panel.get("characters"), f"scorecard.panels.{panel_id}.characters")
        for character_id in sorted(characters):
            scores = _mapping(
                characters[character_id],
                f"scorecard.panels.{panel_id}.characters.{character_id}",
            )
            for dimension in CONSISTENCY_DIMENSIONS:
                score = scores[dimension]
                entries.append(
                    {
                        "score": score,
                        "by_character": character_id,
                        "by_dimension": dimension,
                        "by_expression": MATRIX_BY_PANEL[panel_id]["expression"],
                        "by_lighting": MATRIX_BY_PANEL[panel_id]["lighting_condition"],
                        "by_scene": MATRIX_BY_PANEL[panel_id]["scene"],
                        "by_view": MATRIX_BY_PANEL[panel_id]["view"],
                    }
                )
    scored = [entry for entry in entries if entry["score"] is not None]
    groups: dict[str, dict[str, float]] = {}
    for axis, expected_names in EXPECTED_GROUPS.items():
        groups[axis] = {}
        for name in sorted(expected_names):
            values = [entry["score"] for entry in scored if entry[axis] == name]
            groups[axis][name] = round(sum(values) / len(values), 3) if values else 0.0
    scores = [entry["score"] for entry in scored]
    return {
        "scorecard": scorecard,
        "scored_dimensions": len(scores),
        "total_dimensions": len(entries),
        "overall_mean": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "minimum_score": min(scores) if scores else 0,
        "group_means": groups,
    }


def _validate_consistency(
    root: Path,
    value: object,
    *,
    candidate: Mapping[str, str],
    provenance: Mapping[str, str],
) -> dict[str, Any]:
    record = _mapping(value, "character_consistency")
    _exact(
        record,
        {
            "scorecard",
            "scored_dimensions",
            "total_dimensions",
            "overall_mean",
            "minimum_score",
            "group_means",
            "panel_attempts",
        },
        "character_consistency",
    )
    scorecard = _artifact(root, record.get("scorecard"), "character_consistency.scorecard")
    metrics = _scorecard_metrics(root / scorecard["path"])
    review = _mapping(metrics["scorecard"].get("review"), "scorecard.review")
    expected_review = {
        "engine_version": candidate["engine_version"],
        "evidence_mode": "live-visual",
        "provider": provenance["provider"],
        "model": provenance["model"],
        "reviewer": provenance["reviewer"],
        "method": provenance["method"],
        "reviewed_at": provenance["reviewed_at"],
    }
    for field, expected in expected_review.items():
        if review.get(field) != expected:
            raise EvidenceError(
                f"scorecard.review.{field} must match the candidate-bound manifest provenance"
            )
    for field in ("scored_dimensions", "total_dimensions", "overall_mean", "minimum_score"):
        if record.get(field) != metrics[field]:
            raise EvidenceError(f"character_consistency.{field} does not match the scorecard")
    reported_groups = _mapping(record.get("group_means"), "character_consistency.group_means")
    if reported_groups != metrics["group_means"]:
        raise EvidenceError("character_consistency.group_means do not match the scorecard")
    scored = metrics["scored_dimensions"]
    total = metrics["total_dimensions"]
    if scored != EXPECTED_SCORE_COUNT or total != EXPECTED_SCORE_COUNT:
        raise EvidenceError(
            f"character_consistency coverage must be {EXPECTED_SCORE_COUNT}/{EXPECTED_SCORE_COUNT}"
        )
    overall = metrics["overall_mean"]
    minimum = metrics["minimum_score"]
    if overall < MIN_OVERALL_MEAN:
        raise EvidenceError(
            f"character_consistency.overall_mean must be at least {MIN_OVERALL_MEAN}"
        )
    if minimum < MIN_INDIVIDUAL_SCORE:
        raise EvidenceError(
            f"character_consistency.minimum_score must be at least {MIN_INDIVIDUAL_SCORE}"
        )
    normalized_groups = metrics["group_means"]
    for axis, expected_names in EXPECTED_GROUPS.items():
        values = normalized_groups[axis]
        if set(values) != expected_names:
            raise EvidenceError(
                f"character_consistency.group_means.{axis} must contain exactly the canonical groups"
            )
        for name in sorted(expected_names):
            mean = values[name]
            if mean < MIN_GROUP_MEAN:
                raise EvidenceError(
                    f"character_consistency.group_means.{axis}.{name} must be at least "
                    f"{MIN_GROUP_MEAN}"
                )
    panel_attempts = _mapping(record.get("panel_attempts"), "character_consistency.panel_attempts")
    if set(panel_attempts) != CANONICAL_PANELS or any(
        not isinstance(value, str) or not value for value in panel_attempts.values()
    ):
        raise EvidenceError(
            "character_consistency.panel_attempts must bind every canonical panel to an attempt"
        )
    return {
        "scorecard": scorecard,
        "scored_dimensions": scored,
        "total_dimensions": total,
        "coverage": 1.0,
        "overall_mean": overall,
        "minimum_score": minimum,
        "group_means": normalized_groups,
        "panel_attempts": dict(sorted(panel_attempts.items())),
    }


def _validate_attempts(root: Path, value: object) -> tuple[list[dict[str, Any]], set[str]]:
    attempts = _list(value, "attempts")
    if not attempts:
        raise EvidenceError("attempts must retain at least one provider result")
    result = []
    ids: set[str] = set()
    for index, item in enumerate(attempts):
        label = f"attempts[{index}]"
        record = _mapping(item, label)
        _exact(record, {"id", "panel_id", "sequence", "kind", "outcome", "raster"}, label)
        attempt_id = _text(record.get("id"), f"{label}.id", maximum=100)
        if attempt_id in ids:
            raise EvidenceError("attempt ids must be unique")
        ids.add(attempt_id)
        sequence = record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise EvidenceError(f"{label}.sequence must be a non-negative integer")
        outcome = record.get("outcome")
        if outcome not in {"accepted", "rejected"}:
            raise EvidenceError(f"{label}.outcome must be accepted or rejected")
        panel_id = _text(record.get("panel_id"), f"{label}.panel_id", maximum=80)
        if panel_id not in CANONICAL_PANELS:
            raise EvidenceError(f"{label}.panel_id must be a canonical benchmark panel")
        kind = record.get("kind")
        if kind not in {"initial", "selective-repair", "full-regeneration"}:
            raise EvidenceError(f"{label}.kind is not a supported attempt kind")
        result.append(
            {
                "id": attempt_id,
                "panel_id": panel_id,
                "sequence": sequence,
                "kind": kind,
                "outcome": outcome,
                "raster": _raster_artifact(root, record.get("raster"), f"{label}.raster"),
            }
        )
    sequences = [(item["panel_id"], item["sequence"]) for item in result]
    if len(sequences) != len(set(sequences)):
        raise EvidenceError("attempt sequence must be unique within each panel")
    accepted = [item for item in result if item["outcome"] == "accepted"]
    if {item["panel_id"] for item in accepted} != CANONICAL_PANELS or len(accepted) != len(
        CANONICAL_PANELS
    ):
        raise EvidenceError("attempts must retain exactly one accepted raster per canonical panel")
    return result, ids


def _validate_defects(
    value: object, attempts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str]]:
    attempt_by_id = {item["id"]: item for item in attempts}
    attempt_ids = set(attempt_by_id)
    defects = _list(value, "defects")
    result = []
    ids: set[str] = set()
    for index, item in enumerate(defects):
        label = f"defects[{index}]"
        record = _mapping(item, label)
        _exact(
            record,
            {
                "id",
                "category",
                "severity",
                "attempt_id",
                "observation",
                "repair",
                "retry_attempt_id",
                "resolution",
                "reviewer_decision",
            },
            label,
        )
        defect_id = _text(record.get("id"), f"{label}.id", maximum=100)
        if defect_id in ids:
            raise EvidenceError("defect ids must be unique")
        ids.add(defect_id)
        attempt_id = record.get("attempt_id")
        if attempt_id not in attempt_ids:
            raise EvidenceError(f"{label}.attempt_id must reference a retained attempt")
        severity = record.get("severity")
        resolution = record.get("resolution")
        if severity not in {"warning", "error"}:
            raise EvidenceError(f"{label}.severity must be warning or error")
        if resolution not in {"repaired", "accepted-warning"}:
            raise EvidenceError(f"{label}.resolution must be repaired or accepted-warning")
        retry_attempt_id = record.get("retry_attempt_id")
        if resolution == "repaired":
            if retry_attempt_id not in attempt_ids:
                raise EvidenceError(f"{label}.retry_attempt_id must reference the retained repair")
            source = attempt_by_id[str(attempt_id)]
            retry = attempt_by_id[str(retry_attempt_id)]
            if (
                source["outcome"] != "rejected"
                or retry["outcome"] != "accepted"
                or source["panel_id"] != retry["panel_id"]
                or retry["sequence"] <= source["sequence"]
                or retry["raster"]["sha256"] == source["raster"]["sha256"]
                or retry["kind"] == "initial"
            ):
                raise EvidenceError(
                    f"{label} repair must link a rejected attempt to a later distinct accepted "
                    "retry on the same panel"
                )
        elif retry_attempt_id is not None:
            raise EvidenceError(f"{label}.retry_attempt_id must be null for an accepted warning")
        if severity == "error" and resolution != "repaired":
            raise EvidenceError(f"{label} error-severity defects must be repaired")
        result.append(
            {
                "id": defect_id,
                "category": _text(record.get("category"), f"{label}.category", maximum=100),
                "severity": severity,
                "attempt_id": attempt_id,
                "observation": _text(record.get("observation"), f"{label}.observation"),
                "repair": _text(record.get("repair"), f"{label}.repair"),
                "retry_attempt_id": retry_attempt_id,
                "resolution": resolution,
                "reviewer_decision": _text(
                    record.get("reviewer_decision"), f"{label}.reviewer_decision"
                ),
            }
        )
    return result, ids


def _validate_warnings(value: object, defect_ids: set[str]) -> list[dict[str, str]]:
    warnings = _list(value, "accepted_warnings")
    result = []
    seen = set()
    for index, item in enumerate(warnings):
        label = f"accepted_warnings[{index}]"
        record = _mapping(item, label)
        _exact(record, {"id", "defect_id", "rationale", "reviewer_decision"}, label)
        warning_id = _text(record.get("id"), f"{label}.id", maximum=100)
        if warning_id in seen:
            raise EvidenceError("accepted warning ids must be unique")
        seen.add(warning_id)
        defect_id = record.get("defect_id")
        if defect_id not in defect_ids:
            raise EvidenceError(f"{label}.defect_id must reference a recorded defect")
        result.append(
            {
                "id": warning_id,
                "defect_id": str(defect_id),
                "rationale": _text(record.get("rationale"), f"{label}.rationale"),
                "reviewer_decision": _text(
                    record.get("reviewer_decision"), f"{label}.reviewer_decision"
                ),
            }
        )
    return result


def _validate_quality_reviews(
    value: object, warning_ids: set[str], accepted_attempt_ids: set[str]
) -> dict[str, Any]:
    reviews = _mapping(value, "quality_reviews")
    if set(reviews) != REQUIRED_QUALITY_REVIEWS:
        raise EvidenceError("quality_reviews must contain exactly every required quality category")
    result = {}
    for category in sorted(REQUIRED_QUALITY_REVIEWS):
        label = f"quality_reviews.{category}"
        review = _mapping(reviews[category], label)
        _exact(review, {"result", "evidence", "warning_id", "attempt_ids"}, label)
        status = review.get("result")
        if status not in {"pass", "warning"}:
            raise EvidenceError(f"{label}.result must be pass or warning")
        warning_id = review.get("warning_id")
        if status == "warning" and warning_id not in warning_ids:
            raise EvidenceError(f"{label}.warning_id must reference an accepted warning")
        if status == "pass" and warning_id is not None:
            raise EvidenceError(f"{label}.warning_id must be null for a passing review")
        attempt_ids = review.get("attempt_ids")
        if (
            not isinstance(attempt_ids, list)
            or set(attempt_ids) != accepted_attempt_ids
            or len(attempt_ids) != len(accepted_attempt_ids)
        ):
            raise EvidenceError(f"{label}.attempt_ids must reference every accepted panel raster")
        result[category] = {
            "result": status,
            "evidence": _text(review.get("evidence"), f"{label}.evidence"),
            "warning_id": warning_id,
            "attempt_ids": sorted(attempt_ids),
        }
    return result


def validate_evidence(
    manifest: object,
    bundle_root: Path,
    *,
    expected_commit: str,
    expected_engine_version: str,
    expected_reviewer_attestation_sha256: str,
) -> dict[str, Any]:
    """Validate one evidence manifest against trusted candidate identity."""
    record = _mapping(manifest, "manifest")
    _exact(
        record,
        {
            "schema_version",
            "kind",
            "candidate",
            "provenance",
            "reviewer_attestation",
            "material_changes",
            "character_consistency",
            "attempts",
            "defects",
            "accepted_warnings",
            "quality_reviews",
            "limitations",
        },
        "manifest",
    )
    if record.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError(f"schema_version must be {SCHEMA_VERSION!r}")
    if record.get("kind") != KIND:
        raise EvidenceError(f"kind must be {KIND!r}")
    root = bundle_root.resolve()
    reviewer_attestation = _artifact(
        root, record.get("reviewer_attestation"), "reviewer_attestation"
    )
    if not _is_digest(expected_reviewer_attestation_sha256, 64):
        raise EvidenceError("trusted reviewer attestation digest must be a lowercase SHA-256")
    if reviewer_attestation["sha256"] != expected_reviewer_attestation_sha256:
        raise EvidenceError(
            "reviewer_attestation does not match the trusted expected attestation digest"
        )
    candidate = _validate_candidate(
        record.get("candidate"),
        expected_commit=expected_commit,
        expected_engine_version=expected_engine_version,
    )
    provenance = _validate_provenance(record.get("provenance"))
    material_changes = _validate_material_changes(root, record.get("material_changes"))
    consistency = _validate_consistency(
        root,
        record.get("character_consistency"),
        candidate=candidate,
        provenance=provenance,
    )
    attempts, _ = _validate_attempts(root, record.get("attempts"))
    attempt_by_id = {item["id"]: item for item in attempts}
    for panel_id, attempt_id in consistency["panel_attempts"].items():
        attempt = attempt_by_id.get(attempt_id)
        if attempt is None or attempt["panel_id"] != panel_id or attempt["outcome"] != "accepted":
            raise EvidenceError(
                "character_consistency.panel_attempts must bind each scorecard panel to its "
                "accepted raster"
            )
    defects, defect_ids = _validate_defects(record.get("defects"), attempts)
    warnings = _validate_warnings(record.get("accepted_warnings"), defect_ids)
    warning_ids = {item["id"] for item in warnings}
    expected_warning_defects = {
        item["id"] for item in defects if item["resolution"] == "accepted-warning"
    }
    recorded_warning_defects = {item["defect_id"] for item in warnings}
    if expected_warning_defects != recorded_warning_defects:
        raise EvidenceError("every accepted-warning defect must have exactly one warning decision")
    accepted_attempt_ids = {item["id"] for item in attempts if item["outcome"] == "accepted"}
    quality_reviews = _validate_quality_reviews(
        record.get("quality_reviews"), warning_ids, accepted_attempt_ids
    )
    limitations = [
        _text(item, f"limitations[{index}]", maximum=500)
        for index, item in enumerate(_list(record.get("limitations"), "limitations"))
    ]
    if not limitations or not any(
        all(term in item.lower() for term in ("provider", "model", "reviewer"))
        for item in limitations
    ):
        raise EvidenceError(
            "limitations must name the provider, model, and reviewer scope of the evidence"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "milestone-live-visual-evidence-summary",
        "status": "passed",
        "decision": "PROMOTION APPROVED",
        "candidate": candidate,
        "provenance": provenance,
        "reviewer_attestation": reviewer_attestation,
        "threshold": {
            "expected_scores": EXPECTED_SCORE_COUNT,
            "minimum_group_mean": MIN_GROUP_MEAN,
            "minimum_individual_score": MIN_INDIVIDUAL_SCORE,
            "minimum_overall_mean": MIN_OVERALL_MEAN,
            "reviewer_approval": "approved",
        },
        "character_consistency": consistency,
        "material_changes": material_changes,
        "quality_reviews": quality_reviews,
        "attempts": attempts,
        "defects": defects,
        "accepted_warnings": warnings,
        "limitations": limitations,
    }


def load_evidence(path: Path) -> dict[str, Any]:
    """Read one bounded UTF-8 evidence manifest."""
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise EvidenceError(f"cannot read evidence manifest: {error.strerror or error}") from error
    try:
        value = loads_bounded_json(payload, source=path.name)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise EvidenceError(f"invalid evidence manifest: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceError("evidence manifest must be a JSON object")
    return value


def write_summary(summary: Mapping[str, Any], json_path: Path, markdown_path: Path) -> None:
    """Publish deterministic JSON and Markdown companions."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    consistency = summary["character_consistency"]
    provenance = summary["provenance"]
    lines = [
        "# v2.2 live visual evidence",
        "",
        f"- Decision: **{summary['decision']}**",
        f"- Candidate: `{summary['candidate']['engine_version']}` at "
        f"`{summary['candidate']['commit_sha']}`",
        f"- Provider/model: `{provenance['provider']}` / `{provenance['model']}`",
        f"- Reviewer: `{provenance['reviewer']}` via {provenance['method']}",
        f"- Reviewed at: `{provenance['reviewed_at']}`",
        f"- Character score: **{consistency['overall_mean']:.3f}/4**; coverage "
        f"**{consistency['scored_dimensions']}/{consistency['total_dimensions']}**",
        f"- Material changes with before/after renders: {len(summary['material_changes'])}",
        f"- Retained attempts: {len(summary['attempts'])}",
        f"- Defects / accepted warnings: {len(summary['defects'])} / "
        f"{len(summary['accepted_warnings'])}",
        "",
        "## Quality review",
        "",
        "| Category | Result | Evidence |",
        "|---|---|---|",
    ]
    for category, review in summary["quality_reviews"].items():
        lines.append(f"| `{category}` | {review['result']} | {review['evidence']} |")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in summary["limitations"])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _published_artifacts(summary: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return only validated files and digests referenced by the public summary."""
    artifacts: dict[str, str] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            path = value.get("path")
            digest = value.get("sha256")
            if isinstance(path, str) and isinstance(digest, str) and _is_digest(digest, 64):
                existing = artifacts.get(path)
                if existing is not None and existing != digest:
                    raise EvidenceError("published evidence path has conflicting digests")
                artifacts[path] = digest
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(summary)
    return sorted(artifacts.items())


def write_evidence_archive(
    summary: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    archive_path: Path,
) -> None:
    """Create a deterministic archive containing only reverified reviewed files."""
    try:
        manifest_payload = manifest_path.read_bytes()
        archived_manifest = loads_bounded_json(manifest_payload, source="evidence manifest")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise EvidenceError(f"evidence manifest is invalid: {error}") from error
    if archived_manifest != manifest:
        raise EvidenceError("evidence manifest changed after validation")
    root = manifest_path.parent.resolve()
    files: list[tuple[str, Path | None, str | None, bytes | None]] = [
        ("manifest.json", None, None, manifest_payload)
    ]
    for relative, digest in _published_artifacts(summary):
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise EvidenceError("published evidence path escapes the bundle") from error
        files.append((relative, path, digest, None))
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, archive_source, expected_digest, retained_payload in sorted(files):
                    if retained_payload is not None:
                        payload = retained_payload
                    else:
                        if archive_source is None:  # pragma: no cover - internal tuple invariant
                            raise EvidenceError("published evidence path is missing")
                        payload = archive_source.read_bytes()
                    if (
                        expected_digest is not None
                        and hashlib.sha256(payload).hexdigest() != expected_digest
                    ):
                        raise EvidenceError(
                            f"published evidence file changed after validation: {relative}"
                        )
                    info = tarfile.TarInfo(f"v2.2-live-visual-evidence/{relative}")
                    info.size = len(payload)
                    info.mode = 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    archive.addfile(info, io.BytesIO(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-engine-version", required=True)
    parser.add_argument("--expected-reviewer-attestation-sha256", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--archive-output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        manifest = load_evidence(arguments.manifest)
        summary = validate_evidence(
            manifest,
            arguments.manifest.parent,
            expected_commit=arguments.expected_commit,
            expected_engine_version=arguments.expected_engine_version,
            expected_reviewer_attestation_sha256=(arguments.expected_reviewer_attestation_sha256),
        )
        write_summary(summary, arguments.json_output, arguments.markdown_output)
        if arguments.archive_output is not None:
            write_evidence_archive(summary, manifest, arguments.manifest, arguments.archive_output)
    except EvidenceError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 1
    print(arguments.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
