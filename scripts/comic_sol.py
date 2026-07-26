#!/usr/bin/env python3
"""Deterministic project lifecycle commands for Comic Sol."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from PIL import Image, ImageFont

from project_io import (
    ProjectLock,
    ProjectTransaction,
    contained_project_path,
    durable_atomic_write,
    validate_source_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
FONT_PATH_COMIC_REGULAR = ROOT / "assets/fonts/ComicNeue-Regular.ttf"
FONT_PATH_COMIC_BOLD = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
FONT_PATH_FALLBACK = ROOT / "assets/fonts/NotoSans-Regular.ttf"
FONT_PATH = FONT_PATH_COMIC_REGULAR
PAGE_WIDTH = 1600
PAGE_HEIGHT = 2400
MARGIN = 64
GUTTER = 32

LINEAR_STATUSES = (
    "INIT",
    "PLANNED",
    "SCRIPTED",
    "STORYBOARDED",
    "REFERENCES_READY",
    "PANELS_READY",
    "QA_READY",
    "LETTERED",
    "COMPOSED",
    "EXPORTED",
    "COMPLETE",
)
TERMINAL_STATUSES = {"COMPLETE", "COMPLETE_WITH_WARNINGS"}
ALL_STATUSES = set(LINEAR_STATUSES) | {"BLOCKED", "COMPLETE_WITH_WARNINGS"}

RESUME_STAGES = (
    "planning",
    "storyboard",
    "generation",
    "lettering",
    "composition",
    "export",
)
STAGE_INVALIDATION_STATUS = {
    "planning": "INIT",
    "storyboard": "SCRIPTED",
    "generation": "REFERENCES_READY",
    "lettering": "QA_READY",
    "composition": "LETTERED",
    "export": "COMPOSED",
}
STAGE_COMPLETION_STATUS = {
    "planning": "SCRIPTED",
    "storyboard": "STORYBOARDED",
    "generation": "QA_READY",
    "lettering": "LETTERED",
    "composition": "COMPOSED",
    "export": "EXPORTED",
}
ARTIFACT_STAGE = {
    "story_plan": "planning",
    "character_bible": "planning",
    "storyboard": "storyboard",
    "qa_report": "export",
    "pdf": "export",
}
TIMESTAMP_KEYS = {"created_at", "updated_at", "detected_at", "completed_at", "timestamp"}
STAGE_CACHE_PATH = Path("logs/stage-cache.json")
GENERATION_COUNTERS_PATH = Path("logs/generation-counters.json")
PANEL_CHECK_IDS = (
    "character-identity", "anatomy", "action", "composition", "continuity",
    "text-free", "technical",
)


@dataclass(frozen=True)
class ResumeAction:
    stage: str
    action: Literal["reuse", "regenerate", "rerun", "blocked"]
    artifact: str
    reason: str

PROJECT_DIRECTORIES = (
    "source",
    "plan",
    "references/characters",
    "references/scenes",
    "prompts/references",
    "prompts/panels",
    "panels/raw",
    "panels/clean",
    "qa/panels",
    "pages",
    "exports",
    "logs",
)

SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
REQUIRED_PILLOW = "12.3.0"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EVENT_DETAIL_KINDS = {
    "project_id": "identifier",
    "panel_id": "identifier",
    "scene_id": "identifier",
    "source_path": "path",
    "artifact_path": "path",
    "attempt_path": "path",
    "source_sha256": "sha256",
    "artifact_sha256": "sha256",
    "count": "count",
    "attempt": "count",
    "attempts": "count",
    "page_count": "count",
    "panel_count": "count",
    "category": "category",
    "action": "category",
    "kind": "category",
    "status": "category",
    "from": "category",
    "to": "category",
    "warning_present": "boolean",
    "reused": "boolean",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Return compact, sorted UTF-8 JSON bytes without a trailing newline.

    Use for single-line JSONL records and for stable hash inputs. Document
    artifacts must use ``canonical_artifact_bytes`` instead.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_artifact_bytes(value: object) -> bytes:
    """Return the two-space sorted UTF-8 JSON bytes the validator requires."""
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def read_json(path: Path) -> dict[str, object]:
    """Read a UTF-8 JSON object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Compatibility wrapper for durable artifact publication."""
    durable_atomic_write(path, payload)


def atomic_write_json(path: Path, value: object) -> None:
    """Atomically write canonical human-readable artifact JSON."""
    atomic_write_bytes(path, canonical_artifact_bytes(value))


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(title: str) -> str:
    """Convert a title to a portable version-1.0 project ID."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"comic-sol-{slug}" if slug else "comic-sol-project"
    return slug[:48].rstrip("-")


def layout_rects(name: str) -> list[dict[str, int]]:
    """Return fresh rectangles for one fixed version-1.0 page layout."""
    inner_width = PAGE_WIDTH - (2 * MARGIN)
    inner_height = PAGE_HEIGHT - (2 * MARGIN)
    half_width = (inner_width - GUTTER) // 2
    half_height = (inner_height - GUTTER) // 2
    third_height = (inner_height - (2 * GUTTER)) // 3
    hero_height = 1176
    support_height = inner_height - GUTTER - hero_height
    layouts = {
        "full-page": [
            {"x": MARGIN, "y": MARGIN, "width": inner_width, "height": inner_height},
        ],
        "two-horizontal": [
            {"x": MARGIN, "y": MARGIN, "width": inner_width, "height": half_height},
            {
                "x": MARGIN,
                "y": MARGIN + half_height + GUTTER,
                "width": inner_width,
                "height": half_height,
            },
        ],
        "three-horizontal": [
            {
                "x": MARGIN,
                "y": MARGIN + (index * (third_height + GUTTER)),
                "width": inner_width,
                "height": third_height,
            }
            for index in range(3)
        ],
        "hero-top-two-bottom": [
            {"x": MARGIN, "y": MARGIN, "width": inner_width, "height": hero_height},
            {
                "x": MARGIN,
                "y": MARGIN + hero_height + GUTTER,
                "width": half_width,
                "height": support_height,
            },
            {
                "x": MARGIN + half_width + GUTTER,
                "y": MARGIN + hero_height + GUTTER,
                "width": half_width,
                "height": support_height,
            },
        ],
        "two-top-hero-bottom": [
            {"x": MARGIN, "y": MARGIN, "width": half_width, "height": support_height},
            {
                "x": MARGIN + half_width + GUTTER,
                "y": MARGIN,
                "width": half_width,
                "height": support_height,
            },
            {
                "x": MARGIN,
                "y": MARGIN + support_height + GUTTER,
                "width": inner_width,
                "height": hero_height,
            },
        ],
    }
    try:
        return [rectangle.copy() for rectangle in layouts[name]]
    except KeyError as error:
        raise ValueError(f"unknown layout: {name}") from error


