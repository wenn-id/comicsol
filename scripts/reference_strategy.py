#!/usr/bin/env python3
"""Shot-aware character reference selection with recorded provenance.

One reference image, or one prose description restated per panel, does not
constrain a character equally well across every camera setup: a close-up needs
the face, a full-body panel needs the whole silhouette, and a profile needs the
head from the side. This module turns the reference views carried by the
Character Identity Pack into an explicit, ordered attachment plan for each
storyboard panel, so reference use is a stated rule instead of a per-panel
improvisation.

Three properties matter more than the ranking itself:

- The canonical view leads every panel. It is the only view cross-checked
  against ``plan/character-bible.json``, so identity stays anchored by the
  reference the bible names before any supplementary or scene-specific view.
- A reference is never attached twice. Views that resolve to one path count once,
  which keeps a provider's hard reference limit spent on distinct information
  rather than on the same sheet under two names.
- Every selection and every omission carries the reason that produced it, and the
  whole plan is published at ``logs/reference-selection.json`` so a drifted panel
  can be debugged against the references it was actually given.

The module is provider-neutral by construction. It emits plain text, relative
project paths, and honours one integer budget the caller supplies; it names no
provider, model, endpoint, or credential, and it transmits nothing. Selection
reads no clock, locale, or random seed, so one pack and one storyboard always
produce identical plan bytes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .character_identity import (
    CANONICAL_VIEW,
    STORYBOARD_PATH,
    check_identity_pack,
    read_identity_pack,
)
from .core_primitives import canonical_artifact_bytes
from .input_limits import MAX_JSON_BYTES, loads_bounded_json
from .project_io import ProjectTransaction, contained_project_path, read_contained_bytes


REFERENCE_PLAN_SCHEMA_VERSION = "1.0"
REFERENCE_PLAN_PATH = "logs/reference-selection.json"

CLOSE_UP = "close-up"
PROFILE = "profile"
THREE_QUARTER = "three-quarter"
FULL_BODY = "full-body"
UNCLASSIFIED = "unclassified"
SHOT_CLASSES = (CLOSE_UP, PROFILE, THREE_QUARTER, FULL_BODY, UNCLASSIFIED)

# Cues are matched against the storyboard panel's free-text `shot` field. The cue
# that appears earliest wins, so a description that opens with its framing is not
# reclassified by a later incidental word; an equal position prefers the longer
# cue, then the class order below. A cue counts only at word boundaries and only
# when no negation governs it, so `profiled character` and `not a close-up` do not
# declare a framing. A panel whose shot matches no cue stays `unclassified` and is
# served the plain identity order instead of being guessed into a class it never
# declared.
SHOT_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CLOSE_UP,
        (
            "extreme close",
            "close-up",
            "close up",
            "closeup",
            "close shot",
            "close on",
            "head shot",
            "headshot",
            "face shot",
            "insert shot",
            "detail shot",
        ),
    ),
    (
        PROFILE,
        ("profile", "side view", "side-view", "side-on", "from the side"),
    ),
    (
        THREE_QUARTER,
        (
            "three-quarter",
            "three quarter",
            "3/4",
            "over-the-shoulder",
            "over the shoulder",
            "medium-wide",
            "medium wide",
            "medium shot",
            "mid-shot",
            "mid shot",
            "waist-up",
            "waist up",
            "bust shot",
        ),
    ),
    (
        FULL_BODY,
        (
            "full-body",
            "full body",
            "full-length",
            "full length",
            "full figure",
            "full shot",
            "head-to-toe",
            "head to toe",
            "wide establishing",
            "establishing shot",
            "wide shot",
            "wide angle",
            "wide view",
            "wide framing",
            "long shot",
            "two-shot",
            "two shot",
        ),
    ),
)

# A framing word inside a longer word is a different word, so a cue must begin and
# end on a word boundary. Hyphens stay allowed on both sides because authored prose
# compounds framing words (`medium-wide shot`), and a trailing plural is allowed
# because `a series of close-ups` still declares close framing.
def _cue_matcher(cue: str) -> re.Pattern[str]:
    """Compile one shot cue into a word-boundary matcher."""
    return re.compile(rf"(?<!\w){re.escape(cue)}s?(?!\w)")


SHOT_CUE_MATCHERS: tuple[tuple[str, tuple[tuple[str, re.Pattern[str]], ...]], ...] = tuple(
    (shot_class, tuple((cue, _cue_matcher(cue)) for cue in cues))
    for shot_class, cues in SHOT_CUES
)

# A cue the author explicitly rules out is not the panel's framing. Only the words
# immediately before a cue can govern it, so the window is small and fixed rather
# than a scan of the whole sentence.
NEGATION_TOKENS = frozenset(
    {
        "avoid",
        "avoiding",
        "except",
        "instead",
        "never",
        "no",
        "none",
        "nor",
        "not",
        "rather",
        "without",
    }
)
NEGATION_LOOKBEHIND_WORDS = 3
_TOKEN_EDGE_CHARACTERS = "\"'(),.;:!?-"

IDENTITY_VIEWS = (CANONICAL_VIEW, CLOSE_UP, PROFILE, THREE_QUARTER, FULL_BODY)

# Selection order per shot class. The canonical view leads every list because it
# is the only view validated against the character bible, so identity is anchored
# before anything else is added. The shot's own view comes next, then the
# remaining identity views in the order that still constrains the needed framing
# best. A view outside this table is scene-specific and always ranks last.
SHOT_VIEW_PREFERENCE: dict[str, tuple[str, ...]] = {
    CLOSE_UP: (CANONICAL_VIEW, CLOSE_UP, THREE_QUARTER, PROFILE, FULL_BODY),
    PROFILE: (CANONICAL_VIEW, PROFILE, THREE_QUARTER, CLOSE_UP, FULL_BODY),
    THREE_QUARTER: (CANONICAL_VIEW, THREE_QUARTER, PROFILE, FULL_BODY, CLOSE_UP),
    FULL_BODY: (CANONICAL_VIEW, FULL_BODY, THREE_QUARTER, PROFILE, CLOSE_UP),
    UNCLASSIFIED: IDENTITY_VIEWS,
}

CANONICAL_ANCHOR = "canonical-anchor"
SHOT_ALIGNED = "shot-aligned"
IDENTITY_SUPPLEMENT = "identity-supplement"
SCENE_SPECIFIC = "scene-specific"
SELECTION_REASONS = (
    CANONICAL_ANCHOR,
    SHOT_ALIGNED,
    IDENTITY_SUPPLEMENT,
    SCENE_SPECIFIC,
)

DUPLICATE_PATH = "duplicate-path"
REFERENCE_BUDGET = "reference-budget"
REFERENCES_UNSUPPORTED = "references-unsupported"
OMISSION_REASONS = (DUPLICATE_PATH, REFERENCE_BUDGET, REFERENCES_UNSUPPORTED)


class ReferenceStrategyError(ValueError):
    """Raised when a reference plan cannot be derived from trusted inputs."""


@dataclass(frozen=True, slots=True)
class ReferenceSelection:
    """One reference view an adapter should attach, and why."""

    character_id: str
    view: str
    path: str
    reason: str
    rank: int

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this selection."""
        return {
            "character_id": self.character_id,
            "path": self.path,
            "rank": self.rank,
            "reason": self.reason,
            "view": self.view,
        }


