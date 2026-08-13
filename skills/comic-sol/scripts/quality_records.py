#!/usr/bin/env python3
"""Shared quality-check IDs and validation for panels and pages."""

from __future__ import annotations

import unicodedata
from typing import Iterable


PANEL_CHECK_IDS = (
    "character-identity",
    "anatomy",
    "action",
    "composition",
    "continuity",
    "text-free",
    "technical",
)
PAGE_CHECK_IDS = (
    "clipped-text",
    "text-overlap",
    "face-action-obstruction",
    "bubble-tail-direction",
    "reading-order",
    "accidental-text-watermark",
    "layout-border-integrity",
)
CHECK_IDS_BY_KIND = {
    "panel-qa": PANEL_CHECK_IDS,
    "page-qa": PAGE_CHECK_IDS,
}
RESULTS = frozenset({"pass", "warning", "fail"})
SEVERITIES = frozenset({"info", "warning", "error"})
GENERIC_EVIDENCE = frozenset({"verified", "looks good", "ok", "pass"})


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _normalized_evidence(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _normalized_text(value).casefold()


def validate_quality_checks(
    checks: object, expected_ids: Iterable[str]
) -> tuple[str, ...]:
    """Return stable issue categories for a quality-check collection."""

    issues: set[str] = set()
    expected = tuple(expected_ids)
    if not isinstance(checks, list):
        return ("quality-check-ids",)

    ids = [item.get("id") if isinstance(item, dict) else None for item in checks]
    if len(ids) != len(expected) or set(ids) != set(expected) or len(ids) != len(set(ids)):
        issues.add("quality-check-ids")

    evidence_values: list[str] = []
    for item in checks:
        if not isinstance(item, dict):
            issues.add("quality-check-structure")
            continue
        if item.get("result") not in RESULTS:
            issues.add("quality-check-result")
        if item.get("severity") not in SEVERITIES:
            issues.add("quality-check-severity")
        evidence = _normalized_evidence(item.get("evidence"))
        evidence_values.append(evidence)
        if not evidence or evidence in GENERIC_EVIDENCE:
            issues.add("quality-evidence-generic")
        if not isinstance(item.get("method"), str) or not item["method"].strip():
            issues.add("quality-check-method")
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip():
            issues.add("quality-check-reviewer")
        if not isinstance(item.get("regions"), list):
            issues.add("quality-check-regions")

    if len(evidence_values) > 1 and len(set(evidence_values)) == 1:
        issues.add("quality-evidence-generic")
    return tuple(sorted(issues))


if __name__ == "__main__":
    assert validate_quality_checks([], PANEL_CHECK_IDS) == ("quality-check-ids",)
    print("quality-records-ok")