def rectangles_overlap(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return whether two positive-area rectangles overlap."""
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def _allocate_project_directory(output_root: Path, base_slug: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        suffix_text = "" if suffix == 1 else f"-{suffix}"
        candidate_slug = f"{base_slug[: 48 - len(suffix_text)].rstrip('-')}{suffix_text}"
        candidate = output_root / candidate_slug
        try:
            candidate.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        return candidate


def _manifest_from_template(
    project_id: str,
    title: str,
    source: bytes,
    request: dict[str, object],
) -> dict[str, object]:
    manifest = read_json(TEMPLATES / "manifest.json")
    timestamp = _utc_now()
    manifest["project_id"] = project_id
    manifest["title"] = title
    manifest["created_at"] = timestamp
    manifest["updated_at"] = timestamp
    manifest["status"] = "INIT"
    manifest_input = manifest["input"]
    if not isinstance(manifest_input, dict):
        raise ValueError("manifest template input must be an object")
    manifest_input["mode"] = request.get("mode", "short_prompt")
    manifest_input["language"] = request.get("language", "en")
    manifest_input["source_sha256"] = hashlib.sha256(source).hexdigest()
    return manifest


def init_project(
    output_root: Path,
    title: str,
    source: bytes,
    request: dict[str, object],
) -> Path:
    """Initialize an exclusive Comic Sol project without overwriting data."""
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    if not isinstance(request, dict):
        raise TypeError("request must be a JSON object")
    if not title.strip():
        raise ValueError("title must not be empty")
    validate_source_bytes(source)

    project_dir = _allocate_project_directory(Path(output_root), slugify(title))
    for relative in PROJECT_DIRECTORIES:
        (project_dir / relative).mkdir(parents=True, exist_ok=False)

    atomic_write_bytes(project_dir / "source/input.txt", source)
    atomic_write_json(project_dir / "source/request.json", request)
    manifest = _manifest_from_template(project_dir.name, title.strip(), source, request)
    atomic_write_json(project_dir / "project.json", manifest)
    append_event(
        project_dir,
        "project.created",
        {
            "project_id": project_dir.name,
            "source_path": "source/input.txt",
            "source_sha256": manifest["input"]["source_sha256"],
        },
    )
    return project_dir


def _relative_event_path(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise ValueError("event path must be a relative project path")
    text = os.fspath(value).replace("\\", "/")
    if (
        not text
        or text.startswith("/")
        or re.match(r"^[A-Za-z]:/", text)
        or ".." in text.split("/")
    ):
        raise ValueError("event path must be a relative project path")
    return Path(text).as_posix()


def _sanitize_event_details(details: dict[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in details.items():
        if SENSITIVE_KEY.search(key):
            continue
        kind = EVENT_DETAIL_KINDS.get(key)
        if kind is None:
            raise ValueError(f"unsupported event detail: {key}")
        if kind == "identifier":
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid event identifier: {key}")
            sanitized[key] = value
        elif kind == "path":
            sanitized[key] = _relative_event_path(value)
        elif kind == "sha256":
            if not isinstance(value, str) or not SHA256.fullmatch(value):
                raise ValueError(f"invalid event SHA-256: {key}")
            sanitized[key] = value
        elif kind == "count":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid event count: {key}")
            sanitized[key] = value
        elif kind == "category":
            if not isinstance(value, str) or not CATEGORY.fullmatch(value):
                raise ValueError(f"invalid event category: {key}")
            sanitized[key] = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"invalid event boolean: {key}")
            sanitized[key] = value
    return sanitized


def canonical_event_record(event: str, details: dict[str, object]) -> bytes:
    """Build one sanitized canonical event line without publishing it."""
    if not isinstance(event, str) or not CATEGORY.fullmatch(event):
        raise ValueError("event name must be a sanitized category")
    if not isinstance(details, dict):
        raise ValueError("event details must be an object")
    event_record = {
        "details": _sanitize_event_details(details),
        "event": event,
        "timestamp": _utc_now(),
    }
    return canonical_json_bytes(event_record) + b"\n"


def _event_log_with(project_dir: Path, event: str, details: dict[str, object]) -> bytes:
    """Return the whole event log plus one record, repairing a torn tail.

    ``append_event`` writes without a transaction, so an interrupted append can
    leave a partial final line. Republishing it verbatim would embed the tear
    permanently, so drop an unparsable trailing record instead.
    """
    event_path = contained_project_path(project_dir, "logs/events.jsonl")
    prior = event_path.read_bytes() if event_path.is_file() else b""
    if prior and not prior.endswith(b"\n"):
        head, _, tail = prior.rpartition(b"\n")
        try:
            json.loads(tail)
        except (json.JSONDecodeError, UnicodeDecodeError):
            prior = head + b"\n" if head else b""
        else:
            prior = prior + b"\n"
    return prior + canonical_event_record(event, details)


def append_event(
    project_dir: Path,
    event: str,
    details: dict[str, object],
) -> None:
    """Append one sanitized canonical JSON object to the project event log."""
    event_path = Path(project_dir) / "logs/events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("ab") as handle:
        handle.write(canonical_event_record(event, details))
        handle.flush()
        os.fsync(handle.fileno())


def _allowed_transition(current: str, target: str) -> bool:
    if current not in ALL_STATUSES or target not in ALL_STATUSES:
        return False
    if current in TERMINAL_STATUSES or current == "BLOCKED":
        return False
    if target == "BLOCKED":
        return True
    if current == "EXPORTED" and target == "COMPLETE_WITH_WARNINGS":
        return True
    if current in LINEAR_STATUSES:
        index = LINEAR_STATUSES.index(current)
        return index + 1 < len(LINEAR_STATUSES) and LINEAR_STATUSES[index + 1] == target
    return False


def transition(
    project_dir: Path,
    target: str,
    warning: str | None = None,
) -> dict[str, object]:
    """Move a project by one legal state, publishing the manifest last."""
    if target == "BLOCKED":
        block_warning = warning or "project blocked"
        reason = re.sub(r"[^a-z0-9]+", "-", block_warning.lower()).strip("-")
        return block_project(project_dir, reason or "project-blocked", block_warning)
    project_dir = Path(project_dir)
    manifest_path = project_dir / "project.json"
    # Read, validate and snapshot the event log under the transaction's lock:
    # the log is republished wholesale, so a pre-lock snapshot would silently
    # drop events appended by a concurrent command.
    with ProjectTransaction(project_dir, "transition") as tx:
        manifest = read_json(manifest_path)
        current = manifest.get("status")
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list):
            raise ValueError("manifest warnings must be an array")
        if current == "EXPORTED" and target == "COMPLETE" and (warnings or warning):
            target = "COMPLETE_WITH_WARNINGS"
        if not isinstance(current, str) or not _allowed_transition(current, target):
            raise ValueError(f"invalid Comic Sol transition: {current} -> {target}")
        if target in TERMINAL_STATUSES:
            from validate_project import require_valid_project
            require_valid_project(project_dir, "final")
        if target == "COMPLETE_WITH_WARNINGS" and not (warnings or warning):
            raise ValueError("COMPLETE_WITH_WARNINGS requires an unresolved warning")
        if warning and warning not in warnings:
            warnings.append(warning)
        manifest["status"] = target
        manifest["updated_at"] = _utc_now()
        events = _event_log_with(
            project_dir,
            "project.transitioned",
            {"from": current, "to": target, "warning_present": warning is not None},
        )
        tx.stage_bytes("logs/events.jsonl", events)
        tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    return manifest


def _without_timestamps(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_timestamps(item)
            for key, item in value.items()
            if key not in TIMESTAMP_KEYS
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def stage_cache_key(
    stage: str,
    canonical_inputs: list[object],
    files: list[Path],
    stage_version: str,
) -> str:
    """Hash timestamp-free semantic inputs, direct file hashes, and a stage version."""
    if stage not in RESUME_STAGES:
        raise ValueError(f"unknown resume stage: {stage}")
    if not isinstance(canonical_inputs, list) or not isinstance(files, list):
        raise TypeError("cache inputs and files must be lists")
    if not isinstance(stage_version, str) or not stage_version:
        raise ValueError("stage version must be a non-empty string")
    file_hashes = [sha256_file(Path(path)) for path in files]
    payload = {
        "files": file_hashes,
        "inputs": _without_timestamps(canonical_inputs),
        "stage": stage,
        "stage_version": stage_version,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _storyboard_panels(storyboard: dict[str, object]) -> list[dict[str, object]]:
    panels: list[dict[str, object]] = []
    pages = storyboard.get("pages", [])
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("panels"), list):
                panels.extend(panel for panel in page["panels"] if isinstance(panel, dict))
    return panels


def _project_files(project_dir: Path, relatives: list[str]) -> list[Path]:
    """Resolve required relative files without silently dropping missing inputs."""
    return [
        _contained_project_path(project_dir, Path(relative))
        for relative in relatives
    ]


def _resume_stage_material(
    project_dir: Path,
    stage: str,
    manifest: dict[str, object],
) -> tuple[list[object], list[Path]]:
    if stage == "planning":
        return [read_json(project_dir / "source/request.json")], [project_dir / "source/input.txt"]
    story = read_json(project_dir / "plan/story-plan.json")
    characters = read_json(project_dir / "plan/character-bible.json")
    if stage == "storyboard":
        character_items = characters.get("characters", [])
        identities = [
            {"id": item.get("id")}
            for item in character_items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ] if isinstance(character_items, list) else []
        return [story, identities], []
    storyboard = read_json(project_dir / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    panel_ids = [panel.get("id") for panel in panels if isinstance(panel.get("id"), str)]
    if not panel_ids:
        raise ValueError("storyboard has no panels")
    if stage == "generation":
        visual_panels = []
        for panel in panels:
            visual_panel = dict(panel)
            text_items = panel.get("text", [])
            sfx_items = [
                dict(text_item)
                for text_item in text_items
                if isinstance(text_item, dict) and text_item.get("kind") == "sfx"
            ] if isinstance(text_items, list) else []
            if sfx_items:
                visual_panel["text"] = sfx_items
            else:
                visual_panel.pop("text", None)
            visual_panels.append(visual_panel)
        dependencies: list[dict[str, object]] = []
        actual_reference_paths: list[str] = []
        prompt_paths: list[str] = []
        for panel_id in panel_ids:
            record = read_json(project_dir / f"qa/panels/{panel_id}.json")
            if record.get("panel_id") != panel_id:
                raise ValueError(f"panel QA record does not match {panel_id}")
            source_prompt_path = record.get("source_prompt_path")
            generation = record.get("generation")
            references = generation.get("reference_paths") if isinstance(generation, dict) else None
            if not isinstance(source_prompt_path, str) or not isinstance(references, list) or not all(
                isinstance(reference, str) for reference in references
            ):
                raise ValueError(f"panel QA dependencies are invalid: {panel_id}")
            prompt_paths.append(source_prompt_path)
            actual_reference_paths.extend(references)
            dependencies.append({
                "panel_id": panel_id,
                "reference_paths": references,
                "source_prompt_path": source_prompt_path,
            })
        reference_paths: list[str] = []
        character_items = characters.get("characters", [])
        if isinstance(character_items, list):
            reference_paths = [
                item["reference_path"]
                for item in character_items
                if isinstance(item, dict) and isinstance(item.get("reference_path"), str)
            ]
        return (
            [visual_panels, characters, manifest.get("capability", {}), dependencies],
            _project_files(
                project_dir,
                list(dict.fromkeys(prompt_paths + reference_paths + actual_reference_paths)),
            ),
        )
    if stage == "lettering":
        text = [panel.get("text", []) for panel in panels]
        return text and [text] or [[]], _project_files(
            project_dir, [f"panels/clean/{panel_id}.png" for panel_id in panel_ids]
        )
    if stage == "composition":
        geometry = []
        pages = storyboard.get("pages", [])
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_panels = page.get("panels", [])
                geometry.append({
                    "number": page.get("number"),
                    "layout": page.get("layout"),
                    "panels": [
                        panel.get("rect") for panel in page_panels if isinstance(panel, dict)
                    ] if isinstance(page_panels, list) else [],
                })
        return [geometry], _project_files(
            project_dir, [f"panels/{panel_id}/lettered.png" for panel_id in panel_ids]
        )
    settings = manifest.get("settings", {})
    project_id = manifest.get("project_id")
    page_count = settings.get("page_count", 0) if isinstance(settings, dict) else 0
    page_paths = [f"pages/page-{number:03d}.png" for number in range(1, page_count + 1)] if isinstance(page_count, int) else []
    return (
        [{"project_id": project_id, "settings": settings}],
        _project_files(project_dir, page_paths + ["qa/report.md"]),
    )


def _manifest_artifact_problem(
    project_dir: Path,
    manifest: dict[str, object],
) -> dict[str, str]:
    problems: dict[str, str] = {}
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return {"planning": "manifest artifacts are invalid"}
    for name, descriptor in artifacts.items():
        stage = ARTIFACT_STAGE.get(name)
        if stage is None or not isinstance(descriptor, dict):
            continue
        relative = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            problems.setdefault(stage, f"artifact descriptor is invalid: {name}")
            continue
        try:
            path = contained_project_path(project_dir, relative)
        except (ValueError, OSError):
            problems.setdefault(
                stage, f"artifact path escapes the project: {name}"
            )
            continue
        if not path.is_file():
            problems.setdefault(stage, f"artifact is missing: {relative}")
        elif sha256_file(path) != expected_hash:
            problems.setdefault(stage, f"artifact hash mismatch: {relative}")
    return problems


def _stage_output_files(
    project_dir: Path,
    stage: str,
    manifest: dict[str, object],
) -> list[Path]:
    """Return every required output path one resume stage is accountable for."""
    if stage == "planning":
        return _project_files(project_dir, ["plan/story-plan.json", "plan/character-bible.json"])
    if stage == "storyboard":
        return _project_files(project_dir, ["plan/storyboard.json"])
    storyboard = read_json(project_dir / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    panel_ids = [panel.get("id") for panel in panels if isinstance(panel.get("id"), str)]
    if not panel_ids:
        raise ValueError("storyboard has no panels")
    if stage == "generation":
        relatives = [f"panels/raw/{panel_id}.png" for panel_id in panel_ids]
        relatives += [f"panels/clean/{panel_id}.png" for panel_id in panel_ids]
        return _project_files(project_dir, relatives)
    if stage == "lettering":
        return _project_files(
            project_dir, [f"panels/{panel_id}/lettered.png" for panel_id in panel_ids]
        )
    if stage == "composition":
        page_numbers = []
        for page in storyboard.get("pages", []):
            if isinstance(page, dict) and isinstance(page.get("number"), int):
                page_numbers.append(page["number"])
        return _project_files(
            project_dir, [f"pages/page-{number:03d}.png" for number in page_numbers]
        )
    project_id = manifest.get("project_id")
    relatives = ["qa/report.md"]
    if isinstance(project_id, str) and project_id:
        relatives.append(f"exports/{project_id}.pdf")
    return _project_files(project_dir, relatives)


def _empty_stage_cache() -> dict[str, object]:
    return {"schema_version": "1.0", "stages": {}}


def _stage_cache_structure_problem(cache: dict[str, object]) -> str | None:
    if set(cache) != {"schema_version", "stages"}:
        return "top level must contain exactly schema_version and stages"
    if cache.get("schema_version") != "1.0":
        return "schema_version must equal 1.0"
    stages = cache.get("stages")
    if not isinstance(stages, dict):
        return "stages must be an object"
    if any(stage not in RESUME_STAGES for stage in stages):
        return "stages contains an unknown stage"
    for stage, entry in stages.items():
        if not isinstance(entry, dict) or set(entry) != {"artifacts", "key"}:
            return f"{stage} must contain exactly artifacts and key"
        key = entry.get("key")
        if not isinstance(key, str) or SHA256.fullmatch(key) is None:
            return f"{stage}.key must be a lowercase SHA-256"
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, dict):
            return f"{stage}.artifacts must be an object"
        for relative, expected_hash in artifacts.items():
            try:
                normalized = _relative_event_path(relative)
            except ValueError:
                return f"{stage}.artifacts contains an invalid path"
            if normalized != relative:
                return f"{stage}.artifacts paths must be normalized POSIX paths"
            if not isinstance(expected_hash, str) or SHA256.fullmatch(expected_hash) is None:
                return f"{stage}.artifacts contains an invalid SHA-256"
    return None


def _load_stage_cache(cache_path: Path) -> tuple[dict[str, object], str | None]:
    if not cache_path.is_file():
        return _empty_stage_cache(), None
    try:
        cache = read_json(cache_path)
    except (OSError, UnicodeError, ValueError) as error:
        return _empty_stage_cache(), f"stage cache is invalid: {type(error).__name__}"
    problem = _stage_cache_structure_problem(cache)
    if problem is not None:
        return _empty_stage_cache(), f"stage cache is invalid: {problem}"
    return cache, None


def record_stage(project_dir: Path, stage: str) -> dict[str, object]:
    """Persist one completed stage's cache key and output hashes for resume."""
    if stage not in RESUME_STAGES:
        raise ValueError(f"unknown resume stage: {stage}")
    project_dir = Path(project_dir).resolve()
    with ProjectTransaction(project_dir, "stage-committed") as tx:
        manifest = read_json(project_dir / "project.json")
        versions = manifest.get("stage_versions")
        if not isinstance(versions, dict) or not isinstance(versions.get(stage), str):
            raise ValueError("manifest stage_versions must contain the stage version")
        output_files = _stage_output_files(project_dir, stage, manifest)
        missing_outputs = [
            path.relative_to(project_dir).as_posix()
            for path in output_files
            if not path.is_file()
        ]
        if missing_outputs:
            raise ValueError(f"stage output is missing: {missing_outputs[0]}")
        canonical_inputs, files = _resume_stage_material(project_dir, stage, manifest)
        key = stage_cache_key(stage, canonical_inputs, files, versions[stage])
        artifacts = {
            path.relative_to(project_dir).as_posix(): sha256_file(path)
            for path in output_files
        }
        cache_path = project_dir / STAGE_CACHE_PATH
        cache, _ = _load_stage_cache(cache_path)
        stages = cache["stages"]
        assert isinstance(stages, dict)
        stages[stage] = {"artifacts": artifacts, "key": key}
        tx.stage_bytes(str(STAGE_CACHE_PATH), canonical_artifact_bytes(cache))
        tx.stage_bytes(
            "logs/events.jsonl",
            _event_log_with(project_dir, "stage.recorded", {"action": stage}),
        )
    return {"artifacts": len(artifacts), "stage": stage}


def _accepted_panel_problem(
    project_dir: Path,
    record: dict[str, object],
) -> str | None:
    required_fields = {
        "schema_version", "panel_id", "source_prompt_path", "raw_path", "clean_path",
        "raw_sha256", "dimensions", "attempts", "generation", "checks", "decision",
        "retry_reason", "unresolved_warnings",
    }
    if not required_fields <= set(record) or set(record) - required_fields - {
        "failure_category", "override_reason",
    }:
        return "accepted panel QA record is invalid"
    if record.get("schema_version") != "1.0":
        return "accepted panel QA record is invalid"
    if record.get("failure_category") in {
        "corrupt", "corrupt_image", "safety", "safety_refusal",
    }:
        return "non-overridable panel failure cannot be reused"
    # Import lazily because the standalone validator imports this lifecycle module.
    # Reuse must nevertheless honor the exact public panel-record schema.
    from validate_project import validate_panel_record

    schema_issues = validate_panel_record(record)
    if schema_issues:
        first = schema_issues[0]
        return f"accepted panel QA record is invalid: {first.field}: {first.message}"
    panel_id = record.get("panel_id")
    if not isinstance(panel_id, str) or not IDENTIFIER.fullmatch(panel_id):
        return "accepted panel QA record is invalid"
    attempts = record.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        return "accepted panel QA record is invalid"
    dimensions = record.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != {"width", "height"}:
        return "accepted panel QA record is invalid"
    generation = record.get("generation")
    if not isinstance(generation, dict) or set(generation) != {
        "capability_name", "completed_at", "reference_paths",
    }:
        return "accepted panel QA record is invalid"
    references = generation.get("reference_paths")
    if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
        return "accepted panel QA record is invalid"
    checks = record.get("checks")
    if not isinstance(checks, list) or tuple(
        check.get("id") if isinstance(check, dict) else None for check in checks
    ) != PANEL_CHECK_IDS:
        return "accepted panel QA record is invalid"
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"id", "result", "severity", "evidence"}
            or check.get("result") not in {"pass", "fail", "warning"}
            or check.get("severity") not in {"error", "warning"}
            or not isinstance(check.get("evidence"), str)
            or not check["evidence"].strip()
        ):
            return "accepted panel QA record is invalid"
    if any(
        check.get("result") == "fail" and check.get("severity") == "error"
        for check in checks
    ):
        return "error-level panel failure cannot be reused"
    has_warning = any(
        check.get("result") == "warning"
        or (check.get("result") == "fail" and check.get("severity") == "warning")
        for check in checks
    )
    unresolved = record.get("unresolved_warnings")
    if not isinstance(unresolved, list) or not all(
        isinstance(item, str) and item.strip() for item in unresolved
    ):
        return "accepted panel QA record is invalid"
    if record.get("decision") == "accept" and (has_warning or unresolved):
        return "accepted panel QA record is invalid"
    if record.get("decision") == "accept_with_warnings" and (not has_warning or not unresolved):
        return "accepted panel QA record is invalid"
    raw_path = record.get("raw_path")
    clean_path = record.get("clean_path")
    raw_hash = record.get("raw_sha256")
    if not isinstance(raw_path, str) or not isinstance(clean_path, str):
        return "accepted panel paths are invalid"
    if (
        record.get("source_prompt_path") != f"prompts/panels/{panel_id}.txt"
        or raw_path != f"panels/raw/{panel_id}.png"
        or clean_path != f"panels/clean/{panel_id}.png"
    ):
        return "accepted panel paths do not match the canonical project layout"
    if not isinstance(raw_hash, str) or SHA256.fullmatch(raw_hash) is None:
        return "accepted panel hash is invalid"
    try:
        raw = _contained_project_path(project_dir, Path(raw_path))
        clean = _contained_project_path(project_dir, Path(clean_path))
    except ValueError:
        return "accepted panel path escapes the project directory"
    if not raw.is_file():
        return f"artifact is missing: {raw_path}"
    if sha256_file(raw) != raw_hash:
        return f"artifact hash mismatch: {raw_path}"
    if not clean.is_file():
        return f"artifact is missing: {clean_path}"
    try:
        raw_size = _verify_raster(raw)
        clean_size = _verify_raster(clean)
    except ValueError:
        return "accepted panel image is corrupt"
    recorded_size = (dimensions.get("width"), dimensions.get("height"))
    if raw_size != recorded_size or clean_size != raw_size:
        return "accepted panel dimensions do not match recorded artifacts"
    return None


