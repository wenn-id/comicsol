#!/usr/bin/env python3
"""Provider-neutral character-consistency QA records."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .character_identity import IDENTITY_PACK_PATH, STORYBOARD_PATH, validate_identity_pack
from .core_primitives import PANEL_ID_PATTERN, canonical_artifact_bytes
from .input_limits import MAX_JSON_BYTES, loads_bounded_json
from .project_io import ProjectTransaction, read_contained_bytes
from .quality_records import GENERIC_EVIDENCE
from .reference_strategy import REFERENCE_PLAN_PATH, project_reference_plan


CHARACTER_TRAITS = (
    "face",
    "hair",
    "age-appearance",
    "clothing",
    "accessories",
    "proportions",
    "immutable-traits",
)
CHECK_FIELDS = {
    "evidence",
    "id",
    "method",
    "provenance",
    "regions",
    "result",
    "reviewer",
    "severity",
}
REGION_FIELDS = {
    "character_id",
    "evidence",
    "expected",
    "repair_guidance",
    "result",
    "severity",
    "trait",
}
PROVENANCE_FIELDS = {
    "characters",
    "identity_pack_path",
    "identity_pack_sha256",
    "panel_id",
    "reference_plan_path",
    "reference_plan_sha256",
}
MAX_ASSESSMENT_BYTES = 256 * 1024


class CharacterQualityError(ValueError):
    """Raised when character-consistency evidence cannot be trusted."""


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_artifact_bytes(document)).hexdigest()


def _entries(document: Mapping[str, Any], field: str) -> dict[str, Mapping[str, Any]]:
    values = document.get(field)
    if not isinstance(values, list):
        raise CharacterQualityError(f"{field} must be an array")
    entries = {
        item["id"]: item
        for item in values
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if len(entries) != len(values):
        raise CharacterQualityError(f"{field} must contain unique character objects")
    return entries


def _storyboard_characters(storyboard: Mapping[str, Any], panel_id: str) -> tuple[str, ...]:
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise CharacterQualityError("storyboard pages must be an array")
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        panels = page.get("panels")
        if not isinstance(panels, list):
            continue
        for panel in panels:
            if isinstance(panel, Mapping) and panel.get("id") == panel_id:
                characters = panel.get("characters")
                if not isinstance(characters, list) or any(
                    not isinstance(character_id, str) for character_id in characters
                ):
                    raise CharacterQualityError(
                        "storyboard panel characters must be an array of IDs"
                    )
                if len(set(characters)) != len(characters):
                    raise CharacterQualityError("storyboard panel characters must be unique")
                return tuple(characters)
    raise CharacterQualityError(f"storyboard has no panel '{panel_id}'")


def character_consistency_context(
    identity_pack: Mapping[str, Any],
    character_bible: Mapping[str, Any],
    reference_plan: Mapping[str, Any],
    panel_id: str,
    *,
    storyboard: Mapping[str, Any],
) -> dict[str, Any]:
    """Return canonical expectations and reference provenance for one panel."""
    identity_issues = validate_identity_pack(identity_pack, character_bible=character_bible)
    if identity_issues:
        raise CharacterQualityError(
            "identity pack does not match the character bible: " + identity_issues[0]
        )
    pack = _entries(identity_pack, "characters")
    bible = _entries(character_bible, "characters")
    panels = reference_plan.get("panels")
    if not isinstance(panels, list):
        raise CharacterQualityError("reference plan panels must be an array")
    panel = next(
        (item for item in panels if isinstance(item, Mapping) and item.get("panel_id") == panel_id),
        None,
    )
    if panel is None:
        raise CharacterQualityError(f"reference plan has no panel '{panel_id}'")
    character_ids = panel.get("characters")
    selected = panel.get("selected")
    if not isinstance(character_ids, list) or not isinstance(selected, list):
        raise CharacterQualityError("reference plan panel is incomplete")
    try:
        expected_plan = project_reference_plan(
            identity_pack,
            storyboard,
            reference_budget=panel.get("reference_budget"),
        )
    except ValueError as error:
        raise CharacterQualityError(f"reference plan is invalid: {error}") from error
    if reference_plan != expected_plan:
        raise CharacterQualityError(
            "reference plan does not match the identity pack and storyboard"
        )
    storyboard_ids = _storyboard_characters(storyboard, panel_id)
    unknown_storyboard_ids = [
        character_id for character_id in storyboard_ids if character_id not in pack
    ]
    if unknown_storyboard_ids:
        raise CharacterQualityError(
            f"storyboard uses unknown character: {unknown_storyboard_ids[0]!r}"
        )
    expected_character_ids = [
        character_id for character_id in pack if character_id in storyboard_ids
    ]
    if character_ids != expected_character_ids:
        raise CharacterQualityError("reference plan characters do not match the storyboard panel")

    characters = []
    for character_id in character_ids:
        if (
            not isinstance(character_id, str)
            or character_id not in pack
            or character_id not in bible
        ):
            raise CharacterQualityError(f"unknown character: {character_id!r}")
        identity = pack[character_id]
        source = bible[character_id]
        immutable = identity.get("immutable_traits")
        wardrobe = identity.get("wardrobe")
        proportions = identity.get("proportions")
        if not all(isinstance(value, Mapping) for value in (immutable, wardrobe, proportions)):
            raise CharacterQualityError(f"character '{character_id}' identity is incomplete")
        expected = (
            immutable.get("face"),
            immutable.get("hair"),
            source.get("age_band"),
            wardrobe.get("base"),
            wardrobe.get("accessories"),
            dict(proportions),
            immutable.get("invariants"),
        )
        characters.append(
            {
                "character_id": character_id,
                "selected_references": [
                    dict(item)
                    for item in selected
                    if isinstance(item, Mapping) and item.get("character_id") == character_id
                ],
                "source_fingerprint_sha256": identity.get("source_fingerprint_sha256"),
                "traits": [
                    {"expected": value, "trait": trait}
                    for trait, value in zip(CHARACTER_TRAITS, expected)
                ],
            }
        )

    return {
        "characters": characters,
        "identity_pack_sha256": _digest(identity_pack),
        "panel_id": panel_id,
        "reference_plan_sha256": _digest(reference_plan),
    }


def _assessment_map(assessments: object) -> dict[tuple[str, str], Mapping[str, Any]]:
    if not isinstance(assessments, list):
        raise CharacterQualityError("assessments must be an array")
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in assessments:
        if not isinstance(item, Mapping):
            raise CharacterQualityError("assessments must contain objects")
        character_id = item.get("character_id")
        trait = item.get("trait")
        if not isinstance(character_id, str) or trait not in CHARACTER_TRAITS:
            raise CharacterQualityError("assessment character_id and trait are invalid")
        key = (character_id, trait)
        if key in indexed:
            raise CharacterQualityError(f"duplicate assessment: {character_id}/{trait}")
        result = item.get("result")
        severity = item.get("severity")
        evidence = item.get("evidence")
        if result not in {"pass", "warning", "fail"}:
            raise CharacterQualityError(f"invalid result: {character_id}/{trait}")
        if severity not in {"warning", "error"}:
            raise CharacterQualityError(f"invalid severity: {character_id}/{trait}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise CharacterQualityError(f"missing evidence: {character_id}/{trait}")
        if " ".join(evidence.split()).casefold() in GENERIC_EVIDENCE:
            raise CharacterQualityError(f"generic evidence: {character_id}/{trait}")
        indexed[key] = item
    return indexed


def _repair_guidance(region: Mapping[str, Any]) -> str | None:
    if region["result"] == "pass" and region["severity"] != "warning":
        return None
    expected = region["expected"]
    if isinstance(expected, list):
        expected_text = "; ".join(str(item) for item in expected) or "no accessories"
    elif isinstance(expected, Mapping):
        values = [expected.get("build"), *(expected.get("notes") or [])]
        expected_text = "; ".join(str(item) for item in values if item)
    else:
        expected_text = str(expected)
    return (
        f"Repair {region['character_id']} {region['trait']} to match {expected_text}; "
        f"observed: {region['evidence']}"
    )


def build_character_identity_check(
    context: Mapping[str, Any],
    assessments: object,
    *,
    method: str,
    reviewer: str,
) -> dict[str, Any]:
    """Build one rich ``character-identity`` panel check from reviewer evidence."""
    if not isinstance(method, str) or not method.strip():
        raise CharacterQualityError("review method must not be empty")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise CharacterQualityError("reviewer must not be empty")
    indexed = _assessment_map(assessments)
    characters = context.get("characters")
    if not isinstance(characters, list):
        raise CharacterQualityError("context characters must be an array")

    expected_keys: list[tuple[str, str]] = []
    regions: list[dict[str, Any]] = []
    provenance_characters: list[dict[str, Any]] = []
    for character in characters:
        if not isinstance(character, Mapping) or not isinstance(character.get("character_id"), str):
            raise CharacterQualityError("context character is invalid")
        character_id = character["character_id"]
        traits = character.get("traits")
        if not isinstance(traits, list):
            raise CharacterQualityError(f"context traits are missing for {character_id}")
        provenance_characters.append(
            {
                "character_id": character_id,
                "selected_references": character.get("selected_references", []),
                "source_fingerprint_sha256": character.get("source_fingerprint_sha256"),
            }
        )
        for expectation in traits:
            if not isinstance(expectation, Mapping):
                raise CharacterQualityError(f"context trait is invalid for {character_id}")
            trait = expectation.get("trait")
            key = (character_id, trait)
            expected_keys.append(key)
            assessment = indexed.get(key)
            if assessment is None:
                raise CharacterQualityError(f"missing assessment: {character_id}/{trait}")
            region = {
                "character_id": character_id,
                "evidence": " ".join(str(assessment["evidence"]).split()),
                "expected": expectation.get("expected"),
                "result": assessment["result"],
                "severity": assessment["severity"],
                "trait": trait,
            }
            region["repair_guidance"] = _repair_guidance(region)
            regions.append(region)
    extra = sorted(set(indexed) - set(expected_keys))
    if extra:
        character_id, trait = extra[0]
        raise CharacterQualityError(f"unexpected assessment: {character_id}/{trait}")

    hard_failures = sum(
        item["result"] == "fail" and item["severity"] == "error" for item in regions
    )
    warnings = sum(
        not (item["result"] == "fail" and item["severity"] == "error")
        and (item["result"] != "pass" or item["severity"] == "warning")
        for item in regions
    )
    passes = len(regions) - hard_failures - warnings
    if hard_failures:
        result, severity = "fail", "error"
    elif warnings:
        result, severity = "warning", "warning"
    else:
        result, severity = "pass", "error"
    hard_label = "hard failure" if hard_failures == 1 else "hard failures"
    warning_label = "warning" if warnings == 1 else "warnings"
    return {
        "evidence": (
            f"{len(regions)} trait checks: {passes} pass, {warnings} {warning_label}, "
            f"{hard_failures} {hard_label}"
        ),
        "id": "character-identity",
        "method": method.strip(),
        "provenance": {
            "characters": provenance_characters,
            "identity_pack_path": IDENTITY_PACK_PATH,
            "identity_pack_sha256": context.get("identity_pack_sha256"),
            "panel_id": context.get("panel_id"),
            "reference_plan_path": REFERENCE_PLAN_PATH,
            "reference_plan_sha256": context.get("reference_plan_sha256"),
        },
        "regions": regions,
        "result": result,
        "reviewer": reviewer.strip(),
        "severity": severity,
    }


def apply_character_identity_check(
    panel_record: Mapping[str, Any], check: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace the panel's identity check and recompute its quality decision."""
    if "override_reason" in panel_record:
        raise CharacterQualityError(
            "an overridden panel must be regenerated before recording a new review"
        )
    if check.get("id") != "character-identity":
        raise CharacterQualityError("check id must be character-identity")
    checks = panel_record.get("checks")
    if not isinstance(checks, list):
        raise CharacterQualityError("panel checks must be an array")
    indexes = [
        index
        for index, item in enumerate(checks)
        if isinstance(item, Mapping) and item.get("id") == "character-identity"
    ]
    if len(indexes) != 1:
        raise CharacterQualityError("panel must contain one character-identity check")

    updated = deepcopy(dict(panel_record))
    updated_checks = deepcopy(checks)
    updated_checks[indexes[0]] = deepcopy(dict(check))
    updated["checks"] = updated_checks
    hard_failure = any(
        isinstance(item, Mapping)
        and item.get("result") == "fail"
        and item.get("severity") == "error"
        for item in updated_checks
    )
    has_warning = any(
        isinstance(item, Mapping)
        and (item.get("result") == "warning" or item.get("severity") == "warning")
        for item in updated_checks
    )
    if hard_failure:
        updated["decision"] = "regenerate"
    elif has_warning:
        updated["decision"] = "accept-warning"
    else:
        updated["decision"] = "accept"

    warnings: list[str] = []
    for item in updated_checks:
        if not isinstance(item, Mapping):
            continue
        if item.get("id") == "character-identity":
            for region in item.get("regions", []) or []:
                if (
                    isinstance(region, Mapping)
                    and not (region.get("result") == "fail" and region.get("severity") == "error")
                    and (region.get("result") != "pass" or region.get("severity") == "warning")
                    and isinstance(region.get("repair_guidance"), str)
                ):
                    warnings.append(region["repair_guidance"])
        elif (item.get("result") == "warning" or item.get("severity") == "warning") and isinstance(
            item.get("evidence"), str
        ):
            warnings.append(item["evidence"])
    updated["unresolved_warnings"] = list(dict.fromkeys(warnings))
    return updated