@dataclass(frozen=True, slots=True)
class ReferenceOmission:
    """One reference view that was deliberately not attached, and why."""

    character_id: str
    view: str
    path: str
    reason: str

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this omission."""
        return {
            "character_id": self.character_id,
            "path": self.path,
            "reason": self.reason,
            "view": self.view,
        }


@dataclass(frozen=True, slots=True)
class PanelReferencePlan:
    """The complete, ordered reference decision for one storyboard panel."""

    panel_id: str
    shot_class: str
    shot_cue: str | None
    reference_budget: int | None
    character_ids: tuple[str, ...]
    selected: tuple[ReferenceSelection, ...]
    omitted: tuple[ReferenceOmission, ...]

    @property
    def attachment_paths(self) -> tuple[str, ...]:
        """Return the distinct relative paths to attach, in attachment order."""
        return tuple(item.path for item in self.selected)

    def as_record(self) -> dict[str, Any]:
        """Return the canonical provenance record for this panel."""
        return {
            "characters": list(self.character_ids),
            "omitted": [item.as_record() for item in self.omitted],
            "panel_id": self.panel_id,
            "reference_budget": self.reference_budget,
            "selected": [item.as_record() for item in self.selected],
            "shot_class": self.shot_class,
            "shot_cue": self.shot_cue,
        }


# --------------------------------------------------------------------------- #
# Shot classification
# --------------------------------------------------------------------------- #


def _negated(text: str, position: int) -> bool:
    """Report whether a negation word governs the cue found at ``position``."""
    preceding = text[:position].split()[-NEGATION_LOOKBEHIND_WORDS:]
    return any(
        token.strip(_TOKEN_EDGE_CHARACTERS) in NEGATION_TOKENS for token in preceding
    )


def classify_shot(shot: object) -> tuple[str, str | None]:
    """Return the closed shot class for a panel shot, and the cue that chose it.

    Classification is a pure function of the authored text: the same `shot`
    string always produces the same class. A cue counts only when it stands as a
    word and is not governed by a negation, and an unrecognized description is
    reported as ``unclassified`` with no cue rather than being guessed.
    """
    if not isinstance(shot, str):
        return UNCLASSIFIED, None
    text = shot.casefold()
    best: tuple[int, int, int, str, str] | None = None
    for class_index, (shot_class, matchers) in enumerate(SHOT_CUE_MATCHERS):
        for cue, matcher in matchers:
            for match in matcher.finditer(text):
                if _negated(text, match.start()):
                    continue
                candidate = (match.start(), -len(cue), class_index, shot_class, cue)
                if best is None or candidate < best:
                    best = candidate
                break
    if best is None:
        return UNCLASSIFIED, None
    return best[3], best[4]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def pack_entries(
    pack: Mapping[str, Any], character_ids: Iterable[str]
) -> list[Mapping[str, Any]]:
    """Return the requested identity-pack entries in pack order.

    Pack order, not panel order, keeps one character's reference ordering stable
    project-wide, exactly as the identity prompt block does. An ID the pack does
    not carry fails closed instead of silently generating an unreferenced
    character.
    """
    characters = pack.get("characters")
    if not isinstance(characters, list):
        raise ReferenceStrategyError("identity pack characters must be a list")
    entries = {
        entry["id"]: entry
        for entry in characters
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    requested = set(character_ids)
    unknown = sorted(requested - set(entries))
    if unknown:
        raise ReferenceStrategyError(
            "identity pack has no entry for: " + ", ".join(unknown)
        )
    return [entry for key, entry in entries.items() if key in requested]


def ranked_views(
    entry: Mapping[str, Any], shot_class: str
) -> list[tuple[str, str, str]]:
    """Return one character's ``(view, path, reason)`` candidates, best first.

    Ranking is total and deterministic: the preference order decides first, then
    the view name, then the path, so two views a shot class does not distinguish
    still resolve to one fixed order.
    """
    preference = SHOT_VIEW_PREFERENCE.get(shot_class)
    if preference is None:
        raise ReferenceStrategyError(f"unknown shot class: {shot_class!r}")
    aligned = None if shot_class == UNCLASSIFIED else shot_class
    ranked: list[tuple[int, str, str, str]] = []
    for view in entry.get("reference_views") or []:
        if not isinstance(view, Mapping):
            continue
        name = view.get("view")
        path = view.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            continue
        if name in preference:
            rank = preference.index(name)
            if name == CANONICAL_VIEW:
                reason = CANONICAL_ANCHOR
            elif name == aligned:
                reason = SHOT_ALIGNED
            else:
                reason = IDENTITY_SUPPLEMENT
        else:
            rank = len(preference)
            reason = SCENE_SPECIFIC
        ranked.append((rank, name, path, reason))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(name, path, reason) for _, name, path, reason in ranked]


def _validated_budget(reference_budget: object) -> int | None:
    """Return a validated reference budget, where ``None`` means unlimited."""
    if reference_budget is None:
        return None
    if (
        not isinstance(reference_budget, int)
        or isinstance(reference_budget, bool)
        or reference_budget < 0
    ):
        raise ReferenceStrategyError(
            "reference budget must be None or a non-negative integer"
        )
    return reference_budget


def _panel_character_ids(panel: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a panel's unique character IDs in authored order."""
    characters = panel.get("characters")
    if not isinstance(characters, list) or any(
        not isinstance(item, str) for item in characters
    ):
        raise ReferenceStrategyError(
            f"storyboard panel '{panel.get('id')}' characters must be an array of IDs"
        )
    return tuple(dict.fromkeys(characters))