def build_resume_plan(project_dir: Path) -> list[ResumeAction]:
    """Return a read-only deterministic reuse/repair plan for a generated project."""
    project_dir = Path(project_dir).resolve()
    manifest = read_json(project_dir / "project.json")
    cache_path = project_dir / STAGE_CACHE_PATH
    cache, cache_problem = _load_stage_cache(cache_path)
    cached_stages = cache.get("stages")
    assert isinstance(cached_stages, dict)
    versions = manifest.get("stage_versions")
    if not isinstance(versions, dict):
        raise ValueError("manifest stage_versions must be an object")
    manifest_artifacts = manifest.get("artifacts", {})
    semantic_manifest_paths = {
        descriptor.get("path")
        for name, descriptor in manifest_artifacts.items()
        if name in {"story_plan", "character_bible", "storyboard"}
        and isinstance(descriptor, dict)
        and isinstance(descriptor.get("path"), str)
    } if isinstance(manifest_artifacts, dict) else set()
    problems = _manifest_artifact_problem(project_dir, manifest)
    stale_from: int | None = None
    stale_reason = ""
    actions: list[ResumeAction] = []

    for index, stage in enumerate(RESUME_STAGES):
        cached = cached_stages.get(stage)
        problem = cache_problem if index == 0 and cache_problem is not None else problems.get(stage)
        if not isinstance(cached, dict):
            problem = problem or "stage cache entry is missing"
        else:
            artifacts = cached.get("artifacts")
            if not isinstance(artifacts, dict):
                problem = problem or "cached artifact map is invalid"
            else:
                try:
                    expected_artifacts = {
                        path.relative_to(project_dir).as_posix()
                        for path in _stage_output_files(project_dir, stage, manifest)
                    }
                except (KeyError, OSError, TypeError, ValueError) as error:
                    problem = problem or f"stage outputs are unavailable: {type(error).__name__}"
                else:
                    if set(artifacts) != expected_artifacts:
                        problem = problem or "cached artifact set does not match stage outputs"
                    for relative, expected_hash in artifacts.items():
                        # Canonical semantic edits are accepted through their manifest
                        # descriptor and invalidate consumers via stage input keys. Final
                        # generated outputs still must match the cache that produced them.
                        if relative in semantic_manifest_paths:
                            continue
                        path = _contained_project_path(project_dir, Path(relative))
                        if not path.is_file():
                            problem = problem or f"artifact is missing: {relative}"
                            break
                        if not isinstance(expected_hash, str) or sha256_file(path) != expected_hash:
                            problem = problem or f"artifact hash mismatch: {relative}"
                            break
            if problem is None:
                try:
                    canonical_inputs, files = _resume_stage_material(project_dir, stage, manifest)
                    current_key = stage_cache_key(stage, canonical_inputs, files, versions[stage])
                except (KeyError, OSError, TypeError, ValueError) as error:
                    problem = f"stage inputs are unavailable: {type(error).__name__}"
                else:
                    if cached.get("key") != current_key:
                        problem = "stage cache key changed"
        if problem is not None and stale_from is None:
            stale_from, stale_reason = index, problem
        if stale_from is None:
            actions.append(ResumeAction(stage, "reuse", "stage", "cache key and artifacts match"))
        else:
            action = "regenerate" if stage == "generation" else "rerun"
            reason = stale_reason if index == stale_from else f"depends on stale {RESUME_STAGES[stale_from]} stage"
            actions.append(ResumeAction(stage, action, "stage", reason))

    for temporary in sorted(project_dir.rglob("*.tmp")):
        if temporary.is_file():
            actions.append(ResumeAction(
                "generation",
                "rerun",
                temporary.relative_to(project_dir).as_posix(),
                "interrupted temporary file ignored and preserved",
            ))
    generation_cache_reusable = any(
        action.stage == "generation"
        and action.artifact == "stage"
        and action.action == "reuse"
        for action in actions
    )
    for record_path in sorted((project_dir / "qa/panels").glob("*.json")):
        try:
            record = read_json(record_path)
        except (OSError, UnicodeError, ValueError) as error:
            actions.append(ResumeAction(
                "generation", "regenerate", record_path.stem,
                f"panel QA record is invalid: {type(error).__name__}",
            ))
            continue
        panel_id = record.get("panel_id")
        decision = record.get("decision")
        if not isinstance(panel_id, str):
            continue
        accepted = decision in {"accept", "accept_with_warnings"}
        panel_problem = _accepted_panel_problem(project_dir, record) if accepted else None
        if accepted and panel_problem is None and not generation_cache_reusable:
            panel_problem = "generation stage cache is stale or missing"
        actions.append(ResumeAction(
            "generation",
            "reuse" if accepted and panel_problem is None else "regenerate",
            panel_id,
            (
                "accepted QA artifact is reusable"
                if accepted and panel_problem is None
                else panel_problem or "panel QA requires regeneration"
            ),
        ))
    return actions


