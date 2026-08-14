#!/usr/bin/env python3
"""Strict offline validation for Comic Sol version-1.0 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError

from project_io import contained_project_path, open_path_nofollow
from raster_limits import MAX_DECODED_PIXELS
from page_quality import validate_page_quality
from quality_records import PANEL_CHECK_IDS, validate_quality_checks
from typography import lettering_geometry_hash

from comic_sol import (
    ALL_STATUSES,
    CATEGORY,
    LINEAR_STATUSES,
    MARGIN,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    canonical_artifact_bytes,
    layout_rects,
    rectangles_overlap,
    sha256_file,
)


ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
PANEL_ID_PATTERN = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
STAGES = ("all", "plan", "storyboard", "panels", "final", "export-ready")
LAYOUTS = {
    "full-page",
    "two-horizontal",
    "three-horizontal",
    "hero-top-two-bottom",
    "two-top-hero-bottom",
    "four-grid",
}
ANCHORS = {
    "top-left", "top-center", "top-right", "middle-left", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
}
MAX_PAGES = 4
MAX_PANELS = 12


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    field: str
    message: str


class ProjectValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        """Initialize an error containing project validation issues."""
        self.issues = tuple(issues)
        super().__init__(f"Comic Sol project has {len(self.issues)} validation issue(s)")


def _sorted(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    """Return validation issues in deterministic order."""
    return sorted(issues, key=lambda item: (item.path, item.field, item.message))


def _add(issues: list[ValidationIssue], path: str, field: str, message: str) -> None:
    """Append a validation issue to the issue collection."""
    issues.append(ValidationIssue(path, field, message))


def _object(
    value: object,
    allowed: set[str],
    required: set[str],
    issues: list[ValidationIssue],
    path: str,
    field: str,
) -> dict[str, object] | None:
    """Validate an object shape and report field violations."""
    if not isinstance(value, dict):
        _add(issues, path, field, "must be an object")
        return None
    for key in sorted(set(value) - allowed):
        _add(issues, path, f"{field}.{key}".strip("."), "unknown field")
    for key in sorted(required - set(value)):
        _add(issues, path, f"{field}.{key}".strip("."), "required field is missing")
    return value


def _nonempty_string(
    value: object, issues: list[ValidationIssue], path: str, field: str
) -> bool:
    """Validate a non-empty string field."""
    if not isinstance(value, str) or not value.strip():
        _add(issues, path, field, "must be a non-empty string")
        return False
    return True


def _identifier(value: object, issues: list[ValidationIssue], path: str, field: str) -> bool:
    """Validate a Comic Sol identifier field."""
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        _add(issues, path, field, "must match ^[a-z][a-z0-9-]{0,47}$")
        return False
    return True


def _integer(
    value: object,
    minimum: int,
    maximum: int,
    issues: list[ValidationIssue],
    path: str,
    field: str,
) -> bool:
    """Validate an integer field within inclusive bounds."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _add(issues, path, field, f"must be an integer from {minimum} to {maximum}")
        return False
    return True


