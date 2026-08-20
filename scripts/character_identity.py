#!/usr/bin/env python3
"""Versioned character identity packs and provider-neutral identity context.

A Character Identity Pack is a companion plan artifact that carries the stable
visual identity of every recurring character in one structured, versioned place:
immutable traits, wardrobe and accessories, proportions, and the canonical
reference views. Panel prompt construction reads the pack instead of re-deriving
loosely repeated prose from the character bible, which is the drift source this
artifact exists to remove.

The pack is derived deterministically from ``plan/character-bible.json`` and may
then be extended by the agent with additional reference views, explicit
proportions, and identity notes. Derivation and rendering never consult a clock,
a locale, or a random seed, so an identical bible always produces identical pack
bytes and an identical prompt block.

This module is provider-neutral by construction. It emits plain text and
relative reference paths; selecting an image capability, attaching references,
and transmitting anything remain the agent session's responsibility, exactly as
required by the provider boundary in ``AGENTS.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .core_primitives import canonical_artifact_bytes, canonical_json_bytes
from .project_io import ProjectTransaction, contained_project_path, open_path_nofollow


IDENTITY_PACK_SCHEMA_VERSION = "1.0"
SUPPORTED_IDENTITY_PACK_SCHEMA_VERSIONS = frozenset({IDENTITY_PACK_SCHEMA_VERSION})

IDENTITY_PACK_PATH = "plan/character-identity-pack.json"
CHARACTER_BIBLE_PATH = "plan/character-bible.json"
STORYBOARD_PATH = "plan/storyboard.json"

CANONICAL_VIEW = "canonical"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,47}$")

PACK_FIELDS = ("characters", "schema_version")
CHARACTER_FIELDS = (
    "avoid",
    "id",
    "immutable_traits",
    "proportions",
    "reference_views",
    "source_fingerprint_sha256",
    "wardrobe",
)
IMMUTABLE_TRAIT_FIELDS = ("face", "hair", "invariants", "silhouette")
WARDROBE_FIELDS = ("accessories", "base", "palette")
PROPORTION_FIELDS = ("build", "notes")
REFERENCE_VIEW_FIELDS = ("path", "view")

MIN_INVARIANTS = 2
MAX_INVARIANTS = 5


class IdentityPackError(ValueError):
    """Raised when a pack cannot be read, derived, or trusted."""


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """Provider-neutral identity material for one panel prompt."""

    panel_id: str
    character_ids: tuple[str, ...]
    prompt_block: str
    reference_paths: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _read_json(project_dir: Path, relative: str) -> Any:
    """Read one contained project JSON document without following symlinks."""
    path = contained_project_path(project_dir, relative)
    try:
        with open_path_nofollow(path) as stream:
            return json.load(stream)
    except json.JSONDecodeError as error:
        raise IdentityPackError(f"{relative} is not valid JSON: {error}") from error


def read_character_bible(project_dir: Path) -> Mapping[str, Any]:
    """Return the character bible as a mapping, or fail closed."""
    value = _read_json(Path(project_dir), CHARACTER_BIBLE_PATH)
    if not isinstance(value, dict):
        raise IdentityPackError(f"{CHARACTER_BIBLE_PATH} must contain a JSON object")
    return value


def read_identity_pack(project_dir: Path) -> Mapping[str, Any]:
    """Return the persisted identity pack as a mapping, or fail closed."""
    value = _read_json(Path(project_dir), IDENTITY_PACK_PATH)
    if not isinstance(value, dict):
        raise IdentityPackError(f"{IDENTITY_PACK_PATH} must contain a JSON object")
    return value


def identity_pack_exists(project_dir: Path) -> bool:
    """Return whether the project already persists an identity pack."""
    return contained_project_path(Path(project_dir), IDENTITY_PACK_PATH).is_file()


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #


def fingerprint_digest(fingerprint: object) -> str:
    """Return the canonical SHA-256 of a character's visual fingerprint.

    The digest binds a pack entry to the exact bible fingerprint it was derived
    from, so a later bible edit is reported as staleness instead of silently
    diverging from the identity the accepted panels were generated against.
    """
    return hashlib.sha256(canonical_json_bytes(fingerprint)).hexdigest()


def _strings(value: object) -> list[str]:
    """Return a list copy of a string sequence, or an empty list."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def derive_character_entry(character: Mapping[str, Any]) -> dict[str, Any]:
    """Derive one identity-pack entry from a character-bible record."""
    if not isinstance(character, Mapping):
        raise IdentityPackError("character bible entries must be JSON objects")
    character_id = character.get("id")
    if not isinstance(character_id, str) or ID_PATTERN.fullmatch(character_id) is None:
        raise IdentityPackError(f"character id is not a valid ID: {character_id!r}")

    fingerprint = character.get("visual_fingerprint")
    if not isinstance(fingerprint, Mapping):
        raise IdentityPackError(
            f"character {character_id} has no visual_fingerprint object"
        )
    reference_path = character.get("reference_path")
    if not isinstance(reference_path, str) or not reference_path.strip():
        raise IdentityPackError(f"character {character_id} has no reference_path")

    return {
        "avoid": _strings(fingerprint.get("avoid")),
        "id": character_id,
        "immutable_traits": {
            "face": fingerprint.get("face"),
            "hair": fingerprint.get("hair"),
            "invariants": _strings(fingerprint.get("invariants")),
            "silhouette": fingerprint.get("silhouette"),
        },
        "proportions": {
            "build": fingerprint.get("silhouette"),
            "notes": [],
        },
        "reference_views": [
            {"path": reference_path.replace("\\", "/"), "view": CANONICAL_VIEW}
        ],
        "source_fingerprint_sha256": fingerprint_digest(fingerprint),
        "wardrobe": {
            "accessories": _strings(fingerprint.get("signature_props")),
            "base": fingerprint.get("wardrobe"),
            "palette": _strings(fingerprint.get("palette")),
        },
    }