def _warning_reason(warning: object) -> str | None:
    if not isinstance(warning, str):
        return None
    return re.sub(r"[^a-z0-9]+", "-", warning.lower()).strip("-")


def block_project(project_dir: Path, reason: str, warning: str) -> dict[str, object]:
    """Record a recoverable block with its last normal state."""
    if not isinstance(reason, str) or CATEGORY.fullmatch(reason) is None:
        raise ValueError("blocked reason must be a stable category")
    if not isinstance(warning, str) or not warning.strip():
        raise ValueError("blocked warning must not be empty")
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = contained_project_path(project_dir, "project.json", must_exist=True)
    with ProjectLock(project_dir):
        manifest = read_json(manifest_path)
        current = manifest.get("status")
        if not isinstance(current, str) or not _allowed_transition(current, "BLOCKED"):
            raise ValueError(f"invalid Comic Sol transition: {current} -> BLOCKED")
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list):
            raise ValueError("manifest warnings must be an array")
        normalized_warning = warning.strip()
        if normalized_warning not in warnings:
            warnings.append(normalized_warning)
        manifest.update({
            "blocked_from": current,
            "blocked_reason": reason,
            "status": "BLOCKED",
            "updated_at": _utc_now(),
        })
        append_event(
            project_dir,
            "project.transitioned",
            {"from": current, "to": "BLOCKED", "warning_present": True},
        )
        atomic_write_json(manifest_path, manifest)
        return manifest


