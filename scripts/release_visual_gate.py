"""Validate candidate-bound live visual summaries at release trust boundaries."""

from __future__ import annotations

import math
from typing import Any, Mapping


SUMMARY_ASSET = "v2.2-live-visual-summary.json"
MARKDOWN_ASSET = "v2.2-live-visual-summary.md"
BUNDLE_ASSET = "v2.2-live-visual-evidence.tar.gz"
REQUIRED_MATERIAL_CHANGES = {
    *(f"CS-{number:03d}" for number in range(19, 27)),
    "CS-034",
    "CS-035",
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


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} is not a JSON object")
    return value


def _is_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_score(value: object, *, minimum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and minimum <= value <= 5.0
    )


def validate_live_visual_summary(
    summary: Mapping[str, Any],
    *,
    expected_commit: str,
    expected_version: str,
    expected_reviewer_attestation_sha256: str,
    summary_sha256: str,
) -> dict[str, Any]:
    """Return canonical release-gate evidence or fail closed on any mismatch."""
    if not _is_digest(expected_commit, 40):
        raise RuntimeError("trusted live visual candidate commit is invalid")
    if not isinstance(expected_version, str) or not expected_version:
        raise RuntimeError("trusted live visual candidate version is invalid")
    if not _is_digest(expected_reviewer_attestation_sha256, 64):
        raise RuntimeError("trusted live visual reviewer attestation digest is invalid")
    if not _is_digest(summary_sha256, 64):
        raise RuntimeError("live visual summary digest is invalid")
    if summary.get("schema_version") != "1.0":
        raise RuntimeError("live visual summary schema is unsupported")
    if summary.get("kind") != "milestone-live-visual-evidence-summary":
        raise RuntimeError("live visual summary kind is invalid")
    if summary.get("status") != "passed" or summary.get("decision") != "PROMOTION APPROVED":
        raise RuntimeError("live visual evidence is not promotion-approved")

    candidate = _mapping(summary.get("candidate"), "live visual candidate")
    expected_candidate = {
        "engine_version": expected_version,
        "commit_sha": expected_commit,
        "milestone": "v2.2",
    }
    if candidate != expected_candidate:
        raise RuntimeError("live visual evidence belongs to another candidate")

    provenance = _mapping(summary.get("provenance"), "live visual provenance")
    if provenance.get("evidence_mode") != "live-visual" or provenance.get("approval") != "approved":
        raise RuntimeError("live visual reviewer provenance is not approved")
    for field in ("provider", "model", "reviewer", "method", "reviewed_at"):
        if not isinstance(provenance.get(field), str) or not provenance[field]:
            raise RuntimeError(f"live visual provenance {field} is invalid")

    reviewer_attestation = _mapping(
        summary.get("reviewer_attestation"), "live visual reviewer attestation"
    )
    if reviewer_attestation.get("sha256") != expected_reviewer_attestation_sha256:
        raise RuntimeError("live visual reviewer attestation belongs to another approval")

    threshold = _mapping(summary.get("threshold"), "live visual threshold")
    expected_threshold = {
        "expected_scores": 105,
        "minimum_group_mean": 3.0,
        "minimum_individual_score": 3,
        "minimum_overall_mean": 3.5,
        "reviewer_approval": "approved",
    }
    if threshold != expected_threshold:
        raise RuntimeError("live visual promotion threshold is invalid")

    consistency = _mapping(
        summary.get("character_consistency"), "live visual character consistency"
    )
    if (
        consistency.get("scored_dimensions") != 105
        or consistency.get("total_dimensions") != 105
        or consistency.get("coverage") != 1.0
        or not _is_score(consistency.get("overall_mean"), minimum=3.5)
        or not _is_score(consistency.get("minimum_score"), minimum=3.0)
    ):
        raise RuntimeError(
            "live visual character consistency does not meet the promotion threshold"
        )
    group_means = _mapping(consistency.get("group_means"), "live visual group means")
    if set(group_means) != set(EXPECTED_GROUPS):
        raise RuntimeError("live visual group mean axes are not canonical")
    for axis, expected_members in EXPECTED_GROUPS.items():
        groups = group_means[axis]
        group = _mapping(groups, "live visual group")
        if set(group) != expected_members:
            raise RuntimeError("live visual group mean members are not canonical")
        if any(not _is_score(score, minimum=3.0) for score in group.values()):
            raise RuntimeError("live visual group mean does not meet the promotion threshold")

    material_changes = summary.get("material_changes")
    if not isinstance(material_changes, list):
        raise RuntimeError("live visual material changes are invalid")
    change_ids = [item.get("id") for item in material_changes if isinstance(item, Mapping)]
    if len(change_ids) != len(material_changes) or set(change_ids) != REQUIRED_MATERIAL_CHANGES:
        raise RuntimeError("live visual material change coverage is incomplete")

    quality_reviews = _mapping(summary.get("quality_reviews"), "live visual quality reviews")
    if set(quality_reviews) != REQUIRED_QUALITY_REVIEWS:
        raise RuntimeError("live visual quality review coverage is incomplete")
    if any(
        not isinstance(review, Mapping) or review.get("result") not in {"pass", "warning"}
        for review in quality_reviews.values()
    ):
        raise RuntimeError("live visual quality review contains a blocking result")

    limitations = summary.get("limitations")
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(not isinstance(item, str) or not item for item in limitations)
    ):
        raise RuntimeError("live visual limitations are missing")

    return {
        "status": "passed",
        "decision": "PROMOTION APPROVED",
        "summary_asset": SUMMARY_ASSET,
        "summary_sha256": summary_sha256,
        "reviewer_attestation_sha256": expected_reviewer_attestation_sha256,
        "candidate": expected_candidate,
    }