def derive_identity_pack(character_bible: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a complete identity pack from a character bible.

    Entries keep the bible's authored order so the prompt block a panel receives
    is stable for the whole project.
    """
    if not isinstance(character_bible, Mapping):
        raise IdentityPackError("character bible must be a JSON object")
    characters = character_bible.get("characters")
    if not isinstance(characters, list):
        raise IdentityPackError("character bible characters must be a list")
    return {
        "characters": [derive_character_entry(item) for item in characters],
        "schema_version": IDENTITY_PACK_SCHEMA_VERSION,
    }


def merge_authored_entries(
    derived: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Re-derive a pack while preserving authored, non-derivable additions.

    Only fields the character bible cannot express are carried over: extra
    reference views, explicit proportion notes, and the proportion ``build``
    once an author has overridden the silhouette default. Immutable traits,
    wardrobe, palette, and the fingerprint digest are always re-derived so the
    pack can never drift away from the bible it points at.
    """
    merged = deepcopy(dict(derived))
    if not isinstance(existing, Mapping):
        return merged
    authored = {
        entry.get("id"): entry
        for entry in existing.get("characters", [])
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    for entry in merged["characters"]:
        previous = authored.get(entry["id"])
        if not isinstance(previous, Mapping):
            continue

        previous_views = previous.get("reference_views")
        if isinstance(previous_views, list):
            extra = [
                view
                for view in previous_views
                if isinstance(view, Mapping)
                and view.get("view") != CANONICAL_VIEW
                and isinstance(view.get("view"), str)
                and isinstance(view.get("path"), str)
            ]
            entry["reference_views"] = entry["reference_views"] + sorted(
                ({"path": view["path"], "view": view["view"]} for view in extra),
                key=lambda view: view["view"],
            )

        previous_proportions = previous.get("proportions")
        if isinstance(previous_proportions, Mapping):
            notes = _strings(previous_proportions.get("notes"))
            if notes:
                entry["proportions"]["notes"] = notes
            build = previous_proportions.get("build")
            if isinstance(build, str) and build.strip():
                entry["proportions"]["build"] = build
    return merged


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _check_object(
    value: object,
    fields: Sequence[str],
    issues: list[str],
    prefix: str,
) -> Mapping[str, Any] | None:
    """Require an object whose keys are exactly ``fields``."""
    if not isinstance(value, Mapping):
        issues.append(f"{prefix} must be an object")
        return None
    present = tuple(sorted(value))
    if present != tuple(fields):
        missing = sorted(set(fields) - set(present))
        unknown = sorted(set(present) - set(fields))
        if missing:
            issues.append(f"{prefix} is missing fields: {', '.join(missing)}")
        if unknown:
            issues.append(f"{prefix} has unknown fields: {', '.join(unknown)}")
    return value


def _check_text(value: object, issues: list[str], prefix: str) -> None:
    """Require a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{prefix} must be a non-empty string")


def _check_text_list(
    value: object,
    issues: list[str],
    prefix: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> None:
    """Require a list of non-empty, unique strings within bounds."""
    if not isinstance(value, list):
        issues.append(f"{prefix} must be an array of strings")
        return
    if any(not isinstance(item, str) or not item.strip() for item in value):
        issues.append(f"{prefix} must contain only non-empty strings")
        return
    if len(set(value)) != len(value):
        issues.append(f"{prefix} must not repeat a value")
    if len(value) < minimum:
        issues.append(f"{prefix} requires at least {minimum} entries")
    if maximum is not None and len(value) > maximum:
        issues.append(f"{prefix} allows at most {maximum} entries")


def _validate_reference_views(
    value: object,
    issues: list[str],
    prefix: str,
) -> list[str]:
    """Validate reference views and return their declared relative paths."""
    if not isinstance(value, list) or not value:
        issues.append(f"{prefix} must be a non-empty array of reference views")
        return []

    paths: list[str] = []
    names: list[str] = []
    for index, view in enumerate(value):
        view_prefix = f"{prefix}[{index}]"
        entry = _check_object(view, REFERENCE_VIEW_FIELDS, issues, view_prefix)
        if entry is None:
            continue
        name = entry.get("view")
        if not isinstance(name, str) or ID_PATTERN.fullmatch(name) is None:
            issues.append(f"{view_prefix}.view must be a valid ID")
        else:
            names.append(name)
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            issues.append(f"{view_prefix}.path must be a non-empty relative path")
        elif path != path.replace("\\", "/") or path.startswith("/") or ".." in Path(path).parts:
            issues.append(f"{view_prefix}.path must be a POSIX relative project path")
        else:
            paths.append(path)

    if len(set(names)) != len(names):
        issues.append(f"{prefix} must not repeat a view name")
    if CANONICAL_VIEW not in names:
        issues.append(f"{prefix} must include the '{CANONICAL_VIEW}' view")
    return paths


def _validate_entry(
    entry: object,
    issues: list[str],
    prefix: str,
) -> tuple[str | None, list[str]]:
    """Validate one pack entry and return its ID and reference paths."""
    record = _check_object(entry, CHARACTER_FIELDS, issues, prefix)
    if record is None:
        return None, []

    character_id = record.get("id")
    if not isinstance(character_id, str) or ID_PATTERN.fullmatch(character_id) is None:
        issues.append(f"{prefix}.id must be a valid ID")
        character_id = None

    traits = _check_object(
        record.get("immutable_traits"),
        IMMUTABLE_TRAIT_FIELDS,
        issues,
        f"{prefix}.immutable_traits",
    )
    if traits is not None:
        for field in ("face", "hair", "silhouette"):
            _check_text(traits.get(field), issues, f"{prefix}.immutable_traits.{field}")
        _check_text_list(
            traits.get("invariants"),
            issues,
            f"{prefix}.immutable_traits.invariants",
            minimum=MIN_INVARIANTS,
            maximum=MAX_INVARIANTS,
        )

    wardrobe = _check_object(
        record.get("wardrobe"), WARDROBE_FIELDS, issues, f"{prefix}.wardrobe"
    )
    if wardrobe is not None:
        _check_text(wardrobe.get("base"), issues, f"{prefix}.wardrobe.base")
        _check_text_list(wardrobe.get("accessories"), issues, f"{prefix}.wardrobe.accessories")
        _check_text_list(
            wardrobe.get("palette"), issues, f"{prefix}.wardrobe.palette", minimum=1
        )

    proportions = _check_object(
        record.get("proportions"), PROPORTION_FIELDS, issues, f"{prefix}.proportions"
    )
    if proportions is not None:
        _check_text(proportions.get("build"), issues, f"{prefix}.proportions.build")
        _check_text_list(proportions.get("notes"), issues, f"{prefix}.proportions.notes")

    _check_text_list(record.get("avoid"), issues, f"{prefix}.avoid")

    digest = record.get("source_fingerprint_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        issues.append(f"{prefix}.source_fingerprint_sha256 must be a SHA-256 value")

    paths = _validate_reference_views(
        record.get("reference_views"), issues, f"{prefix}.reference_views"
    )
    return character_id, paths


def validate_identity_pack(
    pack: object,
    *,
    character_bible: Mapping[str, Any] | None = None,
    project_dir: Path | None = None,
) -> tuple[str, ...]:
    """Return sorted, stable issue messages for an identity pack.

    An empty result means the pack is structurally valid, consistent with the
    character bible it was derived from when one is supplied, and backed by
    reference files that exist inside the project boundary when ``project_dir``
    is supplied. Generation must not start while any issue remains.
    """
    issues: list[str] = []
    record = _check_object(pack, PACK_FIELDS, issues, "character-identity-pack")
    if record is None:
        return tuple(issues)

    version = record.get("schema_version")
    if version not in SUPPORTED_IDENTITY_PACK_SCHEMA_VERSIONS:
        issues.append(
            "character-identity-pack.schema_version must be one of "
            + ", ".join(sorted(SUPPORTED_IDENTITY_PACK_SCHEMA_VERSIONS))
        )

    characters = record.get("characters")
    if not isinstance(characters, list):
        issues.append("character-identity-pack.characters must be an array")
        return tuple(sorted(set(issues)))

    identifiers: list[str] = []
    reference_paths: list[tuple[str, str]] = []
    for index, entry in enumerate(characters):
        prefix = f"character-identity-pack.characters[{index}]"
        character_id, paths = _validate_entry(entry, issues, prefix)
        if character_id is not None:
            identifiers.append(character_id)
            reference_paths.extend((character_id, path) for path in paths)
    if len(set(identifiers)) != len(identifiers):
        issues.append("character-identity-pack.characters must not repeat a character id")

    if character_bible is not None:
        issues.extend(_bible_issues(characters, character_bible))
    if project_dir is not None:
        issues.extend(_reference_issues(Path(project_dir), reference_paths))
    return tuple(sorted(set(issues)))


def _bible_issues(
    characters: Sequence[Any],
    character_bible: Mapping[str, Any],
) -> list[str]:
    """Report divergence between the pack and the character bible."""
    issues: list[str] = []
    bible_entries = character_bible.get("characters")
    if not isinstance(bible_entries, list):
        return ["plan/character-bible.json characters must be an array"]

    bible = {
        entry["id"]: entry
        for entry in bible_entries
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    packed = {
        entry["id"]: entry
        for entry in characters
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    for missing in sorted(set(bible) - set(packed)):
        issues.append(f"character-identity-pack is missing bible character '{missing}'")
    for unknown in sorted(set(packed) - set(bible)):
        issues.append(
            f"character-identity-pack character '{unknown}' is not in the character bible"
        )

    for character_id in sorted(set(bible) & set(packed)):
        entry = packed[character_id]
        fingerprint = bible[character_id].get("visual_fingerprint")
        if not isinstance(fingerprint, Mapping):
            issues.append(
                f"character bible character '{character_id}' has no visual_fingerprint"
            )
            continue
        if entry.get("source_fingerprint_sha256") != fingerprint_digest(fingerprint):
            issues.append(
                f"character-identity-pack character '{character_id}' is stale: "
                "re-derive it after the character bible fingerprint changed"
            )
        traits = entry.get("immutable_traits")
        if isinstance(traits, Mapping):
            for field in ("face", "hair", "silhouette"):
                if traits.get(field) != fingerprint.get(field):
                    issues.append(
                        f"character-identity-pack character '{character_id}' "
                        f"immutable_traits.{field} must match the bible verbatim"
                    )
            if _strings(traits.get("invariants")) != _strings(fingerprint.get("invariants")):
                issues.append(
                    f"character-identity-pack character '{character_id}' "
                    "immutable_traits.invariants must match the bible verbatim"
                )
        views = entry.get("reference_views")
        canonical = None
        if isinstance(views, list):
            canonical = next(
                (
                    view.get("path")
                    for view in views
                    if isinstance(view, Mapping) and view.get("view") == CANONICAL_VIEW
                ),
                None,
            )
        if canonical != bible[character_id].get("reference_path"):
            issues.append(
                f"character-identity-pack character '{character_id}' canonical view "
                "must be the character bible reference_path"
            )
    return issues


def _reference_issues(
    project_dir: Path,
    reference_paths: Iterable[tuple[str, str]],
) -> list[str]:
    """Report reference views whose files are missing or escape the project."""
    issues: list[str] = []
    for character_id, relative in reference_paths:
        try:
            resolved = contained_project_path(project_dir, relative)
        except (OSError, ValueError) as error:
            issues.append(
                f"character-identity-pack character '{character_id}' reference view "
                f"is not a contained project path: {relative} ({error})"
            )
            continue
        if not resolved.is_file():
            issues.append(
                f"character-identity-pack character '{character_id}' reference view "
                f"file is missing: {relative}"
            )
    return issues


# --------------------------------------------------------------------------- #
# Provider-neutral prompt context
# --------------------------------------------------------------------------- #


def _entries_by_id(pack: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    characters = pack.get("characters")
    if not isinstance(characters, list):
        raise IdentityPackError("identity pack characters must be a list")
    return {
        entry["id"]: entry
        for entry in characters
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }


def _selected(
    pack: Mapping[str, Any], character_ids: Iterable[str] | None
) -> list[Mapping[str, Any]]:
    """Return requested entries in pack order, failing closed on unknown IDs."""
    entries = _entries_by_id(pack)
    if character_ids is None:
        return list(entries.values())
    requested = set(character_ids)
    unknown = sorted(requested - set(entries))
    if unknown:
        raise IdentityPackError(
            "identity pack has no entry for: " + ", ".join(unknown)
        )
    return [entry for key, entry in entries.items() if key in requested]


def _join(values: object) -> str:
    return "; ".join(_strings(values))


def identity_prompt_block(
    pack: Mapping[str, Any],
    character_ids: Iterable[str] | None = None,
) -> str:
    """Render the deterministic identity clause for a panel prompt.

    The output is plain text with no provider name, model name, credential, or
    endpoint in it. An adapter decides how to transmit it; the engine only
    guarantees that the same pack and the same character selection always render
    the same bytes.
    """
    lines = [f"IDENTITY LOCK (character-identity-pack {IDENTITY_PACK_SCHEMA_VERSION})"]
    for entry in _selected(pack, character_ids):
        traits = entry.get("immutable_traits") or {}
        wardrobe = entry.get("wardrobe") or {}
        proportions = entry.get("proportions") or {}
        views = entry.get("reference_views") or []

        lines.append(f"- {entry['id']}")
        lines.append(
            "  immutable: "
            + "; ".join(
                str(traits.get(field, ""))
                for field in ("silhouette", "face", "hair")
            )
        )
        lines.append("  invariants: " + _join(traits.get("invariants")))
        build = proportions.get("build")
        notes = _strings(proportions.get("notes"))
        proportion_text = str(build) if isinstance(build, str) else ""
        if notes:
            proportion_text = "; ".join([proportion_text, *notes])
        lines.append("  proportions: " + proportion_text)
        lines.append("  wardrobe: " + str(wardrobe.get("base", "")))
        accessories = _join(wardrobe.get("accessories"))
        if accessories:
            lines.append("  accessories: " + accessories)
        lines.append("  palette: " + _join(wardrobe.get("palette")))
        avoid = _join(entry.get("avoid"))
        if avoid:
            lines.append("  avoid: " + avoid)
        lines.append(
            "  reference views: "
            + "; ".join(
                f"{view['view']}={view['path']}"
                for view in views
                if isinstance(view, Mapping)
                and isinstance(view.get("view"), str)
                and isinstance(view.get("path"), str)
            )
        )
    return "\n".join(lines)


def identity_reference_paths(
    pack: Mapping[str, Any],
    character_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return the relative reference views an adapter should attach."""
    paths: list[str] = []
    for entry in _selected(pack, character_ids):
        views = entry.get("reference_views")
        if not isinstance(views, list):
            continue
        for view in views:
            if isinstance(view, Mapping) and isinstance(view.get("path"), str):
                paths.append(view["path"])
    return tuple(dict.fromkeys(paths))


def storyboard_panel_characters(storyboard: Mapping[str, Any], panel_id: str) -> tuple[str, ...]:
    """Return the character IDs a storyboard panel uses, in pack-safe order."""
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise IdentityPackError("storyboard pages must be a list")
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        for panel in page.get("panels", []) or []:
            if isinstance(panel, Mapping) and panel.get("id") == panel_id:
                return tuple(dict.fromkeys(_strings(panel.get("characters"))))
    raise IdentityPackError(f"storyboard has no panel '{panel_id}'")


def panel_identity_context(
    pack: Mapping[str, Any],
    storyboard: Mapping[str, Any],
    panel_id: str,
) -> IdentityContext:
    """Assemble the identity context for one storyboard panel."""
    character_ids = storyboard_panel_characters(storyboard, panel_id)
    return IdentityContext(
        panel_id=panel_id,
        character_ids=character_ids,
        prompt_block=identity_prompt_block(pack, character_ids),
        reference_paths=identity_reference_paths(pack, character_ids),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def identity_pack_bytes(pack: Mapping[str, Any]) -> bytes:
    """Return the canonical on-disk bytes for an identity pack."""
    return canonical_artifact_bytes(pack)


def write_identity_pack(project_dir: Path, pack: Mapping[str, Any]) -> Path:
    """Publish an identity pack atomically inside the project boundary."""
    project_dir = Path(project_dir)
    payload = identity_pack_bytes(pack)
    with ProjectTransaction(project_dir, "character-identity-pack") as transaction:
        transaction.stage_bytes(IDENTITY_PACK_PATH, payload)
    return project_dir / IDENTITY_PACK_PATH


def derive_and_write_identity_pack(project_dir: Path) -> tuple[Path, tuple[str, ...]]:
    """Derive the pack from the bible, validate it, then publish it atomically.

    Re-running this on an unchanged project rewrites byte-identical content, so
    resume and retry reuse the same identity context rather than a fresh
    paraphrase of it. Authored reference views and proportion notes survive the
    re-derivation; derived identity fields are always rebuilt from the bible.
    """
    project_dir = Path(project_dir)
    bible = read_character_bible(project_dir)
    existing = read_identity_pack(project_dir) if identity_pack_exists(project_dir) else None
    pack = merge_authored_entries(derive_identity_pack(bible), existing)
    issues = validate_identity_pack(pack, character_bible=bible, project_dir=project_dir)
    if issues:
        return project_dir / IDENTITY_PACK_PATH, issues
    return write_identity_pack(project_dir, pack), ()


def check_identity_pack(project_dir: Path) -> tuple[str, ...]:
    """Validate the persisted pack against the bible and the reference files."""
    project_dir = Path(project_dir)
    if not identity_pack_exists(project_dir):
        return (f"{IDENTITY_PACK_PATH} is missing; derive it before generation",)
    return validate_identity_pack(
        read_identity_pack(project_dir),
        character_bible=read_character_bible(project_dir),
        project_dir=project_dir,
    )


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive, validate, and render Comic Sol character identity packs.",
    )
    parser.add_argument("project_dir", type=Path, help="generated project directory")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--derive",
        action="store_true",
        help="derive the pack from the character bible and publish it atomically",
    )
    action.add_argument(
        "--check",
        action="store_true",
        help="validate the persisted pack, its bible consistency, and its reference files",
    )
    action.add_argument(
        "--panel",
        metavar="PANEL_ID",
        help="print the provider-neutral identity block for one storyboard panel",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.derive:
            path, issues = derive_and_write_identity_pack(arguments.project_dir)
            if issues:
                for issue in issues:
                    print(issue, file=sys.stderr)
                return 1
            print(path.as_posix())
            return 0

        if arguments.check:
            issues = check_identity_pack(arguments.project_dir)
            if issues:
                for issue in issues:
                    print(issue, file=sys.stderr)
                return 1
            print("character-identity-pack-ok")
            return 0

        issues = check_identity_pack(arguments.project_dir)
        if issues:
            for issue in issues:
                print(issue, file=sys.stderr)
            return 1
        context = panel_identity_context(
            read_identity_pack(arguments.project_dir),
            _read_json(Path(arguments.project_dir), STORYBOARD_PATH),
            arguments.panel,
        )
        print(context.prompt_block)
        return 0
    except (IdentityPackError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