def _resolved_block(manifest: dict[str, object], reason: str) -> bool:
    if reason != "image-capability-unavailable":
        return True
    capability = manifest.get("capability")
    return isinstance(capability, dict) and capability.get("status") == "available"


def _next_resume_action(project_dir: Path, stage: str | None) -> dict[str, str] | None:
    if stage is None:
        return None
    if stage in {"planning", "storyboard", "generation"}:
        return {"agent_required": stage}
    commands = {
        "lettering": "scripts/letter_panels.py",
        "composition": "scripts/compose_pages.py",
        "export": "scripts/export_pdf.py",
    }
    return {"command": f"{sys.executable} {ROOT / commands[stage]} {project_dir}"}


def resume_project(project_dir: Path) -> dict[str, object]:
    """Recover transactions and move a blocked project to its last valid state."""
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = contained_project_path(project_dir, "project.json", must_exist=True)
    ProjectTransaction.recover(project_dir)
    with ProjectLock(project_dir):
        manifest = read_json(manifest_path)
        if manifest.get("status") != "BLOCKED":
            raise ValueError("resume requires a BLOCKED project")
        actions = build_resume_plan(project_dir)
        stage_actions = {
            action.stage: action
            for action in actions
            if action.artifact == "stage"
        }
        preserved: list[str] = []
        stale_stage: str | None = None
        for stage in RESUME_STAGES:
            action = stage_actions.get(stage)
            if stale_stage is None and action is not None and action.action == "reuse":
                preserved.append(stage)
            elif stale_stage is None:
                stale_stage = stage
        invalidated = (
            list(RESUME_STAGES[RESUME_STAGES.index(stale_stage):])
            if stale_stage is not None else []
        )
        blocked_from = manifest.get("blocked_from")
        if blocked_from not in LINEAR_STATUSES:
            blocked_from = STAGE_COMPLETION_STATUS[preserved[-1]] if preserved else "INIT"
            manifest["blocked_from"] = blocked_from
        reason = manifest.get("blocked_reason")
        if not isinstance(reason, str) or CATEGORY.fullmatch(reason) is None:
            reason = "legacy-blocked"
            manifest["blocked_reason"] = reason
        if not _resolved_block(manifest, reason):
            atomic_write_json(manifest_path, manifest)
            return {
                "status": "BLOCKED",
                "preserved": preserved,
                "invalidated": [],
                "next_action": {"required": "image capability available"},
            }
        # Drop outer lock so invalidate_from can acquire its own ProjectTransaction lock
    if stale_stage is not None:
        invalidate_from(project_dir, stale_stage)
    # Use the same rule as invalidate_from so a standalone `invalidate` and a
    # `resume` that invalidates the same stage agree. They differ for the
    # generation stage, whose invalidation preserves the canonical references.
    if stale_stage is not None:
        recovery_status = _post_invalidation_status(
            project_dir, stale_stage, str(blocked_from)
        )
    else:
        recovery_status = STAGE_COMPLETION_STATUS[preserved[-1]] if preserved else "INIT"
    with ProjectLock(project_dir):
        manifest = read_json(manifest_path)
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        manifest["warnings"] = [
            item for item in warnings if _warning_reason(item) != reason
        ]
        manifest["status"] = recovery_status
        manifest["blocked_from"] = None
        manifest["blocked_reason"] = None
        manifest["updated_at"] = _utc_now()
        atomic_write_json(manifest_path, manifest)
        for stage in preserved:
            try:
                outputs = _stage_output_files(project_dir, stage, manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for output in outputs:
                append_event(
                    project_dir,
                    "artifact.reused",
                    {
                        "artifact_path": output.relative_to(project_dir).as_posix(),
                        "reused": True,
                    },
                )
        return {
            "status": recovery_status,
            "preserved": preserved,
            "invalidated": invalidated,
            "next_action": _next_resume_action(project_dir, stale_stage),
        }


def _references_ready(project_dir: Path) -> bool:
    """Report whether every canonical character reference image exists."""
    try:
        bible = read_json(project_dir / "plan/character-bible.json")
    except (OSError, UnicodeError, ValueError):
        return False
    characters = bible.get("characters")
    if not isinstance(characters, list) or not characters:
        return False
    for character in characters:
        if not isinstance(character, dict) or not isinstance(character.get("id"), str):
            return False
        reference = project_dir / f"references/characters/{character['id']}.png"
        if not reference.is_file():
            return False
    return True


def _earlier_status(candidate: str, ceiling: str) -> str:
    """Hold ``candidate`` at ``ceiling``; a non-linear ceiling constrains nothing."""
    if candidate not in LINEAR_STATUSES or ceiling not in LINEAR_STATUSES:
        return candidate
    return min(candidate, ceiling, key=LINEAR_STATUSES.index)


def _post_invalidation_status(project_dir: Path, stage: str, reached: str) -> str:
    """Return the honest status a project holds after invalidating from a stage.

    Invalidating panel generation preserves the canonical references, but only
    when they were actually produced, and rewinding must never advance a
    project past ``reached`` -- the furthest point it actually got.
    """
    status = STAGE_INVALIDATION_STATUS[stage]
    if status == "REFERENCES_READY" and not _references_ready(project_dir):
        status = "STORYBOARDED"
    return _earlier_status(status, reached)


def invalidate_from(project_dir: Path, stage: str) -> list[str]:
    """Forget manifest/cache descriptors from a stage onward without deleting artifacts."""
    if stage not in RESUME_STAGES:
        raise ValueError(f"unknown resume stage: {stage}")
    project_dir = Path(project_dir).resolve()
    manifest_path = project_dir / "project.json"
    start = RESUME_STAGES.index(stage)
    removed: list[str] = []
    with ProjectTransaction(project_dir, "invalidate") as tx:
        manifest = read_json(manifest_path)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValueError("manifest artifacts must be an object")
        for name in ARTIFACT_STAGE:
            if name not in artifacts:
                continue
            owner = ARTIFACT_STAGE.get(name)
            if owner is not None and RESUME_STAGES.index(owner) >= start:
                removed.append(name)
                del artifacts[name]
        manifest["status"] = _post_invalidation_status(
            project_dir, stage, str(manifest.get("status"))
        )
        manifest["updated_at"] = _utc_now()

        cache_path = project_dir / STAGE_CACHE_PATH
        if cache_path.is_file():
            cache, _ = _load_stage_cache(cache_path)
            cached_stages = cache.get("stages")
            if isinstance(cached_stages, dict):
                for downstream in RESUME_STAGES[start:]:
                    cached_stages.pop(downstream, None)
                tx.stage_bytes(
                    str(STAGE_CACHE_PATH), canonical_artifact_bytes(cache)
                )
        tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    return removed


def _contained_project_path(project_dir: Path, path: Path) -> Path:
    project_root = Path(project_dir).resolve(strict=True)
    if path.is_absolute():
        try:
            path = path.relative_to(project_root)
        except ValueError as error:
            raise ValueError("path escapes the project directory") from error
    return contained_project_path(project_root, path)


def _read_generation_counters(project_dir: Path) -> dict[str, object]:
    path = project_dir / GENERATION_COUNTERS_PATH
    if path.is_file():
        return read_json(path)
    return {"global_extra_calls": 0, "panels": {}, "schema_version": "1.0"}


def record_generation_attempt(
    project_dir: Path,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    attempt_path: Path,
) -> dict[str, int]:
    """Account for a retained image call while enforcing both retry budgets."""
    if not IDENTIFIER.fullmatch(panel_id):
        raise ValueError("invalid panel ID")
    if kind not in {"initial", "visual_retry", "transient_repeat"}:
        raise ValueError("unknown generation attempt kind")
    project_dir = Path(project_dir)
    attempt = _contained_project_path(project_dir, Path(attempt_path))
    attempt_relative = attempt.relative_to(project_dir.resolve(strict=True))
    counter_names = {
        "initial": "initial",
        "transient_repeat": "transient_repeats",
        "visual_retry": "visual_retries",
    }
    limits = {"initial": 1, "transient_repeat": 1, "visual_retry": 2}
    limit_messages = {
        "initial": "at most one initial attempt is allowed per panel",
        "transient_repeat": "at most one transient repeat is allowed per panel",
        "visual_retry": "at most two visual retries are allowed per panel",
    }
    with ProjectLock(project_dir):
        attempt = contained_project_path(
            project_dir, attempt_relative, must_exist=True
        )
        if not attempt.is_file():
            raise ValueError("attempt path must be a retained file")
        _verify_raster(attempt)

        counters = _read_generation_counters(project_dir)
        panels = counters.get("panels")
        if not isinstance(panels, dict):
            raise ValueError("generation counter panels must be an object")
        panel = panels.get(panel_id)
        if panel is None:
            panel = {"initial": 0, "transient_repeats": 0, "visual_retries": 0}
            panels[panel_id] = panel
        if not isinstance(panel, dict):
            raise ValueError("panel generation counters must be an object")
        for name in counter_names.values():
            value = panel.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("panel generation counters must be non-negative integers")
        global_extras = counters.get("global_extra_calls", 0)
        if (
            isinstance(global_extras, bool)
            or not isinstance(global_extras, int)
            or global_extras < 0
        ):
            raise ValueError("global generation counter must be a non-negative integer")

        counter_name = counter_names[kind]
        if panel[counter_name] >= limits[kind]:
            raise ValueError(limit_messages[kind])
        if kind != "initial" and global_extras >= 8:
            raise ValueError("at most eight extra calls are allowed per project")

        panel[counter_name] += 1
        if kind != "initial":
            global_extras += 1
            counters["global_extra_calls"] = global_extras
        atomic_write_json(project_dir / GENERATION_COUNTERS_PATH, counters)
        append_event(
            project_dir,
            "generation.attempt-recorded",
            {
                "attempt_path": attempt_relative,
                "kind": kind,
                "panel_id": panel_id,
            },
        )
        return {
            "global_extra_calls": global_extras,
            "initial": panel["initial"],
            "transient_repeats": panel["transient_repeats"],
            "visual_retries": panel["visual_retries"],
        }


def _verify_raster(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("attempt must be a readable raster")
            image.load()
            if image.width < 512 or image.height < 512:
                raise ValueError("attempt must be a readable raster at least 512px")
            return image.width, image.height
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("attempt must be a readable raster") from error


def promote_attempt(project_dir: Path, panel_id: str, attempt_path: Path) -> Path:
    """Verify and atomically copy one retained attempt into the accepted raw slot."""
    if not IDENTIFIER.fullmatch(panel_id):
        raise ValueError("invalid panel ID")
    project_dir = Path(project_dir)
    attempt_relative = Path(attempt_path)
    attempt = _contained_project_path(project_dir, attempt_relative)
    if not attempt.is_file():
        raise ValueError("attempt path must be a retained file")
    if attempt_relative.is_absolute():
        attempt_relative = attempt.relative_to(project_dir.resolve(strict=True))
    with ProjectLock(project_dir):
        attempt = contained_project_path(project_dir, attempt_relative, must_exist=True)
        if not attempt.is_file():
            raise ValueError("attempt path must be a retained file")
        _verify_raster(attempt)
        destination = project_dir / f"panels/raw/{panel_id}.png"
        event_details = {"attempt_path": attempt_relative, "panel_id": panel_id}
        replaced = destination.is_file()
        if destination.is_file():
            old_bytes = destination.read_bytes()
            old_sha = sha256_file(destination)
            new_sha = sha256_file(attempt)
            if old_sha == new_sha:
                return destination
            number = 1
            while True:
                archive = destination.with_name(f"{panel_id}.attempt-{number}.png")
                candidate = archive.resolve()
                if not archive.exists() and candidate != attempt.resolve() and candidate != destination.resolve():
                    durable_atomic_write(archive, old_bytes)
                    break
                number += 1
        attempt = contained_project_path(project_dir, attempt_relative, must_exist=True)
        durable_atomic_write(destination, attempt.read_bytes())
        append_event(project_dir, "generation.attempt-promoted", event_details)
        if replaced:
            append_event(
                project_dir,
                "artifact.regenerated",
                {
                    "artifact_path": destination.relative_to(project_dir).as_posix(),
                    "reused": False,
                },
            )
        return destination


def record_override(project_dir: Path, panel_id: str, reason: str) -> None:
    """Downgrade an overridable visual QA failure to a recorded warning."""
    if not IDENTIFIER.fullmatch(panel_id):
        raise ValueError("invalid panel ID")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("override reason must not be empty")
    project_dir = Path(project_dir)
    record_path = project_dir / f"qa/panels/{panel_id}.json"
    with ProjectTransaction(project_dir, "override") as tx:
        _stage_override(project_dir, panel_id, reason, record_path, tx)


def _stage_override(
    project_dir: Path,
    panel_id: str,
    reason: str,
    record_path: Path,
    tx: ProjectTransaction,
) -> None:
    """Validate and stage one override. Runs under the transaction's lock."""
    record = read_json(record_path)
    if record.get("panel_id") != panel_id:
        raise ValueError("panel QA record does not match the requested panel")
    category = record.get("failure_category")
    if category in {"corrupt", "corrupt_image", "safety", "safety_refusal"}:
        raise ValueError(f"{category} cannot be overridden")
    if category != "visual_qa":
        raise ValueError("only non-safety visual QA errors can be overridden")
    checks = record.get("checks")
    failed_checks = [
        check
        for check in checks
        if isinstance(check, dict)
        and check.get("result") == "fail"
        and check.get("severity") == "error"
    ] if isinstance(checks, list) else []
    if not failed_checks:
        raise ValueError("override requires an error-level failed check")
    if record.get("decision") != "regenerate":
        raise ValueError("overridable visual QA errors must require regeneration")
    raw_path = record.get("raw_path")
    clean_path = record.get("clean_path")
    if (
        raw_path != f"panels/raw/{panel_id}.png"
        or clean_path != f"panels/clean/{panel_id}.png"
    ):
        raise ValueError("corrupt images cannot be overridden")
    try:
        raw = _contained_project_path(project_dir, Path(raw_path))
        clean = _contained_project_path(project_dir, Path(clean_path))
        raw_size = _verify_raster(raw)
        clean_size = _verify_raster(clean)
        dimensions = record.get("dimensions")
        recorded_size = (
            dimensions.get("width"), dimensions.get("height")
        ) if isinstance(dimensions, dict) else None
        if (
            record.get("raw_sha256") != sha256_file(raw)
            or recorded_size != raw_size
            or clean_size != raw_size
        ):
            raise ValueError("recorded panel artifacts do not match")
    except (OSError, ValueError) as error:
        raise ValueError("corrupt images cannot be overridden") from error
    warnings = record.get("unresolved_warnings")
    if not isinstance(warnings, list):
        raise ValueError("panel unresolved_warnings must be an array")
    manifest_path = project_dir / "project.json"
    manifest = read_json(manifest_path)
    manifest_warnings = manifest.get("warnings")
    if not isinstance(manifest_warnings, list):
        raise ValueError("manifest warnings must be an array")
    normalized_reason = reason.strip()
    if normalized_reason not in warnings:
        warnings.append(normalized_reason)
    for check in failed_checks:
        check["severity"] = "warning"
    record["decision"] = "accept_with_warnings"
    record["retry_reason"] = None
    record["override_reason"] = normalized_reason

    if normalized_reason not in manifest_warnings:
        manifest_warnings.append(normalized_reason)
        manifest["updated_at"] = _utc_now()

    tx.stage_bytes(f"qa/panels/{panel_id}.json", canonical_artifact_bytes(record))
    tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    tx.stage_bytes(
        "logs/events.jsonl",
        _event_log_with(project_dir, "panel.overridden", {"panel_id": panel_id, "action": "accepted"}),
    )


def doctor(output_root: Path) -> tuple[bool, list[str]]:
    """Check the deterministic local runtime without probing agent tools."""
    healthy = True
    messages: list[str] = []

    if sys.version_info[:2] == (3, 11):
        messages.append(f"PASS Python 3.11 ({sys.version.split()[0]})")
    else:
        healthy = False
        messages.append(f"FAIL Python 3.11 required; found {sys.version.split()[0]}")

    try:
        import PIL

        if PIL.__version__ == REQUIRED_PILLOW:
            messages.append(f"PASS Pillow {REQUIRED_PILLOW}")
        else:
            healthy = False
            messages.append(
                f"FAIL Pillow {REQUIRED_PILLOW} required; found {PIL.__version__}"
            )
    except Exception as error:
        healthy = False
        messages.append(f"FAIL Pillow check: {type(error).__name__}: {error}")

    font_checks = (
        ("Comic Neue Regular", FONT_PATH_COMIC_REGULAR),
        ("Comic Neue Bold", FONT_PATH_COMIC_BOLD),
        ("Noto Sans fallback", FONT_PATH_FALLBACK),
    )
    for label, path in font_checks:
        try:
            ImageFont.truetype(str(path), 42)
            messages.append(f"PASS font {label} loads at 42px")
        except Exception as error:
            healthy = False
            messages.append(f"FAIL font {label} at 42px: {type(error).__name__}: {error}")

    template_names = (
        "manifest.json",
        "character-bible.json",
        "story-plan.json",
        "storyboard.json",
        "panel-record.json",
        "qa-report.md.tmpl",
    )
    missing_templates = [name for name in template_names if not (TEMPLATES / name).is_file()]
    if missing_templates:
        healthy = False
        messages.append(f"FAIL templates missing: {', '.join(missing_templates)}")
    else:
        messages.append("PASS templates available")

    output_root = Path(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_root, prefix=".doctor-", delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        messages.append(f"PASS output root writable: {output_root.resolve()}")
    except OSError as error:
        healthy = False
        messages.append(f"FAIL output root not writable: {type(error).__name__}: {error}")

    messages.append("INFO image capability: inspect in agent session")
    return healthy, messages


def finalize_project(project_dir: Path) -> dict[str, object]:
    """Run all deterministic finalization steps and transition to terminal status.

    Order: lettering → composition → page-QA gate → guarded export →
    report → descriptor recording → export stage → terminal transition.
    Page-QA records are agent-produced; this function fails closed if they
    are absent or stale rather than fabricating visual evidence.
    """
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = project_dir / "project.json"

    # 1. Determine stale stages from the resume plan.
    plan = build_resume_plan(project_dir)
    stale = {
        a.stage for a in plan
        if a.artifact == "stage" and a.action in {"run", "rerun"}
    }

    # 2. Lettering (if stale), advance status.
    manifest = read_json(manifest_path)
    need_lettering = "lettering" in stale or not all(
        (project_dir / f"panels/{pid}/lettered.png").is_file()
        for pid in (manifest.get("panels") if isinstance(manifest.get("panels"), list) else [])
    )
    if need_lettering:
        from letter_panels import letter_project
        letter_project(project_dir)
        record_stage(project_dir, "lettering")
    manifest = read_json(manifest_path)
    if _allowed_transition(str(manifest.get("status")), "LETTERED"):
        transition(project_dir, "LETTERED")

    # 3. Composition (if stale), advance status. compose_project writes
    #    cache/composition.json and its manifest descriptor.
    need_composition = "composition" in stale or not (project_dir / "cache/composition.json").is_file()
    if need_composition:
        from compose_pages import compose_project
        compose_project(project_dir)
        record_stage(project_dir, "composition")
    manifest = read_json(manifest_path)
    if _allowed_transition(str(manifest.get("status")), "COMPOSED"):
        transition(project_dir, "COMPOSED")

    # 4. Fail closed on agent-produced page-QA integrity records.
    manifest = read_json(manifest_path)
    settings = manifest.get("settings")
    page_count = settings.get("page_count", 0) if isinstance(settings, dict) else 0
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("page_qa_required: settings.page_count must be a positive integer")
    for page_number in range(1, page_count + 1):
        qa_rel = f"qa/pages/page-{page_number:03d}.json"
        qa_path = project_dir / qa_rel
        if not qa_path.is_file():
            raise ValueError(
                f"page_qa_required: {qa_rel} is missing. Inspect the composed "
                f"page and write the record from templates/page-qa.json."
            )
        record = read_json(qa_path)
        page_rel = f"pages/page-{page_number:03d}.png"
        page_path = project_dir / page_rel
        if not page_path.is_file():
            raise ValueError(f"page_qa_required: {page_rel} is missing")
        if record.get("page_sha256") != sha256_file(page_path):
            raise ValueError(f"page_qa_required: {qa_rel} hash is stale")

    # 5. Guarded export (validates export-ready, writes PDF, records descriptor).
    from export_pdf import guarded_export
    guarded_export(project_dir)
    manifest = read_json(manifest_path)
    if _allowed_transition(str(manifest.get("status")), "EXPORTED"):
        transition(project_dir, "EXPORTED")

    # 6. Render the QA report, which projects the terminal status and records
    #    its own descriptor. Final validation requires both before the terminal
    #    transition, so the report cannot honestly be rendered afterwards.
    manifest = read_json(manifest_path)
    warnings = manifest.get("warnings")
    final_status = (
        "COMPLETE_WITH_WARNINGS"
        if isinstance(warnings, list) and warnings
        else "COMPLETE"
    )
    from render_report import render_report
    render_report(project_dir)

    # 7. render_report and compose_project record their own descriptors.

    # 8. Record export stage cache.
    record_stage(project_dir, "export")

    # 9. Confirm the warning state still matches what the report projected.
    manifest = read_json(manifest_path)
    warnings = manifest.get("warnings")
    has_warnings = isinstance(warnings, list) and len(warnings) > 0
    actual_status = "COMPLETE_WITH_WARNINGS" if has_warnings else "COMPLETE"
    if actual_status != final_status:
        raise ValueError(
            "warning state changed after the QA report was rendered; "
            f"report projects {final_status} but the project is {actual_status}"
        )

    # 10. Guarded terminal transition (runs final validation internally).
    if str(manifest.get("status")) not in TERMINAL_STATUSES:
        transition(project_dir, final_status)

    return {
        "status": final_status,
        "pdf": f"exports/{manifest.get('project_id', 'unknown')}.pdf",
        "report": "qa/report.md",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comic_sol.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--output-root", required=True, type=Path)
    init_parser.add_argument("--title", required=True)
    init_parser.add_argument("--source", required=True, type=Path)
    init_parser.add_argument("--request-json", required=True, type=Path)

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("project_dir", type=Path)
    transition_parser.add_argument("target")
    transition_parser.add_argument("--warning")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("project_dir", type=Path)
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument(
        "--output-root", type=Path, default=Path("comic-sol-output")
    )

    resume_parser = subparsers.add_parser("resume-plan")
    resume_parser.add_argument("project_dir", type=Path)
    resume_parser.add_argument("--json", action="store_true", dest="as_json")

    resume_execute_parser = subparsers.add_parser("resume")
    resume_execute_parser.add_argument("project_dir", type=Path)
    resume_execute_parser.add_argument("--json", action="store_true", dest="as_json")

    invalidate_parser = subparsers.add_parser("invalidate")
    invalidate_parser.add_argument("project_dir", type=Path)
    invalidate_parser.add_argument("stage", choices=RESUME_STAGES)

    record_stage_parser = subparsers.add_parser("record-stage")
    record_stage_parser.add_argument("project_dir", type=Path)
    record_stage_parser.add_argument("stage", choices=RESUME_STAGES)

    attempt_parser = subparsers.add_parser("record-attempt")
    attempt_parser.add_argument("project_dir", type=Path)
    attempt_parser.add_argument("panel_id")
    attempt_parser.add_argument("kind", choices=("initial", "visual_retry", "transient_repeat"))
    attempt_parser.add_argument("path", type=Path)

    promote_parser = subparsers.add_parser("promote-attempt")
    promote_parser.add_argument("project_dir", type=Path)
    promote_parser.add_argument("panel_id")
    promote_parser.add_argument("path", type=Path)

    override_parser = subparsers.add_parser("override-panel")
    override_parser.add_argument("project_dir", type=Path)
    override_parser.add_argument("panel_id")
    override_parser.add_argument("--reason", required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("project_dir", type=Path)
    finalize_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic Comic Sol lifecycle CLI."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            request = read_json(arguments.request_json)
            source = arguments.source.read_bytes()
            validate_source_bytes(source, arguments.source.suffix)
            project = init_project(
                arguments.output_root,
                arguments.title,
                source,
                request,
            )
            print(project.resolve())
        elif arguments.command == "transition":
            manifest = transition(
                arguments.project_dir, arguments.target, arguments.warning
            )
            print(f"{manifest['project_id']}: {manifest['status']}")
        elif arguments.command == "status":
            manifest = read_json(arguments.project_dir / "project.json")
            if arguments.as_json:
                print(
                    json.dumps(
                        manifest, ensure_ascii=False, indent=2, sort_keys=True
                    )
                )
            else:
                print(f"{manifest['project_id']}: {manifest['status']}")
        elif arguments.command == "doctor":
            healthy, messages = doctor(arguments.output_root)
            print("\n".join(messages))
            return 0 if healthy else 1
        elif arguments.command == "resume-plan":
            actions = build_resume_plan(arguments.project_dir)
            if arguments.as_json:
                print(json.dumps([asdict(action) for action in actions], ensure_ascii=False, indent=2, sort_keys=True))
            else:
                for action in actions:
                    print(f"{action.stage}: {action.action} {action.artifact} — {action.reason}")
        elif arguments.command == "resume":
            result = resume_project(arguments.project_dir)
            if arguments.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"status: {result['status']}")
                next_action = result["next_action"]
                if isinstance(next_action, dict) and "agent_required" in next_action:
                    print(f"agent required: {next_action['agent_required']}")
                elif isinstance(next_action, dict) and "command" in next_action:
                    print(f"next command: {next_action['command']}")
                elif isinstance(next_action, dict) and "required" in next_action:
                    print(f"required: {next_action['required']}")
        elif arguments.command == "invalidate":
            # Invalidating a BLOCKED project would leave blocked_from and
            # blocked_reason set, which every later validation rejects.
            if read_json(Path(arguments.project_dir) / "project.json").get(
                "status"
            ) == "BLOCKED":
                raise ValueError(
                    "project is BLOCKED; run `comic_sol.py resume PROJECT_DIR` "
                    "to clear the block before invalidating a stage"
                )
            removed = invalidate_from(arguments.project_dir, arguments.stage)
            print("\n".join(removed) if removed else "no manifest artifacts removed")
        elif arguments.command == "record-stage":
            recorded = record_stage(arguments.project_dir, arguments.stage)
            print(f"{recorded['stage']}: recorded {recorded['artifacts']} artifact(s)")
        elif arguments.command == "record-attempt":
            counts = record_generation_attempt(
                arguments.project_dir, arguments.panel_id, arguments.kind, arguments.path
            )
            print(json.dumps(counts, sort_keys=True))
        elif arguments.command == "promote-attempt":
            print(promote_attempt(arguments.project_dir, arguments.panel_id, arguments.path))
        elif arguments.command == "override-panel":
            record_override(arguments.project_dir, arguments.panel_id, arguments.reason)
            print(f"{arguments.panel_id}: accepted with warnings")
        elif arguments.command == "finalize":
            result = finalize_project(arguments.project_dir)
            if arguments.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"{result['status']}: {result['pdf']} | {result['report']}")
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