def _expected_value_is_valid(trait: object, value: object) -> bool:
    if trait in {"face", "hair", "age-appearance", "clothing"}:
        return isinstance(value, str) and bool(value.strip())
    if trait in {"accessories", "immutable-traits"}:
        return isinstance(value, list) and all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )
    if trait == "proportions":
        return (
            isinstance(value, Mapping)
            and set(value) == {"build", "notes"}
            and isinstance(value.get("build"), str)
            and bool(value["build"].strip())
            and isinstance(value.get("notes"), list)
            and all(isinstance(item, str) and bool(item.strip()) for item in value["notes"])
        )
    return False


def validate_character_identity_check(
    check: Mapping[str, Any], *, allow_override: bool = False
) -> tuple[str, ...]:
    """Return stable categories for a rich character-identity check."""
    if "provenance" not in check:
        return () if check.get("regions") == [] else ("character-provenance-structure",)
    issues: set[str] = set()
    if set(check) != CHECK_FIELDS:
        issues.add("character-check-structure")
    provenance = check.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != PROVENANCE_FIELDS:
        issues.add("character-provenance-structure")
    else:
        if provenance.get("identity_pack_path") != IDENTITY_PACK_PATH:
            issues.add("character-provenance-path")
        if provenance.get("reference_plan_path") != REFERENCE_PLAN_PATH:
            issues.add("character-provenance-path")
        for field in ("identity_pack_sha256", "reference_plan_sha256"):
            value = provenance.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                issues.add("character-provenance-hash")
        if not isinstance(provenance.get("panel_id"), str):
            issues.add("character-provenance-panel")
        if not isinstance(provenance.get("characters"), list):
            issues.add("character-provenance-characters")

    regions = check.get("regions")
    if not isinstance(regions, list) or not regions:
        issues.add("character-trait-structure")
        return tuple(sorted(issues))
    keys: list[tuple[str, str]] = []
    traits_by_character: dict[str, list[str]] = {}
    for region in regions:
        if not isinstance(region, Mapping) or set(region) != REGION_FIELDS:
            issues.add("character-trait-structure")
            continue
        character_id = region.get("character_id")
        trait = region.get("trait")
        if not isinstance(character_id, str) or not character_id.strip():
            issues.add("character-trait-identity")
            continue
        if trait not in CHARACTER_TRAITS:
            issues.add("character-trait-id")
            continue
        key = (character_id, trait)
        if key in keys:
            issues.add("character-trait-duplicate")
        keys.append(key)
        traits_by_character.setdefault(character_id, []).append(trait)
        if not _expected_value_is_valid(trait, region.get("expected")):
            issues.add("character-trait-expected")
        if region.get("result") not in {"pass", "warning", "fail"}:
            issues.add("character-trait-result")
        if region.get("severity") not in {"warning", "error"}:
            issues.add("character-trait-severity")
        evidence = region.get("evidence")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or " ".join(evidence.split()).casefold() in GENERIC_EVIDENCE
        ):
            issues.add("character-trait-evidence")
        needs_repair = region.get("result") != "pass" or region.get("severity") == "warning"
        guidance = region.get("repair_guidance")
        if needs_repair and (not isinstance(guidance, str) or not guidance.strip()):
            issues.add("character-trait-repair-guidance")
        if not needs_repair and guidance is not None:
            issues.add("character-trait-repair-guidance")
    if any(tuple(traits) != CHARACTER_TRAITS for traits in traits_by_character.values()):
        issues.add("character-trait-order")

    hard_failure = any(
        isinstance(region, Mapping)
        and region.get("result") == "fail"
        and region.get("severity") == "error"
        for region in regions
    )
    has_warning = any(
        isinstance(region, Mapping)
        and not (region.get("result") == "fail" and region.get("severity") == "error")
        and (region.get("result") != "pass" or region.get("severity") == "warning")
        for region in regions
    )
    has_failed_warning = any(
        isinstance(region, Mapping)
        and region.get("result") == "fail"
        and region.get("severity") == "warning"
        for region in regions
    )
    expected_outcome = (
        ("fail", "error")
        if hard_failure
        else ("fail", "warning")
        if allow_override and has_failed_warning
        else ("warning", "warning")
        if has_warning
        else ("pass", "error")
    )
    if (check.get("result"), check.get("severity")) != expected_outcome:
        issues.add("character-check-outcome")
    return tuple(sorted(issues))


