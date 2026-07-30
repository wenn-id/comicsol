#!/usr/bin/env python3
"""Shared schema-2.0 quality record contracts for panels and pages."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable


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
RECOMPUTABLE_BINDING_FIELDS = frozenset(
    {
        "raw_sha256",
        "raw_width",
        "raw_height",
        "clean_sha256",
        "clean_width",
        "clean_height",
        "page_sha256",
        "page_width",
        "page_height",
        "normalization_sha256",
        "storyboard_sha256",
        "composition_cache_sha256",
        "reference_sha256s",
        "lettering_sha256s",
    }
)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


@dataclass(frozen=True)
class QualityCheck:
    id: str
    result: str
    severity: str
    evidence: str
    method: str
    reviewer: str
    regions: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _normalized_text(self.evidence))


@dataclass(frozen=True)
class QualityBinding:
    name: str
    value: str | int | list[object]


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


def quality_record_hash(record: dict[str, Any]) -> str:
    """Hash every persisted field using canonical UTF-8 JSON."""

    payload = json.dumps(
        record,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _reject_private_absolute_paths(value: object, field: str = "record") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_private_absolute_paths(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_private_absolute_paths(item, f"{field}[{index}]")
    elif isinstance(value, str) and _is_absolute_path(value):
        raise ValueError(f"{field} contains a private absolute path")


def read_quality_record(path: Path, kind: str) -> dict[str, Any]:
    """Read schema 1.0 or 2.0 records without claiming legacy compliance."""

    if kind not in CHECK_IDS_BY_KIND:
        raise ValueError(f"unknown quality record kind: {kind}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read quality record: {path.name}") from exc
    if not isinstance(record, dict):
        raise ValueError("quality record must be an object")
    schema = record.get("schema_version")
    if schema not in {"1.0", "2.0"}:
        raise ValueError("quality record schema_version must equal 1.0 or 2.0")
    if schema == "2.0" and record.get("kind") != kind:
        raise ValueError(f"quality record kind must equal {kind}")
    _reject_private_absolute_paths(record)
    return record


def migrate_quality_record(
    record: dict[str, Any], kind: str, recomputed: dict[str, Any]
) -> dict[str, Any]:
    """Create a conservative schema-2.0 record without inventing review facts."""

    if kind not in CHECK_IDS_BY_KIND:
        raise ValueError(f"unknown quality record kind: {kind}")
    if record.get("schema_version") != "1.0":
        raise ValueError("only schema 1.0 quality records can be migrated")
    unknown = sorted(set(recomputed) - RECOMPUTABLE_BINDING_FIELDS)
    if unknown:
        raise ValueError("unknown recomputed field: " + ", ".join(unknown))

    source = deepcopy(record)
    subject_key = "panel_id" if kind == "panel-qa" else "page_id"
    subject_id = source.get(subject_key)
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError(f"legacy record requires {subject_key}")

    migrated_checks = [
        {
            "id": check_id,
            "result": "fail",
            "severity": "error",
            "evidence": "",
            "method": "migration-required",
            "reviewer": "",
            "regions": [],
        }
        for check_id in CHECK_IDS_BY_KIND[kind]
    ]
    migrated = {
        "schema_version": "2.0",
        "kind": kind,
        "subject_id": subject_id,
        "bindings": deepcopy(recomputed),
        "checks": migrated_checks,
        "review": {
            "method": "migration-required",
            "reviewer": "",
            "reviewed_at": "",
        },
        "decision": "regenerate",
        "unresolved_warnings": ["quality-migration-required"],
    }
    _reject_private_absolute_paths(migrated)
    return migrated