def _relative_path(
    value: object,
    issues: list[ValidationIssue],
    path: str,
    field: str,
    nullable: bool = False,
) -> bool:
    """Validate a normalized relative project path field."""
    if nullable and value is None:
        return True
    if not isinstance(value, str) or not value:
        _add(issues, path, field, "must be a relative project path")
        return False
    if "\\" in value:
        _add(issues, path, field, "must use POSIX separators")
        return False
    parts = value.split("/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value) or any(part in {"", ".", ".."} for part in parts):
        _add(issues, path, field, "must be a normalized relative project path without traversal")
        return False
    return True


def _sha256(
    value: object,
    issues: list[ValidationIssue],
    path: str,
    field: str,
    nullable: bool = False,
) -> bool:
    """Validate a lowercase SHA-256 digest field."""
    if nullable and value is None:
        return True
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        _add(issues, path, field, "must be a lowercase 64-character SHA-256")
        return False
    return True


def _timestamp(
    value: object,
    issues: list[ValidationIssue],
    path: str,
    field: str,
    nullable: bool = False,
) -> bool:
    """Validate an ISO 8601 UTC timestamp field."""
    if nullable and value is None:
        return True
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        _add(issues, path, field, "must be an ISO 8601 UTC timestamp")
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _add(issues, path, field, "must be an ISO 8601 UTC timestamp")
        return False
    return True


def _string_list(
    value: object,
    issues: list[ValidationIssue],
    path: str,
    field: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> list[str] | None:
    """Validate a list of non-empty string values."""
    if not isinstance(value, list):
        _add(issues, path, field, "must be an array")
        return None
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        upper = "unbounded" if maximum is None else str(maximum)
        _add(issues, path, field, f"must contain {minimum} to {upper} items")
    for index, item in enumerate(value):
        _nonempty_string(item, issues, path, f"{field}[{index}]")
    return [item for item in value if isinstance(item, str)]


def validate_manifest(data: dict[str, object]) -> list[ValidationIssue]:
    """Validate the project manifest structure and references."""
    path = "project.json"
    issues: list[ValidationIssue] = []
    required_fields = {
        "schema_version", "project_id", "title", "created_at", "updated_at",
        "status", "input", "settings", "capability", "artifacts",
        "stage_versions", "panels", "warnings",
    }
    fields = required_fields | {"blocked_from", "blocked_reason"}
    root = _object(data, fields, required_fields, issues, path, "")
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "1.0":
        _add(issues, path, "schema_version", "must equal 1.0")
    _identifier(root.get("project_id"), issues, path, "project_id")
    _nonempty_string(root.get("title"), issues, path, "title")
    _timestamp(root.get("created_at"), issues, path, "created_at")
    _timestamp(root.get("updated_at"), issues, path, "updated_at")
    if root.get("status") not in ALL_STATUSES:
        _add(issues, path, "status", "unknown manifest status")
    if root.get("status") == "BLOCKED":
        if root.get("blocked_from") not in LINEAR_STATUSES:
            _add(issues, path, "blocked_from", "must be a normal pipeline status")
        blocked_reason = root.get("blocked_reason")
        if (
            not isinstance(blocked_reason, str)
            or CATEGORY.fullmatch(blocked_reason) is None
        ):
            _add(issues, path, "blocked_reason", "must be a stable category")
    elif root.get("blocked_from") is not None or root.get("blocked_reason") is not None:
        _add(issues, path, "status", "blocked fields must be null when not BLOCKED")

    input_fields = {"mode", "source_path", "source_sha256", "request_path", "language"}
    input_data = _object(root.get("input"), input_fields, input_fields, issues, path, "input")
    if input_data is not None:
        if input_data.get("mode") not in {"short_prompt", "pasted_story", "source_file", "resume"}:
            _add(issues, path, "input.mode", "unknown input mode")
        _relative_path(input_data.get("source_path"), issues, path, "input.source_path")
        _relative_path(input_data.get("request_path"), issues, path, "input.request_path")
        if input_data.get("source_path") != "source/input.txt":
            _add(issues, path, "input.source_path", "must equal source/input.txt")
        if input_data.get("request_path") != "source/request.json":
            _add(issues, path, "input.request_path", "must equal source/request.json")
        _sha256(input_data.get("source_sha256"), issues, path, "input.source_sha256")
        _nonempty_string(input_data.get("language"), issues, path, "input.language")

    setting_fields = {
        "page_width", "page_height", "reading_direction", "page_count",
        "panel_count", "style_anchor", "max_panel_retries",
    }
    settings = _object(root.get("settings"), setting_fields, setting_fields, issues, path, "settings")
    if settings is not None:
        exact = {"page_width": PAGE_WIDTH, "page_height": PAGE_HEIGHT, "max_panel_retries": 2}
        for field, expected in exact.items():
            if settings.get(field) != expected:
                _add(issues, path, f"settings.{field}", f"must equal {expected}")
        if settings.get("reading_direction") != "ltr":
            _add(issues, path, "settings.reading_direction", "must equal ltr")
        _integer(settings.get("page_count"), 1, MAX_PAGES, issues, path, "settings.page_count")
        _integer(settings.get("panel_count"), 0, MAX_PANELS, issues, path, "settings.panel_count")
        _nonempty_string(settings.get("style_anchor"), issues, path, "settings.style_anchor")

    capability_fields = {
        "status", "name", "supports_reference_images", "supports_dimensions", "detected_at",
    }
    capability = _object(root.get("capability"), capability_fields, capability_fields, issues, path, "capability")
    if capability is not None:
        if capability.get("status") not in {"not_checked", "available", "unavailable"}:
            _add(issues, path, "capability.status", "unknown capability status")
        if capability.get("name") is not None:
            _nonempty_string(capability.get("name"), issues, path, "capability.name")
        elif capability.get("status") == "available":
            _add(issues, path, "capability.name", "is required when capability is available")
        for field in ("supports_reference_images", "supports_dimensions"):
            if not isinstance(capability.get(field), bool):
                _add(issues, path, f"capability.{field}", "must be boolean")
        _timestamp(capability.get("detected_at"), issues, path, "capability.detected_at", nullable=True)
        if capability.get("status") in {"available", "unavailable"} and capability.get("detected_at") is None:
            _add(issues, path, "capability.detected_at", "is required after capability detection")

    artifacts = _object(root.get("artifacts"), {"story_plan", "character_bible", "storyboard", "qa_report", "pdf", "pdf_verification", "composition_cache"}, set(), issues, path, "artifacts")
    if artifacts is not None:
        for name, descriptor in artifacts.items():
            item = _object(descriptor, {"path", "sha256"}, {"path", "sha256"}, issues, path, f"artifacts.{name}")
            if item is not None:
                _relative_path(item.get("path"), issues, path, f"artifacts.{name}.path")
                _sha256(item.get("sha256"), issues, path, f"artifacts.{name}.sha256")

    version_fields = {"planning", "storyboard", "generation", "lettering", "composition", "export"}
    versions = _object(root.get("stage_versions"), version_fields, version_fields, issues, path, "stage_versions")
    if versions is not None:
        for name, value in versions.items():
            if not isinstance(value, str) or not value.isdecimal():
                _add(issues, path, f"stage_versions.{name}", "must be a decimal version string")
    panels = _string_list(root.get("panels"), issues, path, "panels", maximum=12)
    if panels is not None:
        for index, panel_id in enumerate(panels):
            if PANEL_ID_PATTERN.fullmatch(panel_id) is None:
                _add(issues, path, f"panels[{index}]", "must match pNN-NN")
        if len(set(panels)) != len(panels):
            _add(issues, path, "panels", "panel IDs must be unique")
    warnings = _string_list(root.get("warnings"), issues, path, "warnings")
    if root.get("status") == "COMPLETE" and warnings:
        _add(issues, path, "status", "must be COMPLETE_WITH_WARNINGS while warnings remain")
    if root.get("status") == "COMPLETE_WITH_WARNINGS" and warnings == []:
        _add(issues, path, "warnings", "COMPLETE_WITH_WARNINGS requires an unresolved warning")
    return _sorted(issues)


def validate_character_bible(data: dict[str, object]) -> list[ValidationIssue]:
    """Validate the character bible artifact."""
    path = "plan/character-bible.json"
    issues: list[ValidationIssue] = []
    root = _object(data, {"schema_version", "characters"}, {"schema_version", "characters"}, issues, path, "")
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "1.0":
        _add(issues, path, "schema_version", "must equal 1.0")
    characters = root.get("characters")
    if not isinstance(characters, list):
        _add(issues, path, "characters", "must be an array")
        return _sorted(issues)
    ids: list[str] = []
    character_fields = {
        "id", "name", "role", "age_band", "pronouns", "visual_fingerprint",
        "personality", "motivation", "speech", "reference_path",
    }
    fingerprint_fields = {
        "silhouette", "face", "hair", "wardrobe", "palette",
        "signature_props", "invariants", "avoid",
    }
    for index, value in enumerate(characters):
        prefix = f"characters[{index}]"
        character = _object(value, character_fields, character_fields, issues, path, prefix)
        if character is None:
            continue
        if _identifier(character.get("id"), issues, path, f"{prefix}.id"):
            ids.append(character["id"])
        for field in ("name", "role", "age_band", "pronouns", "motivation", "speech"):
            _nonempty_string(character.get(field), issues, path, f"{prefix}.{field}")
        _string_list(character.get("personality"), issues, path, f"{prefix}.personality", minimum=1)
        _relative_path(character.get("reference_path"), issues, path, f"{prefix}.reference_path")
        if isinstance(character.get("id"), str) and character.get("reference_path") != f"references/characters/{character['id']}.png":
            _add(issues, path, f"{prefix}.reference_path", "must match the character ID")
        fingerprint = _object(
            character.get("visual_fingerprint"), fingerprint_fields, fingerprint_fields,
            issues, path, f"{prefix}.visual_fingerprint",
        )
        if fingerprint is not None:
            for field in ("silhouette", "face", "hair", "wardrobe"):
                _nonempty_string(fingerprint.get(field), issues, path, f"{prefix}.visual_fingerprint.{field}")
            _string_list(fingerprint.get("palette"), issues, path, f"{prefix}.visual_fingerprint.palette", minimum=1)
            _string_list(fingerprint.get("signature_props"), issues, path, f"{prefix}.visual_fingerprint.signature_props", minimum=1)
            _string_list(fingerprint.get("invariants"), issues, path, f"{prefix}.visual_fingerprint.invariants", minimum=2, maximum=5)
            _string_list(fingerprint.get("avoid"), issues, path, f"{prefix}.visual_fingerprint.avoid", minimum=1)
    if len(set(ids)) != len(ids):
        _add(issues, path, "characters", "character IDs must be unique")
    return _sorted(issues)


def validate_story_plan(data: dict[str, object]) -> list[ValidationIssue]:
    """Validate the story-plan artifact."""
    path = "plan/story-plan.json"
    issues: list[ValidationIssue] = []
    fields = {
        "schema_version", "title", "logline", "theme", "tone", "rating",
        "setting", "beginning", "turn", "climax", "ending", "scenes",
    }
    root = _object(data, fields, fields, issues, path, "")
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "1.0":
        _add(issues, path, "schema_version", "must equal 1.0")
    for field in ("title", "logline", "theme", "setting", "beginning", "turn", "climax", "ending"):
        _nonempty_string(root.get(field), issues, path, field)
    _string_list(root.get("tone"), issues, path, "tone", minimum=1)
    if root.get("rating") != "teen":
        _add(issues, path, "rating", "must equal teen")
    scenes = root.get("scenes")
    if not isinstance(scenes, list):
        _add(issues, path, "scenes", "must be an array")
        return _sorted(issues)
    if not 2 <= len(scenes) <= 5:
        _add(issues, path, "scenes", "must contain 2 to 5 scenes")
    scene_fields = {"id", "purpose", "location", "time", "characters", "continuity_anchor"}
    ids: list[str] = []
    for index, value in enumerate(scenes):
        prefix = f"scenes[{index}]"
        scene = _object(value, scene_fields, scene_fields, issues, path, prefix)
        if scene is None:
            continue
        if _identifier(scene.get("id"), issues, path, f"{prefix}.id"):
            ids.append(scene["id"])
        for field in ("purpose", "location", "time", "continuity_anchor"):
            _nonempty_string(scene.get(field), issues, path, f"{prefix}.{field}")
        character_ids = _string_list(scene.get("characters"), issues, path, f"{prefix}.characters")
        if character_ids is not None:
            for item_index, character_id in enumerate(character_ids):
                _identifier(character_id, issues, path, f"{prefix}.characters[{item_index}]")
            if len(set(character_ids)) != len(character_ids):
                _add(issues, path, f"{prefix}.characters", "character IDs must be unique")
    if len(set(ids)) != len(ids):
        _add(issues, path, "scenes", "scene IDs must be unique")
    return _sorted(issues)


def _validate_text_item(
    value: object,
    panel_characters: set[str],
    known_characters: set[str],
    issues: list[ValidationIssue],
    path: str,
    prefix: str,
) -> int:
    """Validate one storyboard text item."""
    base_fields = {"id", "kind", "speaker", "content", "anchor", "priority"}
    dialogue_fields = {"voice_source", "speaker_anchor", "tail_target"}
    item = _object(value, base_fields | dialogue_fields, base_fields, issues, path, prefix)
    if item is None:
        return 0
    _identifier(item.get("id"), issues, path, f"{prefix}.id")
    kind = item.get("kind")
    if kind not in {"dialogue", "caption", "sfx"}:
        _add(issues, path, f"{prefix}.kind", "must be dialogue, caption, or sfx")
    content = item.get("content")
    word_count = 0
    if _nonempty_string(content, issues, path, f"{prefix}.content"):
        assert isinstance(content, str)
        word_count = len(content.split())
        limits = {"dialogue": 32, "caption": 45, "sfx": 3}
        if kind in limits and word_count > limits[kind]:
            _add(issues, path, f"{prefix}.content", f"exceeds {limits[kind]}-word {kind} limit")
        if unicodedata.normalize("NFC", content) != content:
            _add(issues, path, f"{prefix}.content", "must be Unicode NFC-normalized")
        if any(unicodedata.category(char) == "Cc" and char != "\n" for char in content):
            _add(issues, path, f"{prefix}.content", "contains a disallowed control character")
    speaker = item.get("speaker")
    if kind == "dialogue":
        if not _identifier(speaker, issues, path, f"{prefix}.speaker"):
            pass
        elif speaker not in known_characters or speaker not in panel_characters:
            _add(issues, path, f"{prefix}.speaker", "must reference a character present in the panel")
    elif speaker is not None:
        _add(issues, path, f"{prefix}.speaker", "must be null for caption and sfx")
    if item.get("anchor") not in ANCHORS:
        _add(issues, path, f"{prefix}.anchor", "unknown text anchor")
    has_legacy_tail = "tail_target" in item
    has_voice_source = "voice_source" in item
    has_speaker_anchor = "speaker_anchor" in item
    if kind == "dialogue":
        if has_legacy_tail:
            _add(
                issues,
                path,
                f"{prefix}.tail_target",
                "balloon-tail-migration-required: replace tail_target with explicit voice_source and speaker_anchor",
            )
        else:
            if item.get("voice_source") not in {"human", "device"}:
                _add(issues, path, f"{prefix}.voice_source", "must be human or device")
            speaker_anchor = item.get("speaker_anchor")
            if (
                not isinstance(speaker_anchor, list)
                or len(speaker_anchor) != 2
                or any(
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                    or not 0 <= number <= 1
                    for number in speaker_anchor
                )
            ):
                _add(
                    issues,
                    path,
                    f"{prefix}.speaker_anchor",
                    "must be finite normalized [x, y] coordinates",
                )
    else:
        if has_legacy_tail:
            _add(issues, path, f"{prefix}.tail_target", "must be omitted for caption and sfx")
        if has_voice_source:
            _add(issues, path, f"{prefix}.voice_source", "must be omitted for caption and sfx")
        if has_speaker_anchor:
            _add(issues, path, f"{prefix}.speaker_anchor", "must be omitted for caption and sfx")
    _integer(item.get("priority"), 1, 1_000_000, issues, path, f"{prefix}.priority")
    return word_count


def validate_storyboard(
    data: dict[str, object],
    story: dict[str, object],
    characters: dict[str, object],
) -> list[ValidationIssue]:
    """Validate the storyboard artifact and panel layout data."""
    path = "plan/storyboard.json"
    issues: list[ValidationIssue] = []
    root = _object(data, {"schema_version", "pages"}, {"schema_version", "pages"}, issues, path, "")
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "1.0":
        _add(issues, path, "schema_version", "must equal 1.0")
    pages = root.get("pages")
    if not isinstance(pages, list):
        _add(issues, path, "pages", "must be an array")
        return _sorted(issues)
    if not 1 <= len(pages) <= 4:
        _add(issues, path, "pages", "must contain 1 to 4 pages")

    known_scenes = {
        scene.get("id") for scene in story.get("scenes", [])
        if isinstance(scene, dict) and isinstance(scene.get("id"), str)
    }
    scene_anchors = {
        scene.get("id"): scene.get("continuity_anchor", "").strip()
        for scene in story.get("scenes", [])
        if isinstance(scene, dict)
        and isinstance(scene.get("id"), str)
        and isinstance(scene.get("continuity_anchor"), str)
    }
    known_characters = {
        character.get("id") for character in characters.get("characters", [])
        if isinstance(character, dict) and isinstance(character.get("id"), str)
    }
    character_invariants = {
        character.get("id"): set(character.get("visual_fingerprint", {}).get("invariants", []))
        for character in characters.get("characters", [])
        if isinstance(character, dict)
        and isinstance(character.get("id"), str)
        and isinstance(character.get("visual_fingerprint"), dict)
    }
    page_fields = {"number", "layout", "panels"}
    panel_fields = {
        "id", "order", "scene_id", "rect", "beat", "characters", "shot",
        "composition", "action", "expression", "lighting", "continuity",
        "negative", "text",
    }
    all_panel_ids: list[str] = []
    all_text_ids: list[str] = []
    total_panels = 0
    for page_index, value in enumerate(pages):
        page_prefix = f"pages[{page_index}]"
        page = _object(value, page_fields, page_fields, issues, path, page_prefix)
        if page is None:
            continue
        page_number = page.get("number")
        _integer(page_number, 1, 4, issues, path, f"{page_prefix}.number")
        if page_number != page_index + 1:
            _add(issues, path, f"{page_prefix}.number", "pages must be numbered contiguously from 1")
        layout = page.get("layout")
        if layout not in LAYOUTS:
            _add(issues, path, f"{page_prefix}.layout", "unknown layout")
        panels = page.get("panels")
        if not isinstance(panels, list):
            _add(issues, path, f"{page_prefix}.panels", "must be an array")
            continue
        total_panels += len(panels)
        if not 1 <= len(panels) <= 4:
            _add(issues, path, f"{page_prefix}.panels", "must contain 1 to 4 panels")
        expected_rects = layout_rects(layout) if layout in LAYOUTS else []
        if expected_rects and len(panels) != len(expected_rects):
            _add(issues, path, f"{page_prefix}.panels", "panel count must match the fixed layout")
        page_rectangles: list[tuple[str, dict[str, int]]] = []
        for panel_index, panel_value in enumerate(panels):
            prefix = f"{page_prefix}.panels[{panel_index}]"
            panel = _object(panel_value, panel_fields, panel_fields, issues, path, prefix)
            if panel is None:
                continue
            panel_id = panel.get("id")
            if not isinstance(panel_id, str) or PANEL_ID_PATTERN.fullmatch(panel_id) is None:
                _add(issues, path, f"{prefix}.id", "must match pNN-NN")
            else:
                all_panel_ids.append(panel_id)
                expected_id = f"p{page_index + 1:02d}-{panel_index + 1:02d}"
                if panel_id != expected_id:
                    _add(issues, path, f"{prefix}.id", f"must equal {expected_id}")
            if panel.get("order") != panel_index + 1:
                _add(issues, path, f"{prefix}.order", "must match reading order")
            if panel.get("scene_id") not in known_scenes:
                _add(issues, path, f"{prefix}.scene_id", "must reference a story scene")
            for field in ("beat", "shot", "composition", "action", "expression", "lighting"):
                _nonempty_string(panel.get(field), issues, path, f"{prefix}.{field}")
            panel_characters = _string_list(panel.get("characters"), issues, path, f"{prefix}.characters") or []
            for item_index, character_id in enumerate(panel_characters):
                if character_id not in known_characters:
                    _add(issues, path, f"{prefix}.characters[{item_index}]", "must reference the character bible")
            if len(set(panel_characters)) != len(panel_characters):
                _add(issues, path, f"{prefix}.characters", "character IDs must be unique")
            continuity = _string_list(panel.get("continuity"), issues, path, f"{prefix}.continuity") or []
            if len(set(continuity)) != len(continuity):
                _add(issues, path, f"{prefix}.continuity", "continuity anchors must be unique")
            for continuity_index, anchor in enumerate(continuity):
                anchor_field = f"{prefix}.continuity[{continuity_index}]"
                owner, separator, fact = anchor.partition(":")
                if not separator or not fact.strip() or owner not in known_characters | known_scenes:
                    _add(issues, path, anchor_field, "must reference a known character or scene anchor")
                elif owner in character_invariants and fact.strip() not in character_invariants[owner]:
                    _add(issues, path, anchor_field, "must reuse an exact character invariant")
                elif owner in scene_anchors and fact.strip() != scene_anchors[owner]:
                    _add(issues, path, anchor_field, "must reuse the exact scene continuity anchor")
            _string_list(panel.get("negative"), issues, path, f"{prefix}.negative", minimum=1)
            rect = _object(panel.get("rect"), {"x", "y", "width", "height"}, {"x", "y", "width", "height"}, issues, path, f"{prefix}.rect")
            valid_rect: dict[str, int] | None = None
            if rect is not None and all(isinstance(rect.get(key), int) and not isinstance(rect.get(key), bool) for key in ("x", "y", "width", "height")):
                valid_rect = {key: int(rect[key]) for key in ("x", "y", "width", "height")}
                if (
                    valid_rect["width"] <= 0 or valid_rect["height"] <= 0
                    or valid_rect["x"] < MARGIN or valid_rect["y"] < MARGIN
                    or valid_rect["x"] + valid_rect["width"] > PAGE_WIDTH - MARGIN
                    or valid_rect["y"] + valid_rect["height"] > PAGE_HEIGHT - MARGIN
                ):
                    _add(issues, path, f"{prefix}.rect", "must stay inside the page margin with positive area")
                if panel_index < len(expected_rects) and valid_rect != expected_rects[panel_index]:
                    _add(issues, path, f"{prefix}.rect", "must equal the fixed layout rectangle")
                page_rectangles.append((prefix, valid_rect))
            elif rect is not None:
                _add(issues, path, f"{prefix}.rect", "rectangle values must be integers")
            text_items = panel.get("text")
            if not isinstance(text_items, list):
                _add(issues, path, f"{prefix}.text", "must be an array")
            else:
                if len(text_items) > 3:
                    _add(issues, path, f"{prefix}.text", "must contain 0 to 3 text items")
                total_words = 0
                for text_index, text_value in enumerate(text_items):
                    text_prefix = f"{prefix}.text[{text_index}]"
                    total_words += _validate_text_item(
                        text_value, set(panel_characters), known_characters,
                        issues, path, text_prefix,
                    )
                    if isinstance(text_value, dict) and isinstance(text_value.get("id"), str):
                        all_text_ids.append(text_value["id"])
                if total_words > 45:
                    _add(issues, path, f"{prefix}.text", "panel text exceeds 45 total words")
        for first_index, (first_prefix, first) in enumerate(page_rectangles):
            for second_prefix, second in page_rectangles[first_index + 1:]:
                if rectangles_overlap(first, second):
                    _add(issues, path, f"{first_prefix}.rect", f"overlaps {second_prefix}.rect")
    if total_panels > 12:
        _add(issues, path, "pages.panels", "project must contain at most 12 panels")
    if len(set(all_panel_ids)) != len(all_panel_ids):
        _add(issues, path, "pages.panels", "panel IDs must be unique")
    if len(set(all_text_ids)) != len(all_text_ids):
        _add(issues, path, "pages.panels.text", "text IDs must be unique")
    return _sorted(issues)


def _validate_panel_record_v2(data: dict[str, object]) -> list[ValidationIssue]:
    """Validate a schema-v2 panel quality record."""
    panel_name = data.get("subject_id") if isinstance(data, dict) else "unknown"
    path = f"qa/panels/{panel_name}.json"
    issues: list[ValidationIssue] = []
    required_fields = {
        "schema_version", "kind", "subject_id", "bindings", "checks",
        "review", "decision", "unresolved_warnings",
    }
    fields = required_fields | {"override_reason"}
    root = _object(data, fields, required_fields, issues, path, "")
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "2.0":
        _add(issues, path, "schema_version", "must equal 2.0")
    if root.get("kind") != "panel-qa":
        _add(issues, path, "kind", "must equal panel-qa")
    subject_id = root.get("subject_id")
    if not isinstance(subject_id, str) or PANEL_ID_PATTERN.fullmatch(subject_id) is None:
        _add(issues, path, "subject_id", "must match pNN-NN")

    binding_fields = {
        "raw_path", "raw_sha256", "raw_width", "raw_height",
        "clean_path", "clean_sha256", "clean_width", "clean_height",
        "normalization_path", "normalization_sha256",
    }
    bindings = _object(
        root.get("bindings"), binding_fields, binding_fields, issues, path, "bindings"
    )
    if bindings is not None:
        for name in ("raw_path", "clean_path", "normalization_path"):
            _relative_path(bindings.get(name), issues, path, f"bindings.{name}")
        if isinstance(subject_id, str):
            canonical_paths = {
                "raw_path": f"panels/raw/{subject_id}.png",
                "clean_path": f"panels/{subject_id}/clean.png",
                "normalization_path": f"panels/{subject_id}/normalization.json",
            }
            for name, expected in canonical_paths.items():
                if bindings.get(name) != expected:
                    _add(issues, path, f"bindings.{name}", "must match the canonical panel path")
        for name in ("raw_sha256", "clean_sha256", "normalization_sha256"):
            _sha256(bindings.get(name), issues, path, f"bindings.{name}")
        for name in ("raw_width", "raw_height", "clean_width", "clean_height"):
            _integer(bindings.get(name), 1, 100_000, issues, path, f"bindings.{name}")

    checks = root.get("checks")
    for category in validate_quality_checks(checks, PANEL_CHECK_IDS):
        _add(issues, path, f"checks.{category}", category)

    review_fields = {"method", "reviewer", "reviewed_at"}
    review = _object(root.get("review"), review_fields, review_fields, issues, path, "review")
    if review is not None:
        _nonempty_string(review.get("method"), issues, path, "review.method")
        _nonempty_string(review.get("reviewer"), issues, path, "review.reviewer")
        _timestamp(review.get("reviewed_at"), issues, path, "review.reviewed_at")

    decision = root.get("decision")
    if decision not in {"accept", "accept-warning", "regenerate"}:
        _add(issues, path, "decision", "unknown quality decision")
    has_error_failure = isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("result") == "fail"
        and check.get("severity") == "error"
        for check in checks
    )
    if has_error_failure and decision != "regenerate":
        _add(issues, path, "decision", "error-level failures require regenerate")
    has_warning = isinstance(checks, list) and any(
        isinstance(check, dict)
        and (check.get("result") == "warning" or check.get("severity") == "warning")
        for check in checks
    )
    if has_warning and decision not in {"accept-warning", "regenerate"}:
        _add(issues, path, "decision", "warnings require accept-warning or regenerate")
    unresolved = _string_list(
        root.get("unresolved_warnings"), issues, path, "unresolved_warnings"
    )
    if decision == "accept-warning" and not unresolved:
        _add(issues, path, "unresolved_warnings", "accepted warnings must be recorded")
    if decision == "accept" and unresolved:
        _add(issues, path, "unresolved_warnings", "accepted record cannot have warnings")
    override_reason = root.get("override_reason")
    if "override_reason" in root:
        _nonempty_string(override_reason, issues, path, "override_reason")
        if decision != "accept-warning":
            _add(issues, path, "override_reason", "is allowed only for accept-warning")
        has_failed_warning = isinstance(checks, list) and any(
            isinstance(check, dict)
            and check.get("result") == "fail"
            and check.get("severity") == "warning"
            for check in checks
        )
        if not has_failed_warning:
            _add(
                issues,
                path,
                "override_reason",
                "requires a failed check downgraded to warning severity",
            )
        if unresolved is not None and override_reason not in unresolved:
            _add(
                issues,
                path,
                "override_reason",
                "must also appear in unresolved_warnings",
            )
    return _sorted(issues)


def validate_panel_record(data: dict[str, object]) -> list[ValidationIssue]:
    """Validate a panel quality record from either supported schema."""
    if isinstance(data, dict) and data.get("schema_version") == "2.0":
        return _validate_panel_record_v2(data)
    panel_name = data.get("panel_id") if isinstance(data, dict) else "unknown"
    path = f"qa/panels/{panel_name}.json"
    issues: list[ValidationIssue] = []
    fields = {
        "schema_version", "panel_id", "source_prompt_path", "raw_path", "clean_path",
        "raw_sha256", "dimensions", "attempts", "generation", "checks",
        "decision", "retry_reason", "unresolved_warnings",
    }
    root = _object(
        data,
        fields | {"failure_category", "override_reason"},
        fields,
        issues,
        path,
        "",
    )
    if root is None:
        return _sorted(issues)
    if root.get("schema_version") != "1.0":
        _add(issues, path, "schema_version", "must equal 1.0")
    if not isinstance(root.get("panel_id"), str) or PANEL_ID_PATTERN.fullmatch(root["panel_id"]) is None:
        _add(issues, path, "panel_id", "must match pNN-NN")
    for field in ("source_prompt_path", "raw_path", "clean_path"):
        _relative_path(root.get(field), issues, path, field, nullable=True)
    _sha256(root.get("raw_sha256"), issues, path, "raw_sha256", nullable=True)
    dimensions = _object(root.get("dimensions"), {"width", "height"}, {"width", "height"}, issues, path, "dimensions")
    if dimensions is not None:
        _integer(dimensions.get("width"), 0, 100_000, issues, path, "dimensions.width")
        _integer(dimensions.get("height"), 0, 100_000, issues, path, "dimensions.height")
    _integer(root.get("attempts"), 0, 3, issues, path, "attempts")
    generation_fields = {"capability_name", "reference_paths", "completed_at"}
    generation = _object(root.get("generation"), generation_fields, generation_fields, issues, path, "generation")
    if generation is not None:
        if generation.get("capability_name") is not None:
            _nonempty_string(generation.get("capability_name"), issues, path, "generation.capability_name")
        references = generation.get("reference_paths")
        if not isinstance(references, list):
            _add(issues, path, "generation.reference_paths", "must be an array")
        else:
            for index, reference in enumerate(references):
                _relative_path(reference, issues, path, f"generation.reference_paths[{index}]")
        _timestamp(generation.get("completed_at"), issues, path, "generation.completed_at", nullable=True)
    checks = root.get("checks")
    if not isinstance(checks, list):
        _add(issues, path, "checks", "must be an array")
    else:
        actual_ids = [check.get("id") if isinstance(check, dict) else None for check in checks]
        if tuple(actual_ids) != PANEL_CHECK_IDS:
            _add(issues, path, "checks", "must contain the seven required checks in normative order")
        check_fields = {"id", "result", "severity", "evidence"}
        for index, value in enumerate(checks):
            prefix = f"checks[{index}]"
            check = _object(value, check_fields, check_fields, issues, path, prefix)
            if check is None:
                continue
            if check.get("result") not in {"pass", "fail", "warning"}:
                _add(issues, path, f"{prefix}.result", "unknown check result")
            if check.get("severity") not in {"error", "warning"}:
                _add(issues, path, f"{prefix}.severity", "unknown check severity")
            _nonempty_string(check.get("evidence"), issues, path, f"{prefix}.evidence")
    decision = root.get("decision")
    if decision not in {"accept", "regenerate", "accept_with_warnings"}:
        _add(issues, path, "decision", "unknown panel decision")
    has_error_failure = isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("result") == "fail"
        and check.get("severity") == "error"
        for check in checks
    )
    if has_error_failure and decision != "regenerate":
        _add(issues, path, "decision", "error-level failed checks require regeneration")
    has_warning = isinstance(checks, list) and any(
        isinstance(check, dict)
        and (
            check.get("result") == "warning"
            or (check.get("result") == "fail" and check.get("severity") == "warning")
        )
        for check in checks
    )
    if has_warning and decision not in {"accept_with_warnings", "regenerate"}:
        _add(issues, path, "decision", "warning checks require accept_with_warnings or regeneration")
    retry_reason = root.get("retry_reason")
    if decision == "regenerate":
        _nonempty_string(retry_reason, issues, path, "retry_reason")
    elif retry_reason is not None:
        _add(issues, path, "retry_reason", "must be null unless regenerating")
    failure_category = root.get("failure_category")
    if failure_category is not None and (
        not isinstance(failure_category, str)
        or CATEGORY.fullmatch(failure_category) is None
    ):
        _add(issues, path, "failure_category", "must be a sanitized category string or null")
    override_reason = root.get("override_reason")
    if "override_reason" in root:
        _nonempty_string(override_reason, issues, path, "override_reason")
    unresolved = _string_list(root.get("unresolved_warnings"), issues, path, "unresolved_warnings")
    if decision == "accept_with_warnings" and (not has_warning or not unresolved):
        _add(issues, path, "unresolved_warnings", "accepted warnings require check evidence and user-visible impact")
    if "override_reason" in root:
        has_overridden_failure = isinstance(checks, list) and any(
            isinstance(check, dict)
            and check.get("result") == "fail"
            and check.get("severity") == "warning"
            for check in checks
        )
        if failure_category != "visual_qa":
            _add(issues, path, "failure_category", "an override must record visual_qa")
        if decision != "accept_with_warnings":
            _add(issues, path, "override_reason", "is allowed only for accept_with_warnings")
        if not has_overridden_failure:
            _add(issues, path, "override_reason", "requires a failed check downgraded to warning severity")
        if unresolved is not None and override_reason not in unresolved:
            _add(issues, path, "override_reason", "must also appear in unresolved_warnings")
    attempts = root.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts > 0:
        for field in ("source_prompt_path", "raw_path", "clean_path"):
            if root.get(field) is None:
                _add(issues, path, field, "is required after generation")
        if root.get("raw_sha256") is None:
            _add(issues, path, "raw_sha256", "is required after generation")
        if isinstance(dimensions, dict):
            if not isinstance(dimensions.get("width"), int) or dimensions.get("width", 0) <= 0:
                _add(issues, path, "dimensions.width", "must be positive after generation")
            if not isinstance(dimensions.get("height"), int) or dimensions.get("height", 0) <= 0:
                _add(issues, path, "dimensions.height", "must be positive after generation")
        if isinstance(generation, dict):
            if generation.get("capability_name") is None:
                _add(issues, path, "generation.capability_name", "is required after generation")
            if generation.get("completed_at") is None:
                _add(issues, path, "generation.completed_at", "is required after generation")
    if decision in {"accept", "accept_with_warnings"} and attempts == 0:
        _add(issues, path, "attempts", "accepted panels require a generation attempt")
    return _sorted(issues)


