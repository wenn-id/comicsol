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
from .project_io import contained_project_path


SUPPORTED_STYLES = ("regular", "bold")
FONT_ROLES = ("regular", "bold", "fallback")
REMEDIATION = "choose supported text or bundle a tested font"


@dataclass(frozen=True)
class TypographyIssue:
    category: str
    item_id: str
    codepoint: str
    character: str
    style: str
    checked_fonts: tuple[str, ...]
    remediation: str


class TypographyPreflightError(ValueError):
    """Raised when authored text cannot be rendered under the pinned policy."""

    def __init__(self, issues: Sequence[TypographyIssue]):
        """Initialize an error describing typography preflight issues."""
        self.issues = tuple(issues)
        details = "; ".join(
            f"{issue.category}: {issue.codepoint} in {issue.item_id} "
            f"({issue.style}; checked {', '.join(issue.checked_fonts)}; "
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
# placement gained an `attribution` record, so geometry written by the previous
# engine is reported as stale — and re-lettered — rather than read as if it
# already carried verifiable speaker attribution.
LETTERING_GEOMETRY_SCHEMA_VERSION = "1.1"


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


def _shaping_supported(character: str) -> bool:
    """Report whether the runtime supports text shaping."""
    codepoint = ord(character)
    if codepoint >= 0x1F000:
        return False
    name = unicodedata.name(character, "")
    return not (
        "ARABIC" in name
        or "CJK" in name
        or "IDEOGRAPH" in name
        or 0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x9FFF
    )


def _font_policy(font_policy: Mapping[str, object]) -> tuple[dict[str, Path], dict[str, str]]:
    """Return the configured font policy for a lettering style."""
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
                    continue
                if character.isspace():
                    non_glyphs.append({
                        "codepoint": codepoint,
                        "item_id": item_id,
                        "policy": "normalized-space",
                    })
                    continue
                shaping = _shaping_supported(character)
                checked = (identifiers[role], identifiers["fallback"])
                selected_role: str | None = None
                if font_supports(paths[role], character):
                    selected_role = role
                elif font_supports(paths["fallback"], character):
                    selected_role = "fallback"
                category = None
                if not shaping:
                    category = "unsupported-shaping"
                elif selected_role is None:
                    category = "missing-glyph"
                if category is not None:
                    issues.append(TypographyIssue(
                        category=category,
                        item_id=item_id,
                        codepoint=codepoint,
                        character=character,
                        style=style,
                        checked_fonts=checked,
                        remediation=REMEDIATION,
                    ))
                    continue
                assert selected_role is not None
                glyphs.append({
                    "character": character,
                    "codepoint": codepoint,
                    "coverage": "supported",
                    "font_id": identifiers[selected_role],
                    "item_id": item_id,
                    "shaping": "supported",
                    "style": style,
                })

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
        "font_policy": policy_descriptor,
        "font_policy_sha256": _sha256_bytes(_canonical_bytes(policy_binding)),
        "glyphs": glyphs,
        "input_sha256": _sha256_bytes(_canonical_bytes(canonical_items)),
        "issues": [],
        "kind": "typography-preflight",
        "non_glyphs": non_glyphs,
        "schema_version": "1.0",
        "status": "pass",
    }


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