def _bound_document(
    project_dir: Path,
    provenance: Mapping[str, Any],
    *,
    path_field: str,
    hash_field: str,
    expected_path: str,
    label: str,
    issues: list[str],
) -> Mapping[str, Any] | None:
    if provenance.get(path_field) != expected_path:
        issues.append(f"{label} path is not canonical")
        return None
    try:
        payload = read_contained_bytes(
            project_dir, expected_path, max_bytes=MAX_JSON_BYTES
        )
    except (OSError, ValueError) as error:
        issues.append(f"{label} cannot be read: {type(error).__name__}")
        return None
    try:
        document = loads_bounded_json(payload, source=expected_path)
    except (UnicodeDecodeError, ValueError):
        issues.append(f"{label} is not valid JSON")
        return None
    if not isinstance(document, Mapping):
        issues.append(f"{label} must contain a JSON object")
        return None
    if _digest(document) != provenance.get(hash_field):
        issues.append(f"{label} hash does not match the reviewed artifact")
    return document


def validate_character_quality_provenance(
    project_dir: Path, panel_record: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return stale reasons for a rich character-identity review, if present."""
    checks = panel_record.get("checks")
    if not isinstance(checks, list):
        return ("panel checks must be an array",)
    check = next(
        (
            item
            for item in checks
            if isinstance(item, Mapping) and item.get("id") == "character-identity"
        ),
        None,
    )
    if check is None or "provenance" not in check:
        return ()
    provenance = check.get("provenance")
    if not isinstance(provenance, Mapping):
        return ("character identity provenance must be an object",)

    issues: list[str] = []
    identity_pack = _bound_document(
        Path(project_dir),
        provenance,
        path_field="identity_pack_path",
        hash_field="identity_pack_sha256",
        expected_path=IDENTITY_PACK_PATH,
        label="identity pack",
        issues=issues,
    )
    reference_plan = _bound_document(
        Path(project_dir),
        provenance,
        path_field="reference_plan_path",
        hash_field="reference_plan_sha256",
        expected_path=REFERENCE_PLAN_PATH,
        label="reference plan",
        issues=issues,
    )
    panel_id = panel_record.get("subject_id")
    if provenance.get("panel_id") != panel_id:
        issues.append("reviewed panel does not match the QA subject")
    if identity_pack is None or reference_plan is None or not isinstance(panel_id, str):
        return tuple(sorted(set(issues)))
    try:
        bible_payload = read_contained_bytes(
            project_dir, "plan/character-bible.json", max_bytes=MAX_JSON_BYTES
        )
        character_bible = loads_bounded_json(
            bible_payload, source="plan/character-bible.json"
        )
        if not isinstance(character_bible, Mapping):
            raise CharacterQualityError("character bible must contain a JSON object")
        identity_issues = validate_identity_pack(
            identity_pack,
            character_bible=character_bible,
            project_dir=Path(project_dir),
        )
        if identity_issues:
            issues.append("character bible and identity pack disagree: " + identity_issues[0])
        storyboard = loads_bounded_json(
            read_contained_bytes(project_dir, STORYBOARD_PATH, max_bytes=MAX_JSON_BYTES),
            source=STORYBOARD_PATH,
        )
        if not isinstance(storyboard, Mapping):
            raise CharacterQualityError("storyboard must contain a JSON object")
        context = character_consistency_context(
            identity_pack,
            character_bible,
            reference_plan,
            panel_id,
            storyboard=storyboard,
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        issues.append(f"character identity context cannot be rebuilt: {type(error).__name__}")
        return tuple(sorted(set(issues)))

    expected_characters = [
        {
            "character_id": character["character_id"],
            "selected_references": character["selected_references"],
            "source_fingerprint_sha256": character["source_fingerprint_sha256"],
        }
        for character in context["characters"]
    ]
    if provenance.get("characters") != expected_characters:
        issues.append("character identity or selected references changed since review")
    expected_traits = [
        (character["character_id"], trait["trait"], trait["expected"])
        for character in context["characters"]
        for trait in character["traits"]
    ]
    regions = check.get("regions")
    actual_traits = (
        [
            (region.get("character_id"), region.get("trait"), region.get("expected"))
            for region in regions
            if isinstance(region, Mapping)
        ]
        if isinstance(regions, list)
        else []
    )
    if actual_traits != expected_traits:
        issues.append("reviewed character trait expectations are stale")
    return tuple(sorted(set(issues)))


def _read_document(project_dir: Path, relative: str) -> Mapping[str, Any]:
    try:
        value = loads_bounded_json(
            read_contained_bytes(project_dir, relative, max_bytes=MAX_JSON_BYTES),
            source=relative,
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise CharacterQualityError(
            f"{relative} cannot be read as JSON: {type(error).__name__}"
        ) from error
    if not isinstance(value, Mapping):
        raise CharacterQualityError(f"{relative} must contain a JSON object")
    return value


def record_character_quality_review(
    project_dir: Path,
    panel_id: str,
    assessments: object,
    *,
    method: str,
    reviewer: str,
) -> Path:
    """Publish one character review into its existing panel QA record atomically."""
    if not isinstance(panel_id, str) or PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise CharacterQualityError("panel id must match pNN-NN")
    project_dir = Path(project_dir)
    relative = f"qa/panels/{panel_id}.json"
    with ProjectTransaction(project_dir, "character-quality-review") as transaction:
        identity_pack = _read_document(project_dir, IDENTITY_PACK_PATH)
        character_bible = _read_document(project_dir, "plan/character-bible.json")
        reference_plan = _read_document(project_dir, REFERENCE_PLAN_PATH)
        storyboard = _read_document(project_dir, STORYBOARD_PATH)
        context = character_consistency_context(
            identity_pack,
            character_bible,
            reference_plan,
            panel_id,
            storyboard=storyboard,
        )
        check = build_character_identity_check(
            context, assessments, method=method, reviewer=reviewer
        )
        check_issues = validate_character_identity_check(check)
        if check_issues:
            raise CharacterQualityError("invalid character review: " + check_issues[0])
        record = _read_document(project_dir, relative)
        updated = apply_character_identity_check(record, check)
        from .validate_project import validate_panel_record

        record_issues = validate_panel_record(updated)
        if record_issues:
            first = record_issues[0]
            raise CharacterQualityError(
                f"invalid completed panel record: {first.field}: {first.message}"
            )
        transaction.stage_bytes(relative, canonical_artifact_bytes(updated))
    return project_dir / relative


def main(argv: list[str] | None = None, *, input_stream=None) -> int:
    """Print review context or record normalized assessments from standard input."""
    parser = argparse.ArgumentParser(
        description="Prepare and record provider-neutral character-consistency QA."
    )
    parser.add_argument("project_dir", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--context", metavar="PANEL_ID")
    action.add_argument("--record", metavar="PANEL_ID")
    parser.add_argument("--method")
    parser.add_argument("--reviewer")
    arguments = parser.parse_args(argv)
    try:
        if arguments.context:
            project_dir = arguments.project_dir
            context = character_consistency_context(
                _read_document(project_dir, IDENTITY_PACK_PATH),
                _read_document(project_dir, "plan/character-bible.json"),
                _read_document(project_dir, REFERENCE_PLAN_PATH),
                arguments.context,
                storyboard=_read_document(project_dir, STORYBOARD_PATH),
            )
            print(json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not arguments.method or not arguments.reviewer:
            raise CharacterQualityError("--record requires --method and --reviewer")
        stream = sys.stdin if input_stream is None else input_stream
        payload = stream.read(MAX_ASSESSMENT_BYTES + 1)
        if len(payload.encode("utf-8")) > MAX_ASSESSMENT_BYTES:
            raise CharacterQualityError("assessment input exceeds 256 KiB")
        try:
            assessments = json.loads(payload)
        except json.JSONDecodeError as error:
            raise CharacterQualityError(f"assessment input is not valid JSON: {error}") from error
        path = record_character_quality_review(
            arguments.project_dir,
            arguments.record,
            assessments,
            method=arguments.method,
            reviewer=arguments.reviewer,
        )
    except (CharacterQualityError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
