#!/usr/bin/env python3
"""Deterministic typography policy and preflight records for Comic Sol."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from .comic_sol import atomic_write_json
from .core_primitives import canonical_json_bytes
from .font_cmap import font_supports
from .font_coverage import (
    LINEAR_SCRIPTS,
    SHAPING_LINEAR,
    is_combining,
    recommended_font,
    script_for_codepoint,
    shaping_policy,
)
from .project_io import contained_project_path


SUPPORTED_STYLES = ("regular", "bold")
FONT_ROLES = ("regular", "bold", "fallback")
REMEDIATION = "choose supported text or bundle a tested font"

# Optional per-script faces are declared under this font-policy key and are
# recorded as `script:<name>` roles. The key is absent from most policies, and an
# absent key contributes nothing to `font_policy_sha256`, so projects lettered
# with the bundled three-role policy keep the digest they were published with.
SCRIPT_EXTENSION_KEY = "scripts"
SCRIPT_ROLE_PREFIX = "script:"

# Every check the preflight performs, in the order it performs them. The record
# carries one entry per check so a reviewer can see what was verified rather
# than inferring it from the absence of issues.
PREFLIGHT_CHECKS = (
    "typography-shaping-policy",
    "typography-glyph-coverage",
)
# Named so that `validate_project` can require the exact values lettering writes
# rather than accept any record that merely reports a passing result.
TYPOGRAPHY_CHECK_METHOD = "font-cmap-policy-v1"
TYPOGRAPHY_CHECK_REVIEWER = "comic-sol"
TYPOGRAPHY_SCHEMA_VERSION = "1.1"


def script_role(script: str) -> str:
    """Return the font-policy role name that serves one script."""
    return f"{SCRIPT_ROLE_PREFIX}{script}"


@dataclass(frozen=True)
class TypographyIssue:
    category: str
    item_id: str
    codepoint: str
    character: str
    style: str
    checked_fonts: tuple[str, ...]
    remediation: str
    script: str = "unassigned"
    reason: str = ""


class TypographyPreflightError(ValueError):
    """Raised when authored text cannot be rendered under the pinned policy."""

    def __init__(self, issues: Sequence[TypographyIssue]):
        """Initialize an error describing typography preflight issues."""
        self.issues = tuple(issues)
        details = "; ".join(
            f"{issue.category}: {issue.codepoint} in {issue.item_id} "
            f"({issue.script}; {issue.style}; "
            f"checked {', '.join(issue.checked_fonts)}; "
            f"{issue.reason + '; ' if issue.reason else ''}"
            f"{issue.remediation})"
            for issue in self.issues
        )
        if any(issue.category == "unsupported-shaping" for issue in self.issues):
            details = f"unsupported shaping policy; {details}"
        super().__init__(details)


def _canonical_bytes(value: object) -> bytes:
    """Serialize canonical JSON bytes with typography's historical newline."""
    return canonical_json_bytes(value) + b"\n"


def _sha256_bytes(payload: bytes) -> str:
    """Return the SHA-256 digest of bytes."""
    return hashlib.sha256(payload).hexdigest()


# Lettering geometry carries its own artifact version, independent from the
# `project.json` version owned by scripts/schema.py. It moved to 1.1 when every
# placement gained an `attribution` record, and to 1.2 when every panel gained an
# `sfx` provenance block naming which effects the image model produced and which
# this engine lettered. Geometry written by a previous engine is reported as
# stale — and re-lettered — rather than read as if it already carried a claim it
# never recorded.
LETTERING_GEOMETRY_SCHEMA_VERSION = "1.2"


def lettering_geometry_hash(record: Mapping[str, object]) -> str:
    """Hash geometry by canonical semantic JSON rather than source formatting."""
    payload = dict(record)
    payload.pop("geometry_sha256", None)
    return _sha256_bytes(_canonical_bytes(payload))