def _contained_project_path(project_dir: Path, relative_path: str) -> Path:
    """Resolve a safe project path while recording validation issues."""
    return contained_project_path(project_dir, relative_path)


def _read_canonical_json(
    project_dir: Path,
    relative_path: str,
    issues: list[ValidationIssue],
) -> dict[str, object] | None:
    """Read canonical JSON while recording validation issues."""
    try:
        path = _contained_project_path(project_dir, relative_path)
    except ValueError as error:
        _add(issues, relative_path, "file", str(error))
        return None
    if not path.is_file():
        _add(issues, relative_path, "file", "required file is missing")
        return None
    try:
        path = contained_project_path(project_dir, relative_path, must_exist=True)
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        _add(issues, relative_path, "file", f"cannot read JSON: {type(error).__name__}: {error}")
        return None
    canonical = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # Normalize CRLF so the comparison passes on Windows checkouts where
    # Git may have converted tracked JSON files (ponytail: keep the
    # canonical artifact LF-only; tolerate CRLF as input).
    raw_normalized = raw.replace(b"\r\n", b"\n")
    if raw_normalized != canonical:
        _add(issues, relative_path, "file", "JSON must use canonical two-space sorted UTF-8 formatting")
    return data


def validate_panel_provenance(
    project_dir: Path,
    record: dict[str, object],
) -> tuple[ValidationIssue, ...]:
    """Recompute schema-2.0 panel bindings and reject stale provenance."""
    panel_id = record.get("subject_id")
    record_path = f"qa/panels/{panel_id if isinstance(panel_id, str) else 'unknown'}.json"
    issues: list[ValidationIssue] = []
    bindings = record.get("bindings")

    def stale(field: str, detail: str) -> None:
        """Report whether a panel quality record is stale."""
        _add(
            issues,
            record_path,
            f"bindings.{field}",
            f"quality-record-stale: {detail}",
        )

    if not isinstance(bindings, dict):
        stale("bindings", "bindings object is missing")
        return tuple(_sorted(issues))

    required = {
        "raw_path", "raw_sha256", "raw_width", "raw_height",
        "clean_path", "clean_sha256", "clean_width", "clean_height",
        "normalization_path", "normalization_sha256",
    }
    for field in sorted(required - set(bindings)):
        stale(field, "required provenance binding is missing")

    if isinstance(panel_id, str):
        canonical_paths = {
            "raw_path": f"panels/raw/{panel_id}.png",
            "clean_path": f"panels/{panel_id}/clean.png",
            "normalization_path": f"panels/{panel_id}/normalization.json",
        }
        for field, expected in canonical_paths.items():
            if bindings.get(field) != expected:
                stale(field, "binding does not match the canonical panel path")

    resolved: dict[str, Path] = {}
    for prefix in ("raw", "clean", "normalization"):
        path_field = f"{prefix}_path"
        value = bindings.get(path_field)
        if not isinstance(value, str):
            continue
        try:
            path = contained_project_path(project_dir, value, must_exist=True)
        except (OSError, ValueError):
            stale(path_field, "bound artifact is missing or outside the project")
            continue
        if not path.is_file():
            stale(path_field, "bound artifact is not a regular file")
            continue
        resolved[prefix] = path
        digest_field = f"{prefix}_sha256"
        expected = bindings.get(digest_field)
        if not isinstance(expected, str) or SHA256_PATTERN.fullmatch(expected) is None:
            stale(digest_field, "bound SHA-256 is invalid")
        elif sha256_file(path) != expected:
            stale(digest_field, "bound SHA-256 does not match current bytes")

    for prefix in ("raw", "clean"):
        path = resolved.get(prefix)
        if path is None:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with open_path_nofollow(path) as stream, Image.open(stream) as image:
                    if image.width * image.height > MAX_DECODED_PIXELS:
                        raise Image.DecompressionBombError("raster exceeds decode limit")
                    image.load()
                    actual_size = image.size
        except (
            OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning,
        ):
            stale(f"{prefix}_path", "bound raster is unreadable or exceeds the decode limit")
            continue
        for axis, actual in zip(("width", "height"), actual_size):
            field = f"{prefix}_{axis}"
            if field in bindings and bindings.get(field) != actual:
                stale(field, f"recorded {axis} does not match current raster")

    normalization_path = resolved.get("normalization")
    if normalization_path is not None:
        try:
            normalization = json.loads(normalization_path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            stale("normalization_path", "normalization record is unreadable")
        else:
            if not isinstance(normalization, dict):
                stale("normalization_path", "normalization record must be an object")
            else:
                source = normalization.get("source")
                clean = normalization.get("clean")
                if not isinstance(source, dict) or not isinstance(clean, dict):
                    stale("normalization_path", "normalization record structure is invalid")
                else:
                    for field, normalized_value in (
                        ("raw_path", source.get("path")),
                        ("raw_sha256", source.get("sha256")),
                        ("clean_path", clean.get("path")),
                        ("clean_sha256", clean.get("sha256")),
                    ):
                        if bindings.get(field) != normalized_value:
                            stale(field, "binding disagrees with normalization record")
                    for prefix, size in (
                        ("raw", source.get("size")),
                        ("clean", clean.get("size")),
                    ):
                        if not isinstance(size, list) or len(size) != 2:
                            stale(
                                "normalization_path",
                                f"normalization {prefix} size is invalid",
                            )
                            continue
                        for axis, actual in zip(("width", "height"), size):
                            field = f"{prefix}_{axis}"
                            if field in bindings and bindings.get(field) != actual:
                                stale(
                                    field,
                                    "binding disagrees with normalization record",
                                )
    return tuple(_sorted(issues))


def validate_lettering_provenance(
    project_dir: Path,
    panel_id: str,
) -> tuple[ValidationIssue, ...]:
    """Recompute typography/lettering bindings and reject invalid geometry."""
    issues: list[ValidationIssue] = []
    geometry_relative = f"panels/{panel_id}/lettering.json"
    typography_relative = f"panels/{panel_id}/typography.json"

    def stale(field: str, detail: str) -> None:
        """Report whether a panel quality record is stale."""
        _add(
            issues,
            geometry_relative,
            field,
            f"lettering-record-stale: {detail}",
        )

    try:
        geometry_path = contained_project_path(
            project_dir, geometry_relative, must_exist=True
        )
        geometry = json.loads(geometry_path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        stale("geometry.path", "lettering geometry is missing or unreadable")
        return tuple(_sorted(issues))
    if not isinstance(geometry, dict):
        stale("geometry.path", "lettering geometry must be an object")
        return tuple(_sorted(issues))
    if geometry.get("panel_id") != panel_id:
        stale("panel_id", "geometry panel ID does not match its path")
    if geometry.get("geometry_sha256") != lettering_geometry_hash(geometry):
        stale("geometry_sha256", "canonical geometry hash does not match")

    try:
        typography_path = contained_project_path(
            project_dir, typography_relative, must_exist=True
        )
        typography = json.loads(typography_path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        stale("typography.path", "typography preflight is missing or unreadable")
        typography = None
    if isinstance(typography, dict):
        if typography.get("status") != "pass" or typography.get("issues") != []:
            stale("typography.status", "typography preflight is not a clean pass")
        glyphs = typography.get("glyphs")
        if not isinstance(glyphs, list):
            stale("glyphs", "typography glyph list is missing")
        else:
            for glyph in glyphs:
                if not isinstance(glyph, dict):
                    stale("glyphs", "typography glyph entry is invalid")
                    break
                font_id = glyph.get("font_id")
                if (
                    not isinstance(font_id, str)
                    or not font_id
                    or font_id == ".notdef"
                    or "/" in font_id
                    or "\\" in font_id
                ):
                    stale("glyphs.font_id", "glyph resolves to .notdef or a private path")
                    break
                if glyph.get("coverage") != "supported" or glyph.get("shaping") != "supported":
                    stale("glyphs", "glyph coverage or shaping is unsupported")
                    break

    bindings = geometry.get("bindings")
    if not isinstance(bindings, dict):
        stale("bindings", "geometry bindings are missing")
        bindings = {}
    for digest_field, path_field in (
        ("clean_sha256", "clean_path"),
        ("storyboard_sha256", "storyboard_path"),
    ):
        relative = bindings.get(path_field)
        expected = bindings.get(digest_field)
        try:
            artifact = (
                contained_project_path(project_dir, relative, must_exist=True)
                if isinstance(relative, str)
                else None
            )
        except (OSError, ValueError):
            artifact = None
        if artifact is None or not artifact.is_file():
            stale(f"bindings.{path_field}", "bound artifact is missing")
        elif not isinstance(expected, str) or sha256_file(artifact) != expected:
            stale(f"bindings.{digest_field}", "bound artifact hash does not match")

    if isinstance(typography, dict):
        typography_hash = hashlib.sha256(
            canonical_artifact_bytes(typography)
        ).hexdigest()
        if bindings.get("typography_sha256") != typography_hash:
            stale("typography.path", "typography record hash does not match")
        if bindings.get("font_policy_sha256") != typography.get("font_policy_sha256"):
            stale("bindings.font_policy_sha256", "font policy hash does not match preflight")

    lettered = geometry.get("lettered")
    if not isinstance(lettered, dict):
        stale("lettered", "lettered artifact descriptor is missing")
    else:
        relative = lettered.get("path")
        try:
            artifact = (
                contained_project_path(project_dir, relative, must_exist=True)
                if isinstance(relative, str)
                else None
            )
        except (OSError, ValueError):
            artifact = None
        if artifact is None or not artifact.is_file():
            stale("lettered.path", "lettered image is missing")
        elif lettered.get("sha256") != sha256_file(artifact):
            stale("lettered.sha256", "lettered image hash does not match")

    items = geometry.get("items")
    if not isinstance(items, list):
        stale("items", "geometry items must be an array")
    else:
        orders: list[int] = []
        for entry in items:
            if not isinstance(entry, dict):
                stale("items", "geometry item must be an object")
                continue
            order = entry.get("reading_order")
            if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
                stale("items.reading_order", "reading order must be a positive integer")
            else:
                orders.append(order)
            box = entry.get("box")
            if (
                not isinstance(box, dict)
                or any(
                    not isinstance(box.get(key), int)
                    or isinstance(box.get(key), bool)
                    for key in ("x", "y", "width", "height")
                )
                or box.get("width", 0) <= 0
                or box.get("height", 0) <= 0
            ):
                stale("items.box", "item box is missing or non-positive")
            tail = entry.get("tail")
            if tail is not None:
                expected_tail_fields = {
                    "attachment", "base", "control", "length", "policy_version",
                    "source_gap", "speaker_anchor", "tip", "voice_source", "width",
                }

                def finite_point(value: object) -> bool:
                    """Report whether a value is a finite coordinate point."""
                    return (
                        isinstance(value, list)
                        and len(value) == 2
                        and all(
                            isinstance(coordinate, (int, float))
                            and not isinstance(coordinate, bool)
                            and math.isfinite(coordinate)
                            for coordinate in value
                        )
                    )

                valid_tail = (
                    isinstance(tail, dict)
                    and set(tail) == expected_tail_fields
                    and tail.get("policy_version") == "organic-cubic-v1"
                    and tail.get("voice_source") in {"human", "device"}
                    and finite_point(tail.get("speaker_anchor"))
                    and all(0 <= coordinate <= 1 for coordinate in tail.get("speaker_anchor", []))
                    and finite_point(tail.get("attachment"))
                    and finite_point(tail.get("tip"))
                )
                base = tail.get("base") if isinstance(tail, dict) else None
                control = tail.get("control") if isinstance(tail, dict) else None
                valid_tail = (
                    valid_tail
                    and isinstance(base, list)
                    and len(base) == 2
                    and all(finite_point(point) for point in base)
                    and isinstance(control, list)
                    and len(control) == 2
                    and all(
                        isinstance(side, list)
                        and len(side) == 2
                        and all(finite_point(point) for point in side)
                        for side in control
                    )
                    and all(
                        isinstance(tail.get(field), (int, float))
                        and not isinstance(tail.get(field), bool)
                        and math.isfinite(tail[field])
                        and tail[field] > 0
                        for field in ("length", "source_gap", "width")
                    )
                )
                if not valid_tail:
                    stale(
                        "items.tail",
                        "tail must be a finite organic-cubic-v1 semantic geometry record",
                    )
        if len(orders) != len(set(orders)):
            stale("items.reading_order", "reading order values must be unique")

    return tuple(_sorted(issues))


def _load_artifact(
    project_dir: Path,
    relative_path: str,
    validator: Callable[[dict[str, object]], list[ValidationIssue]],
    issues: list[ValidationIssue],
) -> dict[str, object] | None:
    """Load a manifest artifact while recording validation issues."""
    data = _read_canonical_json(project_dir, relative_path, issues)
    if data is None:
        return None
    issues.extend(validator(data))
    return data


def _validate_raster(
    project_dir: Path,
    relative_path: object,
    issue_path: str,
    field: str,
    issues: list[ValidationIssue],
    expected_ratio: float | None = None,
) -> tuple[int, int] | None:
    """Validate raster artifact dimensions and decodability."""
    local_issues: list[ValidationIssue] = []
    if not _relative_path(relative_path, local_issues, issue_path, field):
        issues.extend(local_issues)
        return None
    assert isinstance(relative_path, str)
    try:
        image_path = _contained_project_path(project_dir, relative_path)
    except ValueError:
        _add(issues, issue_path, field, "referenced image path escapes the project boundary")
        return None
    if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        _add(issues, issue_path, field, "must be PNG, JPEG, or WebP")
        return None
    if not image_path.is_file():
        _add(issues, issue_path, field, "referenced image is missing")
        return None
    try:
        image_path = contained_project_path(project_dir, relative_path, must_exist=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with open_path_nofollow(image_path) as stream, Image.open(stream) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    _add(issues, issue_path, field, "must contain PNG, JPEG, or WebP data")
                width, height = image.size
                if width * height > MAX_DECODED_PIXELS:
                    raise Image.DecompressionBombError("raster exceeds decode limit")
                if width < 512 or height < 512:
                    _add(issues, issue_path, field, "image dimensions must both be at least 512px")
                if "A" in image.mode or "transparency" in image.info:
                    _add(issues, issue_path, field, "image has unintended alpha transparency")
                if expected_ratio is not None and height > 0:
                    actual_ratio = width / height
                    if abs(actual_ratio - expected_ratio) / expected_ratio > 0.02:
                        _add(issues, issue_path, field, "image aspect ratio differs from storyboard by more than 2%")
                image.load()
                return width, height
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        _add(issues, issue_path, field, f"image is unreadable: {type(error).__name__}")
        return None


def _storyboard_panel_map(storyboard: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return storyboard panels keyed by canonical panel identifier."""
    result: dict[str, dict[str, object]] = {}
    for page in storyboard.get("pages", []):
        if not isinstance(page, dict):
            continue
        for panel in page.get("panels", []):
            if isinstance(panel, dict) and isinstance(panel.get("id"), str):
                result[panel["id"]] = panel
    return result


def validate_page_qa_record(record: dict[str, object]) -> list[ValidationIssue]:
    """Validate a readable legacy schema-1.0 page-QA record for migration."""
    issues: list[ValidationIssue] = []
    allowed = {"page", "page_path", "page_sha256", "schema_version", "status"}
    required = {"page", "page_path", "page_sha256", "schema_version", "status"}
    data = _object(record, allowed, required, issues, "qa/pages/", "")
    if data is not None:
        page = data.get("page")
        if not isinstance(page, int) or page < 1:
            _add(issues, "qa/pages/", "page", "must be a positive integer")
        _relative_path(data.get("page_path"), issues, "qa/pages/", "page_path")
        _sha256(data.get("page_sha256"), issues, "qa/pages/", "page_sha256")
        status = data.get("status")
        if status != "reviewed":
            _add(issues, "qa/pages/", "status", 'must be "reviewed"')
    return _sorted(issues)


def require_valid_project(project_dir: Path, stage: str) -> None:
    """Raise ProjectValidationError when the project fails validation."""
    issues = validate_project(project_dir, stage)
    if issues:
        raise ProjectValidationError(issues)


def validate_pdf_verification(
    project_dir: Path,
    project_id: str,
    page_count: int,
) -> list[ValidationIssue]:
    """Validate the exported PDF's hash-bound full-content verification record."""
    relative = "exports/pdf-verification.json"
    try:
        path = contained_project_path(project_dir, relative)
    except (OSError, ValueError):
        return [ValidationIssue(relative, "pdf-verification-stale", "verification path escapes the project boundary")]
    stale_reasons: list[str] = []
    try:
        payload = path.read_bytes()
        record = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        record = None
        stale_reasons.append("verification record is missing or unreadable")

    if isinstance(record, dict):
        if record.get("schema_version") != "1.0":
            stale_reasons.append("schema version is unsupported")
        if record.get("kind") != "pdf-verification":
            stale_reasons.append("record kind is invalid")
        verified_at = record.get("verified_at")
        if (
            not isinstance(verified_at, str)
            or TIMESTAMP_PATTERN.fullmatch(verified_at) is None
        ):
            stale_reasons.append("verification timestamp is not ISO 8601 UTC")
        expected_pdf = f"exports/{project_id}.pdf"
        if record.get("pdf_path") != expected_pdf:
            stale_reasons.append("PDF path binding is stale")
        try:
            pdf_path = contained_project_path(project_dir, expected_pdf)
        except (OSError, ValueError):
            pdf_path = None
        pdf_hash = record.get("pdf_sha256")
        if (
            not isinstance(pdf_hash, str)
            or not SHA256_PATTERN.fullmatch(pdf_hash)
            or pdf_path is None
            or not pdf_path.is_file()
            or sha256_file(pdf_path) != pdf_hash
        ):
            stale_reasons.append("PDF hash binding is stale")
        if record.get("page_count") != page_count:
            stale_reasons.append("page count binding is stale")

        sources = record.get("source_pages")
        if not isinstance(sources, list) or len(sources) != page_count:
            stale_reasons.append("ordered source page bindings are incomplete")
        else:
            for page_number, source in enumerate(sources, 1):
                expected_page = f"pages/page-{page_number:03d}.png"
                expected_qa = f"qa/pages/page-{page_number:03d}.json"
                if not isinstance(source, dict):
                    stale_reasons.append(f"page {page_number} binding is invalid")
                    continue
                if source.get("path") != expected_page:
                    stale_reasons.append(f"page {page_number} path binding is stale")
                if source.get("page_qa_path") != expected_qa:
                    stale_reasons.append(f"page {page_number} QA path binding is stale")
                if source.get("dimensions") != [PAGE_WIDTH, PAGE_HEIGHT]:
                    stale_reasons.append(f"page {page_number} dimensions are stale")
                for binding, relative_path in (
                    ("sha256", expected_page),
                    ("page_qa_sha256", expected_qa),
                ):
                    expected_hash = source.get(binding)
                    try:
                        current_path = contained_project_path(project_dir, relative_path)
                    except (OSError, ValueError):
                        current_path = None
                    if (
                        not isinstance(expected_hash, str)
                        or not SHA256_PATTERN.fullmatch(expected_hash)
                        or current_path is None
                        or not current_path.is_file()
                        or sha256_file(current_path) != expected_hash
                    ):
                        stale_reasons.append(
                            f"page {page_number} {binding} binding is stale"
                        )
    elif not stale_reasons:
        stale_reasons.append("verification record must be an object")

    if not stale_reasons:
        return []
    return [ValidationIssue(
        relative,
        "pdf-verification-stale",
        "; ".join(dict.fromkeys(stale_reasons)),
    )]


def validate_project(project_dir: Path, stage: str = "all") -> list[ValidationIssue]:
    """Validate all artifacts required for a project stage."""
    project_dir = Path(project_dir)
    if stage not in STAGES:
        raise ValueError(f"unknown validation stage: {stage}")
    if not project_dir.is_dir():
        raise FileNotFoundError(f"project directory does not exist: {project_dir}")

    issues: list[ValidationIssue] = []
    manifest = _load_artifact(project_dir, "project.json", validate_manifest, issues)
    needs_plan = stage in {"all", "plan", "storyboard", "panels", "final", "export-ready"}
    needs_storyboard = stage in {"all", "storyboard", "panels", "final", "export-ready"}
    needs_panels = stage in {"all", "panels", "final", "export-ready"}
    story = None
    characters = None
    storyboard = None
    panel_errors: list[tuple[str, str]] = []
    panel_warnings: list[tuple[str, str]] = []
    if needs_plan:
        story = _load_artifact(project_dir, "plan/story-plan.json", validate_story_plan, issues)
        characters = _load_artifact(
            project_dir, "plan/character-bible.json", validate_character_bible, issues
        )
        if story is not None and characters is not None:
            known = {
                character.get("id") for character in characters.get("characters", [])
                if isinstance(character, dict)
            }
            for scene_index, scene in enumerate(story.get("scenes", [])):
                if not isinstance(scene, dict):
                    continue
                for character_index, character_id in enumerate(scene.get("characters", [])):
                    if character_id not in known:
                        _add(
                            issues,
                            "plan/story-plan.json",
                            f"scenes[{scene_index}].characters[{character_index}]",
                            "must reference the character bible",
                        )
    if needs_storyboard:
        storyboard = _read_canonical_json(project_dir, "plan/storyboard.json", issues)
        if storyboard is not None and story is not None and characters is not None:
            issues.extend(validate_storyboard(storyboard, story, characters))
        if storyboard is not None and manifest is not None:
            pages = storyboard.get("pages")
            settings = manifest.get("settings")
            if isinstance(pages, list) and isinstance(settings, dict):
                actual_pages = len(pages)
                actual_panels = sum(
                    len(page.get("panels", []))
                    for page in pages
                    if isinstance(page, dict) and isinstance(page.get("panels"), list)
                )
                if settings.get("page_count") != actual_pages:
                    _add(issues, "project.json", "settings.page_count", "must match the storyboard page count")
                if settings.get("panel_count") != actual_panels:
                    _add(issues, "project.json", "settings.panel_count", "must match the storyboard panel count")
            panel_map = _storyboard_panel_map(storyboard)
            manifest_panels = manifest.get("panels", [])
            if isinstance(manifest_panels, list) and manifest_panels != list(panel_map):
                _add(issues, "project.json", "panels", "must match storyboard panel order")

    if needs_panels and storyboard is not None:
        panel_map = _storyboard_panel_map(storyboard)
        for panel_id, panel in panel_map.items():
            record_relative = f"qa/panels/{panel_id}.json"
            record = _read_canonical_json(project_dir, record_relative, issues)
            if record is None:
                continue
            issues.extend(validate_panel_record(record))
            is_quality_v2 = record.get("schema_version") == "2.0"
            bindings = record.get("bindings") if is_quality_v2 else None
            if not isinstance(bindings, dict):
                bindings = {}
            if not is_quality_v2:
                _add(
                    issues,
                    record_relative,
                    "quality-migration-required",
                    "schema 1.0 quality record remains readable but must be reviewed as schema 2.0",
                )
            else:
                issues.extend(validate_panel_provenance(project_dir, record))
            if stage in {"all", "final", "export-ready"}:
                checks = record.get("checks")
                has_error_failure = isinstance(checks, list) and any(
                    isinstance(check, dict)
                    and check.get("result") == "fail"
                    and check.get("severity") == "error"
                    for check in checks
                )
                hard_failure = record.get("failure_category") in {
                    "corrupt", "corrupt_image", "safety", "safety_refusal",
                }
                if record.get("decision") == "regenerate" or has_error_failure or hard_failure:
                    reason = record.get("retry_reason")
                    panel_errors.append((
                        record_relative,
                        reason if isinstance(reason, str) and reason.strip()
                        else "panel has an unresolved error",
                    ))
                unresolved = record.get("unresolved_warnings")
                if isinstance(unresolved, list):
                    panel_warnings.extend(
                        (record_relative, warning)
                        for warning in unresolved
                        if isinstance(warning, str) and warning.strip()
                    )
            record_panel_id = record.get("subject_id") if is_quality_v2 else record.get("panel_id")
            if record_panel_id != panel_id:
                id_field = "subject_id" if is_quality_v2 else "panel_id"
                _add(issues, record_relative, id_field, "must match its storyboard panel")
            rect = panel.get("rect")
            expected_ratio = None
            if isinstance(rect, dict) and isinstance(rect.get("width"), int) and isinstance(rect.get("height"), int) and rect["height"] > 0:
                expected_ratio = rect["width"] / rect["height"]
            raw_path = bindings.get("raw_path") if is_quality_v2 else record.get("raw_path")
            clean_path = bindings.get("clean_path") if is_quality_v2 else record.get("clean_path")
            raw_size = _validate_raster(
                project_dir, raw_path, record_relative,
                "bindings.raw_path" if is_quality_v2 else "raw_path",
                issues, expected_ratio,
            )
            _validate_raster(
                project_dir, clean_path, record_relative,
                "bindings.clean_path" if is_quality_v2 else "clean_path",
                issues, expected_ratio,
            )
            if raw_size is not None and not is_quality_v2:
                dimensions = record.get("dimensions")
                if isinstance(dimensions, dict) and (
                    dimensions.get("width"), dimensions.get("height")
                ) != raw_size:
                    _add(issues, record_relative, "dimensions", "must match the raw image")
            raw_hash = bindings.get("raw_sha256") if is_quality_v2 else record.get("raw_sha256")
            if isinstance(raw_path, str) and isinstance(raw_hash, str):
                try:
                    image_path = _contained_project_path(project_dir, raw_path)
                except ValueError:
                    pass
                else:
                    if image_path.is_file() and SHA256_PATTERN.fullmatch(raw_hash):
                        image_path = contained_project_path(
                            project_dir, raw_path, must_exist=True
                        )
                        if sha256_file(image_path) != raw_hash:
                            hash_field = "bindings.raw_sha256" if is_quality_v2 else "raw_sha256"
                            _add(issues, record_relative, hash_field, "hash does not match the raw image")
            clean_hash = bindings.get("clean_sha256") if is_quality_v2 else None
            if is_quality_v2 and isinstance(clean_path, str) and isinstance(clean_hash, str):
                try:
                    clean_file = _contained_project_path(project_dir, clean_path)
                except ValueError:
                    pass
                else:
                    if clean_file.is_file() and SHA256_PATTERN.fullmatch(clean_hash):
                        clean_file = contained_project_path(
                            project_dir, clean_path, must_exist=True
                        )
                        if sha256_file(clean_file) != clean_hash:
                            _add(
                                issues,
                                record_relative,
                                "bindings.clean_sha256",
                                "hash does not match the clean image",
                            )
            prompt_path = None if is_quality_v2 else record.get("source_prompt_path")
            if isinstance(prompt_path, str):
                try:
                    prompt_file = _contained_project_path(project_dir, prompt_path)
                except ValueError:
                    _add(issues, record_relative, "source_prompt_path", "referenced prompt path escapes the project boundary")
                else:
                    if not prompt_file.is_file():
                        _add(issues, record_relative, "source_prompt_path", "referenced prompt is missing")
            generation = None if is_quality_v2 else record.get("generation")
            if isinstance(generation, dict):
                for reference_index, reference_path in enumerate(generation.get("reference_paths", [])):
                    _validate_raster(
                        project_dir,
                        reference_path,
                        record_relative,
                        f"generation.reference_paths[{reference_index}]",
                        issues,
                    )
        if manifest is not None:
            source_path = manifest.get("input", {}).get("source_path") if isinstance(manifest.get("input"), dict) else None
            source_hash = manifest.get("input", {}).get("source_sha256") if isinstance(manifest.get("input"), dict) else None
            if isinstance(source_path, str) and isinstance(source_hash, str):
                try:
                    source_file = _contained_project_path(project_dir, source_path)
                except ValueError:
                    _add(issues, "project.json", "input.source_path", "referenced source path escapes the project boundary")
                else:
                    if not source_file.is_file():
                        _add(issues, "project.json", "input.source_path", "referenced source is missing")
                    elif SHA256_PATTERN.fullmatch(source_hash):
                        source_file = contained_project_path(
                            project_dir, source_path, must_exist=True
                        )
                        if sha256_file(source_file) != source_hash:
                            _add(issues, "project.json", "input.source_sha256", "hash does not match the source")

    if stage in {"all", "final", "export-ready"} and manifest is not None:
        for record_path, reason in panel_errors:
            _add(
                issues,
                record_path,
                "decision",
                f"unresolved panel error blocks final validation: {reason}",
            )
        manifest_warnings = manifest.get("warnings")
        recorded_warnings = {
            warning
            for warning in manifest_warnings
            if isinstance(warning, str)
        } if isinstance(manifest_warnings, list) else set()
        for _, warning in panel_warnings:
            if warning not in recorded_warnings:
                _add(
                    issues,
                    "project.json",
                    "warnings",
                    f"must include unresolved panel warning: {warning}",
                )
        status = manifest.get("status")
        if panel_errors and status in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
            _add(
                issues,
                "project.json",
                "status",
                "cannot be terminal success while unresolved panel errors remain",
            )
        if panel_warnings and status == "COMPLETE":
            _add(
                issues,
                "project.json",
                "status",
                "must be COMPLETE_WITH_WARNINGS while panel warnings remain",
            )
        artifacts = manifest.get("artifacts")
        if isinstance(artifacts, dict):
            for name, descriptor in artifacts.items():
                if not isinstance(descriptor, dict):
                    continue
                relative_path = descriptor.get("path")
                expected_hash = descriptor.get("sha256")
                if isinstance(relative_path, str) and isinstance(expected_hash, str):
                    try:
                        artifact = _contained_project_path(project_dir, relative_path)
                    except ValueError:
                        _add(issues, "project.json", f"artifacts.{name}.path", "referenced artifact path escapes the project boundary")
                    else:
                        if not artifact.is_file():
                            _add(issues, "project.json", f"artifacts.{name}.path", "referenced artifact is missing")
                        elif SHA256_PATTERN.fullmatch(expected_hash):
                            artifact = contained_project_path(
                                project_dir, relative_path, must_exist=True
                            )
                            if sha256_file(artifact) != expected_hash:
                                _add(issues, "project.json", f"artifacts.{name}.sha256", "hash does not match the artifact")

    # Fail-closed artifact enumeration for terminal / export-ready stages.
    if stage in {"all", "final", "export-ready"} and manifest is not None:
        settings = manifest.get("settings")
        page_count_value = settings.get("page_count") if isinstance(settings, dict) else 0
        page_count = (
            page_count_value
            if isinstance(page_count_value, int) and not isinstance(page_count_value, bool)
            else 0
        )
        # An out-of-range page_count is already reported above. Enumerating it
        # anyway would let a corrupt manifest drive unbounded filesystem work.
        page_count = max(0, min(page_count, MAX_PAGES))
        panels = manifest.get("panels", [])
        if not isinstance(panels, list):
            panels = []
        panels = panels[:MAX_PANELS]

        _validate_required_artifacts(
            project_dir, manifest, page_count, panels, issues,
            require_terminal=(stage != "export-ready"),
        )

        # Page-QA records.
        for page_number in range(1, page_count + 1):
            page_qa_relative = f"qa/pages/page-{page_number:03d}.json"
            page_qa = _read_canonical_json(project_dir, page_qa_relative, issues)
            expected_page = f"pages/page-{page_number:03d}.png"
            if page_qa is not None:
                if page_qa.get("schema_version") == "2.0":
                    for issue in validate_page_quality(project_dir, page_number):
                        _add(issues, issue.path, issue.field, issue.message)
                else:
                    issues.extend(validate_page_qa_record(page_qa))
                    _add(
                        issues,
                        page_qa_relative,
                        "schema_version",
                        "quality-migration-required: schema 1.0 page QA must be migrated",
                    )
                    if page_qa.get("page") != page_number:
                        _add(issues, page_qa_relative, "page",
                             "must match the canonical page number")
                    if page_qa.get("page_path") != expected_page:
                        _add(issues, page_qa_relative, "page_path",
                             "must match the canonical page path")
            page_path = project_dir / expected_page
            if not page_path.is_file():
                _add(issues, expected_page, "", "composed page is missing")
            elif page_qa is not None and page_qa.get("schema_version") != "2.0":
                page_hash = sha256_file(page_path)
                if isinstance(page_qa.get("page_sha256"), str):
                    if page_qa["page_sha256"] != page_hash:
                        _add(issues, page_qa_relative, "page_sha256",
                             "hash does not match the page image")

        # Lettered panels and their current typography/geometry provenance.
        for panel_id in panels:
            lettered_relative = f"panels/{panel_id}/lettered.png"
            try:
                lettered = contained_project_path(project_dir, lettered_relative)
            except (OSError, ValueError):
                _add(issues, lettered_relative, "", "lettered panel path escapes the project boundary")
                continue
            if not lettered.is_file():
                _add(issues, f"panels/{panel_id}/lettered.png", "",
                     "lettered panel is missing")
            issues.extend(validate_lettering_provenance(project_dir, panel_id))

        # export-ready does not require report, PDF, or export cache.
        if stage != "export-ready":
            report_path = project_dir / "qa/report.md"
            if not report_path.is_file():
                _add(issues, "qa/report.md", "", "QA report is missing")

            project_id = manifest.get("project_id")
            if isinstance(project_id, str) and project_id:
                pdf_relative = f"exports/{project_id}.pdf"
                try:
                    pdf_path = contained_project_path(project_dir, pdf_relative)
                except (OSError, ValueError):
                    _add(issues, "project.json", "project_id", "exported PDF path escapes the project boundary")
                    pdf_path = None
                if pdf_path is not None:
                    if not pdf_path.is_file():
                        _add(issues, f"exports/{project_id}.pdf", "",
                             "exported PDF is missing")
                    issues.extend(
                        validate_pdf_verification(project_dir, project_id, page_count)
                    )

        # Composition cache required.
        comp_cache = project_dir / "cache/composition.json"
        if not comp_cache.is_file():
            _add(issues, "cache/composition.json", "",
                 "composition stage cache is missing")

    return _sorted(issues)


def _validate_required_artifacts(
    project_dir: Path,
    manifest: dict[str, object],
    page_count: int,
    panels: list[str],
    issues: list[ValidationIssue],
    require_terminal: bool,
) -> None:
    """Report missing required artifact descriptors."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    required = {
        "character_bible", "story_plan", "storyboard", "composition_cache",
    }
    if require_terminal:
        required.update({"qa_report", "pdf", "pdf_verification"})
    for name in sorted(required):
        if name not in artifacts:
            _add(issues, "project.json", f"artifacts.{name}",
                 "required artifact descriptor is missing")

    expected_paths = {
        "character_bible": "plan/character-bible.json",
        "story_plan": "plan/story-plan.json",
        "storyboard": "plan/storyboard.json",
        "composition_cache": "cache/composition.json",
    }
    if require_terminal:
        expected_paths.update({
            "qa_report": "qa/report.md",
            "pdf_verification": "exports/pdf-verification.json",
        })
        project_id = manifest.get("project_id")
        if isinstance(project_id, str) and ID_PATTERN.fullmatch(project_id):
            expected_paths["pdf"] = f"exports/{project_id}.pdf"
    for name, expected in expected_paths.items():
        descriptor = artifacts.get(name)
        if isinstance(descriptor, dict) and descriptor.get("path") != expected:
            _add(issues, "project.json", f"artifacts.{name}.path",
                 f"must equal {expected}")


class _ValidationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        """Report command-line validation errors without usage noise."""
        raise ValueError(f"invalid invocation: {message}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = _ValidationArgumentParser(prog="validate_project.py")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--stage", choices=STAGES, default="all")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run strict project validation from the command line."""
    try:
        parser = _build_parser()
        arguments = parser.parse_args(argv)
        issues = validate_project(arguments.project_dir, arguments.stage)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    if issues:
        if arguments.as_json:
            print(json.dumps(
                {"issues": [
                    {"path": issue.path, "field": issue.field, "message": issue.message}
                    for issue in issues
                ]},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ))
        else:
            for issue in issues:
                print(f"{issue.path}:{issue.field}: {issue.message}")
        return 2
    if arguments.as_json:
        print(json.dumps({"issues": []}, indent=2, sort_keys=True))
    else:
        print(f"VALID {arguments.project_dir} ({arguments.stage})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