def _plan_panel(
    pack: Mapping[str, Any],
    panel: Mapping[str, Any],
    reference_budget: int | None,
) -> PanelReferencePlan:
    """Build one panel's plan by spending the budget breadth-first.

    Breadth-first across characters is the whole point of the allocation: every
    character in the panel receives its canonical anchor before any character
    receives a second view, so a tight provider limit degrades by dropping
    supplementary detail instead of dropping a character's identity entirely.
    """
    panel_id = panel.get("id")
    if not isinstance(panel_id, str):
        raise ReferenceStrategyError("storyboard panel id must be a string")
    shot_class, shot_cue = classify_shot(panel.get("shot"))
    entries = pack_entries(pack, _panel_character_ids(panel))
    candidates = [
        (str(entry["id"]), ranked_views(entry, shot_class)) for entry in entries
    ]

    selected: list[ReferenceSelection] = []
    omitted: list[ReferenceOmission] = []
    attached: set[str] = set()
    depth = max((len(views) for _, views in candidates), default=0)
    for index in range(depth):
        for character_id, views in candidates:
            if index >= len(views):
                continue
            view, path, reason = views[index]
            if reference_budget == 0:
                omitted.append(
                    ReferenceOmission(character_id, view, path, REFERENCES_UNSUPPORTED)
                )
                continue
            if path in attached:
                omitted.append(
                    ReferenceOmission(character_id, view, path, DUPLICATE_PATH)
                )
                continue
            if reference_budget is not None and len(selected) >= reference_budget:
                omitted.append(
                    ReferenceOmission(character_id, view, path, REFERENCE_BUDGET)
                )
                continue
            attached.add(path)
            selected.append(
                ReferenceSelection(
                    character_id, view, path, reason, len(selected) + 1
                )
            )

    return PanelReferencePlan(
        panel_id=panel_id,
        shot_class=shot_class,
        shot_cue=shot_cue,
        reference_budget=reference_budget,
        character_ids=tuple(str(entry["id"]) for entry in entries),
        selected=tuple(selected),
        omitted=tuple(omitted),
    )


