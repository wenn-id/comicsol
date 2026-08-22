#!/usr/bin/env python3
"""Targeted panel repair planning with recorded strategy and provenance.

Regenerating a whole accepted panel because one character's scarf drifted is
expensive twice over: it spends a scarce retry budget, and it re-rolls every
detail the review already accepted, so a repair can introduce a second defect
while fixing the first. When the active capability can edit a localized part of
an image, the cheaper and safer move is to repair only the part the review
actually faulted and to leave the accepted bytes elsewhere alone.

This module turns that judgement into a stated, deterministic rule. It reads the
panel QA record, classifies every non-passing check into the narrowest scope its
evidence supports, and emits one ordered repair plan per panel.

Four properties matter more than the classification itself:

- Falling back is the default, never the surprise. Selective repair is chosen
  only when every defect is localized, the accepted content still matches the
  record, and the caller reports localized-editing support. Anything else plans
  a full regeneration and records the reason it did.
- Evidence decides the scope, not the check name. ``character-identity`` is
  subject-scoped only because its trait regions name a character; a plain
  identity check with no regions is `unlocalized-evidence`, because a defect
  nobody located is a defect nobody can repair in place.
- Four checks are never localized. Camera framing, cross-panel continuity, the
  scripted action, and whole-raster technical properties describe the panel as a
  whole, so faulting one of them is a statement about the image rather than about
  a patch of it.
- Every non-passing check appears in the plan, repairable or not, and the whole
  plan is published at ``logs/repair-plan.json``, so a repaired panel can be
  debugged against the strategy it was actually given.

The module plans repairs; it never performs one. It edits no raster, calls no
provider, and names no model, endpoint, or credential, exactly as the provider
boundary in ``AGENTS.md`` requires. Classification reads no clock, locale, or
random seed, so one QA record and one capability flag always produce identical
plan bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .character_identity import STORYBOARD_PATH
from .character_quality import validate_character_identity_check
from .core_primitives import (
    PANEL_CHECK_IDS,
    PANEL_ID_PATTERN,
    canonical_artifact_bytes,
)
from .input_limits import InputResourceLimitError, MAX_JSON_BYTES, loads_bounded_json
from .project_io import (
    ProjectTransaction,
    contained_project_path,
    read_contained_bytes,
)
from .quality_records import GENERIC_EVIDENCE


REPAIR_PLAN_SCHEMA_VERSION = "1.0"
REPAIR_PLAN_PATH = "logs/repair-plan.json"

IDENTITY_CHECK = "character-identity"

# The scope of a defect is the narrowest thing a repair has to touch. `subject`
# names one character, `area` names one bounded region of the panel, and `panel`
# means the fault is a statement about the whole image.
SUBJECT_SCOPE = "subject"
AREA_SCOPE = "area"
PANEL_SCOPE = "panel"
DEFECT_SCOPES = (SUBJECT_SCOPE, AREA_SCOPE, PANEL_SCOPE)

NO_REPAIR = "no-repair"
SELECTIVE_REPAIR = "selective-repair"
FULL_REGENERATION = "full-regeneration"
REPAIR_STRATEGIES = (NO_REPAIR, SELECTIVE_REPAIR, FULL_REGENERATION)

# Why a panel that needed repair could not be repaired selectively. The order is
# the precedence order: an unverifiable accepted panel is reported before a
# missing capability, and a missing capability before an individual defect that
# could not be localized, so one panel always reports the same single reason.
STALE_BINDINGS = "stale-bindings"
EDITING_UNSUPPORTED = "editing-unsupported"
PANEL_WIDE_CHECK = "panel-wide-check"
UNLOCALIZED_EVIDENCE = "unlocalized-evidence"
FALLBACK_REASONS = (
    STALE_BINDINGS,
    EDITING_UNSUPPORTED,
    PANEL_WIDE_CHECK,
    UNLOCALIZED_EVIDENCE,
)

# Camera framing, the scripted beat, cross-panel anchors, and whole-raster
# properties cannot be corrected inside a patch of the image: repairing them
# means producing a different panel. They stay panel-wide however much region
# evidence a reviewer attaches to them.
PANEL_WIDE_CHECKS = frozenset({"action", "composition", "continuity", "technical"})
LOCALIZABLE_CHECKS = tuple(
    check_id for check_id in PANEL_CHECK_IDS if check_id not in PANEL_WIDE_CHECKS
)

# Bounded areas reuse the storyboard's own eight-anchor vocabulary rather than
# introducing pixel rectangles, because the anchor grid is already normative,
# provider-neutral, and defined relative to the panel rectangle.
PANEL_AREAS = (
    "top-left",
    "top-center",
    "top-right",
    "middle-left",
    "middle-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)
DEFECT_REGION_FIELDS = {
    "area",
    "character_id",
    "evidence",
    "repair_guidance",
    "result",
    "severity",
}
RESULTS = frozenset({"pass", "warning", "fail"})
SEVERITIES = frozenset({"warning", "error"})
CHARACTER_ID_LENGTH = 48

BOUND_ARTIFACTS = (
    ("raw_path", "raw_sha256"),
    ("clean_path", "clean_sha256"),
    ("normalization_path", "normalization_sha256"),
)


class RepairStrategyError(ValueError):
    """Raised when a repair plan cannot be derived from trusted inputs."""


@dataclass(frozen=True, slots=True)
class RepairDefect:
    """One non-passing check, and the narrowest scope its evidence supports."""

    check_id: str
    scope: str
    target: str | None
    result: str
    severity: str
    evidence: str
    guidance: str | None
    fallback_reason: str | None

    @property
    def clause(self) -> str:
        """Return the correction text a repair should carry for this defect."""
        return self.guidance if self.guidance is not None else self.evidence

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this defect."""
        return {
            "check_id": self.check_id,
            "evidence": self.evidence,
            "fallback_reason": self.fallback_reason,
            "guidance": self.guidance,
            "result": self.result,
            "scope": self.scope,
            "severity": self.severity,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class RepairTarget:
    """One subject or bounded area a selective repair may edit."""

    scope: str
    target: str
    guidance: tuple[str, ...]
    rank: int

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this target."""
        return {
            "guidance": list(self.guidance),
            "rank": self.rank,
            "scope": self.scope,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class PanelRepairPlan:
    """The complete, ordered repair decision for one reviewed panel."""

    panel_id: str
    decision: str
    strategy: str
    fallback_reason: str | None
    localized_edit_supported: bool
    accepted_raw_path: str
    accepted_raw_sha256: str
    defects: tuple[RepairDefect, ...]
    targets: tuple[RepairTarget, ...]
    unaffected_checks: tuple[str, ...]
    unaffected_subjects: tuple[str, ...]

    @property
    def preserves_accepted_content(self) -> bool:
        """Report whether the plan keeps any accepted content in place."""
        return self.strategy != FULL_REGENERATION

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this panel."""
        return {
            "accepted_raw_path": self.accepted_raw_path,
            "accepted_raw_sha256": self.accepted_raw_sha256,
            "decision": self.decision,
            "defects": [item.as_record() for item in self.defects],
            "fallback_reason": self.fallback_reason,
            "localized_edit_supported": self.localized_edit_supported,
            "panel_id": self.panel_id,
            "strategy": self.strategy,
            "targets": [item.as_record() for item in self.targets],
            "unaffected": {
                "checks": list(self.unaffected_checks),
                "subjects": list(self.unaffected_subjects),
            },
        }


# --------------------------------------------------------------------------- #
# Defect classification
# --------------------------------------------------------------------------- #


def _non_passing(entry: Mapping[str, Any]) -> bool:
    """Report whether a check or region records anything other than a clean pass."""
    return entry.get("result") != "pass" or entry.get("severity") == "warning"


def _text(value: object, label: str) -> str:
    """Return one normalized, specific evidence string, or fail closed."""
    if not isinstance(value, str) or not value.strip():
        raise RepairStrategyError(f"{label} must be a non-empty string")
    normalized = " ".join(value.split())
    if normalized.casefold() in GENERIC_EVIDENCE:
        raise RepairStrategyError(f"{label} must be specific evidence")
    return normalized


def _identity_defects(check: Mapping[str, Any], evidence: str) -> list[RepairDefect]:
    """Classify a failing ``character-identity`` check by reviewed subject."""
    regions = check.get("regions")
    if "provenance" not in check or not isinstance(regions, list) or not regions:
        return [
            RepairDefect(
                IDENTITY_CHECK, PANEL_SCOPE, None,
                str(check.get("result")), str(check.get("severity")),
                evidence, None, UNLOCALIZED_EVIDENCE,
            )
        ]
    defects: list[RepairDefect] = []
    for region in regions:
        if not isinstance(region, Mapping):
            raise RepairStrategyError("character-identity region must be an object")
        if not _non_passing(region):
            continue
        character_id = region.get("character_id")
        if not isinstance(character_id, str) or not character_id.strip():
            raise RepairStrategyError("character-identity region must name a character")
        defects.append(
            RepairDefect(
                IDENTITY_CHECK,
                SUBJECT_SCOPE,
                character_id,
                str(region.get("result")),
                str(region.get("severity")),
                _text(region.get("evidence"), "region evidence"),
                _text(region.get("repair_guidance"), "region repair guidance"),
                None,
            )
        )
    if not defects:
        # A failing parent whose every trait passed cannot say which subject
        # drifted, so the panel is repaired as a whole rather than guessed at.
        return [
            RepairDefect(
                IDENTITY_CHECK, PANEL_SCOPE, None,
                str(check.get("result")), str(check.get("severity")),
                evidence, None, UNLOCALIZED_EVIDENCE,
            )
        ]
    return defects


def _defect_region_scope(region: Mapping[str, Any]) -> tuple[str, str]:
    """Return the scope and target a bounded defect region names, or fail closed."""
    if set(region) != DEFECT_REGION_FIELDS:
        raise RepairStrategyError(
            "defect region fields must be exactly "
            + ", ".join(sorted(DEFECT_REGION_FIELDS))
        )
    character_id = region.get("character_id")
    area = region.get("area")
    named = [value for value in (character_id, area) if value is not None]
    if len(named) != 1:
        raise RepairStrategyError(
            "defect region must name exactly one of character_id or area"
        )
    if character_id is not None:
        if (
            not isinstance(character_id, str)
            or not character_id.strip()
            or len(character_id) > CHARACTER_ID_LENGTH
        ):
            raise RepairStrategyError("defect region character_id must be an ID")
        return SUBJECT_SCOPE, character_id
    if area not in PANEL_AREAS:
        raise RepairStrategyError("defect region area must be a panel anchor area")
    return AREA_SCOPE, str(area)


def _localized_defects(
    check: Mapping[str, Any],
    check_id: str,
    evidence: str,
    reviewed_cast: tuple[str, ...],
) -> list[RepairDefect]:
    """Classify a failing localizable check by its bounded defect regions.

    A subject target has to be a subject this panel's review actually covered.
    The reviewed cast comes from the trait review, so naming a character outside
    it — or naming one at all when no trait review established a cast — cannot be
    verified, and a localized edit aimed at an unverified subject would leave the
    stated preservation boundary meaningless.
    """
    regions = check.get("regions")
    if not isinstance(regions, list):
        raise RepairStrategyError(f"{check_id} regions must be an array")
    defects: list[RepairDefect] = []
    for region in regions:
        if not isinstance(region, Mapping):
            raise RepairStrategyError(f"{check_id} region must be an object")
        scope, target = _defect_region_scope(region)
        if scope == SUBJECT_SCOPE and target not in reviewed_cast:
            raise RepairStrategyError(
                f"{check_id} region names a character this panel's review did "
                f"not cover: {target!r}"
            )
        if region.get("result") not in RESULTS or region.get("severity") not in SEVERITIES:
            raise RepairStrategyError(f"{check_id} region result or severity is invalid")
        if not _non_passing(region):
            continue
        defects.append(
            RepairDefect(
                check_id,
                scope,
                target,
                str(region.get("result")),
                str(region.get("severity")),
                _text(region.get("evidence"), f"{check_id} region evidence"),
                _text(region.get("repair_guidance"), f"{check_id} repair guidance"),
                None,
            )
        )
    if not defects:
        return [
            RepairDefect(
                check_id, PANEL_SCOPE, None,
                str(check.get("result")), str(check.get("severity")),
                evidence, None, UNLOCALIZED_EVIDENCE,
            )
        ]
    return defects


def _check_defects(
    check: Mapping[str, Any], reviewed_cast: tuple[str, ...]
) -> list[RepairDefect]:
    """Classify one panel check into zero or more scoped defects."""
    check_id = check.get("id")
    if check_id not in PANEL_CHECK_IDS:
        raise RepairStrategyError(f"unknown panel check: {check_id!r}")
    if check.get("result") not in RESULTS or check.get("severity") not in SEVERITIES:
        raise RepairStrategyError(f"{check_id} result or severity is invalid")
    if not _non_passing(check):
        return []
    evidence = _text(check.get("evidence"), f"{check_id} evidence")
    if check_id in PANEL_WIDE_CHECKS:
        return [
            RepairDefect(
                str(check_id), PANEL_SCOPE, None,
                str(check.get("result")), str(check.get("severity")),
                evidence, None, PANEL_WIDE_CHECK,
            )
        ]
    if check_id == IDENTITY_CHECK:
        return _identity_defects(check, evidence)
    return _localized_defects(check, str(check_id), evidence, reviewed_cast)


def _validated_identity_check(
    record: Mapping[str, Any], checks: list[Mapping[str, Any]], panel_id: str
) -> Mapping[str, Any]:
    """Return the panel's ``character-identity`` check once it can be trusted.

    A subject-scoped repair edits accepted artwork, so the region that names the
    subject has to be the region the character-quality gate would accept. The
    trait contract is validated by its owner rather than re-implemented here, and
    the record must also agree with itself: provenance names this panel, and no
    region faults a character the review never covered. Without those checks a
    malformed region could aim a localized edit at an arbitrary character.
    """
    identity = next(check for check in checks if check.get("id") == IDENTITY_CHECK)
    issues = validate_character_identity_check(
        identity,
        allow_override=(
            "override_reason" in record
            and identity.get("result") == "fail"
            and identity.get("severity") == "warning"
        ),
    )
    if issues:
        raise RepairStrategyError(
            f"character-identity check cannot be trusted: {issues[0]}"
        )
    provenance = identity.get("provenance")
    if isinstance(provenance, Mapping):
        if provenance.get("panel_id") != panel_id:
            raise RepairStrategyError(
                "character-identity provenance names a different panel"
            )
        reviewed = {
            entry.get("character_id")
            for entry in provenance.get("characters") or []
            if isinstance(entry, Mapping)
        }
        faulted = {
            region["character_id"]
            for region in identity.get("regions") or []
            if isinstance(region, Mapping)
        }
        unreviewed = sorted(faulted - reviewed)
        if unreviewed:
            raise RepairStrategyError(
                "character-identity region names an unreviewed character: "
                f"{unreviewed[0]!r}"
            )
    return identity


def _ordered_checks(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return a record's seven checks in normative order, or fail closed."""
    checks = record.get("checks")
    if not isinstance(checks, list):
        raise RepairStrategyError("panel checks must be an array")
    if not all(isinstance(check, Mapping) for check in checks):
        raise RepairStrategyError("panel checks must contain objects")
    identifiers = tuple(check.get("id") for check in checks)
    if identifiers != PANEL_CHECK_IDS:
        raise RepairStrategyError(
            "panel checks must be the seven required checks in normative order"
        )
    return list(checks)


def _reviewed_cast(identity: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the characters this panel's trait review covered, in review order.

    Provenance is the authority on who was reviewed, not the region list: a
    region can only be trusted to name a subject once that subject appears here,
    and a panel with no trait review has established no cast at all.
    """
    provenance = identity.get("provenance")
    if not isinstance(provenance, Mapping):
        return ()
    characters = provenance.get("characters")
    if not isinstance(characters, list):
        return ()
    reviewed = [
        entry["character_id"]
        for entry in characters
        if isinstance(entry, Mapping) and isinstance(entry.get("character_id"), str)
    ]
    return tuple(dict.fromkeys(reviewed))


def _repair_targets(defects: list[RepairDefect]) -> tuple[RepairTarget, ...]:
    """Group localized defects into ordered, deduplicated repair targets."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for defect in defects:
        if defect.scope == PANEL_SCOPE or defect.target is None:
            continue
        grouped.setdefault((defect.scope, defect.target), []).append(defect.clause)
    return tuple(
        RepairTarget(scope, target, tuple(clauses), rank)
        for rank, ((scope, target), clauses) in enumerate(grouped.items(), start=1)
    )


def panel_repair_plan(
    record: Mapping[str, Any],
    *,
    localized_edit_supported: bool,
    accepted_content_stale: bool = False,
) -> PanelRepairPlan:
    """Return the repair decision for one schema-2.0 panel QA record.

    The decision is a pure function of the record, the capability flag, and
    whether the accepted artifacts still match their bindings, so the same three
    inputs always select the same strategy and record the same reason.
    """
    if not isinstance(record, Mapping):
        raise RepairStrategyError("panel QA record must be a JSON object")
    if record.get("schema_version") != "2.0":
        raise RepairStrategyError("repair planning requires a schema-2.0 panel QA record")
    panel_id = record.get("subject_id")
    if not isinstance(panel_id, str) or PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise RepairStrategyError("panel QA subject_id must match pNN-NN")
    decision = record.get("decision")
    if decision not in {"accept", "accept-warning", "regenerate"}:
        raise RepairStrategyError("unknown panel quality decision")
    bindings = record.get("bindings")
    if not isinstance(bindings, Mapping):
        raise RepairStrategyError("panel QA bindings must be an object")
    raw_path = bindings.get("raw_path")
    raw_sha256 = bindings.get("raw_sha256")
    if not isinstance(raw_path, str) or not isinstance(raw_sha256, str):
        raise RepairStrategyError("panel QA bindings must bind the accepted raster")

    checks = _ordered_checks(record)
    identity = _validated_identity_check(record, checks, panel_id)
    reviewed_cast = _reviewed_cast(identity)
    defects: list[RepairDefect] = []
    for check in checks:
        defects.extend(_check_defects(check, reviewed_cast))
    unaffected_checks = tuple(
        str(check["id"]) for check in checks if not _non_passing(check)
    )
    faulted_subjects = {
        defect.target for defect in defects if defect.scope == SUBJECT_SCOPE
    }
    unaffected_subjects = tuple(
        subject for subject in reviewed_cast if subject not in faulted_subjects
    )

    if decision != "regenerate":
        # An accepted panel is not repaired. Its warnings are still recorded, so
        # the plan states what remains unresolved instead of implying a clean run.
        strategy, fallback_reason = NO_REPAIR, None
    elif accepted_content_stale:
        strategy, fallback_reason = FULL_REGENERATION, STALE_BINDINGS
    elif not localized_edit_supported:
        strategy, fallback_reason = FULL_REGENERATION, EDITING_UNSUPPORTED
    else:
        blocked = next(
            (defect.fallback_reason for defect in defects if defect.fallback_reason),
            None,
        )
        if blocked is not None:
            strategy, fallback_reason = FULL_REGENERATION, blocked
        elif not defects:
            raise RepairStrategyError(
                "a panel requiring regeneration must record a non-passing check"
            )
        else:
            strategy, fallback_reason = SELECTIVE_REPAIR, None

    return PanelRepairPlan(
        panel_id=panel_id,
        decision=str(decision),
        strategy=strategy,
        fallback_reason=fallback_reason,
        localized_edit_supported=bool(localized_edit_supported),
        accepted_raw_path=raw_path,
        accepted_raw_sha256=raw_sha256,
        defects=tuple(defects),
        targets=_repair_targets(defects) if strategy == SELECTIVE_REPAIR else (),
        unaffected_checks=unaffected_checks,
        unaffected_subjects=unaffected_subjects,
    )


# --------------------------------------------------------------------------- #
# Accepted content verification
# --------------------------------------------------------------------------- #


def accepted_content_is_stale(project_dir: Path, record: Mapping[str, Any]) -> bool:
    """Report whether a reviewed panel's bound artifacts still match the record.

    Selective repair only makes sense while the accepted bytes are the bytes the
    review accepted. An unreadable or changed binding is not an error here: it
    withdraws the preservation guarantee, so the plan falls back to a full
    regeneration and records ``stale-bindings`` rather than silently preserving
    content nobody verified.
    """
    bindings = record.get("bindings")
    if not isinstance(bindings, Mapping):
        return True
    for path_field, hash_field in BOUND_ARTIFACTS:
        relative = bindings.get(path_field)
        expected = bindings.get(hash_field)
        if not isinstance(relative, str) or not isinstance(expected, str):
            return True
        try:
            payload = read_contained_bytes(Path(project_dir), relative)
        except (OSError, ValueError):
            return True
        if hashlib.sha256(payload).hexdigest() != expected:
            return True
    return False


# --------------------------------------------------------------------------- #
# Project-wide planning
# --------------------------------------------------------------------------- #


def _read_document(project_dir: Path, relative: str) -> Mapping[str, Any]:
    """Read one contained JSON object without following symlinks, or fail closed."""
    try:
        value = loads_bounded_json(
            read_contained_bytes(
                Path(project_dir), relative, max_bytes=MAX_JSON_BYTES
            ),
            source=relative,
        )
    except InputResourceLimitError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RepairStrategyError(
            f"{relative} cannot be read as JSON: {type(error).__name__}"
        ) from error
    if not isinstance(value, Mapping):
        raise RepairStrategyError(f"{relative} must contain a JSON object")
    return value


def _review_exists(project_dir: Path, relative: str) -> bool:
    """Report whether a panel QA record is present, or fail closed.

    ``lstat`` distinguishes the two cases that must not be confused: a review
    that was never written, which simply has no plan yet, and a review that
    exists but cannot be trusted — a refused path, a symlink, a directory, or an
    unreadable entry. Only the first is a skip; the second is an error, because a
    silently skipped panel is exactly the panel whose repair nobody would notice
    was missing.
    """
    try:
        path = contained_project_path(Path(project_dir), relative)
    except ValueError as error:
        raise RepairStrategyError(f"{relative} is not a contained path: {error}") from error
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RepairStrategyError(
            f"{relative} cannot be inspected: {type(error).__name__}"
        ) from error
    return True


def storyboard_panel_ids(storyboard: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every storyboard panel ID in page and reading order.

    A malformed page, panel list, panel, or panel ID is rejected rather than
    skipped: a skipped panel is exactly the panel whose repair nobody could
    afterwards account for.
    """
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise RepairStrategyError("storyboard pages must be a list")
    identifiers: list[str] = []
    for page_index, page in enumerate(pages):
        prefix = f"storyboard pages[{page_index}]"
        if not isinstance(page, Mapping):
            raise RepairStrategyError(f"{prefix} must be an object")
        panels = page.get("panels")
        if not isinstance(panels, list):
            raise RepairStrategyError(f"{prefix}.panels must be an array")
        for panel_index, panel in enumerate(panels):
            panel_prefix = f"{prefix}.panels[{panel_index}]"
            if not isinstance(panel, Mapping):
                raise RepairStrategyError(f"{panel_prefix} must be an object")
            panel_id = panel.get("id")
            if not isinstance(panel_id, str):
                raise RepairStrategyError(f"{panel_prefix}.id must be a string")
            identifiers.append(panel_id)
    if len(set(identifiers)) != len(identifiers):
        raise RepairStrategyError("storyboard must not repeat a panel id")
    return tuple(identifiers)


def panel_repair_plans(
    project_dir: Path, *, localized_edit_supported: bool
) -> tuple[PanelRepairPlan, ...]:
    """Return a repair plan for every reviewed panel, in storyboard order.

    A panel with no QA record has not been reviewed yet and therefore carries no
    repair decision. Only a genuinely absent record is skipped: a record that
    exists but cannot be resolved, read, or classified fails closed, because
    treating an unreadable review as an absent one would publish a plan that
    quietly omits the one panel whose repair strategy matters.
    """
    project_dir = Path(project_dir)
    storyboard = _read_document(project_dir, STORYBOARD_PATH)
    plans: list[PanelRepairPlan] = []
    for panel_id in storyboard_panel_ids(storyboard):
        relative = f"qa/panels/{panel_id}.json"
        if not _review_exists(project_dir, relative):
            continue
        record = _read_document(project_dir, relative)
        plans.append(
            panel_repair_plan(
                record,
                localized_edit_supported=localized_edit_supported,
                accepted_content_stale=accepted_content_is_stale(project_dir, record),
            )
        )
    return tuple(plans)


def project_repair_plan(
    project_dir: Path, *, localized_edit_supported: bool
) -> dict[str, Any]:
    """Return the whole project's repair-plan document."""
    return {
        "panels": [
            plan.as_record()
            for plan in panel_repair_plans(
                project_dir, localized_edit_supported=localized_edit_supported
            )
        ],
        "schema_version": REPAIR_PLAN_SCHEMA_VERSION,
    }


def read_repair_plan(project_dir: Path) -> Mapping[str, Any]:
    """Read the published repair plan, or fail closed."""
    return _read_document(Path(project_dir), REPAIR_PLAN_PATH)


def recorded_panel_plan(
    project_dir: Path, panel_id: str
) -> Mapping[str, Any] | None:
    """Return one panel's recorded plan entry, or ``None`` when none is published."""
    try:
        document = read_repair_plan(project_dir)
    except (RepairStrategyError, OSError):
        return None
    panels = document.get("panels")
    if not isinstance(panels, list):
        return None
    return next(
        (
            entry
            for entry in panels
            if isinstance(entry, Mapping) and entry.get("panel_id") == panel_id
        ),
        None,
    )


def write_repair_plan(project_dir: Path, document: Mapping[str, Any]) -> Path:
    """Publish an already-derived repair-plan document atomically.

    Prefer ``plan_and_write_repair_plan``, which derives and publishes under one
    lock. This entry point is for a document the caller already holds, and it
    cannot promise that the reviews behind that document are still current.
    """
    project_dir = Path(project_dir)
    payload = canonical_artifact_bytes(document)
    with ProjectTransaction(project_dir, "repair-plan") as transaction:
        transaction.stage_bytes(REPAIR_PLAN_PATH, payload)
    return project_dir / REPAIR_PLAN_PATH


def plan_and_write_repair_plan(
    project_dir: Path, *, localized_edit_supported: bool
) -> Path:
    """Plan every reviewed panel's repair, then publish the record.

    Reading the reviews, hashing the bound artifacts, and publishing the plan all
    happen inside one transaction, so they share the lock that serializes every
    other project operation. Splitting them would let a concurrent review or a
    replaced raster land between the hash and the publication, and the plan would
    then claim to preserve bytes that were no longer the reviewed bytes.

    Re-running this on an unchanged project rewrites byte-identical content, so a
    resume repairs exactly what the interrupted run planned to repair.
    """
    project_dir = Path(project_dir)
    with ProjectTransaction(project_dir, "repair-plan") as transaction:
        document = project_repair_plan(
            project_dir, localized_edit_supported=localized_edit_supported
        )
        transaction.stage_bytes(REPAIR_PLAN_PATH, canonical_artifact_bytes(document))
    return project_dir / REPAIR_PLAN_PATH


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_defect_regions(check: Mapping[str, Any]) -> tuple[str, ...]:
    """Return stable categories for the bounded defect regions of one check.

    Only the localizable checks may carry bounded regions. A panel-wide check
    with regions is rejected rather than honoured, because accepting it would
    invite a localized repair for a fault that is not localized. The
    ``character-identity`` check keeps its own trait-region contract and is
    validated by ``character_quality.py``, so it is not re-judged here.
    """
    check_id = check.get("id")
    if check_id == IDENTITY_CHECK:
        return ()
    regions = check.get("regions")
    if not isinstance(regions, list):
        return ("repair-region-structure",)
    if not regions:
        return ()
    if check_id not in LOCALIZABLE_CHECKS:
        return ("repair-region-scope",)

    issues: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for region in regions:
        if not isinstance(region, Mapping):
            issues.add("repair-region-structure")
            continue
        try:
            key = _defect_region_scope(region)
        except RepairStrategyError:
            issues.add("repair-region-structure")
            continue
        if key in seen:
            issues.add("repair-region-duplicate")
        seen.add(key)
        if region.get("result") not in RESULTS:
            issues.add("repair-region-result")
        if region.get("severity") not in SEVERITIES:
            issues.add("repair-region-severity")
        evidence = region.get("evidence")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or " ".join(evidence.split()).casefold() in GENERIC_EVIDENCE
        ):
            issues.add("repair-region-evidence")
        guidance = region.get("repair_guidance")
        needs_repair = _non_passing(region)
        if needs_repair and (not isinstance(guidance, str) or not guidance.strip()):
            issues.add("repair-region-guidance")
        if not needs_repair and guidance is not None:
            issues.add("repair-region-guidance")
    return tuple(sorted(issues))


def validate_repair_plan(project_dir: Path) -> tuple[str, ...]:
    """Return stable categories for a published repair plan, if one exists.

    The plan is re-derived from the current QA records and compared with what was
    published. A plan that no longer matches its inputs is stale, which is the
    only honest verdict: it described a repair for a panel that has since been
    reviewed again.
    """
    project_dir = Path(project_dir)
    try:
        document = read_repair_plan(project_dir)
    except RepairStrategyError:
        return ("repair-plan-structure",)
    if set(document) != {"panels", "schema_version"}:
        return ("repair-plan-structure",)
    if document.get("schema_version") != REPAIR_PLAN_SCHEMA_VERSION:
        return ("repair-plan-schema-version",)
    panels = document.get("panels")
    if not isinstance(panels, list):
        return ("repair-plan-structure",)

    issues: set[str] = set()
    try:
        storyboard = _read_document(project_dir, STORYBOARD_PATH)
        ordering = storyboard_panel_ids(storyboard)
    except RepairStrategyError:
        return ("repair-plan-storyboard",)

    recorded_ids: list[str] = []
    for entry in panels:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("panel_id"), str):
            issues.add("repair-plan-structure")
            continue
        panel_id = str(entry["panel_id"])
        recorded_ids.append(panel_id)
        if panel_id not in ordering:
            issues.add("repair-plan-panel-unknown")
            continue
        supported = entry.get("localized_edit_supported")
        if not isinstance(supported, bool):
            issues.add("repair-plan-structure")
            continue
        relative = f"qa/panels/{panel_id}.json"
        try:
            record = _read_document(project_dir, relative)
        except RepairStrategyError:
            issues.add("repair-plan-record-missing")
            continue
        try:
            expected = panel_repair_plan(
                record,
                localized_edit_supported=supported,
                accepted_content_stale=accepted_content_is_stale(project_dir, record),
            ).as_record()
        except RepairStrategyError:
            issues.add("repair-plan-record-invalid")
            continue
        if dict(entry) != expected and record.get("decision") == "regenerate":
            # A panel still awaiting repair must carry a current plan. An entry
            # for a panel the review has since accepted describes a repair that
            # already succeeded, so it is history rather than a stale claim.
            issues.add("repair-plan-stale")
    if len(set(recorded_ids)) != len(recorded_ids):
        issues.add("repair-plan-panel-duplicate")
    expected_order = [panel_id for panel_id in ordering if panel_id in set(recorded_ids)]
    if recorded_ids != expected_order:
        issues.add("repair-plan-panel-order")
    if _unplanned_repairs(project_dir, ordering, set(recorded_ids)):
        # Checking only the entries that are present would let a truncated plan
        # validate while omitting a panel that still has to be repaired.
        issues.add("repair-plan-incomplete")
    return tuple(sorted(issues))


def _unplanned_repairs(
    project_dir: Path, ordering: tuple[str, ...], recorded: set[str]
) -> tuple[str, ...]:
    """Return reviewed panels awaiting repair that the plan does not cover."""
    unplanned: list[str] = []
    for panel_id in ordering:
        if panel_id in recorded:
            continue
        relative = f"qa/panels/{panel_id}.json"
        try:
            if not _review_exists(project_dir, relative):
                continue
            record = _read_document(project_dir, relative)
        except RepairStrategyError:
            unplanned.append(panel_id)
            continue
        if record.get("decision") == "regenerate":
            unplanned.append(panel_id)
    return tuple(unplanned)


# --------------------------------------------------------------------------- #
# Provider-neutral rendering
# --------------------------------------------------------------------------- #


def repair_plan_block(plan: PanelRepairPlan) -> str:
    """Render one panel's repair decision as deterministic, plain text.

    The block is what an agent reads before repairing: the strategy, the exact
    targets it may touch, the accepted content it must leave alone, and the
    correction text derived from the review.
    """
    editing = "supported" if plan.localized_edit_supported else "unsupported"
    strategy = plan.strategy
    if plan.fallback_reason is not None:
        strategy = f"{strategy} ({plan.fallback_reason})"
    lines = [
        f"REPAIR PLAN (repair-plan {REPAIR_PLAN_SCHEMA_VERSION})",
        f"- panel: {plan.panel_id}",
        f"- quality decision: {plan.decision}",
        f"- strategy: {strategy}",
        f"- localized editing: {editing}",
        f"- accepted raster: {plan.accepted_raw_path} ({plan.accepted_raw_sha256})",
    ]
    if plan.strategy == NO_REPAIR:
        lines.append("- repair nothing: the review accepted this panel")
    elif plan.strategy == SELECTIVE_REPAIR:
        lines.append("- repair only these targets, leaving all other pixels unchanged:")
        for target in plan.targets:
            lines.append(f"  {target.rank}. {target.scope} {target.target}")
            lines.extend(f"     {clause}" for clause in target.guidance)
    else:
        lines.append("- regenerate the whole panel with one correction clause:")
        lines.extend(f"    {defect.clause}" for defect in plan.defects)
    lines.append(
        "- preserve:"
        if plan.preserves_accepted_content
        else "- reviewed clean, but a full regeneration re-rolls them:"
    )
    lines.append(
        "  checks: "
        + (", ".join(plan.unaffected_checks) if plan.unaffected_checks else "none")
    )
    lines.append(
        "  subjects: "
        + (", ".join(plan.unaffected_subjects) if plan.unaffected_subjects else "none")
    )
    if plan.defects:
        lines.append("- defects:")
        for defect in plan.defects:
            target = "panel" if defect.target is None else f"{defect.scope} {defect.target}"
            reason = "" if defect.fallback_reason is None else f" [{defect.fallback_reason}]"
            lines.append(
                f"  {defect.check_id} {target} "
                f"{defect.result}/{defect.severity}{reason}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan how each reviewed panel should be repaired, and record why."
        ),
    )
    parser.add_argument("project_dir", type=Path, help="generated project directory")
    parser.add_argument(
        "--localized-edit",
        action="store_true",
        dest="localized_edit",
        help=(
            "the detected capability can edit a bounded part of an existing "
            "raster; omit it and every repair plans a full regeneration"
        ),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--plan",
        action="store_true",
        help="publish the whole project's repair-plan record atomically",
    )
    action.add_argument(
        "--panel",
        metavar="PANEL_ID",
        help="print the provider-neutral repair plan for one reviewed panel",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.plan:
            path = plan_and_write_repair_plan(
                arguments.project_dir,
                localized_edit_supported=arguments.localized_edit,
            )
            print(path.as_posix())
            return 0

        project_dir = Path(arguments.project_dir)
        record = _read_document(project_dir, f"qa/panels/{arguments.panel}.json")
        plan = panel_repair_plan(
            record,
            localized_edit_supported=arguments.localized_edit,
            accepted_content_stale=accepted_content_is_stale(project_dir, record),
        )
        print(repair_plan_block(plan))
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