def normalize_content(text: str) -> str:
    """Normalize authored text without changing punctuation, emoji, or newlines."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFC", text)
    normalized = "".join(
        " " if unicodedata.category(character) == "Cc" and character != "\n" else character
        for character in normalized
    )
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def display_content(kind: object, text: str) -> str:
    """Return visible text while preserving the authored storyboard value."""
    normalized = normalize_content(text)
    return normalized.upper() if kind == "dialogue" else normalized


def _style_spans(text: str) -> tuple[tuple[str, str], ...]:
    """Return validated styled text spans for lettering."""
    parts = text.split("**")
    if len(parts) % 2 == 0 or any(
        not parts[index].strip() for index in range(1, len(parts), 2)
    ):
        return ((text, "regular"),)
    return tuple(
        (part, "bold" if index % 2 else "regular")
        for index, part in enumerate(parts)
        if part
    )


def _shaping_decision(character: str) -> tuple[bool, str]:
    """Return whether a character places linearly, and why not when it does not.

    Classification is delegated to the declared Unicode block table rather than
    matched against character names. Name matching could only recognize scripts
    someone had thought to spell out, which let Hebrew, Devanagari, and Thai pass
    a gate that Arabic and CJK were caught by, even though none of them survive
    advance-only placement.
    """
    shaping, reason = shaping_policy(ord(character))
    return shaping == SHAPING_LINEAR, reason


def _combining_refusal(
    character: str,
    role: str,
    base: tuple[str, str] | None,
) -> str:
    """Return why a combining mark cannot be positioned, or an empty string.

    A single mark carries no advance and a negative left bearing, so it lands
    over the base it follows and needs no shaping. Three arrangements do need
    real anchor handling, and none of them is fixed by any font choice:

    - a mark with no base has nothing to attach to;
    - a second mark must stack above the first, which requires the anchor
      geometry that nominal advances cannot express;
    - a mark drawn from a different face than its base is positioned against
      metrics that were never designed together.

    Faces are compared by policy role rather than by font ID, because a font ID
    is a bare file name: two roles resolving to same-named files in different
    directories would otherwise look like one face. Roles are unique by
    construction, and two roles never split a base from its mark when they point
    at the same file, because resolution always tries the styled role first.
    """
    if not is_combining(ord(character)):
        return ""
    if base is None:
        return "combining mark has no base glyph to attach to"
    base_character, base_role = base
    if is_combining(ord(base_character)):
        return "stacked combining marks require anchor positioning"
    if base_role != role:
        return "combining mark and its base glyph resolve to different faces"
    return ""


def _remediation(category: str, script: str) -> str:
    """Return the shortest action that resolves one preflight issue."""
    if category == "missing-glyph":
        font = recommended_font(script)
        if font is not None:
            return (
                f"configure the {script} script font extension with "
                f"{font.family} ({font.license_id}), or {REMEDIATION}"
            )
    return REMEDIATION


def _font_policy(font_policy: Mapping[str, object]) -> tuple[dict[str, Path], dict[str, str]]:
    """Return the configured font policy, including any script extensions.

    Required roles are validated first so a policy missing `regular`, `bold`, or
    `fallback` is refused the same way it always was. Script extensions are
    appended as `script:<name>` roles, which keeps one flat mapping for lookup,
    hashing, and reporting.
    """
    paths: dict[str, Path] = {}
    identifiers: dict[str, str] = {}
    for role in FONT_ROLES:
        value = font_policy.get(role)
        if not isinstance(value, (str, Path)):
            raise ValueError(f"font policy requires {role}")
        path = Path(value)
        if not path.is_file():
            raise ValueError(f"font policy {role} is unavailable: {path.name}")
        paths[role] = path
        identifiers[role] = path.name

    extensions = font_policy.get(SCRIPT_EXTENSION_KEY)
    if extensions is None:
        return paths, identifiers
    if not isinstance(extensions, Mapping):
        raise ValueError("font policy script extensions must be a mapping")
    for script in sorted(extensions):
        if not isinstance(script, str) or not script:
            raise ValueError("font policy script extension requires a script name")
        if script not in LINEAR_SCRIPTS:
            # Glyphs alone do not make a script letterable: refusing here keeps a
            # bidirectional or reordering script from appearing to be supported
            # merely because a covering face was configured for it.
            raise ValueError(
                f"font policy script extension cannot be lettered: {script}"
            )
        value = extensions[script]
        if not isinstance(value, (str, Path)):
            raise ValueError(f"font policy script extension {script} requires a path")
        path = Path(value)
        if not path.is_file():
            raise ValueError(
                f"font policy script extension {script} is unavailable: {path.name}"
            )
        role = script_role(script)
        paths[role] = path
        identifiers[role] = path.name
    return paths, identifiers


@lru_cache(maxsize=None)
def _hash_font_file(path: str) -> str:
    """Return a cached digest for the immutable font file at ``path``."""
    return _sha256_bytes(Path(path).read_bytes())


@lru_cache(maxsize=None)
def _font_policy_hashes(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return cached font digests in deterministic role order."""
    return tuple(_hash_font_file(path) for path in paths)