def _storyboard_panels(storyboard: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return every storyboard panel in page and reading order.

    A malformed page, panel list, panel, or panel ID is rejected rather than
    skipped. Skipping one would publish a plan that silently covers fewer panels
    than the storyboard has, and a panel with no recorded plan is exactly the
    panel whose references nobody can later account for.
    """
    if not isinstance(storyboard, Mapping):
        raise ReferenceStrategyError("storyboard must be a JSON object")
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ReferenceStrategyError("storyboard pages must be a list")

    panels: list[Mapping[str, Any]] = []
    for page_index, page in enumerate(pages):
        prefix = f"storyboard pages[{page_index}]"
        if not isinstance(page, Mapping):
            raise ReferenceStrategyError(f"{prefix} must be an object")
        page_panels = page.get("panels")
        if not isinstance(page_panels, list):
            raise ReferenceStrategyError(f"{prefix}.panels must be an array")
        for panel_index, panel in enumerate(page_panels):
            panel_prefix = f"{prefix}.panels[{panel_index}]"
            if not isinstance(panel, Mapping):
                raise ReferenceStrategyError(f"{panel_prefix} must be an object")
            if not isinstance(panel.get("id"), str):
                raise ReferenceStrategyError(f"{panel_prefix}.id must be a string")
            panels.append(panel)

    identifiers = [str(panel["id"]) for panel in panels]
    if len(set(identifiers)) != len(identifiers):
        raise ReferenceStrategyError("storyboard must not repeat a panel id")
    return panels


def panel_reference_plan(
    pack: Mapping[str, Any],
    storyboard: Mapping[str, Any],
    panel_id: str,
    *,
    reference_budget: int | None = None,
) -> PanelReferencePlan:
    """Return the reference plan for one storyboard panel."""
    budget = _validated_budget(reference_budget)
    for panel in _storyboard_panels(storyboard):
        if panel.get("id") == panel_id:
            return _plan_panel(pack, panel, budget)
    raise ReferenceStrategyError(f"storyboard has no panel '{panel_id}'")


def panel_reference_plans(
    pack: Mapping[str, Any],
    storyboard: Mapping[str, Any],
    *,
    reference_budget: int | None = None,
) -> tuple[PanelReferencePlan, ...]:
    """Return every panel's reference plan in storyboard order."""
    budget = _validated_budget(reference_budget)
    return tuple(
        _plan_panel(pack, panel, budget) for panel in _storyboard_panels(storyboard)
    )


def project_reference_plan(
    pack: Mapping[str, Any],
    storyboard: Mapping[str, Any],
    *,
    reference_budget: int | None = None,
) -> dict[str, Any]:
    """Return the whole project's reference-selection document."""
    return {
        "panels": [
            plan.as_record()
            for plan in panel_reference_plans(
                pack, storyboard, reference_budget=reference_budget
            )
        ],
        "schema_version": REFERENCE_PLAN_SCHEMA_VERSION,
    }


# --------------------------------------------------------------------------- #
# Provider-neutral rendering
# --------------------------------------------------------------------------- #


def reference_plan_block(plan: PanelReferencePlan) -> str:
    """Render one panel's plan as deterministic, provider-neutral plain text.

    The block is what an agent reads before attaching references: an ordered
    attachment list and the omissions it should not silently re-add.
    """
    budget = "unlimited" if plan.reference_budget is None else str(plan.reference_budget)
    cue = "" if plan.shot_cue is None else f" (cue: {plan.shot_cue})"
    lines = [
        f"REFERENCE PLAN (reference-selection {REFERENCE_PLAN_SCHEMA_VERSION})",
        f"- panel: {plan.panel_id}",
        f"- shot class: {plan.shot_class}{cue}",
        f"- reference budget: {budget}",
        "- attach in this order:",
    ]
    if plan.selected:
        lines.extend(
            f"  {item.rank}. {item.character_id} {item.view}={item.path} "
            f"({item.reason})"
            for item in plan.selected
        )
    else:
        lines.append("  none")
    if plan.omitted:
        lines.append("- omitted:")
        lines.extend(
            f"  {item.character_id} {item.view}={item.path} ({item.reason})"
            for item in plan.omitted
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Reading and persistence
# --------------------------------------------------------------------------- #


def read_storyboard(project_dir: Path) -> Mapping[str, Any]:
    """Read the project storyboard without following symlinks, or fail closed."""
    path = contained_project_path(Path(project_dir), STORYBOARD_PATH)
    try:
        value = loads_bounded_json(
            read_contained_bytes(
                Path(project_dir), STORYBOARD_PATH, max_bytes=MAX_JSON_BYTES
            ),
            source=STORYBOARD_PATH,
        )
    except json.JSONDecodeError as error:
        raise ReferenceStrategyError(
            f"{STORYBOARD_PATH} is not valid JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ReferenceStrategyError(f"{STORYBOARD_PATH} must contain a JSON object")
    return value


def reference_plan_bytes(document: Mapping[str, Any]) -> bytes:
    """Return the canonical on-disk bytes for a reference-selection document."""
    return canonical_artifact_bytes(document)


def write_reference_plan(project_dir: Path, document: Mapping[str, Any]) -> Path:
    """Publish a reference-selection document atomically inside the project."""
    project_dir = Path(project_dir)
    payload = reference_plan_bytes(document)
    with ProjectTransaction(project_dir, "reference-selection") as transaction:
        transaction.stage_bytes(REFERENCE_PLAN_PATH, payload)
    return project_dir / REFERENCE_PLAN_PATH


def plan_and_write_reference_plan(
    project_dir: Path, *, reference_budget: int | None = None
) -> tuple[Path, tuple[str, ...]]:
    """Plan every panel's references from the persisted pack, then publish it.

    The identity pack is checked first, so a plan is never derived from a
    missing, invalid, stale, or unbacked pack. Re-running this on an unchanged
    project rewrites byte-identical content, so a resume attaches exactly the
    references the interrupted run attached.
    """
    project_dir = Path(project_dir)
    budget = _validated_budget(reference_budget)
    issues = check_identity_pack(project_dir)
    if issues:
        return project_dir / REFERENCE_PLAN_PATH, issues
    document = project_reference_plan(
        read_identity_pack(project_dir),
        read_storyboard(project_dir),
        reference_budget=budget,
    )
    return write_reference_plan(project_dir, document), ()


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan which character references each panel receives, and record why."
        ),
    )
    parser.add_argument("project_dir", type=Path, help="generated project directory")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="COUNT",
        help=(
            "maximum reference images one panel may receive; omit for unlimited, "
            "and pass 0 when the detected capability supports no reference images"
        ),
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--plan",
        action="store_true",
        help="publish the whole project's reference-selection record atomically",
    )
    action.add_argument(
        "--panel",
        metavar="PANEL_ID",
        help="print the provider-neutral reference plan for one storyboard panel",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.plan:
            path, issues = plan_and_write_reference_plan(
                arguments.project_dir, reference_budget=arguments.budget
            )
            if issues:
                for issue in issues:
                    print(issue, file=sys.stderr)
                return 1
            print(path.as_posix())
            return 0

        issues = check_identity_pack(arguments.project_dir)
        if issues:
            for issue in issues:
                print(issue, file=sys.stderr)
            return 1
        plan = panel_reference_plan(
            read_identity_pack(arguments.project_dir),
            read_storyboard(arguments.project_dir),
            arguments.panel,
            reference_budget=arguments.budget,
        )
        print(reference_plan_block(plan))
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