def preflight_text_items(
    items: Sequence[Mapping[str, object]],
    font_policy: Mapping[str, object],
) -> dict[str, object]:
    """Validate all normalized visible characters before lettering mutates output."""
    if not isinstance(items, (list, tuple)):
        raise TypeError("items must be an array")
    paths, identifiers = _font_policy(font_policy)
    canonical_items = [dict(value) for value in items]
    glyphs: list[dict[str, object]] = []
    non_glyphs: list[dict[str, str]] = []
    issues: list[TypographyIssue] = []

    for item_index, text_item in enumerate(canonical_items):
        item_id = text_item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"text item {item_index + 1} requires an ID")
        raw_content = text_item.get("content", "")
        if not isinstance(raw_content, str):
            raise TypeError(f"text item {item_id} content must be a string")
        content = display_content(text_item.get("kind"), raw_content)
        # The character and policy role a combining mark would attach to. It
        # carries across styled spans, because emphasis does not interrupt a
        # base-and-mark pair, and it is cleared by whitespace, which does.
        base_face: tuple[str, str] | None = None
        for span, style in _style_spans(content):
            role = "bold" if style == "bold" else "regular"
            for character in span:
                codepoint = f"U+{ord(character):04X}"
                if character == "\n":
                    non_glyphs.append({
                        "codepoint": codepoint,
                        "item_id": item_id,
                        "policy": "line-break",
                    })
                    base_face = None
                    continue
                if character.isspace():
                    non_glyphs.append({
                        "codepoint": codepoint,
                        "item_id": item_id,
                        "policy": "normalized-space",
                    })
                    base_face = None
                    continue
                script = script_for_codepoint(ord(character))
                shaping, reason = _shaping_decision(character)
                # Resolution order is styled face, then the Unicode fallback,
                # then the script's own extension face. The bundled comic face
                # wins wherever it can so a Japanese page still letters its Latin
                # interjections in the comic voice rather than in the CJK face.
                candidates = (role, "fallback", script_role(script))
                checked = tuple(
                    identifiers[candidate]
                    for candidate in candidates
                    if candidate in identifiers
                )
                selected_role: str | None = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate in paths
                        and font_supports(paths[candidate], character)
                    ),
                    None,
                )
                category = None
                if not shaping:
                    category = "unsupported-shaping"
                elif selected_role is None:
                    category = "missing-glyph"
                else:
                    # Coverage is settled, so the resolved face is known and the
                    # mark can be checked against the base it will attach to.
                    mark_reason = _combining_refusal(
                        character, selected_role, base_face
                    )
                    if mark_reason:
                        category = "unsupported-shaping"
                        reason = mark_reason
                if category is not None:
                    issues.append(TypographyIssue(
                        category=category,
                        item_id=item_id,
                        codepoint=codepoint,
                        character=character,
                        style=style,
                        checked_fonts=checked,
                        remediation=_remediation(category, script),
                        script=script,
                        reason=reason,
                    ))
                    continue
                assert selected_role is not None
                glyphs.append({
                    "character": character,
                    "codepoint": codepoint,
                    "coverage": "supported",
                    "font_id": identifiers[selected_role],
                    "item_id": item_id,
                    "script": script,
                    "shaping": "supported",
                    "style": style,
                })
                base_face = (character, selected_role)

    if issues:
        raise TypographyPreflightError(issues)

    policy_descriptor = dict(sorted(identifiers.items()))
    ordered_roles = tuple(sorted(paths))
    hashes = _font_policy_hashes(tuple(str(paths[role]) for role in ordered_roles))
    policy_binding = {
        role: {"font_id": identifiers[role], "sha256": digest}
        for role, digest in zip(ordered_roles, hashes, strict=True)
    }
    return {
        "checks": _preflight_checks(glyphs),
        "font_policy": policy_descriptor,
        "font_policy_sha256": _sha256_bytes(_canonical_bytes(policy_binding)),
        "glyphs": glyphs,
        "input_sha256": _sha256_bytes(_canonical_bytes(canonical_items)),
        "issues": [],
        "kind": "typography-preflight",
        "non_glyphs": non_glyphs,
        "schema_version": TYPOGRAPHY_SCHEMA_VERSION,
        "scripts": _script_summary(glyphs),
        "status": "pass",
    }


def _script_summary(glyphs: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Summarize which script each face was asked to letter.

    A per-script roll-up makes an unintended face substitution visible: Japanese
    dialogue served entirely by the Latin fallback is a defect the per-character
    list can hide behind its own length.
    """
    served: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for glyph in glyphs:
        script = str(glyph.get("script", "unassigned"))
        counts[script] = counts.get(script, 0) + 1
        served.setdefault(script, set()).add(str(glyph.get("font_id", "")))
    return [
        {
            "codepoints": counts[script],
            "font_ids": sorted(served[script]),
            "script": script,
        }
        for script in sorted(counts)
    ]


def _preflight_checks(glyphs: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Record every preflight check that ran and what it proved.

    Preflight raises on the first failing batch, so a persisted record only ever
    holds passing checks. Recording them anyway states the guarantee positively:
    a reader can tell that coverage was verified for this many glyphs, instead of
    having to trust that an empty issue list means the work was done.
    """
    verified = len(glyphs)
    evidence = {
        "typography-shaping-policy": (
            f"Declared block policy admitted {verified} glyph(s) as linearly placeable."
        ),
        "typography-glyph-coverage": (
            f"Every one of {verified} glyph(s) resolved to a font that maps it."
        ),
    }
    return [
        {
            "evidence": evidence[check_id],
            "id": check_id,
            "method": TYPOGRAPHY_CHECK_METHOD,
            "result": "pass",
            "reviewer": TYPOGRAPHY_CHECK_REVIEWER,
            "severity": "error",
        }
        for check_id in PREFLIGHT_CHECKS
    ]


def write_typography_preflight(
    project_dir: Path,
    panel_id: str,
    result: Mapping[str, object],
) -> Path:
    """Write typography preflight evidence for a project."""
    if re.fullmatch(r"p[0-9]{2}-[0-9]{2}", panel_id) is None:
        raise ValueError("invalid panel ID")
    destination = contained_project_path(
        Path(project_dir), f"panels/{panel_id}/typography.json"
    )
    atomic_write_json(destination, dict(result))
    return destination
