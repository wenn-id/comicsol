#!/usr/bin/env python3
"""Deterministic project lifecycle commands for Comic Sol."""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import importlib.util
import io
import json
import math
import os
import re
import shlex
import sys
import tempfile
import unicodedata
import warnings
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from PIL import Image, ImageFont

from .project_io import (
    PROJECT_OPERATION_LOCK_TIMEOUT,
    ProjectLock,
    ProjectTransaction,
    cleanup_owned_directory,
    contained_project_path,
    durable_atomic_write,
    fsync_directory_tree,
    publish_directory_noreplace,
    read_bytes_nofollow,
    read_contained_bytes,
    read_json_nofollow,
    sha256_file,
    validate_source_bytes,
)
from .input_limits import (
    MAX_JSON_BYTES,
    MAX_OVERRIDE_REASON_CHARS,
    MAX_TITLE_CHARS,
    MAX_WARNING_CHARS,
    OVERRIDE_REASON_LIMIT_MESSAGE,
    REQUEST_TITLE_LIMIT_MESSAGE,
    TITLE_LIMIT_MESSAGE,
    WARNING_LIMIT_MESSAGE,
    InputResourceLimitError,
    loads_bounded_json,
    validate_narrative,
)
from .raster_limits import (
    MAX_DECODED_PIXELS,
    MAX_ENCODED_RASTER_BYTES,
    MIN_RASTER_DIMENSION,
)
from .repair_strategy import REPAIR_STRATEGIES, recorded_panel_plan
from .schema import (
    CURRENT_PROJECT_SCHEMA_VERSION,
    LEGACY_PROJECT_SCHEMA_VERSION,
    migrate_project_manifest_in_memory,
    read_project_manifest,
)
from .sfx_verification import (
    is_generated_sfx,
    normalized_text_material,
    sfx_material,
)
from .stage_registry import (
    ARTIFACT_STAGE,
    RESUME_STAGES,
    STAGE_COMPLETION_STATUS,
    STAGE_INVALIDATION_STATUS,
    get_stage,
)
from .starter_templates import (
    STARTER_IDS,
    StarterProject,
    inventory_starters,
    load_starter,
)
from .core_primitives import (
    PANEL_ID_PATTERN,
    canonical_artifact_bytes,
    canonical_json_bytes,
    rectangles_overlap as _rectangles_overlap,
)
from .character_identity import IDENTITY_PACK_PATH, validate_identity_pack
from .handoff import (
    BATCHES_PATH,
    HANDOFF_CONTRACT_VERSION,
    HANDOFF_MANIFEST_PATH,
    LOCKED_SCOPE_FIXED_PATHS,
    HandoffContractError,
    HandoffResultError,
    StaleLockedScopeError,
    assert_current_locked_scope,
    attempt_id as generation_attempt_id,
    build_generation_batches,
    build_generation_job,
    build_generation_receipt,
    build_handoff_manifest,
    generation_job_sha256,
    locked_scope_sha256_from_content,
    reconcile_job_receipts,
    validate_generation_batches,
    validate_generation_job,
    validate_generation_receipt,
    validate_handoff_manifest,
)
from .reference_strategy import project_reference_plan, reference_plan_bytes

# Public compatibility exports: sibling engines and downstream callers import
# layout geometry from this facade.
from .layouts import MARGIN, PAGE_HEIGHT, PAGE_WIDTH, layout_rects  # noqa: F401
from .lifecycle_contracts import (
    ALL_STATUSES,  # noqa: F401  (public facade export; mcp_server imports it here)
    CATEGORY,
    IDENTIFIER,
    LINEAR_STATUSES,
    TERMINAL_STATUSES,
    allowed_transition as _allowed_transition_impl,
)

rectangles_overlap = _rectangles_overlap
_allowed_transition = _allowed_transition_impl


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
FONT_PATH_COMIC_REGULAR = ROOT / "assets/fonts/ComicNeue-Regular.ttf"
FONT_PATH_COMIC_BOLD = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
FONT_PATH_FALLBACK = ROOT / "assets/fonts/NotoSans-Regular.ttf"
FONT_PATH = FONT_PATH_COMIC_REGULAR
GUTTER = 32

TIMESTAMP_KEYS = {"created_at", "updated_at", "detected_at", "completed_at", "timestamp"}
STAGE_CACHE_PATH = Path("logs/stage-cache.json")
GENERATION_COUNTERS_PATH = Path("logs/generation-counters.json")
GENERATION_COUNTER_NAMES = {
    "initial": "initial",
    "transient_repeat": "transient_repeats",
    "visual_retry": "visual_retries",
}
GENERATION_LIMITS = {"initial": 1, "transient_repeat": 1, "visual_retry": 2}
GENERATION_LIMIT_MESSAGES = {
    "initial": "at most one initial attempt is allowed per panel",
    "transient_repeat": "at most one transient repeat is allowed per panel",
    "visual_retry": "at most two visual retries are allowed per panel",
}
# Both quality schemas spell an accepted panel, and either spelling protects the
# accepted raster from being replaced before a fresh review asks for a repair.
ACCEPTED_DECISIONS = frozenset({"accept", "accept-warning", "accept_with_warnings"})
# A record whose decision is none of the known spellings is not a review anyone
# can act on, so it withholds permission rather than granting it by default.
REPAIR_DECISIONS = frozenset({"regenerate"})
ProgressCallback = Callable[[dict[str, object]], None]


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
REQUEST_SETTING_ALLOWLIST = frozenset({"mode", "language", "title"})
REQUEST_MODES = frozenset({"short_prompt", "pasted_story", "source_file", "resume"})
REQUIRED_PILLOW = "12.3.0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EVENT_DETAIL_KINDS = {
    "project_id": "identifier",
    "panel_id": "identifier",
    "scene_id": "identifier",
    "job_id": "sha256",
    "attempt_id": "identifier",
    "text_id": "identifier",
    "source_path": "path",
    "artifact_path": "path",
    "attempt_path": "path",
    "raster_path": "path",
    "activated_path": "path",
    "source_sha256": "sha256",
    "artifact_sha256": "sha256",
    "raster_sha256": "sha256",
    "activated_sha256": "sha256",
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
    "strategy": "category",
    "warning_present": "boolean",
    "reused": "boolean",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, object]:
    """Read a bounded UTF-8 JSON object without following path symlinks."""
    value = read_json_nofollow(Path(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return cast(dict[str, object], value)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Compatibility wrapper for durable artifact publication."""
    durable_atomic_write(path, payload)


def atomic_write_json(path: Path, value: object) -> None:
    """Atomically write canonical human-readable artifact JSON."""
    atomic_write_bytes(path, canonical_artifact_bytes(value))


def slugify(title: str) -> str:
    """Convert a title to a portable version-1.0 project ID."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"comic-sol-{slug}" if slug else "comic-sol-project"
    return slug[:48].rstrip("-")


def _project_directory_candidate(output_root: Path, base_slug: str, suffix: int) -> Path:
    suffix_text = "" if suffix == 1 else f"-{suffix}"
    candidate_slug = f"{base_slug[: 48 - len(suffix_text)].rstrip('-')}{suffix_text}"
    return output_root / candidate_slug


def _path_entry_exists(path: Path) -> bool:
    """Return whether any entry, including a broken symlink, occupies a path."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _create_project_tree(project_dir: Path) -> None:
    """Create the complete empty project directory skeleton."""
    for relative in PROJECT_DIRECTORIES:
        (project_dir / relative).mkdir(parents=True, exist_ok=False)


def _staging_directory_identity(staging: Path) -> tuple[int, int]:
    """Capture the filesystem identity used to authorize staging cleanup."""
    metadata = staging.lstat()
    return metadata.st_dev, metadata.st_ino


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


def validate_request_settings(request: dict[str, object]) -> dict[str, object]:
    """Validate the small, non-secret request record persisted in each project."""
    if not isinstance(request, dict):
        raise TypeError("request must be a JSON object")

    def reject_sensitive_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if SENSITIVE_KEY.search(str(key)):
                    raise ValueError(f"sensitive request setting is not allowed: {key}")
                reject_sensitive_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_sensitive_keys(nested)

    reject_sensitive_keys(request)
    if any(not isinstance(key, str) for key in request):
        raise ValueError("request setting keys must be strings")
    unknown = sorted(set(request) - REQUEST_SETTING_ALLOWLIST)
    if unknown:
        raise ValueError(f"unsupported request setting: {unknown[0]}")
    mode = request.get("mode", "short_prompt")
    language = request.get("language", "en")
    title = request.get("title")
    if not isinstance(mode, str) or mode not in REQUEST_MODES:
        raise ValueError(
            "request mode must be one of short_prompt, pasted_story, source_file, or resume"
        )
    if not isinstance(language, str) or not language.strip() or len(language) > 35:
        raise ValueError("request language must be a non-empty language tag")
    if title is not None:
        validate_narrative(title, message=REQUEST_TITLE_LIMIT_MESSAGE, max_chars=MAX_TITLE_CHARS)
    return {key: request[key] for key in sorted(request)}


def _manifest_from_template(
    project_id: str,
    title: str,
    source: bytes,
    request: dict[str, object],
    *,
    image_capability: object | None = None,
    page_count: int = 2,
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
    if image_capability is not None:
        try:
            _, _, _, details, _ = _image_capability_diagnostic(image_capability)
            capability = details.get("capability")
            if isinstance(capability, dict):
                manifest_capability = manifest.get("capability")
                if isinstance(manifest_capability, dict):
                    manifest_capability["status"] = capability.get("status", "not_checked")
                    manifest_capability["name"] = capability.get("name")
                    manifest_capability["supports_reference_images"] = capability.get(
                        "supports_reference_images", False
                    )
                    manifest_capability["supports_dimensions"] = capability.get(
                        "supports_dimensions", False
                    )
                    manifest_capability["detected_at"] = timestamp
        except Exception:
            # Capability detection failures should not block project initialization
            pass
    settings = manifest["settings"]
    if not isinstance(settings, dict):
        raise ValueError("manifest template settings must be an object")
    settings["page_count"] = page_count
    return manifest


def _write_starter_artifacts(staging: Path, starter: StarterProject) -> None:
    """Materialize standard starter artifacts inside the unpublished init tree."""
    atomic_write_json(staging / "plan/story-plan.json", starter.story_plan)
    atomic_write_json(staging / "plan/character-bible.json", starter.character_bible)
    atomic_write_json(staging / "plan/storyboard.json", starter.storyboard)


def _apply_starter_manifest(
    manifest: dict[str, object],
    staging: Path,
    starter: StarterProject,
) -> None:
    """Record normal plan/storyboard descriptors without changing project schema."""
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("manifest template settings must be an object")
    settings["page_count"] = starter.page_count
    settings["panel_count"] = len(starter.panel_ids)
    manifest["panels"] = list(starter.panel_ids)
    manifest["artifacts"] = {
        name: {"path": relative, "sha256": sha256_file(staging / relative)}
        for name, relative in (
            ("story_plan", "plan/story-plan.json"),
            ("character_bible", "plan/character-bible.json"),
            ("storyboard", "plan/storyboard.json"),
        )
    }


def init_project(
    output_root: Path,
    title: str,
    source: bytes | None = None,
    request: dict[str, object] | None = None,
    *,
    image_capability: object | None = None,
    page_count: int | None = None,
    starter: str | None = None,
) -> Path:
    """Stage a complete blank or starter project, then publish it exclusively."""
    selected_starter: StarterProject | None = None
    if starter is not None:
        if source is not None or request is not None or page_count is not None:
            raise ValueError(
                "starter cannot be combined with explicit source, request, or page count"
            )
        selected_starter = load_starter(
            TEMPLATES,
            starter,
            request_validator=validate_request_settings,
        )
        source = selected_starter.source
        request = selected_starter.request
        page_count = selected_starter.page_count
    else:
        if source is None:
            raise TypeError("source must be bytes")
        if request is None:
            raise TypeError("request must be a JSON object")
        if page_count is None:
            page_count = 2

    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    if not isinstance(request, dict):
        raise TypeError("request must be a JSON object")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or not 1 <= page_count <= 4:
        raise ValueError("page count must be an integer from 1 to 4")
    validate_narrative(title, message=TITLE_LIMIT_MESSAGE, max_chars=MAX_TITLE_CHARS)
    validate_source_bytes(source)
    validated_request = validate_request_settings(request)

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    base_slug = slugify(title)
    suffix = 1
    while True:
        project_dir = _project_directory_candidate(output_root, base_slug, suffix)
        if _path_entry_exists(project_dir):
            suffix += 1
            continue

        staging = Path(
            tempfile.mkdtemp(
                dir=output_root,
                prefix=".comic-sol-init-",
                suffix=".tmp",
            )
        )
        staging_identity: tuple[int, int] | None = None
        try:
            staging_identity = _staging_directory_identity(staging)
            _create_project_tree(staging)
            atomic_write_bytes(staging / "source/input.txt", source)
            atomic_write_json(staging / "source/request.json", validated_request)
            if selected_starter is not None:
                _write_starter_artifacts(staging, selected_starter)
            manifest = _manifest_from_template(
                project_dir.name,
                title.strip(),
                source,
                validated_request,
                image_capability=image_capability,
                page_count=page_count,
            )
            if selected_starter is not None:
                _apply_starter_manifest(manifest, staging, selected_starter)
            atomic_write_json(staging / "project.json", manifest)
            append_event(
                staging,
                "project.created",
                {
                    "project_id": project_dir.name,
                    "source_path": "source/input.txt",
                    "source_sha256": manifest["input"]["source_sha256"],
                },
            )
            if selected_starter is not None:
                from .validate_project import require_valid_project

                require_valid_project(staging, "storyboard")
                record_stage(staging, "planning")
                transition(staging, "PLANNED")
                transition(staging, "SCRIPTED")
                record_stage(staging, "storyboard")
                transition(staging, "STORYBOARDED")
            fsync_directory_tree(staging)
            try:
                publish_directory_noreplace(
                    staging,
                    project_dir,
                    expected_identity=staging_identity,
                )
            except FileExistsError:
                suffix += 1
                continue
            return project_dir
        finally:
            if staging_identity is not None:
                try:
                    cleanup_owned_directory(staging, staging_identity)
                except OSError:
                    # Swallow cleanup failures to avoid masking exceptions from try block
                    pass


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


def _refresh_handoff_manifest_stage(
    project_dir: Path,
    manifest: dict[str, object],
    target_status: str,
    tx: "ProjectTransaction",
) -> None:
    """Refresh ``handoff/manifest.json`` so its ``stage`` tracks ``project.json``.

    The handoff binding is the authoritative bridge between project status and
    handoff state. A normal ``transition()`` rewrites ``project.json`` and would
    otherwise leave the manifest with the previous stage, which makes
    ``validate_project`` report a stage mismatch and breaks reprepare. Run this
    inside the same transaction that publishes ``project.json`` so the bridge
    stays consistent or rolls back together.
    """
    binding = manifest.get("handoff")
    if not isinstance(binding, dict):
        return
    manifest_path = binding.get("manifest_path")
    locked_scope = binding.get("locked_scope_sha256")
    if not isinstance(manifest_path, str) or manifest_path != HANDOFF_MANIFEST_PATH:
        return
    if not isinstance(locked_scope, str):
        return
    handoff_bytes = None
    try:
        handoff_bytes = read_contained_bytes(project_dir, manifest_path, max_bytes=MAX_JSON_BYTES)
    except FileNotFoundError:
        return
    try:
        handoff_manifest = loads_bounded_json(handoff_bytes, source=manifest_path)
    except (ValueError, TypeError):
        return
    if not isinstance(handoff_manifest, dict):
        return
    if handoff_manifest.get("stage") == target_status:
        return
    handoff_manifest["stage"] = target_status
    issues = validate_handoff_manifest(handoff_manifest)
    if issues:
        raise HandoffContractError(issues)
    tx.stage_bytes(manifest_path, canonical_artifact_bytes(handoff_manifest))


def append_event(
    project_dir: Path,
    event: str,
    details: dict[str, object],
) -> None:
    """Append one sanitized canonical JSON object transactionally."""
    with ProjectTransaction(Path(project_dir), "event-appended") as transaction:
        transaction.append_bytes(
            "logs/events.jsonl",
            canonical_event_record(event, details),
            repair_torn_jsonl=True,
        )


def transition(
    project_dir: Path,
    target: str,
    warning: str | None = None,
) -> dict[str, object]:
    """Move a project by one legal state, publishing the manifest last."""
    if warning is not None:
        validate_narrative(warning, message=WARNING_LIMIT_MESSAGE, max_chars=MAX_WARNING_CHARS)
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
        manifest = read_project_manifest(manifest_path, normalize_legacy=False)
        current = manifest.get("status")
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list):
            raise ValueError("manifest warnings must be an array")
        if current == "EXPORTED" and target == "COMPLETE" and (warnings or warning):
            target = "COMPLETE_WITH_WARNINGS"
        if not isinstance(current, str) or not _allowed_transition(current, target):
            raise ValueError(f"invalid Comic Sol transition: {current} -> {target}")
        if target in TERMINAL_STATUSES:
            from .validate_project import require_valid_project

            require_valid_project(project_dir, "final")
        if target == "COMPLETE_WITH_WARNINGS" and not (warnings or warning):
            raise ValueError("COMPLETE_WITH_WARNINGS requires an unresolved warning")
        if warning and warning not in warnings:
            warnings.append(warning)
        manifest["status"] = target
        manifest["updated_at"] = _utc_now()
        tx.append_bytes(
            "logs/events.jsonl",
            canonical_event_record(
                "project.transitioned",
                {"from": current, "to": target, "warning_present": warning is not None},
            ),
            repair_torn_jsonl=True,
        )
        _refresh_handoff_manifest_stage(project_dir, manifest, target, tx)
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
    return [_contained_project_path(project_dir, Path(relative)) for relative in relatives]


def _panel_clean_relative_path(project_dir: Path, panel_id: object) -> str:
    """Return the clean-artifact path for either supported panel schema."""
    if not isinstance(panel_id, str):
        raise ValueError("panel ID must be a string")
    record = read_json(project_dir / f"qa/panels/{panel_id}.json")
    if record.get("schema_version") == "2.0":
        bindings = record.get("bindings")
        clean_path = bindings.get("clean_path") if isinstance(bindings, dict) else None
        expected = f"panels/{panel_id}/clean.png"
        if not isinstance(clean_path, str) or clean_path != expected:
            raise ValueError(f"panel QA clean path is not canonical: {panel_id}")
        return clean_path
    return f"panels/clean/{panel_id}.png"


def _resume_stage_material(
    project_dir: Path,
    stage: str,
    manifest: dict[str, object],
) -> tuple[list[object], list[Path]]:
    material_kind = get_stage(stage).material_kind
    if material_kind == "planning":
        return [read_json(project_dir / "source/request.json")], [project_dir / "source/input.txt"]
    story = read_json(project_dir / "plan/story-plan.json")
    characters = read_json(project_dir / "plan/character-bible.json")
    if material_kind == "storyboard":
        character_items = characters.get("characters", [])
        identities = (
            [
                {"id": item.get("id")}
                for item in character_items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            if isinstance(character_items, list)
            else []
        )
        return [story, identities], []
    storyboard = read_json(project_dir / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    panel_ids = [panel.get("id") for panel in panels if isinstance(panel.get("id"), str)]
    if not panel_ids:
        raise ValueError("storyboard has no panels")
    if material_kind == "generation":
        visual_panels = []
        for panel in panels:
            visual_panel = dict(panel)
            text_items = panel.get("text", [])
            # Only SFX the image model is asked to draw is generation material.
            # Routing an effect to deterministic lettering therefore invalidates
            # generation for that panel — the artwork must come back without the
            # baked effect — while leaving planning and the storyboard untouched.
            generated_sfx = (
                [sfx_material(text_item) for text_item in text_items if is_generated_sfx(text_item)]
                if isinstance(text_items, list)
                else []
            )
            if generated_sfx:
                visual_panel["text"] = generated_sfx
            else:
                visual_panel.pop("text", None)
            visual_panels.append(visual_panel)
        dependencies: list[dict[str, object]] = []
        actual_reference_paths: list[str] = []
        prompt_paths: list[str] = []
        for panel_id in panel_ids:
            record = read_json(project_dir / f"qa/panels/{panel_id}.json")
            if record.get("schema_version") == "2.0":
                record_panel_id = record.get("subject_id")
                source_prompt_path = f"prompts/panels/{panel_id}.txt"
                references = []
            else:
                record_panel_id = record.get("panel_id")
                source_prompt_path = record.get("source_prompt_path")
                generation = record.get("generation")
                references = (
                    generation.get("reference_paths") if isinstance(generation, dict) else None
                )
            if record_panel_id != panel_id:
                raise ValueError(f"panel QA record does not match {panel_id}")
            if (
                not isinstance(source_prompt_path, str)
                or not isinstance(references, list)
                or not all(isinstance(reference, str) for reference in references)
            ):
                raise ValueError(f"panel QA dependencies are invalid: {panel_id}")
            prompt_paths.append(source_prompt_path)
            actual_reference_paths.extend(references)
            dependencies.append(
                {
                    "panel_id": panel_id,
                    "reference_paths": references,
                    "source_prompt_path": source_prompt_path,
                }
            )
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
    if material_kind == "lettering":
        # Normalized the same way generation material is, so restating the default
        # render mode does not re-letter a page that would come out identical.
        text = [normalized_text_material(panel.get("text", [])) for panel in panels]
        return [text] if text else [[]], _project_files(
            project_dir,
            [_panel_clean_relative_path(project_dir, panel_id) for panel_id in panel_ids],
        )
    if material_kind == "composition":
        geometry = []
        pages = storyboard.get("pages", [])
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_panels = page.get("panels", [])
                geometry.append(
                    {
                        "number": page.get("number"),
                        "layout": page.get("layout"),
                        "panels": [
                            panel.get("rect") for panel in page_panels if isinstance(panel, dict)
                        ]
                        if isinstance(page_panels, list)
                        else [],
                    }
                )
        return [geometry], _project_files(
            project_dir, [f"panels/{panel_id}/lettered.png" for panel_id in panel_ids]
        )
    settings = manifest.get("settings", {})
    project_id = manifest.get("project_id")
    page_count = settings.get("page_count", 0) if isinstance(settings, dict) else 0
    page_paths = (
        [f"pages/page-{number:03d}.png" for number in range(1, page_count + 1)]
        if isinstance(page_count, int)
        else []
    )
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
            problems.setdefault(stage, f"artifact path escapes the project: {name}")
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
    output_kind = get_stage(stage).output_kind
    if output_kind == "planning":
        return _project_files(project_dir, ["plan/story-plan.json", "plan/character-bible.json"])
    if output_kind == "storyboard":
        return _project_files(project_dir, ["plan/storyboard.json"])
    storyboard = read_json(project_dir / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    panel_ids = [panel.get("id") for panel in panels if isinstance(panel.get("id"), str)]
    if not panel_ids:
        raise ValueError("storyboard has no panels")
    if output_kind == "generation":
        relatives = [f"panels/raw/{panel_id}.png" for panel_id in panel_ids]
        relatives += [_panel_clean_relative_path(project_dir, panel_id) for panel_id in panel_ids]
        return _project_files(project_dir, relatives)
    if output_kind == "lettering":
        return _project_files(
            project_dir, [f"panels/{panel_id}/lettered.png" for panel_id in panel_ids]
        )
    if output_kind == "composition":
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
        manifest = read_project_manifest(project_dir / "project.json")
        versions = manifest.get("stage_versions")
        if not isinstance(versions, dict) or not isinstance(versions.get(stage), str):
            raise ValueError("manifest stage_versions must contain the stage version")
        output_files = _stage_output_files(project_dir, stage, manifest)
        missing_outputs = [
            path.relative_to(project_dir).as_posix() for path in output_files if not path.is_file()
        ]
        if missing_outputs:
            raise ValueError(f"stage output is missing: {missing_outputs[0]}")
        canonical_inputs, files = _resume_stage_material(project_dir, stage, manifest)
        key = stage_cache_key(stage, canonical_inputs, files, versions[stage])
        artifacts = {
            path.relative_to(project_dir).as_posix(): sha256_file(path) for path in output_files
        }
        cache_path = project_dir / STAGE_CACHE_PATH
        cache, _ = _load_stage_cache(cache_path)
        stages = cache["stages"]
        assert isinstance(stages, dict)
        stages[stage] = {"artifacts": artifacts, "key": key}
        tx.stage_bytes(str(STAGE_CACHE_PATH), canonical_artifact_bytes(cache))
        tx.append_bytes(
            "logs/events.jsonl",
            canonical_event_record("stage.recorded", {"action": stage}),
            repair_torn_jsonl=True,
        )
    return {"artifacts": len(artifacts), "stage": stage}


def _accepted_panel_problem(
    project_dir: Path,
    record: dict[str, object],
) -> str | None:
    """Validate schema once, then verify artifacts needed for safe reuse."""
    # Import lazily because the standalone validator imports this lifecycle module.
    from .character_quality import validate_character_quality_provenance
    from .validate_project import validate_panel_provenance, validate_panel_record

    schema_issues = validate_panel_record(record)
    if schema_issues:
        first = schema_issues[0]
        return f"accepted panel QA record is invalid: {first.field}: {first.message}"

    is_v2 = record.get("schema_version") == "2.0"
    if not is_v2 and record.get("failure_category") in {
        "corrupt",
        "corrupt_image",
        "safety",
        "safety_refusal",
    }:
        return "non-overridable panel failure cannot be reused"

    if is_v2:
        provenance_issues = validate_panel_provenance(project_dir, record)
        if provenance_issues:
            first = provenance_issues[0]
            return f"accepted panel provenance is invalid: {first.field}: {first.message}"
        character_issues = validate_character_quality_provenance(project_dir, record)
        if character_issues:
            return f"accepted panel character consistency is invalid: {character_issues[0]}"
        panel_id = record.get("subject_id")
        bindings = record.get("bindings")
        if not isinstance(panel_id, str) or not isinstance(bindings, dict):
            return "accepted panel QA record is invalid"
        expected_paths = {
            "raw_path": f"panels/raw/{panel_id}.png",
            "clean_path": f"panels/{panel_id}/clean.png",
            "normalization_path": f"panels/{panel_id}/normalization.json",
        }
        if any(bindings.get(field) != value for field, value in expected_paths.items()):
            return "accepted panel paths do not match the canonical project layout"
        artifact_specs = (
            (
                "raw",
                expected_paths["raw_path"],
                bindings.get("raw_sha256"),
                (bindings.get("raw_width"), bindings.get("raw_height")),
            ),
            (
                "clean",
                expected_paths["clean_path"],
                bindings.get("clean_sha256"),
                (bindings.get("clean_width"), bindings.get("clean_height")),
            ),
            (
                "normalization",
                expected_paths["normalization_path"],
                bindings.get("normalization_sha256"),
                None,
            ),
        )
    else:
        panel_id = record.get("panel_id")
        raw_path = record.get("raw_path")
        clean_path = record.get("clean_path")
        if (
            not isinstance(panel_id, str)
            or not isinstance(raw_path, str)
            or not isinstance(clean_path, str)
        ):
            return "accepted panel QA record is invalid"
        expected_paths = {
            "source_prompt_path": f"prompts/panels/{panel_id}.txt",
            "raw_path": f"panels/raw/{panel_id}.png",
            "clean_path": f"panels/clean/{panel_id}.png",
        }
        if any(record.get(field) != value for field, value in expected_paths.items()):
            return "accepted panel paths do not match the canonical project layout"
        dimensions = record.get("dimensions")
        recorded_size = (
            (dimensions.get("width"), dimensions.get("height"))
            if isinstance(dimensions, dict)
            else None
        )
        artifact_specs = (
            ("raw", raw_path, record.get("raw_sha256"), recorded_size),
            ("clean", clean_path, None, recorded_size),
        )

    resolved: dict[str, Path] = {}
    for name, relative, expected_hash, expected_size in artifact_specs:
        try:
            path = _contained_project_path(project_dir, Path(relative))
        except (TypeError, ValueError):
            return "accepted panel path escapes the project directory"
        resolved[name] = path
        if not path.is_file():
            return f"artifact is missing: {relative}"
        if expected_hash is not None and sha256_file(path) != expected_hash:
            return f"artifact hash mismatch: {relative}"
        if name in {"raw", "clean"}:
            try:
                size = _verify_raster(path)
            except ValueError:
                return "accepted panel image is corrupt"
            if expected_size is not None and size != expected_size:
                return "accepted panel dimensions do not match recorded artifacts"

    if not is_v2:
        try:
            clean_size = _verify_raster(resolved["clean"])
            raw_size = _verify_raster(resolved["raw"])
        except ValueError:
            return "accepted panel image is corrupt"
        if clean_size != raw_size:
            return "accepted panel dimensions do not match recorded artifacts"
    return None


def build_resume_plan(project_dir: Path) -> list[ResumeAction]:
    """Return a read-only deterministic reuse/repair plan for a generated project."""
    project_dir = Path(project_dir).resolve()
    manifest = read_project_manifest(project_dir / "project.json")
    cache_path = project_dir / STAGE_CACHE_PATH
    cache, cache_problem = _load_stage_cache(cache_path)
    cached_stages = cache.get("stages")
    assert isinstance(cached_stages, dict)
    versions = manifest.get("stage_versions")
    if not isinstance(versions, dict):
        raise ValueError("manifest stage_versions must be an object")
    manifest_artifacts = manifest.get("artifacts", {})
    semantic_manifest_paths = (
        {
            descriptor.get("path")
            for name, descriptor in manifest_artifacts.items()
            if name in {"story_plan", "character_bible", "storyboard"}
            and isinstance(descriptor, dict)
            and isinstance(descriptor.get("path"), str)
        }
        if isinstance(manifest_artifacts, dict)
        else set()
    )
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
            action = get_stage(stage).stale_action
            reason = (
                stale_reason
                if index == stale_from
                else f"depends on stale {RESUME_STAGES[stale_from]} stage"
            )
            actions.append(ResumeAction(stage, action, "stage", reason))

    for temporary in sorted(project_dir.rglob("*.tmp")):
        if temporary.is_file():
            actions.append(
                ResumeAction(
                    "generation",
                    "rerun",
                    temporary.relative_to(project_dir).as_posix(),
                    "interrupted temporary file ignored and preserved",
                )
            )
    generation_cache_reusable = any(
        action.stage == "generation" and action.artifact == "stage" and action.action == "reuse"
        for action in actions
    )
    for record_path in sorted((project_dir / "qa/panels").glob("*.json")):
        try:
            record = read_json(record_path)
        except (OSError, UnicodeError, ValueError) as error:
            actions.append(
                ResumeAction(
                    "generation",
                    "regenerate",
                    record_path.stem,
                    f"panel QA record is invalid: {type(error).__name__}",
                )
            )
            continue
        panel_id = (
            record.get("subject_id")
            if record.get("schema_version") == "2.0"
            else record.get("panel_id")
        )
        decision = record.get("decision")
        if not isinstance(panel_id, str):
            continue
        accepted = decision in (
            {"accept", "accept-warning"}
            if record.get("schema_version") == "2.0"
            else {"accept", "accept_with_warnings"}
        )
        panel_problem = _accepted_panel_problem(project_dir, record) if accepted else None
        if accepted and panel_problem is None and not generation_cache_reusable:
            panel_problem = "generation stage cache is stale or missing"
        actions.append(
            ResumeAction(
                "generation",
                "reuse" if accepted and panel_problem is None else "regenerate",
                panel_id,
                (
                    "accepted QA artifact is reusable"
                    if accepted and panel_problem is None
                    else panel_problem or "panel QA requires regeneration"
                ),
            )
        )
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
    with ProjectTransaction(project_dir, "block-project") as transaction:
        manifest = read_project_manifest(manifest_path, normalize_legacy=False)
        current = manifest.get("status")
        if not isinstance(current, str) or not _allowed_transition(current, "BLOCKED"):
            raise ValueError(f"invalid Comic Sol transition: {current} -> BLOCKED")
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list):
            raise ValueError("manifest warnings must be an array")
        normalized_warning = warning.strip()
        if normalized_warning not in warnings:
            warnings.append(normalized_warning)
        manifest.update(
            {
                "blocked_from": current,
                "blocked_reason": reason,
                "status": "BLOCKED",
                "updated_at": _utc_now(),
            }
        )
        transaction.append_bytes(
            "logs/events.jsonl",
            canonical_event_record(
                "project.transitioned",
                {"from": current, "to": "BLOCKED", "warning_present": True},
            ),
            repair_torn_jsonl=True,
        )
        _refresh_handoff_manifest_stage(project_dir, manifest, "BLOCKED", transaction)
        transaction.stage_bytes("project.json", canonical_artifact_bytes(manifest))
        return manifest


def _resolved_block(manifest: dict[str, object], reason: str) -> bool:
    if reason != "image-capability-unavailable":
        return True
    capability = manifest.get("capability")
    return isinstance(capability, dict) and capability.get("status") == "available"


def _stage_progress_from_plan(
    actions: list[ResumeAction],
) -> tuple[list[str], str | None, list[str]]:
    """Split resume-plan actions into preserved, first-stale, and invalidated stages.

    This is the single derivation shared by ``resume`` and ``status`` so the two
    surfaces never disagree about which stage is the first one that must run
    again. Stages before the first non-``reuse`` action are preserved; that stage
    and everything after it are invalidated.

    A panel with a valid QA record that has decision="regenerate" also requires
    regeneration and prevents the generation stage from being marked complete.
    """
    stage_actions = {action.stage: action for action in actions if action.artifact == "stage"}
    preserved: list[str] = []
    stale_stage: str | None = None

    # Check for panel-level regeneration requirements in the generation stage
    has_panel_regeneration = any(
        action.stage == "generation"
        and action.artifact != "stage"
        and action.action == "regenerate"
        for action in actions
    )

    for stage in RESUME_STAGES:
        action = stage_actions.get(stage)
        # Stage cache says reuse, but panels need regeneration.
        if (
            stale_stage is None
            and stage == "generation"
            and has_panel_regeneration
            and action is not None
            and action.action == "reuse"
        ):
            stale_stage = stage
        elif stale_stage is None and action is not None and action.action == "reuse":
            preserved.append(stage)
        elif stale_stage is None:
            stale_stage = stage
    invalidated = (
        list(RESUME_STAGES[RESUME_STAGES.index(stale_stage) :]) if stale_stage is not None else []
    )
    return preserved, stale_stage, invalidated


def _next_resume_action(project_dir: Path, stage: str | None) -> dict[str, str] | None:
    if stage is None:
        return None
    definition = get_stage(stage)
    if definition.next_action == "agent":
        return {"agent_required": stage}
    if not definition.runner:
        raise ValueError(f"stage runner is not registered: {stage}")
    runner = Path(__file__).resolve().parent / Path(definition.runner).name
    command = [sys.executable, runner]
    if stage == "composition":
        command.append("--all")
    command.append(project_dir)
    return {"command": shlex.join(str(part) for part in command)}


def read_project_status(project_dir: Path) -> dict[str, object]:
    """Recover interrupted publication, then read one consistent manifest."""
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = contained_project_path(project_dir, "project.json", must_exist=True)
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        return read_project_manifest(manifest_path)


def _panel_review_counts(project_dir: Path, manifest: dict[str, object]) -> dict[str, int]:
    """Count expected panels by review outcome from their QA records.

    Expected IDs come from both the recovered manifest and the canonical
    storyboard. ``accepted`` counts either supported acceptance spelling,
    ``failed`` counts an explicit repair decision, and ``pending`` includes
    records without a decision plus expected panels with no QA record. A record
    that cannot be read is reported under ``unreadable`` so a corrupt project
    stays diagnosable instead of silently under-counting.
    """
    counts = {"accepted": 0, "failed": 0, "pending": 0, "unreadable": 0}
    manifest_panels = manifest.get("panels")
    expected_panel_ids = (
        {
            panel_id
            for panel_id in manifest_panels
            if isinstance(panel_id, str) and PANEL_ID_PATTERN.fullmatch(panel_id)
        }
        if isinstance(manifest_panels, list)
        else set()
    )

    try:
        storyboard_path = contained_project_path(
            project_dir, "plan/storyboard.json", must_exist=True
        )
        storyboard = read_json(storyboard_path)
        expected_panel_ids.update(
            panel_id
            for panel in _storyboard_panels(storyboard)
            if isinstance((panel_id := panel.get("id")), str)
            and PANEL_ID_PATTERN.fullmatch(panel_id)
        )
    except (OSError, UnicodeError, ValueError, KeyError):
        # The recovered manifest still gives a useful count when the storyboard
        # is absent or corrupt. Other status fields expose the project state.
        pass

    reviewed_panel_ids: set[str] = set()
    qa_dir = contained_project_path(project_dir, "qa/panels")
    if qa_dir.is_dir():
        for record_path in sorted(qa_dir.glob("*.json")):
            # An unreadable expected record is unreadable, not also pending.
            reviewed_panel_ids.add(record_path.stem)
            try:
                record = read_json(record_path)
            except (OSError, UnicodeError, ValueError):
                counts["unreadable"] += 1
                continue
            decision = record.get("decision") if isinstance(record, dict) else None
            if decision in ACCEPTED_DECISIONS:
                counts["accepted"] += 1
            elif decision in REPAIR_DECISIONS:
                counts["failed"] += 1
            else:
                counts["pending"] += 1

    counts["pending"] += len(expected_panel_ids - reviewed_panel_ids)
    return counts


def _stage_state_from_actions(
    preserved: list[str], stale_stage: str | None, status: str
) -> list[dict[str, str]]:
    """Describe each resume stage as complete, blocked, stale, or pending.

    The first stale stage is reported as ``blocked`` when the project status is
    ``BLOCKED`` and ``stale`` otherwise; stages after it are ``pending``. Stages
    before it are ``complete``. This mirrors the resume plan exactly so the
    visual summary never contradicts what ``resume`` would do.
    """
    stages: list[dict[str, str]] = []
    reached_stale = False
    for stage in RESUME_STAGES:
        if stage in preserved and not reached_stale:
            state = "complete"
        elif stage == stale_stage:
            reached_stale = True
            state = "blocked" if status == "BLOCKED" else "stale"
        else:
            reached_stale = True
            state = "pending"
        stages.append({"stage": stage, "state": state})
    return stages


def summarize_project_status(project_dir: Path) -> dict[str, object]:
    """Return a read-only visual summary of project progress and recovery options.

    The result composes the recovered manifest status, a per-stage completion or
    blocking view, panel review counts, and the next recommended action. The next
    action is derived from the same resume plan that ``resume`` and ``finalize``
    consume, so a human summary can never suggest a step the recovery logic would
    not take. Reading a project never mutates it: recovery of an interrupted
    publication is the only side effect, exactly as ``read_project_status`` does.
    """
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = contained_project_path(project_dir, "project.json", must_exist=True)
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        manifest = read_project_manifest(manifest_path)
        status = str(manifest.get("status", ""))
        actions = build_resume_plan(project_dir)
        preserved, stale_stage, _ = _stage_progress_from_plan(actions)
        if status == "BLOCKED":
            reason = manifest.get("blocked_reason")
            # When blocked due to image-capability-unavailable and capability remains
            # unavailable, return a required action instead of resume
            if reason == "image-capability-unavailable" and not _resolved_block(manifest, reason):
                next_action: dict[str, str] | None = {"required": "image capability available"}
            else:
                next_action = {
                    "resume": str(reason) if isinstance(reason, str) and reason else "blocked",
                }
        elif stale_stage is None and status in TERMINAL_STATUSES:
            # A terminal project whose stages all still reuse is genuinely done.
            next_action = {"done": "project is complete"}
        else:
            # For every other non-blocked project the recommended step is the
            # first stale stage, exactly as resume would run it. A terminal
            # project whose inputs changed is therefore reported as having work
            # left, never as falsely finished.
            next_action = _next_resume_action(project_dir, stale_stage)
        warnings = manifest.get("warnings")
        return {
            "project_id": manifest.get("project_id"),
            "status": status,
            "stages": _stage_state_from_actions(preserved, stale_stage, status),
            "panels": _panel_review_counts(project_dir, manifest),
            "warnings": list(warnings) if isinstance(warnings, list) else [],
            "blocked_reason": manifest.get("blocked_reason"),
            "next_action": next_action,
        }


def resume_project(
    project_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Recover transactions and move a blocked project to its last valid state."""
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = contained_project_path(project_dir, "project.json", must_exist=True)
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        if progress is not None:
            progress(
                {
                    "status": "working",
                    "stage": "resume",
                    "completed": [],
                    "remaining": list(RESUME_STAGES),
                }
            )
        result = _resume_project_locked(project_dir, manifest_path)
        if progress is not None:
            state = str(result.get("status", "")).upper()
            preserved = result.get("preserved")
            invalidated = result.get("invalidated")
            progress(
                {
                    "status": (
                        "blocked"
                        if state == "BLOCKED"
                        else "complete"
                        if state in TERMINAL_STATUSES
                        else "working"
                    ),
                    "stage": "resume",
                    "completed": preserved if isinstance(preserved, list) else [],
                    "remaining": invalidated if isinstance(invalidated, list) else [],
                }
            )
        return result


def _resume_project_locked(project_dir: Path, manifest_path: Path) -> dict[str, object]:
    with ProjectLock(project_dir):
        manifest = read_project_manifest(manifest_path, normalize_legacy=False)
        if manifest.get("status") != "BLOCKED":
            actions = build_resume_plan(project_dir)
            preserved, stale_stage, invalidated = _stage_progress_from_plan(actions)
            return {
                "status": manifest.get("status"),
                "preserved": preserved,
                "invalidated": invalidated,
                "next_action": _next_resume_action(project_dir, stale_stage),
            }
        actions = build_resume_plan(project_dir)
        preserved, stale_stage, invalidated = _stage_progress_from_plan(actions)
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

        # Cache refresh/invalidation, reuse provenance, and the final manifest
        # describe one recovered generation. Retain one journal across all
        # staging so a failure or process interruption can expose only the old
        # BLOCKED state or the complete recovered state after locked recovery.
        with ProjectTransaction(project_dir, "resume-recovery") as tx:
            cache_path = project_dir / STAGE_CACHE_PATH
            cache, _ = _load_stage_cache(cache_path)
            cached_stages = cache.get("stages")
            if not isinstance(cached_stages, dict):
                raise ValueError("stage cache stages must be an object")

            # RESUME_STAGES and each stage output list are canonical. Snapshot
            # every accepted output before staging any publication, then refresh
            # preserved cache digests from those exact manifest-authorized bytes.
            # Artifact files themselves remain untouched.
            preserved_outputs: list[Path] = []
            for stage in preserved:
                entry = cached_stages.get(stage)
                if not isinstance(entry, dict):
                    raise ValueError(f"preserved stage cache entry is missing: {stage}")
                outputs = _stage_output_files(project_dir, stage, manifest)
                entry["artifacts"] = {
                    output.relative_to(project_dir).as_posix(): sha256_file(output)
                    for output in outputs
                }
                preserved_outputs.extend(outputs)

            if stale_stage is not None:
                _invalidate_state_locked(project_dir, stale_stage, manifest, cache)
                recovery_status = _post_invalidation_status(
                    project_dir, stale_stage, str(blocked_from)
                )
            else:
                recovery_status = STAGE_COMPLETION_STATUS[preserved[-1]] if preserved else "INIT"

            # The cache is one publication target even when it is both refreshed
            # upstream and invalidated downstream. Keep it before events and the
            # manifest commit marker in the journal.
            if cache_path.is_file() or preserved:
                tx.stage_bytes(str(STAGE_CACHE_PATH), canonical_artifact_bytes(cache))

            warnings = manifest.get("warnings")
            if not isinstance(warnings, list):
                warnings = []
            manifest["warnings"] = [item for item in warnings if _warning_reason(item) != reason]
            manifest["status"] = recovery_status
            manifest["blocked_from"] = None
            manifest["blocked_reason"] = None
            manifest["updated_at"] = _utc_now()

            # Reuse events use the same accepted output snapshot as the cache,
            # preserving stable upstream-to-downstream provenance ordering.
            for output in preserved_outputs:
                tx.append_bytes(
                    "logs/events.jsonl",
                    canonical_event_record(
                        "artifact.reused",
                        {
                            "artifact_path": output.relative_to(project_dir).as_posix(),
                            "reused": True,
                        },
                    ),
                    repair_torn_jsonl=True,
                )

            # The manifest is the commit marker and is deliberately staged once,
            # after cache and event targets, in its final non-BLOCKED form.
            _refresh_handoff_manifest_stage(project_dir, manifest, recovery_status, tx)
            tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))

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


def _invalidate_state_locked(
    project_dir: Path,
    stage: str,
    manifest: dict[str, object],
    cache: dict[str, object],
) -> list[str]:
    """Mutate invalidated manifest and cache state without staging targets."""
    if stage not in RESUME_STAGES:
        raise ValueError(f"unknown resume stage: {stage}")
    start = RESUME_STAGES.index(stage)
    removed: list[str] = []
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

    if (project_dir / STAGE_CACHE_PATH).is_file():
        cached_stages = cache.get("stages")
        if not isinstance(cached_stages, dict):
            raise ValueError("stage cache stages must be an object")
        for downstream in RESUME_STAGES[start:]:
            cached_stages.pop(downstream, None)
    return removed


def _invalidate_from_locked(project_dir: Path, stage: str, tx: ProjectTransaction) -> list[str]:
    """Apply standalone invalidation while the caller owns the transaction lock."""
    manifest = read_project_manifest(project_dir / "project.json", normalize_legacy=False)
    cache_path = project_dir / STAGE_CACHE_PATH
    cache, _ = _load_stage_cache(cache_path)
    removed = _invalidate_state_locked(project_dir, stage, manifest, cache)
    binding = manifest.get("handoff")
    if isinstance(binding, dict):
        manifest["handoff"] = {
            "contract_version": HANDOFF_CONTRACT_VERSION,
            "locked_scope_sha256": None,
            "manifest_path": None,
        }
    if cache_path.is_file():
        tx.stage_bytes(str(STAGE_CACHE_PATH), canonical_artifact_bytes(cache))
    manifest["status"] = _post_invalidation_status(project_dir, stage, str(manifest.get("status")))
    manifest["updated_at"] = _utc_now()
    tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    return removed


def invalidate_from(project_dir: Path, stage: str) -> list[str]:
    """Forget manifest/cache descriptors from a stage onward without deleting artifacts."""
    project_dir = Path(project_dir).resolve()
    with ProjectTransaction(project_dir, "invalidate") as tx:
        manifest = read_project_manifest(project_dir / "project.json")
        if manifest.get("status") == "BLOCKED":
            raise ValueError("cannot invalidate a BLOCKED project; resume it first")
        return _invalidate_from_locked(project_dir, stage, tx)


def _contained_project_path(project_dir: Path, path: Path) -> Path:
    project_root = Path(project_dir).resolve(strict=True)
    if path.is_absolute():
        # Resolve before comparing: the same location can be spelled two ways
        # (macOS /var vs /private/var, Windows 8.3 names), and only the root
        # was being resolved. contained_project_path re-checks the remainder.
        try:
            path = path.resolve().relative_to(project_root)
        except ValueError as error:
            raise ValueError("path escapes the project directory") from error
    return contained_project_path(project_root, path)


def _read_handoff_json(project_dir: Path, relative: str) -> dict[str, object]:
    payload = read_contained_bytes(project_dir, relative, max_bytes=MAX_JSON_BYTES)
    value = loads_bounded_json(payload, source=relative)
    if not isinstance(value, dict):
        raise HandoffContractError([f"{relative}: must contain a JSON object"])
    return cast(dict[str, object], value)


def _prompt_inventory(project_dir: Path, kind: str) -> dict[str, tuple[str, bytes]]:
    relative_dir = f"prompts/{kind}"
    directory = contained_project_path(project_dir, relative_dir, must_exist=True)
    if directory.is_symlink():
        raise ValueError("security-error: generation prompt directory must not be a symlink")
    if not directory.is_dir():
        raise HandoffContractError([f"{relative_dir}: must be a prompt directory"])
    inventory: dict[str, tuple[str, bytes]] = {}
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        relative = f"{relative_dir}/{entry.name}"
        if entry.is_symlink():
            raise ValueError("security-error: generation prompt must not be a symlink")
        if not entry.is_file() or entry.suffix != ".txt":
            raise HandoffContractError(
                [f"{relative}: generation prompts must be regular .txt files"]
            )
        identifier = entry.stem
        if identifier in inventory:
            raise HandoffContractError([f"{relative}: duplicate prompt identity"])
        inventory[identifier] = (relative, read_contained_bytes(project_dir, relative))
    return inventory


def _receipt_inventory(project_dir: Path) -> list[dict[str, object]]:
    directory = contained_project_path(project_dir, "generation/receipts")
    if not directory.exists():
        return []
    if directory.is_symlink():
        raise ValueError("security-error: receipt directory must not be a symlink")
    if not directory.is_dir():
        raise HandoffContractError(["generation/receipts: must be a directory"])
    receipts: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        relative = f"generation/receipts/{entry.name}"
        if entry.is_symlink():
            raise ValueError("security-error: receipt must not be a symlink")
        if not entry.is_file() or entry.suffix != ".json":
            raise HandoffContractError([f"{relative}: receipts must be regular JSON files"])
        receipt = _read_handoff_json(project_dir, relative)
        issues = validate_generation_receipt(receipt)
        if issues:
            raise HandoffContractError(f"{relative}.{issue}" for issue in issues)
        identifier = receipt.get("attempt_id")
        if entry.stem != identifier:
            raise HandoffContractError([f"{relative}: filename must match attempt_id"])
        if not isinstance(identifier, str) or identifier in identifiers:
            raise HandoffContractError([f"{relative}: duplicate attempt ID"])
        identifiers.add(identifier)
        receipts.append(receipt)
    return receipts


def _prepared_handoff_snapshot(
    project_dir: Path,
    project_manifest: dict[str, object],
) -> dict[str, object] | None:
    binding = project_manifest.get("handoff")
    if not isinstance(binding, dict):
        return None
    manifest_path = binding.get("manifest_path")
    locked_scope = binding.get("locked_scope_sha256")
    if manifest_path is None and locked_scope is None:
        return None
    if manifest_path != HANDOFF_MANIFEST_PATH or not isinstance(locked_scope, str):
        raise HandoffContractError(["project.json.handoff: populated binding is malformed"])

    handoff_manifest = _read_handoff_json(project_dir, HANDOFF_MANIFEST_PATH)
    issues = validate_handoff_manifest(handoff_manifest)
    if issues:
        raise HandoffContractError(f"{HANDOFF_MANIFEST_PATH}.{issue}" for issue in issues)
    for field, expected in (
        ("project_id", project_manifest.get("project_id")),
        ("project_schema_version", project_manifest.get("schema_version")),
        ("stage", project_manifest.get("status")),
        ("locked_scope_sha256", locked_scope),
    ):
        if handoff_manifest.get(field) != expected:
            raise HandoffContractError(
                [f"{HANDOFF_MANIFEST_PATH}.{field}: does not match project.json"]
            )

    batch_map = _read_handoff_json(project_dir, BATCHES_PATH)
    issues = validate_generation_batches(batch_map)
    if issues:
        raise HandoffContractError(f"{BATCHES_PATH}.{issue}" for issue in issues)
    batch_bytes = canonical_artifact_bytes(batch_map)
    batches_descriptor = handoff_manifest.get("batches")
    if (
        not isinstance(batches_descriptor, dict)
        or batches_descriptor.get("sha256") != hashlib.sha256(batch_bytes).hexdigest()
    ):
        raise HandoffContractError([f"{BATCHES_PATH}: digest does not match handoff manifest"])

    descriptors = handoff_manifest.get("jobs")
    if not isinstance(descriptors, list):
        raise HandoffContractError([f"{HANDOFF_MANIFEST_PATH}.jobs: must be an array"])
    jobs: dict[str, dict[str, object]] = {}
    descriptor_by_id: dict[str, dict[str, object]] = {}
    reference_paths: set[str] = set()
    for raw_descriptor in descriptors:
        if not isinstance(raw_descriptor, dict):
            raise HandoffContractError([f"{HANDOFF_MANIFEST_PATH}.jobs: invalid descriptor"])
        descriptor = cast(dict[str, object], raw_descriptor)
        job_id = descriptor.get("job_id")
        if not isinstance(job_id, str):
            raise HandoffContractError([f"{HANDOFF_MANIFEST_PATH}.jobs: invalid job ID"])
        descriptor_by_id[job_id] = descriptor
        if descriptor.get("status") == "missing":
            continue
        relative = descriptor.get("path")
        if not isinstance(relative, str):
            raise HandoffContractError([f"{HANDOFF_MANIFEST_PATH}.jobs: invalid path"])
        job = _read_handoff_json(project_dir, relative)
        issues = validate_generation_job(job)
        if issues:
            raise HandoffContractError(f"{relative}.{issue}" for issue in issues)
        if job.get("job_id") != job_id:
            raise HandoffContractError([f"{relative}.job_id: descriptor binding mismatch"])
        job_bytes = canonical_artifact_bytes(job)
        if descriptor.get("sha256") != hashlib.sha256(job_bytes).hexdigest():
            raise HandoffContractError([f"{relative}.sha256: descriptor binding mismatch"])
        jobs[job_id] = job
        references = job.get("references")
        if isinstance(references, list):
            reference_paths.update(
                reference["path"]
                for reference in references
                if isinstance(reference, dict) and isinstance(reference.get("path"), str)
            )

    batch_job_ids = {
        job_id
        for batch in cast(list[dict[str, object]], batch_map["batches"])
        for job_id in cast(list[str], batch["job_ids"])
    }
    if batch_job_ids != set(descriptor_by_id):
        raise HandoffContractError(["handoff jobs must exactly match generation batches"])

    stale = False
    required_artifacts = handoff_manifest.get("required_artifacts")
    if not isinstance(required_artifacts, list):
        raise HandoffContractError(["required_artifacts: must be an array"])
    for artifact in required_artifacts:
        if not isinstance(artifact, dict):
            raise HandoffContractError(["required_artifacts: invalid descriptor"])
        relative = artifact.get("path")
        expected = artifact.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise HandoffContractError(["required_artifacts: invalid binding"])
        if relative.startswith(("references/characters/", "references/scenes/")):
            reference_paths.add(relative)
        try:
            payload = read_contained_bytes(project_dir, relative)
        except FileNotFoundError:
            stale = True
        else:
            stale = stale or hashlib.sha256(payload).hexdigest() != expected
    try:
        assert_current_locked_scope(
            project_dir,
            locked_scope,
            reference_paths=sorted(reference_paths),
        )
    except (FileNotFoundError, StaleLockedScopeError):
        stale = True

    receipts = _receipt_inventory(project_dir)
    receipts_by_job: dict[str, list[dict[str, object]]] = {job_id: [] for job_id in jobs}
    current_attempt_ids = {
        generation_attempt_id(job_id=job_id, attempt=ordinal)
        for job_id, job in jobs.items()
        for ordinal in range(1, cast(int, job["retry_limit"]) + 2)
    }
    for receipt in receipts:
        receipt_job_id = receipt.get("job_id")
        if not isinstance(receipt_job_id, str) or receipt_job_id not in receipts_by_job:
            if receipt.get("attempt_id") in current_attempt_ids:
                raise HandoffContractError(["receipt job_id: does not name a current handoff job"])
            continue
        job = jobs[receipt_job_id]
        if receipt.get("outcome") == "success":
            raster_path = receipt.get("raster_path")
            raster_sha256 = receipt.get("raster_sha256")
            if raster_path != job.get("target_path"):
                raise HandoffContractError(
                    ["successful receipt raster_path: does not match generation job target_path"]
                )
            if not isinstance(raster_path, str) or not isinstance(raster_sha256, str):
                raise HandoffContractError(
                    ["successful receipt: retained raster binding is malformed"]
                )
            try:
                raster_bytes = read_contained_bytes(
                    project_dir,
                    raster_path,
                    max_bytes=MAX_ENCODED_RASTER_BYTES,
                )
            except FileNotFoundError as error:
                raise HandoffContractError(
                    [f"{raster_path}: successful receipt retained raster is missing"]
                ) from error
            if hashlib.sha256(raster_bytes).hexdigest() != raster_sha256:
                raise HandoffContractError(
                    [f"{raster_path}: successful receipt retained raster digest mismatch"]
                )
            try:
                _validate_handoff_raster(raster_bytes, job)
            except HandoffResultError as error:
                raise HandoffContractError(
                    [
                        f"{raster_path}: successful receipt retained raster violates "
                        f"the generation job: {issue}"
                        for issue in error.issues
                    ]
                ) from error
            if job.get("subject_kind") == "reference":
                try:
                    canonical_path = _reference_canonical_path(
                        project_dir, cast(str, job["subject_id"])
                    )
                except HandoffResultError:
                    stale = True
                else:
                    try:
                        canonical_bytes = read_contained_bytes(
                            project_dir,
                            canonical_path,
                            max_bytes=MAX_ENCODED_RASTER_BYTES,
                        )
                    except FileNotFoundError:
                        stale = True
                    else:
                        stale = stale or canonical_bytes != raster_bytes
        receipts_by_job[receipt_job_id].append(receipt)

    effective_jobs: list[dict[str, object]] = []
    for descriptor in descriptors:
        descriptor = cast(dict[str, object], descriptor)
        job_id = cast(str, descriptor["job_id"])
        job = jobs.get(job_id)
        state = reconcile_job_receipts(
            job=job,
            job_sha256=descriptor.get("sha256") if job is not None else None,
            receipts=receipts_by_job.get(job_id, []),
            declared_status=cast(str, descriptor["status"]),
            stale=stale,
        )
        effective_jobs.append(
            {
                "attempts_remaining": state["attempts_remaining"],
                "attempts_used": state["attempts_used"],
                "job_id": job_id,
                "next_attempt": state["next_attempt"],
                "path": descriptor["path"],
                "status": state["status"],
                "subject_id": None if job is None else job.get("subject_id"),
                "subject_kind": None if job is None else job.get("subject_kind"),
            }
        )
    batches = cast(list[dict[str, object]], batch_map["batches"])
    phase = "panel" if any(batch.get("kind") == "panel" for batch in batches) else "reference"
    return {
        "batch_map": batch_map,
        "effective_jobs": effective_jobs,
        "handoff_manifest": handoff_manifest,
        "jobs": jobs,
        "phase": phase,
        "receipts": receipts,
        "scope_state": "stale" if stale else "current",
    }


def _handoff_next_action(phase: str, jobs: Sequence[Mapping[str, object]]) -> str:
    statuses = {job.get("status") for job in jobs}
    if "stale" in statuses:
        return "invalidate"
    if "failed" in statuses:
        return "retry-exhausted"
    if phase == "reference":
        return "prepare" if statuses == {"completed"} else "render-references"
    return "visual-qa" if statuses == {"completed"} else "render-panels"


def inspect_handoff(project_dir: Path) -> dict[str, object]:
    """Inspect effective handoff state without migration or project-artifact writes."""
    project_dir = Path(project_dir)
    project_manifest = read_project_manifest(project_dir / "project.json")
    binding = project_manifest.get("handoff")
    if project_manifest.get("schema_version") == LEGACY_PROJECT_SCHEMA_VERSION or (
        isinstance(binding, dict)
        and binding.get("manifest_path") is None
        and binding.get("locked_scope_sha256") is None
    ):
        return {
            "batches": [],
            "jobs": [],
            "next_action": "prepare",
            "phase": None,
            "prepared": False,
            "project_stage": project_manifest.get("status"),
            "scope_state": "unprepared",
        }
    if not isinstance(binding, dict):
        raise HandoffContractError(["project.json.handoff: required object is missing"])
    with ProjectLock(project_dir):
        project_manifest = read_project_manifest(project_dir / "project.json")
        snapshot = _prepared_handoff_snapshot(project_dir, project_manifest)
    if snapshot is None:
        return {
            "batches": [],
            "jobs": [],
            "next_action": "prepare",
            "phase": None,
            "prepared": False,
            "project_stage": project_manifest.get("status"),
            "scope_state": "unprepared",
        }
    jobs = cast(list[dict[str, object]], snapshot["effective_jobs"])
    batch_map = cast(dict[str, object], snapshot["batch_map"])
    phase = cast(str, snapshot["phase"])
    return {
        "batches": batch_map["batches"],
        "jobs": jobs,
        "next_action": _handoff_next_action(phase, jobs),
        "phase": phase,
        "prepared": True,
        "project_stage": project_manifest.get("status"),
        "scope_state": snapshot["scope_state"],
    }


def _validate_reference_activation(
    project_dir: Path,
    snapshot: Mapping[str, object],
) -> None:
    jobs = cast(dict[str, dict[str, object]], snapshot["jobs"])
    effective = {
        cast(str, item["job_id"]): item
        for item in cast(list[dict[str, object]], snapshot["effective_jobs"])
    }
    receipts = cast(list[dict[str, object]], snapshot["receipts"])
    for job_id, job in jobs.items():
        if job.get("subject_kind") != "reference":
            continue
        if effective[job_id].get("status") != "completed":
            raise HandoffContractError(["reference jobs must be completed before panel prepare"])
        success = next(
            (
                receipt
                for receipt in receipts
                if receipt.get("job_id") == job_id and receipt.get("outcome") == "success"
            ),
            None,
        )
        if success is None:
            raise HandoffContractError(["reference activation requires a successful receipt"])
        retained_path = success.get("raster_path")
        retained_sha256 = success.get("raster_sha256")
        subject_id = job.get("subject_id")
        prompt_path = job.get("prompt_path")
        if not isinstance(subject_id, str) or not isinstance(prompt_path, str):
            raise HandoffContractError(["reference job binding is malformed"])
        canonical = (
            f"references/scenes/{subject_id}.png"
            if subject_id in _story_scene_ids(project_dir)
            else f"references/characters/{subject_id}.png"
        )
        if not isinstance(retained_path, str) or not isinstance(retained_sha256, str):
            raise HandoffContractError(["successful reference receipt is rasterless"])
        retained = read_contained_bytes(
            project_dir, retained_path, max_bytes=MAX_ENCODED_RASTER_BYTES
        )
        canonical_bytes = read_contained_bytes(
            project_dir, canonical, max_bytes=MAX_ENCODED_RASTER_BYTES
        )
        if (
            hashlib.sha256(retained).hexdigest() != retained_sha256
            or hashlib.sha256(canonical_bytes).hexdigest() != retained_sha256
            or retained != canonical_bytes
        ):
            raise HandoffContractError(
                ["canonical reference does not match its successful receipt"]
            )


def _story_scene_ids(project_dir: Path) -> set[str]:
    story = _read_handoff_json(project_dir, "plan/story-plan.json")
    scenes = story.get("scenes")
    return {
        cast(str, scene["id"])
        for scene in cast(list[dict[str, object]], scenes if isinstance(scenes, list) else [])
        if isinstance(scene, dict) and isinstance(scene.get("id"), str)
    }


def _retained_attempt_bindings(project_dir: Path) -> dict[str, str]:
    """Map every retained attempt path this project produced to its receipt digest.

    Invalidation preserves receipts and rasters, so a re-prepared job must be able
    to tell a retained attempt of its own retired predecessor apart from a file it
    has no provenance for. Only a persisted receipt proves the former.
    """
    bindings: dict[str, str] = {}
    for receipt in _receipt_inventory(project_dir):
        raster_path = receipt.get("raster_path")
        raster_sha256 = receipt.get("raster_sha256")
        if isinstance(raster_path, str) and isinstance(raster_sha256, str):
            bindings[raster_path] = raster_sha256
    return bindings


def _reference_activation_provenance(project_dir: Path) -> dict[tuple[str, str], str]:
    """Map each provably produced reference raster to the prompt that rendered it.

    Invalidation retains receipts, generation jobs, and rasters, so an activated
    canonical PNG can still be traced back to the job that produced it. That
    binding is the only honest way to tell whether canonical artwork came from
    the current prompt or from a retired one.
    """
    provenance: dict[tuple[str, str], str] = {}
    for receipt in _receipt_inventory(project_dir):
        if receipt.get("outcome") != "success":
            continue
        raster_sha256 = receipt.get("raster_sha256")
        job_id = receipt.get("job_id")
        if not isinstance(raster_sha256, str) or not isinstance(job_id, str):
            continue
        try:
            job = _read_handoff_json(project_dir, f"generation/jobs/{job_id}.json")
        except (FileNotFoundError, OSError, ValueError):
            continue
        if validate_generation_job(job) or job.get("subject_kind") != "reference":
            continue
        subject_id = job.get("subject_id")
        prompt_sha256 = job.get("prompt_sha256")
        if isinstance(subject_id, str) and isinstance(prompt_sha256, str):
            provenance[(subject_id, raster_sha256)] = prompt_sha256
    return provenance


def _reference_is_superseded(
    project_dir: Path,
    subject_id: str,
    canonical_relative: str,
    prompt_sha256: str,
    provenance: Mapping[tuple[str, str], str],
) -> bool:
    """Report whether an activated canonical reference came from a retired prompt.

    Only a canonical raster this project can prove it produced is ever treated as
    superseded: artwork with no receipt provenance stays untouched so a
    hand-placed reference is never silently regenerated over.
    """
    try:
        payload = read_contained_bytes(
            project_dir, canonical_relative, max_bytes=MAX_ENCODED_RASTER_BYTES
        )
    except (OSError, ValueError):
        return False
    activated = provenance.get((subject_id, hashlib.sha256(payload).hexdigest()))
    return activated is not None and activated != prompt_sha256


def _reference_can_reactivate(
    project_dir: Path,
    subject_id: str,
    canonical_relative: str,
    job: Mapping[str, object],
) -> bool:
    """Allow canonical replacement only for a proven superseded activation."""
    prompt_sha256 = job.get("prompt_sha256")
    if not isinstance(prompt_sha256, str):
        return False
    try:
        canonical = read_contained_bytes(
            project_dir, canonical_relative, max_bytes=MAX_ENCODED_RASTER_BYTES
        )
    except (OSError, ValueError):
        return False
    prior_prompt = _reference_activation_provenance(project_dir).get(
        (subject_id, hashlib.sha256(canonical).hexdigest())
    )
    return prior_prompt is not None and prior_prompt != prompt_sha256


def _next_reference_attempt_target(
    project_dir: Path,
    subject_id: str,
    bindings: Mapping[str, str],
) -> str:
    """Return the retained reference attempt path a newly derived job may claim.

    References carry no generation counters, so the next free sequence is derived
    from the attempts a receipt already accounts for. A path holding anything this
    project cannot prove it produced is left alone for the unmanaged-collision
    check rather than skipped.
    """
    sequence = 1
    while True:
        relative = f"references/attempts/{subject_id}/initial-{sequence:03d}.png"
        expected = bindings.get(relative)
        if expected is None:
            return relative
        try:
            payload = read_contained_bytes(
                project_dir, relative, max_bytes=MAX_ENCODED_RASTER_BYTES
            )
        except (OSError, ValueError):
            return relative
        if hashlib.sha256(payload).hexdigest() != expected:
            return relative
        sequence += 1


def _next_panel_attempt_identity(project_dir: Path, panel_id: str) -> tuple[str, int]:
    """Return the attempt kind and retained sequence a new panel job may claim.

    The generation counters are authoritative for panel attempt accounting, so a
    panel that already spent its initial attempt regenerates as a bounded visual
    retry instead of claiming the initial slot a second time.
    """
    panels = _read_generation_counters(project_dir).get("panels")
    counts = panels.get(panel_id) if isinstance(panels, dict) else None
    if not isinstance(counts, dict) or not counts.get("initial"):
        return "initial", 1
    retries = counts.get("visual_retries", 0)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise HandoffContractError([f"panel {panel_id}: generation counters are invalid"])
    if retries >= GENERATION_LIMITS["visual_retry"]:
        raise HandoffContractError(
            [f"panel {panel_id}: {GENERATION_LIMIT_MESSAGES['visual_retry']}"]
        )
    return "visual_retry", retries + 1


_JOB_BRIEF_FIELDS = (
    "batch_id",
    "prompt_path",
    "prompt_sha256",
    "references",
    "requested_aspect_ratio",
    "requested_dimensions",
    "retry_limit",
    "subject_id",
    "subject_kind",
)


def _prepared_job_for_brief(
    existing_jobs: Mapping[str, Mapping[str, object]],
    candidate: Mapping[str, object],
) -> dict[str, object] | None:
    """Return the already prepared job that renders this exact brief, when one exists.

    A prepared job keeps the retained target it was published with, so repreparing
    an unchanged brief stays byte-identical even after its attempt was accepted and
    the counters moved on.
    """
    for job in existing_jobs.values():
        if all(job.get(field) == candidate.get(field) for field in _JOB_BRIEF_FIELDS):
            return dict(job)
    return None


def _build_handoff_content(
    project_dir: Path,
    project_manifest: dict[str, object],
    existing: Mapping[str, object] | None,
) -> tuple[str, dict[str, bytes], dict[str, object], list[dict[str, object]]]:
    from .validate_project import require_valid_project

    if existing is not None and existing.get("scope_state") != "current":
        raise StaleLockedScopeError(["locked_scope_sha256: stale project scope"])
    require_valid_project(project_dir, "storyboard")
    story = _read_handoff_json(project_dir, "plan/story-plan.json")
    characters = _read_handoff_json(project_dir, "plan/character-bible.json")
    storyboard = _read_handoff_json(project_dir, "plan/storyboard.json")
    identity_pack = _read_handoff_json(project_dir, IDENTITY_PACK_PATH)
    identity_issues = validate_identity_pack(identity_pack, character_bible=characters)
    if identity_issues:
        raise HandoffContractError(identity_issues)

    reference_prompts = _prompt_inventory(project_dir, "references")
    panel_prompts = _prompt_inventory(project_dir, "panels")
    character_items = characters.get("characters")
    scene_items = story.get("scenes")
    pages = storyboard.get("pages")
    if not isinstance(character_items, list) or not isinstance(scene_items, list):
        raise HandoffContractError(["planning artifacts do not contain ordered identities"])
    if not isinstance(pages, list):
        raise HandoffContractError(["storyboard pages must be an array"])
    character_ids = [
        cast(str, item["id"])
        for item in character_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    scene_ids = [
        cast(str, item["id"])
        for item in scene_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    ambiguous = sorted(set(character_ids) & set(scene_ids) & set(reference_prompts))
    unknown = sorted(set(reference_prompts) - set(character_ids) - set(scene_ids))
    if ambiguous:
        raise HandoffContractError(
            [
                f"prompts/references/{identifier}.txt: character/scene identity is ambiguous"
                for identifier in ambiguous
            ]
        )
    if unknown:
        raise HandoffContractError(
            [
                f"prompts/references/{identifier}.txt: unknown reference identity"
                for identifier in unknown
            ]
        )

    reference_candidates: list[tuple[str, str, str, bytes]] = []
    active_reference_paths: set[str] = set()
    reference_provenance = _reference_activation_provenance(project_dir)
    for item in character_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        identifier = cast(str, item["id"])
        canonical = item.get("reference_path")
        if canonical != f"references/characters/{identifier}.png":
            raise HandoffContractError(
                [f"character {identifier}: canonical reference path is invalid"]
            )
        target = contained_project_path(project_dir, cast(str, canonical))
        prompt = reference_prompts.get(identifier)
        superseded = (
            target.exists()
            and prompt is not None
            and _reference_is_superseded(
                project_dir,
                identifier,
                cast(str, canonical),
                hashlib.sha256(prompt[1]).hexdigest(),
                reference_provenance,
            )
        )
        if target.exists() and not superseded:
            _verify_raster(target, expected_format="PNG")
            active_reference_paths.add(cast(str, canonical))
        else:
            if prompt is None:
                raise HandoffContractError(
                    [f"prompts/references/{identifier}.txt: required prompt is missing"]
                )
            reference_candidates.append((identifier, cast(str, canonical), prompt[0], prompt[1]))
    for identifier in scene_ids:
        prompt = reference_prompts.get(identifier)
        canonical = f"references/scenes/{identifier}.png"
        target = contained_project_path(project_dir, canonical)
        superseded = (
            target.exists()
            and prompt is not None
            and _reference_is_superseded(
                project_dir,
                identifier,
                canonical,
                hashlib.sha256(prompt[1]).hexdigest(),
                reference_provenance,
            )
        )
        if target.exists() and not superseded:
            _verify_raster(target, expected_format="PNG")
            active_reference_paths.add(canonical)
        elif prompt is not None:
            reference_candidates.append((identifier, canonical, prompt[0], prompt[1]))

    panel_ids: list[str] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("panels"), list):
            continue
        panel_ids.extend(
            cast(str, panel["id"])
            for panel in page["panels"]
            if isinstance(panel, dict) and isinstance(panel.get("id"), str)
        )
    if set(panel_prompts) != set(panel_ids):
        missing = sorted(set(panel_ids) - set(panel_prompts))
        orphan = sorted(set(panel_prompts) - set(panel_ids))
        issues = [
            f"prompts/panels/{identifier}.txt: required prompt is missing" for identifier in missing
        ]
        issues.extend(
            f"prompts/panels/{identifier}.txt: orphan panel prompt" for identifier in orphan
        )
        raise HandoffContractError(issues)

    phase = "reference" if reference_candidates else "panel"
    if existing is not None:
        existing_phase = existing.get("phase")
        if existing.get("scope_state") != "current":
            raise StaleLockedScopeError(["locked_scope_sha256: stale project scope"])
        if existing_phase == "panel" and phase != "panel":
            raise StaleLockedScopeError(["handoff phase cannot move backward"])
        if existing_phase == "reference" and phase == "panel":
            _validate_reference_activation(project_dir, existing)

    reference_plan = project_reference_plan(
        identity_pack,
        storyboard,
        reference_budget=0 if phase == "reference" else None,
    )
    selection_bytes = reference_plan_bytes(reference_plan)
    artifacts: dict[str, bytes] = {"logs/reference-selection.json": selection_bytes}
    jobs: list[dict[str, object]] = []
    batches: list[dict[str, object]] = []

    existing_jobs = (
        cast(dict[str, dict[str, object]], existing["jobs"]) if existing is not None else {}
    )
    existing_effective = (
        {
            cast(str, item["job_id"]): item
            for item in cast(list[dict[str, object]], existing["effective_jobs"])
        }
        if existing is not None
        else {}
    )
    existing_batch_map = (
        cast(dict[str, object], existing["batch_map"]) if existing is not None else None
    )
    retained_bindings = _retained_attempt_bindings(project_dir)
    if phase == "reference":
        for identifier, _canonical, prompt_path, prompt_bytes in reference_candidates:
            brief: dict[str, object] = {
                "batch_id": "references-001",
                "prompt_path": prompt_path,
                "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                "references": [],
                "requested_aspect_ratio": None,
                "requested_dimensions": None,
                "retry_limit": 2,
                "subject_id": identifier,
                "subject_kind": "reference",
            }
            prepared = _prepared_job_for_brief(existing_jobs, brief)
            job = (
                prepared
                if prepared is not None
                else build_generation_job(
                    subject_kind="reference",
                    subject_id=identifier,
                    prompt_path=prompt_path,
                    prompt_sha256=cast(str, brief["prompt_sha256"]),
                    references=[],
                    requested_dimensions=None,
                    requested_aspect_ratio=None,
                    attempt_kind="initial",
                    retry_limit=2,
                    batch_id="references-001",
                    target_path=_next_reference_attempt_target(
                        project_dir, identifier, retained_bindings
                    ),
                )
            )
            jobs.append(job)
        batches.append(
            {
                "batch_id": "references-001",
                "job_ids": [cast(str, job["job_id"]) for job in jobs],
                "kind": "reference",
            }
        )
    else:
        if existing_batch_map is not None:
            for batch in cast(list[dict[str, object]], existing_batch_map["batches"]):
                if batch.get("kind") == "reference":
                    batches.append(dict(batch))
                    for job_id in cast(list[str], batch["job_ids"]):
                        jobs.append(existing_jobs[job_id])
        retry_limit = cast(dict[str, object], project_manifest["settings"]).get("max_panel_retries")
        if isinstance(retry_limit, bool) or not isinstance(retry_limit, int):
            raise HandoffContractError(["settings.max_panel_retries: invalid retry budget"])
        plan_by_panel = {
            cast(str, panel["panel_id"]): panel
            for panel in cast(list[dict[str, object]], reference_plan["panels"])
        }
        for page_index, page in enumerate(pages, 1):
            if not isinstance(page, dict) or not isinstance(page.get("panels"), list):
                continue
            batch_id = f"panels-{page_index:03d}"
            page_jobs: list[dict[str, object]] = []
            for panel in page["panels"]:
                if not isinstance(panel, dict) or not isinstance(panel.get("id"), str):
                    continue
                panel_id = cast(str, panel["id"])
                prompt_path, prompt_bytes = panel_prompts[panel_id]
                selected = plan_by_panel[panel_id].get("selected")
                references: list[dict[str, object]] = []
                for selected_item in cast(list[dict[str, object]], selected):
                    relative = cast(str, selected_item["path"])
                    payload = read_contained_bytes(
                        project_dir, relative, max_bytes=MAX_ENCODED_RASTER_BYTES
                    )
                    _verify_raster(
                        contained_project_path(project_dir, relative, must_exist=True),
                        expected_format="PNG",
                    )
                    references.append(
                        {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
                    )
                scene_id = panel.get("scene_id")
                if isinstance(scene_id, str):
                    scene_relative = f"references/scenes/{scene_id}.png"
                    scene_path = contained_project_path(project_dir, scene_relative)
                    if scene_path.exists():
                        scene_payload = read_contained_bytes(
                            project_dir,
                            scene_relative,
                            max_bytes=MAX_ENCODED_RASTER_BYTES,
                        )
                        _verify_raster(scene_path, expected_format="PNG")
                        if scene_relative not in {item["path"] for item in references}:
                            references.append(
                                {
                                    "path": scene_relative,
                                    "sha256": hashlib.sha256(scene_payload).hexdigest(),
                                }
                            )
                rect = panel.get("rect")
                if not isinstance(rect, dict):
                    raise HandoffContractError([f"panel {panel_id}: rectangle is missing"])
                width = rect.get("width")
                height = rect.get("height")
                if (
                    isinstance(width, bool)
                    or not isinstance(width, int)
                    or isinstance(height, bool)
                    or not isinstance(height, int)
                    or width <= 0
                    or height <= 0
                ):
                    raise HandoffContractError([f"panel {panel_id}: rectangle is invalid"])
                divisor = math.gcd(width, height)
                brief = {
                    "batch_id": batch_id,
                    "prompt_path": prompt_path,
                    "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                    "references": references,
                    "requested_aspect_ratio": f"{width // divisor}:{height // divisor}",
                    "requested_dimensions": {"width": width, "height": height},
                    "retry_limit": retry_limit,
                    "subject_id": panel_id,
                    "subject_kind": "panel",
                }
                prepared = _prepared_job_for_brief(existing_jobs, brief)
                if prepared is None:
                    attempt_kind, sequence = _next_panel_attempt_identity(project_dir, panel_id)
                    job = build_generation_job(
                        subject_kind="panel",
                        subject_id=panel_id,
                        prompt_path=prompt_path,
                        prompt_sha256=cast(str, brief["prompt_sha256"]),
                        references=references,
                        requested_dimensions={"width": width, "height": height},
                        requested_aspect_ratio=cast(str, brief["requested_aspect_ratio"]),
                        attempt_kind=attempt_kind,
                        retry_limit=retry_limit,
                        batch_id=batch_id,
                        target_path=(
                            f"panels/attempts/{panel_id}/{attempt_kind}-{sequence:03d}.png"
                        ),
                    )
                else:
                    job = prepared
                jobs.append(job)
                page_jobs.append(job)
            batches.append(
                {
                    "batch_id": batch_id,
                    "job_ids": [cast(str, job["job_id"]) for job in page_jobs],
                    "kind": "panel",
                }
            )

    batch_map = build_generation_batches(batches)
    batch_bytes = canonical_artifact_bytes(batch_map)
    artifacts[BATCHES_PATH] = batch_bytes
    for job in jobs:
        relative = f"generation/jobs/{job['job_id']}.json"
        target = contained_project_path(project_dir, relative)
        target_path = cast(str, job["target_path"])
        retained = contained_project_path(project_dir, target_path)
        managed = cast(str, job["job_id"]) in existing_jobs
        if retained.exists() and not managed:
            raise HandoffContractError([f"{target_path}: unmanaged retained target collision"])
        if not managed:
            retry_limit = cast(int, job["retry_limit"])
            for ordinal in range(1, retry_limit + 2):
                receipt_id = generation_attempt_id(
                    job_id=cast(str, job["job_id"]),
                    attempt=ordinal,
                )
                receipt_relative = f"generation/receipts/{receipt_id}.json"
                receipt_path = contained_project_path(project_dir, receipt_relative)
                if receipt_path.exists() or receipt_path.is_symlink():
                    raise HandoffContractError([f"{receipt_relative}: unmanaged receipt collision"])
        artifacts[relative] = canonical_artifact_bytes(job)

    scope_content: dict[str, bytes] = {
        BATCHES_PATH: batch_bytes,
        "logs/reference-selection.json": selection_bytes,
    }
    for relative in LOCKED_SCOPE_FIXED_PATHS:
        if relative not in scope_content:
            scope_content[relative] = read_contained_bytes(project_dir, relative)
    for _identifier, (relative, payload) in sorted(reference_prompts.items()):
        scope_content[relative] = payload
    for _identifier, (relative, payload) in sorted(panel_prompts.items()):
        scope_content[relative] = payload
    for relative in sorted(active_reference_paths):
        scope_content[relative] = read_contained_bytes(
            project_dir,
            relative,
            max_bytes=MAX_ENCODED_RASTER_BYTES,
        )
    for job in jobs:
        for reference in cast(list[dict[str, object]], job["references"]):
            relative = cast(str, reference["path"])
            scope_content[relative] = read_contained_bytes(
                project_dir, relative, max_bytes=MAX_ENCODED_RASTER_BYTES
            )
    locked_scope = locked_scope_sha256_from_content(scope_content)

    descriptor_jobs: list[dict[str, object]] = []
    for job in jobs:
        job_id = cast(str, job["job_id"])
        status = "ready"
        if job_id in existing_effective:
            status = cast(str, existing_effective[job_id]["status"])
        descriptor_jobs.append(
            {
                "job_id": job_id,
                "path": f"generation/jobs/{job_id}.json",
                "sha256": generation_job_sha256(job),
                "status": status,
            }
        )
    required_content = dict(scope_content)
    required_content.pop(BATCHES_PATH, None)
    required_content[IDENTITY_PACK_PATH] = read_contained_bytes(
        project_dir, IDENTITY_PACK_PATH, max_bytes=MAX_JSON_BYTES
    )
    required_artifacts = [
        {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
        for relative, payload in sorted(required_content.items())
    ]
    handoff_manifest = build_handoff_manifest(
        project_id=cast(str, project_manifest["project_id"]),
        project_schema_version=CURRENT_PROJECT_SCHEMA_VERSION,
        stage=cast(str, project_manifest["status"]),
        locked_scope_sha256=locked_scope,
        batches_path=BATCHES_PATH,
        batches_sha256=hashlib.sha256(batch_bytes).hexdigest(),
        jobs=descriptor_jobs,
        required_artifacts=required_artifacts,
    )
    artifacts[HANDOFF_MANIFEST_PATH] = canonical_artifact_bytes(handoff_manifest)
    project_manifest["handoff"] = {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "locked_scope_sha256": locked_scope,
        "manifest_path": HANDOFF_MANIFEST_PATH,
    }
    artifacts["project.json"] = canonical_artifact_bytes(project_manifest)
    return phase, artifacts, handoff_manifest, descriptor_jobs


def prepare_handoff(project_dir: Path) -> dict[str, object]:
    """Prepare deterministic reference or panel work in one project transaction."""
    project_dir = Path(project_dir)
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        with ProjectTransaction(project_dir, "handoff-prepare") as transaction:
            original = read_project_manifest(project_dir / "project.json", normalize_legacy=False)
            source_version = original.get("schema_version", LEGACY_PROJECT_SCHEMA_VERSION)
            migrated = source_version != CURRENT_PROJECT_SCHEMA_VERSION
            project_manifest = (
                migrate_project_manifest_in_memory(original) if migrated else dict(original)
            )
            existing = _prepared_handoff_snapshot(project_dir, project_manifest)
            phase, artifacts, handoff_manifest, descriptors = _build_handoff_content(
                project_dir,
                project_manifest,
                existing,
            )
            changed_paths = []
            for relative, payload in artifacts.items():
                try:
                    current = read_contained_bytes(project_dir, relative)
                except FileNotFoundError:
                    current = None
                if current != payload:
                    changed_paths.append(relative)
            if existing is not None and existing.get("phase") == phase and changed_paths:
                raise StaleLockedScopeError(
                    ["prepared handoff differs from the complete intended state"]
                )
            if not changed_paths:
                jobs = cast(list[dict[str, object]], existing["effective_jobs"])
                return {
                    "batch_count": len(
                        cast(
                            list[object], cast(dict[str, object], existing["batch_map"])["batches"]
                        )
                    ),
                    "changed": False,
                    "job_counts": _handoff_job_counts(jobs),
                    "locked_scope_sha256": handoff_manifest["locked_scope_sha256"],
                    "manifest_path": HANDOFF_MANIFEST_PATH,
                    "migrated": False,
                    "next_action": _handoff_next_action(phase, jobs),
                    "phase": phase,
                    "project_id": project_manifest["project_id"],
                }
            for relative in changed_paths:
                if relative == "project.json":
                    continue
                transaction.stage_bytes(relative, artifacts[relative])
            transaction.append_bytes(
                "logs/events.jsonl",
                canonical_event_record(
                    "handoff.prepared",
                    {
                        "count": len(descriptors),
                        "kind": phase,
                        "project_id": cast(str, project_manifest["project_id"]),
                    },
                ),
                repair_torn_jsonl=True,
            )
            transaction.stage_bytes("project.json", artifacts["project.json"])
    jobs = [
        {
            "status": descriptor["status"],
        }
        for descriptor in descriptors
    ]
    return {
        "batch_count": len(
            cast(list[object], _read_handoff_json(project_dir, BATCHES_PATH)["batches"])
        ),
        "changed": True,
        "job_counts": _handoff_job_counts(jobs),
        "locked_scope_sha256": handoff_manifest["locked_scope_sha256"],
        "manifest_path": HANDOFF_MANIFEST_PATH,
        "migrated": migrated,
        "next_action": _handoff_next_action(phase, jobs),
        "phase": phase,
        "project_id": project_manifest["project_id"],
    }


def _handoff_job_counts(jobs: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts = {status: 0 for status in ("missing", "ready", "completed", "failed", "stale")}
    for job in jobs:
        status = job.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
    return counts


def _handoff_job_state(
    snapshot: Mapping[str, object], job_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    jobs = cast(dict[str, dict[str, object]], snapshot["jobs"])
    job = jobs.get(job_id)
    if job is None:
        raise HandoffResultError(["job_id: does not name a current handoff job"])
    state = next(
        (
            item
            for item in cast(list[dict[str, object]], snapshot["effective_jobs"])
            if item.get("job_id") == job_id
        ),
        None,
    )
    if state is None:
        raise HandoffResultError(["job_id: effective handoff state is missing"])
    if snapshot.get("scope_state") != "current" or state.get("status") == "stale":
        raise StaleLockedScopeError(["job_id: job or locked scope is stale"])
    return job, state


def _validate_handoff_raster(
    payload: bytes,
    job: Mapping[str, object],
) -> tuple[int, int]:
    if not payload:
        raise HandoffResultError(["result raster: must not be empty"])
    target_path = job.get("target_path")
    if not isinstance(target_path, str) or not target_path.endswith(".png"):
        raise HandoffResultError(["result raster: job target format is unsupported"])
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != "PNG":
                    raise HandoffResultError(["result raster: format must match PNG target"])
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise InputResourceLimitError(
                        f"the decoded raster pixel limit of {MAX_DECODED_PIXELS} pixels"
                    )
                image.load()
                width, height = image.size
    except HandoffResultError:
        raise
    except InputResourceLimitError:
        raise
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise HandoffResultError(["result raster: must be a readable PNG"]) from error
    if width < MIN_RASTER_DIMENSION or height < MIN_RASTER_DIMENSION:
        raise HandoffResultError(
            [f"result raster: dimensions must both be at least {MIN_RASTER_DIMENSION}px"]
        )
    dimensions = job.get("requested_dimensions")
    if isinstance(dimensions, Mapping):
        if (width, height) != (dimensions.get("width"), dimensions.get("height")):
            raise HandoffResultError(["result raster: dimensions do not match the job"])
    aspect_ratio = job.get("requested_aspect_ratio")
    if isinstance(aspect_ratio, str):
        ratio_width, ratio_height = map(int, aspect_ratio.split(":"))
        if width * ratio_height != height * ratio_width:
            raise HandoffResultError(["result raster: aspect ratio does not match the job"])
    return width, height


def _reference_canonical_path(project_dir: Path, subject_id: str) -> str:
    characters = _read_handoff_json(project_dir, "plan/character-bible.json").get("characters")
    character_ids = (
        {
            item.get("id")
            for item in characters
            if isinstance(characters, list) and isinstance(item, dict)
        }
        if isinstance(characters, list)
        else set()
    )
    scene_ids = _story_scene_ids(project_dir)
    if subject_id in character_ids and subject_id in scene_ids:
        raise HandoffResultError(["reference job identity is character/scene ambiguous"])
    if subject_id in character_ids:
        return f"references/characters/{subject_id}.png"
    if subject_id in scene_ids:
        return f"references/scenes/{subject_id}.png"
    raise HandoffResultError(["reference job identity is not present in project planning"])


def _updated_handoff_manifest(
    snapshot: Mapping[str, object],
    job_id: str,
    status: str,
) -> dict[str, object]:
    manifest = deepcopy(cast(dict[str, object], snapshot["handoff_manifest"]))
    descriptors = manifest.get("jobs")
    if not isinstance(descriptors, list):
        raise HandoffContractError(["handoff manifest jobs are malformed"])
    found = False
    for descriptor in descriptors:
        if isinstance(descriptor, dict) and descriptor.get("job_id") == job_id:
            descriptor["status"] = status
            found = True
    if not found:
        raise HandoffContractError(["job_id: descriptor is missing"])
    issues = validate_handoff_manifest(manifest)
    if issues:
        raise HandoffContractError(issues)
    return manifest


def _existing_attempt_receipt(
    snapshot: Mapping[str, object], candidate_attempt_id: str
) -> dict[str, object] | None:
    return next(
        (
            receipt
            for receipt in cast(list[dict[str, object]], snapshot["receipts"])
            if receipt.get("attempt_id") == candidate_attempt_id
        ),
        None,
    )


def _duplicate_success_result(
    project_dir: Path,
    job: Mapping[str, object],
    receipt: Mapping[str, object],
    state: Mapping[str, object],
) -> dict[str, object]:
    raster_path = receipt.get("raster_path")
    raster_sha256 = receipt.get("raster_sha256")
    if not isinstance(raster_path, str) or not isinstance(raster_sha256, str):
        raise HandoffResultError(["duplicate success receipt is rasterless"])
    retained = read_contained_bytes(project_dir, raster_path, max_bytes=MAX_ENCODED_RASTER_BYTES)
    if hashlib.sha256(retained).hexdigest() != raster_sha256:
        raise HandoffResultError(["duplicate success raster binding is stale"])
    activated_path: str | None = None
    activated_sha256: str | None = None
    if job.get("subject_kind") == "reference":
        activated_path = _reference_canonical_path(project_dir, cast(str, job["subject_id"]))
        activated = read_contained_bytes(
            project_dir, activated_path, max_bytes=MAX_ENCODED_RASTER_BYTES
        )
        if activated != retained:
            raise HandoffResultError(["duplicate reference activation is stale"])
        activated_sha256 = raster_sha256
    counters = (
        _read_generation_counters(project_dir) if job.get("subject_kind") == "panel" else None
    )
    return {
        "activated_reference_path": activated_path,
        "activated_reference_sha256": activated_sha256,
        "attempt_id": receipt["attempt_id"],
        "counters": counters,
        "duplicate": True,
        "job_id": job["job_id"],
        "raster_path": raster_path,
        "raster_sha256": raster_sha256,
        "receipt_path": f"generation/receipts/{receipt['attempt_id']}.json",
        "status": state["status"],
    }


def accept_handoff_result(
    project_dir: Path,
    *,
    job_id: str,
    attempt: int,
    raster_path: Path,
    executor_kind: str,
    executor_id: str,
    provider: str | None = None,
    model: str | None = None,
    capabilities_used: Mapping[str, object] | None = None,
    approve_reference: bool = False,
) -> dict[str, object]:
    """Validate and atomically retain one successful executor result."""
    project_dir = Path(project_dir)
    capabilities = dict(
        capabilities_used
        or {"reference_images": False, "dimensions": False, "localized_edit": False}
    )
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        with ProjectTransaction(project_dir, "handoff-accept-result") as transaction:
            project_manifest = read_project_manifest(project_dir / "project.json")
            snapshot = _prepared_handoff_snapshot(project_dir, project_manifest)
            if snapshot is None:
                raise HandoffResultError(["handoff is not prepared"])
            job, state = _handoff_job_state(snapshot, job_id)
            is_reference = job.get("subject_kind") == "reference"
            if is_reference and not approve_reference:
                raise HandoffResultError(["reference result requires --approve-reference"])
            if not is_reference and approve_reference:
                raise HandoffResultError(["--approve-reference is valid only for reference jobs"])
            try:
                source_path = Path(raster_path).expanduser().absolute()
            except (OSError, RuntimeError) as error:
                raise HandoffResultError(
                    ["result raster path: cannot be expanded safely"]
                ) from error
            try:
                payload = read_bytes_nofollow(source_path, max_bytes=MAX_ENCODED_RASTER_BYTES)
            except InputResourceLimitError:
                raise
            except ValueError as error:
                if "symlink" in str(error).lower() or "reparse" in str(error).lower():
                    raise ValueError(
                        "security-error: result raster path must not contain symlinks"
                    ) from error
                raise HandoffResultError(["result raster: cannot be read safely"]) from error
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.EMLINK):
                    raise ValueError(
                        "security-error: result raster path must not contain symlinks"
                    ) from error
                raise HandoffResultError(["result raster: cannot be read safely"]) from error
            _validate_handoff_raster(payload, job)
            raster_sha256 = hashlib.sha256(payload).hexdigest()
            try:
                identifier = generation_attempt_id(job_id=job_id, attempt=attempt)
                receipt = build_generation_receipt(
                    attempt_id=identifier,
                    job_id=job_id,
                    job_sha256=generation_job_sha256(job),
                    raster_path=cast(str, job["target_path"]),
                    raster_sha256=raster_sha256,
                    executor_kind=executor_kind,
                    executor_id=executor_id,
                    provider=provider,
                    model=model,
                    capabilities_used=capabilities,
                    outcome="success",
                    category="accepted",
                )
            except HandoffContractError as error:
                raise HandoffResultError(error.issues) from error
            existing_receipt = _existing_attempt_receipt(snapshot, identifier)
            if existing_receipt is not None:
                if existing_receipt != receipt:
                    raise HandoffResultError([f"attempt {attempt}: conflicting duplicate receipt"])
                return _duplicate_success_result(project_dir, job, receipt, state)
            if state.get("status") == "completed":
                raise HandoffResultError(["job is completed and rejects another result"])
            if state.get("status") == "failed":
                raise HandoffResultError(["job retry budget is exhausted"])
            if state.get("next_attempt") != attempt:
                raise HandoffResultError(
                    [f"attempt ordinal must equal next attempt {state.get('next_attempt')}"]
                )
            target_path = cast(str, job["target_path"])
            if contained_project_path(project_dir, target_path).exists():
                raise HandoffResultError(["result target already exists without this receipt"])

            activated_path: str | None = None
            if is_reference:
                activated_path = _reference_canonical_path(
                    project_dir, cast(str, job["subject_id"])
                )
                if contained_project_path(project_dir, activated_path).exists():
                    if not _reference_can_reactivate(
                        project_dir,
                        cast(str, job["subject_id"]),
                        activated_path,
                        job,
                    ):
                        raise HandoffResultError(
                            ["canonical reference exists and must never be overwritten"]
                        )
                counters = None
            else:
                panel_id = cast(str, job["subject_id"])
                attempt_kind = cast(
                    Literal["initial", "visual_retry", "transient_repeat"],
                    job["attempt_kind"],
                )
                counter_name = _generation_counter_name(panel_id, attempt_kind)
                counter_document, counters, sequence = _advance_generation_counters(
                    project_dir, panel_id, attempt_kind, counter_name
                )
                expected_target = f"panels/attempts/{panel_id}/{attempt_kind}-{sequence:03d}.png"
                if target_path != expected_target:
                    raise HandoffResultError(
                        ["panel job target does not match the next retained counter path"]
                    )

            prior_receipts = [
                receipt_item
                for receipt_item in cast(list[dict[str, object]], snapshot["receipts"])
                if receipt_item.get("job_id") == job_id
            ]
            reconciled = reconcile_job_receipts(
                job=job,
                job_sha256=generation_job_sha256(job),
                receipts=[*prior_receipts, receipt],
                declared_status=cast(str, state["status"]),
                stale=False,
            )
            handoff_manifest = _updated_handoff_manifest(
                snapshot, job_id, cast(str, reconciled["status"])
            )
            transaction.stage_bytes(target_path, payload)
            if activated_path is not None:
                transaction.stage_bytes(activated_path, payload)
            receipt_relative = f"generation/receipts/{identifier}.json"
            transaction.stage_bytes(receipt_relative, canonical_artifact_bytes(receipt))
            if not is_reference:
                transaction.stage_bytes(
                    GENERATION_COUNTERS_PATH.as_posix(),
                    canonical_artifact_bytes(counter_document),
                )
            transaction.stage_bytes(
                HANDOFF_MANIFEST_PATH, canonical_artifact_bytes(handoff_manifest)
            )
            event_details: dict[str, object] = {
                "attempt_id": identifier,
                "attempt_path": target_path,
                "job_id": job_id,
                "kind": "reference" if is_reference else "panel",
                "raster_sha256": raster_sha256,
            }
            if activated_path is not None:
                event_details["activated_path"] = activated_path
                event_details["activated_sha256"] = raster_sha256
            transaction.append_bytes(
                "logs/events.jsonl",
                canonical_event_record("handoff.result-accepted", event_details),
                repair_torn_jsonl=True,
            )
    return {
        "activated_reference_path": activated_path,
        "activated_reference_sha256": raster_sha256 if activated_path is not None else None,
        "attempt_id": identifier,
        "counters": counters,
        "duplicate": False,
        "job_id": job_id,
        "raster_path": target_path,
        "raster_sha256": raster_sha256,
        "receipt_path": receipt_relative,
        "status": reconciled["status"],
    }


def record_handoff_failure(
    project_dir: Path,
    *,
    job_id: str,
    attempt: int,
    executor_kind: str,
    executor_id: str,
    category: str,
    provider: str | None = None,
    model: str | None = None,
    capabilities_used: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Atomically record one sanitized rasterless executor failure."""
    project_dir = Path(project_dir)
    capabilities = dict(
        capabilities_used
        or {"reference_images": False, "dimensions": False, "localized_edit": False}
    )
    with ProjectLock(project_dir):
        ProjectTransaction.recover(project_dir)
        with ProjectTransaction(project_dir, "handoff-record-failure") as transaction:
            project_manifest = read_project_manifest(project_dir / "project.json")
            snapshot = _prepared_handoff_snapshot(project_dir, project_manifest)
            if snapshot is None:
                raise HandoffResultError(["handoff is not prepared"])
            job, state = _handoff_job_state(snapshot, job_id)
            try:
                identifier = generation_attempt_id(job_id=job_id, attempt=attempt)
                receipt = build_generation_receipt(
                    attempt_id=identifier,
                    job_id=job_id,
                    job_sha256=generation_job_sha256(job),
                    raster_path=None,
                    raster_sha256=None,
                    executor_kind=executor_kind,
                    executor_id=executor_id,
                    provider=provider,
                    model=model,
                    capabilities_used=capabilities,
                    outcome="failure",
                    category=category,
                )
            except HandoffContractError as error:
                raise HandoffResultError(error.issues) from error
            existing_receipt = _existing_attempt_receipt(snapshot, identifier)
            if existing_receipt is not None:
                if existing_receipt != receipt:
                    raise HandoffResultError([f"attempt {attempt}: conflicting duplicate receipt"])
                return {
                    "attempt_id": identifier,
                    "attempts_remaining": state["attempts_remaining"],
                    "attempts_used": state["attempts_used"],
                    "category": category,
                    "duplicate": True,
                    "job_id": job_id,
                    "next_attempt": state["next_attempt"],
                    "receipt_path": f"generation/receipts/{identifier}.json",
                    "status": state["status"],
                }
            if state.get("status") == "completed":
                raise HandoffResultError(["job is completed and rejects a failure"])
            if state.get("status") == "failed":
                raise HandoffResultError(["job retry budget is exhausted"])
            if state.get("next_attempt") != attempt:
                raise HandoffResultError(
                    [f"attempt ordinal must equal next attempt {state.get('next_attempt')}"]
                )
            prior_receipts = [
                receipt_item
                for receipt_item in cast(list[dict[str, object]], snapshot["receipts"])
                if receipt_item.get("job_id") == job_id
            ]
            reconciled = reconcile_job_receipts(
                job=job,
                job_sha256=generation_job_sha256(job),
                receipts=[*prior_receipts, receipt],
                declared_status=cast(str, state["status"]),
                stale=False,
            )
            handoff_manifest = _updated_handoff_manifest(
                snapshot, job_id, cast(str, reconciled["status"])
            )
            receipt_relative = f"generation/receipts/{identifier}.json"
            transaction.stage_bytes(receipt_relative, canonical_artifact_bytes(receipt))
            transaction.stage_bytes(
                HANDOFF_MANIFEST_PATH, canonical_artifact_bytes(handoff_manifest)
            )
            transaction.append_bytes(
                "logs/events.jsonl",
                canonical_event_record(
                    "handoff.failure-recorded",
                    {
                        "attempt": attempt,
                        "attempt_id": identifier,
                        "category": category,
                        "job_id": job_id,
                        "kind": cast(str, job["subject_kind"]),
                    },
                ),
                repair_torn_jsonl=True,
            )
    return {
        "attempt_id": identifier,
        "attempts_remaining": reconciled["attempts_remaining"],
        "attempts_used": reconciled["attempts_used"],
        "category": category,
        "duplicate": False,
        "job_id": job_id,
        "next_attempt": reconciled["next_attempt"],
        "receipt_path": receipt_relative,
        "status": reconciled["status"],
    }


def _read_generation_counters(project_dir: Path) -> dict[str, object]:
    path = project_dir / GENERATION_COUNTERS_PATH
    if path.is_file():
        return read_json(path)
    return {"global_extra_calls": 0, "panels": {}, "schema_version": "1.0"}


def _generation_counter_name(panel_id: str, kind: str) -> str:
    if PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise ValueError("invalid panel ID")
    try:
        return GENERATION_COUNTER_NAMES[kind]
    except KeyError as error:
        raise ValueError("unknown generation attempt kind") from error


def _advance_generation_counters(
    project_dir: Path,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    counter_name: str,
) -> tuple[dict[str, object], dict[str, int], int]:
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
    for name in GENERATION_COUNTER_NAMES.values():
        value = panel.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("panel generation counters must be non-negative integers")
        panel[name] = value
    global_extras = counters.get("global_extra_calls", 0)
    if isinstance(global_extras, bool) or not isinstance(global_extras, int) or global_extras < 0:
        raise ValueError("global generation counter must be a non-negative integer")
    if panel[counter_name] >= GENERATION_LIMITS[kind]:
        raise ValueError(GENERATION_LIMIT_MESSAGES[kind])
    if kind != "initial" and global_extras >= 8:
        raise ValueError("at most eight extra calls are allowed per project")

    panel[counter_name] += 1
    if kind != "initial":
        global_extras += 1
        counters["global_extra_calls"] = global_extras
    counts = {
        "global_extra_calls": global_extras,
        "initial": panel["initial"],
        "transient_repeats": panel["transient_repeats"],
        "visual_retries": panel["visual_retries"],
    }
    return counters, counts, panel[counter_name]


def _stage_generation_accounting(
    transaction: ProjectTransaction,
    project_dir: Path,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    attempt_relative: Path,
    counters: dict[str, object],
) -> None:
    transaction.stage_bytes(GENERATION_COUNTERS_PATH.as_posix(), canonical_artifact_bytes(counters))
    transaction.append_bytes(
        "logs/events.jsonl",
        canonical_event_record(
            "generation.attempt-recorded",
            {
                "attempt_path": attempt_relative,
                "kind": kind,
                "panel_id": panel_id,
            },
        ),
        repair_torn_jsonl=True,
    )


def record_generation_attempt(
    project_dir: Path,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    attempt_path: Path,
) -> dict[str, int]:
    """Account for a retained image call while enforcing both retry budgets."""
    counter_name = _generation_counter_name(panel_id, kind)
    project_dir = Path(project_dir).resolve(strict=True)
    attempt = _contained_project_path(project_dir, Path(attempt_path))
    attempt_relative = attempt.relative_to(project_dir)
    with ProjectTransaction(project_dir, "record-generation-attempt") as transaction:
        attempt = contained_project_path(project_dir, attempt_relative, must_exist=True)
        if not attempt.is_file():
            raise ValueError("attempt path must be a retained file")
        _verify_raster(attempt)
        counters, counts, _ = _advance_generation_counters(
            project_dir, panel_id, kind, counter_name
        )
        _stage_generation_accounting(
            transaction, project_dir, panel_id, kind, attempt_relative, counters
        )
    return counts


def retain_generation_attempt(
    project_dir: Path,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    payload: bytes,
    media_type: str,
    width: int,
    height: int,
) -> dict[str, int]:
    """Validate and atomically retain one provider-generated raster attempt."""
    counter_name = _generation_counter_name(panel_id, kind)
    extension = _verify_raster_payload(payload, media_type, width, height)
    project_dir = Path(project_dir).resolve(strict=True)
    with ProjectTransaction(project_dir, "retain-generation-attempt") as transaction:
        counters, counts, sequence = _advance_generation_counters(
            project_dir, panel_id, kind, counter_name
        )
        attempt_relative = Path(f"panels/attempts/{panel_id}/{kind}-{sequence}.{extension}")
        if contained_project_path(project_dir, attempt_relative).exists():
            raise ValueError("generation attempt destination already exists")
        transaction.stage_bytes(attempt_relative.as_posix(), payload)
        _stage_generation_accounting(
            transaction, project_dir, panel_id, kind, attempt_relative, counters
        )
    return counts


def _verify_raster_payload(payload: bytes, media_type: str, width: int, height: int) -> str:
    formats = {
        "image/png": ("PNG", "png"),
        "image/jpeg": ("JPEG", "jpg"),
        "image/webp": ("WEBP", "webp"),
    }
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("attempt must be a readable raster")
    if len(payload) > MAX_ENCODED_RASTER_BYTES:
        raise InputResourceLimitError(
            f"the encoded raster size limit of {MAX_ENCODED_RASTER_BYTES} bytes"
        )
    try:
        expected_format, extension = formats[media_type]
    except (KeyError, TypeError) as error:
        raise ValueError("unsupported raster media type") from error
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != expected_format:
                    raise ValueError("result media type does not match raster")
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise ValueError("attempt exceeds the decoded pixel limit")
                image.load()
                if image.width < MIN_RASTER_DIMENSION or image.height < MIN_RASTER_DIMENSION:
                    raise ValueError(
                        f"attempt must be a readable raster at least {MIN_RASTER_DIMENSION}px"
                    )
                actual_size = image.size
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("attempt must be a readable raster") from error
    if actual_size != (width, height):
        raise ValueError("result dimensions do not match raster")
    return extension


def _verify_raster(
    path: Path,
    *,
    expected_format: str | None = None,
) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            payload = read_bytes_nofollow(path, max_bytes=MAX_ENCODED_RASTER_BYTES)
            with Image.open(io.BytesIO(payload)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("attempt must be a readable raster")
                if expected_format is not None and image.format != expected_format:
                    raise ValueError(f"raster format must match {expected_format}")
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise ValueError("attempt exceeds the decoded pixel limit")
                image.load()
                if image.width < MIN_RASTER_DIMENSION or image.height < MIN_RASTER_DIMENSION:
                    raise ValueError(
                        f"attempt must be a readable raster at least {MIN_RASTER_DIMENSION}px"
                    )
                return image.width, image.height
    except InputResourceLimitError:
        raise
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("attempt must be a readable raster") from error


def _replacement_problem(project_dir: Path, panel_id: str) -> str | None:
    """Report why an accepted panel raster may not be replaced yet.

    Replacing an accepted raster while its QA record still accepts the panel
    would silently invalidate every hash and dimension the record binds, so the
    reviewed panel and the file on disk would disagree with nothing recording
    which one the reader should trust. A repair therefore starts from a record
    that already asked for one: the accepted bytes stay in place until the review
    faults them.

    Only a review that was never written permits replacement, which is what
    initial generation and transient repeats need. A record that exists but
    cannot be resolved, read, or understood is not evidence that anything faulted
    the panel, so it refuses the replacement instead of being read as an absent
    review. The caller runs this inside the promotion transaction, so the record
    is read under the same lock that publishes the replacement.
    """
    record_relative = f"qa/panels/{panel_id}.json"
    try:
        record_path = contained_project_path(project_dir, record_relative)
    except ValueError as error:
        return f"panel QA record path is refused: {record_relative}: {error}"
    try:
        record_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"panel QA record cannot be inspected: {record_relative}: {type(error).__name__}"
    try:
        record = read_json(record_path)
    except InputResourceLimitError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return f"panel QA record cannot be read: {record_relative}: {type(error).__name__}"
    decision = record.get("decision")
    if decision in ACCEPTED_DECISIONS:
        return (
            "accepted panel QA record does not require repair; re-review "
            f"{record_relative} before replacing the accepted raster"
        )
    if decision not in REPAIR_DECISIONS:
        return f"panel QA record has no recognized quality decision: {record_relative}"
    return None


def _recorded_repair_strategy(project_dir: Path, panel_id: str) -> str | None:
    """Return the repair strategy recorded for one panel, when a plan exists."""
    entry = recorded_panel_plan(project_dir, panel_id)
    if entry is None:
        return None
    strategy = entry.get("strategy")
    return strategy if strategy in REPAIR_STRATEGIES else None


def promote_attempt(project_dir: Path, panel_id: str, attempt_path: Path) -> Path:
    """Verify and atomically copy one retained attempt into the accepted raw slot."""
    if PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise ValueError("invalid panel ID")
    project_dir = Path(project_dir)
    attempt_relative = Path(attempt_path)
    attempt = _contained_project_path(project_dir, attempt_relative)
    if not attempt.is_file():
        raise ValueError("attempt path must be a retained file")
    if attempt_relative.is_absolute():
        attempt_relative = attempt.relative_to(project_dir.resolve(strict=True))
    destination_relative = Path(f"panels/raw/{panel_id}.png")
    destination = project_dir / destination_relative
    event_details = {"attempt_path": attempt_relative, "panel_id": panel_id}
    replaced = False
    with ProjectTransaction(project_dir, "promote-attempt") as transaction:
        attempt = contained_project_path(project_dir, attempt_relative, must_exist=True)
        if not attempt.is_file():
            raise ValueError("attempt path must be a retained file")
        _verify_raster(attempt)
        new_bytes = read_contained_bytes(project_dir, attempt_relative)
        if destination.is_file():
            replaced = True
            old_bytes = read_contained_bytes(project_dir, destination_relative)
            if hashlib.sha256(old_bytes).hexdigest() == hashlib.sha256(new_bytes).hexdigest():
                return destination
            # The repaired attempt has passed raster verification above; the
            # accepted bytes are only touched once the review has faulted them.
            problem = _replacement_problem(project_dir, panel_id)
            if problem is not None:
                raise ValueError(problem)
            strategy = _recorded_repair_strategy(project_dir, panel_id)
            if strategy is not None:
                event_details["strategy"] = strategy
            number = 1
            while True:
                archive_relative = Path(f"panels/raw/{panel_id}.attempt-{number}.png")
                try:
                    archive = contained_project_path(project_dir, archive_relative)
                    available = not archive.exists()
                except ValueError:
                    available = False
                if (
                    available
                    and archive_relative != attempt_relative
                    and archive_relative != destination_relative
                ):
                    transaction.stage_bytes(archive_relative.as_posix(), old_bytes)
                    break
                number += 1
        transaction.stage_bytes(destination_relative.as_posix(), new_bytes)
        events = canonical_event_record("generation.attempt-promoted", event_details)
        if replaced:
            events += canonical_event_record(
                "artifact.regenerated",
                {
                    "artifact_path": destination_relative.as_posix(),
                    "reused": False,
                },
            )
        transaction.append_bytes("logs/events.jsonl", events, repair_torn_jsonl=True)
    return destination


def record_override(project_dir: Path, panel_id: str, reason: str) -> None:
    """Downgrade an overridable visual QA failure to a recorded warning."""
    if PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise ValueError("invalid panel ID")
    validate_narrative(
        reason,
        message=OVERRIDE_REASON_LIMIT_MESSAGE,
        max_chars=MAX_OVERRIDE_REASON_CHARS,
    )
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
    is_v2 = record.get("schema_version") == "2.0"
    record_panel_id = record.get("subject_id") if is_v2 else record.get("panel_id")
    if record_panel_id != panel_id:
        raise ValueError("panel QA record does not match the requested panel")
    if not is_v2:
        category = record.get("failure_category")
        if category in {"corrupt", "corrupt_image", "safety", "safety_refusal"}:
            raise ValueError(f"{category} cannot be overridden")
        if category != "visual_qa":
            raise ValueError("only non-safety visual QA errors can be overridden")
    checks = record.get("checks")
    failed_checks = (
        [
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("result") == "fail"
            and check.get("severity") == "error"
        ]
        if isinstance(checks, list)
        else []
    )
    if not failed_checks:
        raise ValueError("override requires an error-level failed check")
    if record.get("decision") != "regenerate":
        raise ValueError("overridable visual QA errors must require regeneration")
    if is_v2:
        problem = _accepted_panel_problem(project_dir, record)
        if problem is not None:
            raise ValueError(f"panel artifacts cannot be overridden: {problem}")
    else:
        raw_path = record.get("raw_path")
        clean_path = record.get("clean_path")
        if raw_path != f"panels/raw/{panel_id}.png" or clean_path != f"panels/clean/{panel_id}.png":
            raise ValueError("corrupt images cannot be overridden")
        try:
            raw = _contained_project_path(project_dir, Path(raw_path))
            clean = _contained_project_path(project_dir, Path(clean_path))
            raw_size = _verify_raster(raw)
            clean_size = _verify_raster(clean)
            dimensions = record.get("dimensions")
            recorded_size = (
                (dimensions.get("width"), dimensions.get("height"))
                if isinstance(dimensions, dict)
                else None
            )
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
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    manifest_warnings = manifest.get("warnings")
    if not isinstance(manifest_warnings, list):
        raise ValueError("manifest warnings must be an array")
    normalized_reason = reason.strip()
    if normalized_reason not in warnings:
        warnings.append(normalized_reason)
    for check in failed_checks:
        check["severity"] = "warning"
        if check.get("id") == "character-identity":
            regions = check.get("regions")
            if isinstance(regions, list):
                for region in regions:
                    if (
                        isinstance(region, dict)
                        and region.get("result") == "fail"
                        and region.get("severity") == "error"
                    ):
                        region["severity"] = "warning"
    record["decision"] = "accept-warning" if is_v2 else "accept_with_warnings"
    if not is_v2:
        record["retry_reason"] = None
    record["override_reason"] = normalized_reason

    if normalized_reason not in manifest_warnings:
        manifest_warnings.append(normalized_reason)
        manifest["updated_at"] = _utc_now()

    tx.stage_bytes(f"qa/panels/{panel_id}.json", canonical_artifact_bytes(record))
    tx.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    tx.append_bytes(
        "logs/events.jsonl",
        canonical_event_record("panel.overridden", {"panel_id": panel_id, "action": "accepted"}),
        repair_torn_jsonl=True,
    )


_IMAGE_CAPABILITY_FIELDS = {
    "status",
    "name",
    "supports_reference_images",
    "supports_dimensions",
}
_IMAGE_CAPABILITY_NAME = "agent-image-generation"


def _image_capability_diagnostic(
    observation: object | None,
) -> tuple[str, str, str, dict[str, object], str]:
    """Render one provider-neutral observation supplied by the active agent."""
    not_checked = {
        "status": "not_checked",
        "name": None,
        "supports_reference_images": False,
        "supports_dimensions": False,
    }
    if observation is None:
        return (
            "warn",
            "Image-generation capability must be inspected in the agent session.",
            "Enable an image-generation skill/tool, then resume the project when panels are needed.",
            {"readiness": "unknown", "capability": not_checked},
            "INFO image capability: inspect in agent session",
        )

    try:
        if not isinstance(observation, dict) or set(observation) != _IMAGE_CAPABILITY_FIELDS:
            raise ValueError("invalid capability observation fields")
        status = observation["status"]
        name = observation["name"]
        supports_references = observation["supports_reference_images"]
        supports_dimensions = observation["supports_dimensions"]
        if status not in {"available", "unavailable"}:
            raise ValueError("invalid capability status")
        if not isinstance(supports_references, bool) or not isinstance(supports_dimensions, bool):
            raise TypeError("capability feature flags must be boolean")
        if status == "available":
            if name != _IMAGE_CAPABILITY_NAME:
                raise ValueError("invalid neutral capability name")
        elif name is not None or supports_references or supports_dimensions:
            raise ValueError("unavailable capability cannot claim a name or features")
    except (KeyError, TypeError, ValueError):
        return (
            "warn",
            "Image-capability detection could not interpret the agent observation.",
            "Inspect the exposed tools again and rerun doctor without credentials or provider payloads.",
            {"readiness": "unknown", "capability": not_checked},
            "WARN image capability: detection failed; inspect exposed tools again",
        )

    capability = {
        "status": status,
        "name": name,
        "supports_reference_images": supports_references,
        "supports_dimensions": supports_dimensions,
    }
    if status == "unavailable":
        return (
            "warn",
            "No usable image-generation capability is exposed in this agent session.",
            "Enable an image-generation skill/tool that creates a local raster, then rerun doctor.",
            {"readiness": "missing", "capability": capability},
            "WARN image capability: no usable text-to-image tool returning a local raster",
        )
    if supports_references and supports_dimensions:
        return (
            "pass",
            f"Image-generation capability {name} is ready for Comic Sol.",
            "No action required.",
            {"readiness": "healthy", "capability": capability},
            f"PASS image capability: {name}; reference images and explicit dimensions supported",
        )

    missing = []
    if not supports_references:
        missing.append("reference images")
    if not supports_dimensions:
        missing.append("explicit dimensions")
    limitations = " and ".join(missing)
    return (
        "warn",
        f"Image-generation capability {name} is usable but lacks {limitations} support.",
        "Continue in degraded mode or enable a capability with the missing feature support.",
        {"readiness": "partial", "capability": capability},
        f"WARN image capability: {name} is usable but lacks {limitations} support",
    )


def doctor_report(
    output_root: Path,
    *,
    image_capability: object | None = None,
) -> dict[str, object]:
    """Return authoritative, actionable diagnostics for agents and humans."""
    checks: list[dict[str, object]] = []

    def add_check(
        check_id: str,
        status: str,
        message: str,
        remediation: str,
        **details: object,
    ) -> None:
        check: dict[str, object] = {
            "id": check_id,
            "status": status,
            "message": message,
            "remediation": remediation,
        }
        if details:
            check["details"] = details
        checks.append(check)

    if sys.version_info[:2] >= (3, 11):
        add_check(
            "runtime", "pass", f"Python 3.11+ ({sys.version.split()[0]})", "No action required."
        )
    else:
        add_check(
            "runtime",
            "fail",
            f"Python 3.11+ required; found {sys.version.split()[0]}",
            "Install Python 3.11 or newer and rerun doctor.",
        )

    try:
        import PIL

        if PIL.__version__ == REQUIRED_PILLOW:
            add_check("pillow", "pass", f"Pillow {REQUIRED_PILLOW}", "No action required.")
        else:
            add_check(
                "pillow",
                "fail",
                f"Pillow {REQUIRED_PILLOW} required; found {PIL.__version__}",
                f"Install the locked Pillow version: python -m pip install Pillow=={REQUIRED_PILLOW}.",
            )
    except Exception as error:
        add_check(
            "pillow",
            "fail",
            f"Pillow check failed: {type(error).__name__}: {error}",
            "Install the Comic Sol runtime again so its bundled Pillow dependency is restored.",
        )

    font_checks = (
        ("Comic Neue Regular", FONT_PATH_COMIC_REGULAR),
        ("Comic Neue Bold", FONT_PATH_COMIC_BOLD),
        ("Noto Sans fallback", FONT_PATH_FALLBACK),
    )
    font_failures: list[str] = []
    font_results: list[tuple[str, bool, str]] = []
    for label, path in font_checks:
        try:
            ImageFont.truetype(str(path), 42)
            font_results.append((label, True, f"font {label} loads at 42px"))
        except Exception as error:
            detail = f"font {label} at 42px: {type(error).__name__}: {error}"
            font_failures.append(detail)
            font_results.append((label, False, detail))
    if font_failures:
        add_check(
            "fonts",
            "fail",
            "Required bundled fonts could not be loaded.",
            "Reinstall Comic Sol or restore the bundled assets/fonts directory.",
            failures=font_failures,
        )
    else:
        add_check("fonts", "pass", "Bundled fonts load at 42px.", "No action required.")

    # Loading a face proves the file is readable, not that it still carries the
    # scripts lettering promises. This reads the bundled cmaps so a font swap that
    # quietly drops a script is reported here instead of at lettering time.
    try:
        from .font_coverage import (
            BUNDLED_COVERAGE_PROBES,
            BUNDLED_TARGET_SCRIPTS,
            missing_coverage_probes,
        )

        policy = {
            "regular": FONT_PATH_COMIC_REGULAR,
            "bold": FONT_PATH_COMIC_BOLD,
            "fallback": FONT_PATH_FALLBACK,
        }
        missing = missing_coverage_probes(policy)
        probe_total = sum(len(BUNDLED_COVERAGE_PROBES[s]) for s in BUNDLED_TARGET_SCRIPTS)
        if missing:
            failures = [
                f"{script} is missing "
                + ", ".join(f"U+{codepoint:04X}" for codepoint in codepoints[:8])
                + (" and more" if len(codepoints) > 8 else "")
                for script, codepoints in sorted(missing.items())
            ]
            add_check(
                "typography",
                "fail",
                "Bundled fonts no longer letter required scripts: " + ", ".join(sorted(missing)),
                "Restore the bundled assets/fonts directory so the documented scripts letter again.",
                failures=failures,
            )
        else:
            add_check(
                "typography",
                "pass",
                f"Bundled fonts map all {probe_total} required "
                f"{'/'.join(BUNDLED_TARGET_SCRIPTS)} codepoints.",
                "No action required.",
            )
    except Exception as error:
        add_check(
            "typography",
            "fail",
            f"Typography coverage inventory failed: {type(error).__name__}: {error}",
            "Restore the bundled assets/fonts directory and rerun doctor.",
        )

    template_names = (
        "manifest.json",
        "character-bible.json",
        "story-plan.json",
        "storyboard.json",
        "panel-record.json",
        "qa-report.md.tmpl",
    )
    json_template_names = set(template_names) - {"qa-report.md.tmpl"}
    missing_templates = [name for name in template_names if not (TEMPLATES / name).is_file()]
    invalid_templates: list[str] = []
    for name in sorted(json_template_names - set(missing_templates)):
        try:
            if not read_json(TEMPLATES / name):
                raise ValueError("template object is empty")
        except Exception as error:
            invalid_templates.append(f"{name} ({type(error).__name__})")
    if missing_templates or invalid_templates:
        failures = missing_templates + invalid_templates
        add_check(
            "templates",
            "fail",
            f"Templates missing or invalid: {', '.join(failures)}",
            "Reinstall Comic Sol so its bundled templates are restored.",
            missing=missing_templates,
            invalid=invalid_templates,
        )
    else:
        add_check("templates", "pass", "Bundled templates available.", "No action required.")

    available_starters, invalid_starters = inventory_starters(
        TEMPLATES,
        request_validator=validate_request_settings,
    )
    if invalid_starters:
        add_check(
            "starter-templates",
            "fail",
            "Starter templates missing or invalid: " + ", ".join(invalid_starters),
            "Reinstall Comic Sol so its versioned starter bundles are restored.",
            available=available_starters,
            invalid=invalid_starters,
        )
    else:
        add_check(
            "starter-templates",
            "pass",
            f"{len(available_starters)} versioned starter templates available.",
            "No action required.",
            available=available_starters,
            version="v1",
        )

    reference_root = ROOT / "references"
    if not reference_root.is_dir():
        reference_root = ROOT / "skill" / "references"
    missing_references = [
        name
        for name in ("workflow.md", "schemas.md", "starter-templates.md", "visual-qa.md")
        if not (reference_root / name).is_file()
    ]
    if missing_references:
        add_check(
            "references",
            "fail",
            f"References missing: {', '.join(missing_references)}",
            "Reinstall Comic Sol or restore its bundled references.",
            missing=missing_references,
        )
    else:
        add_check(
            "references", "pass", "Bundled workflow references available.", "No action required."
        )

    output_root = Path(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_root, prefix=".doctor-", delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        add_check(
            "output-root",
            "pass",
            f"output root writable: {output_root.resolve()}",
            "No action required.",
        )
    except OSError as error:
        add_check(
            "output-root",
            "fail",
            f"Output root is not writable: {type(error).__name__}: {error}",
            "Choose a writable project directory with --output-root or fix its permissions.",
        )

    try:
        try:
            server_module = importlib.import_module("mcp.server.fastmcp")
            exceptions_module = importlib.import_module("mcp.server.fastmcp.exceptions")
            api_name = "FastMCP"
        except ModuleNotFoundError:
            server_module = importlib.import_module("mcp.server.mcpserver")
            exceptions_module = importlib.import_module("mcp.server.mcpserver.exceptions")
            api_name = "MCPServer"
        if not hasattr(server_module, api_name) or not hasattr(exceptions_module, "ToolError"):
            raise ImportError("required MCP server APIs are missing")
    except ModuleNotFoundError:
        add_check(
            "mcp",
            "warn",
            "MCP support is unavailable in this environment.",
            "Install the optional MCP extra with: python -m pip install 'comic-sol[mcp]'.",
        )
    except Exception as error:
        add_check(
            "mcp",
            "warn",
            f"MCP support is installed but unusable ({type(error).__name__}).",
            "Reinstall the optional MCP extra and rerun doctor.",
        )
    else:
        add_check(
            "mcp",
            "pass",
            "MCP support is installed and its server APIs are importable.",
            "No action required.",
        )
    try:
        (
            capability_status,
            capability_message,
            capability_remediation,
            details,
            human_message,
        ) = _image_capability_diagnostic(image_capability)
    except Exception:
        capability_status = "warn"
        capability_message = "Image-capability detection failed safely."
        capability_remediation = "Inspect the exposed tools again and rerun doctor without credentials or provider payloads."
        details = {
            "readiness": "unknown",
            "capability": {
                "status": "not_checked",
                "name": None,
                "supports_reference_images": False,
                "supports_dimensions": False,
            },
        }
        human_message = "WARN image capability: detection failed; inspect exposed tools again"
    add_check(
        "image-capability",
        capability_status,
        capability_message,
        capability_remediation,
        **details,
    )

    ready = not any(check["status"] == "fail" for check in checks)
    messages = [f"{'READY' if ready else 'NOT READY'} Comic Sol diagnostics"]
    runtime_check = next(check for check in checks if check["id"] == "runtime")
    if runtime_check["status"] == "pass":
        messages.append(f"PASS Python 3.11+ ({sys.version.split()[0]})")
    else:
        messages.append(f"FAIL Python 3.11+ required; found {sys.version.split()[0]}")
    pillow_check = next(check for check in checks if check["id"] == "pillow")
    messages.append(
        f"{'PASS' if pillow_check['status'] == 'pass' else 'FAIL'} {pillow_check['message']}"
    )
    for label, passed, detail in font_results:
        messages.append(f"{'PASS' if passed else 'FAIL'} {detail}")
    templates_check = next(check for check in checks if check["id"] == "templates")
    messages.append(
        "PASS templates available"
        if templates_check["status"] == "pass"
        else f"FAIL {templates_check['message']}"
    )
    starters_check = next(check for check in checks if check["id"] == "starter-templates")
    messages.append(
        "PASS starter templates available"
        if starters_check["status"] == "pass"
        else f"FAIL {starters_check['message']}"
    )
    references_check = next(check for check in checks if check["id"] == "references")
    messages.append(
        "PASS references available"
        if references_check["status"] == "pass"
        else f"FAIL {references_check['message']}"
    )
    output_check = next(check for check in checks if check["id"] == "output-root")
    messages.append(
        f"{'PASS' if output_check['status'] == 'pass' else 'FAIL'} {output_check['message']}"
    )
    messages.append(human_message)
    return {"ready": ready, "healthy": ready, "checks": checks, "messages": messages}


def doctor(
    output_root: Path,
    *,
    image_capability: object | None = None,
) -> tuple[bool, list[str]]:
    """Compatibility adapter for the original tuple-based doctor API."""
    report = (
        doctor_report(output_root)
        if image_capability is None
        else doctor_report(output_root, image_capability=image_capability)
    )
    return bool(report["ready"]), list(cast(list[str], report["messages"]))


def finalize_project(
    project_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Serialize one complete deterministic finalization workflow."""
    caller_project_dir = Path(project_dir)
    project_dir = caller_project_dir.resolve(strict=True)
    with ProjectLock(project_dir, timeout=PROJECT_OPERATION_LOCK_TIMEOUT):
        ProjectTransaction.recover(project_dir)
        if progress is not None:
            progress(
                {
                    "status": "working",
                    "stage": "lettering",
                    "completed": [],
                    "remaining": ["composition", "export"],
                }
            )
        result = _finalize_project_locked(project_dir, caller_project_dir, progress)
        if progress is not None:
            progress(
                {
                    "status": "complete",
                    "stage": "export",
                    "completed": ["lettering", "composition", "export"],
                    "remaining": [],
                }
            )
        return result


def _finalize_project_locked(
    project_dir: Path,
    caller_project_dir: Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run all deterministic finalization steps and transition to terminal status.

    Order: lettering → composition → page-QA gate → guarded export →
    report → descriptor recording → export stage → terminal transition.
    Page-QA records are agent-produced; this function fails closed if they
    are absent or stale rather than fabricating visual evidence.
    """
    caller_project_dir = (
        Path(project_dir) if caller_project_dir is None else Path(caller_project_dir)
    )
    project_dir = Path(project_dir).resolve(strict=True)
    manifest_path = project_dir / "project.json"

    # 1. Determine stale stages from the resume plan.
    plan = build_resume_plan(caller_project_dir)
    stale = {a.stage for a in plan if a.artifact == "stage" and a.action in {"regenerate", "rerun"}}

    # 2. Lettering (if stale), advance status.
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    panel_ids = manifest.get("panels")
    if not isinstance(panel_ids, list) or not panel_ids:
        storyboard = read_json(project_dir / "plan/storyboard.json")
        panel_ids = [
            panel["id"]
            for panel in _storyboard_panels(storyboard)
            if isinstance(panel.get("id"), str)
        ]
    need_lettering = "lettering" in stale or not all(
        (project_dir / f"panels/{pid}/lettered.png").is_file() for pid in panel_ids
    )
    if need_lettering:
        from .letter_panels import letter_project

        letter_project(caller_project_dir)
        record_stage(caller_project_dir, "lettering")
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    if _allowed_transition(str(manifest.get("status")), "LETTERED"):
        transition(caller_project_dir, "LETTERED")

    if progress is not None:
        progress(
            {
                "status": "working",
                "stage": "composition",
                "completed": ["lettering"],
                "remaining": ["export"],
            }
        )

    # 3. Composition (if stale), advance status. compose_project writes
    #    cache/composition.json and its manifest descriptor.
    need_composition = (
        "composition" in stale or not (project_dir / "cache/composition.json").is_file()
    )
    if need_composition:
        from .compose_pages import compose_project

        compose_project(caller_project_dir)
        record_stage(caller_project_dir, "composition")
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    if _allowed_transition(str(manifest.get("status")), "COMPOSED"):
        transition(caller_project_dir, "COMPOSED")

    if progress is not None:
        progress(
            {
                "status": "working",
                "stage": "export",
                "completed": ["lettering", "composition"],
                "remaining": [],
            }
        )

    # 4. Fail closed on agent-produced page-QA integrity records.
    from .page_quality import validate_page_quality

    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
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
        page_rel = f"pages/page-{page_number:03d}.png"
        page_path = project_dir / page_rel
        if not page_path.is_file():
            raise ValueError(f"page_qa_required: {page_rel} is missing")
        page_issues = validate_page_quality(caller_project_dir, page_number)
        if page_issues:
            detail = "; ".join(f"{issue.field}: {issue.message}" for issue in page_issues)
            raise ValueError(f"page_qa_required: {qa_rel} is stale: {detail}")

    # 5. Guarded export (validates export-ready, writes PDF, records descriptor).
    from .export_pdf import guarded_export

    guarded_export(caller_project_dir)
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    if _allowed_transition(str(manifest.get("status")), "EXPORTED"):
        transition(caller_project_dir, "EXPORTED")

    # 6. Render the QA report, which projects the terminal status and records
    #    its own descriptor. Final validation requires both before the terminal
    #    transition, so the report cannot honestly be rendered afterwards.
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
    warnings = manifest.get("warnings")
    final_status = (
        "COMPLETE_WITH_WARNINGS" if isinstance(warnings, list) and warnings else "COMPLETE"
    )
    from .render_report import render_report

    render_report(caller_project_dir)

    # 7. render_report and compose_project record their own descriptors.

    # 8. Record export stage cache.
    record_stage(caller_project_dir, "export")

    # 9. Confirm the warning state still matches what the report projected.
    manifest = read_project_manifest(manifest_path, normalize_legacy=False)
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
        transition(caller_project_dir, final_status)

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
    init_parser.add_argument("--source", type=Path)
    init_parser.add_argument("--request-json", type=Path)
    init_parser.add_argument("--page-count", type=int, choices=range(1, 5))
    init_parser.add_argument("--starter", choices=STARTER_IDS)

    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("project_dir", type=Path)
    transition_parser.add_argument("target")
    transition_parser.add_argument("--warning")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("project_dir", type=Path)
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--output-root", type=Path, default=Path("comic-sol-output"))
    doctor_parser.add_argument("--image-capability-status", choices=("available", "unavailable"))
    doctor_parser.add_argument("--image-capability-name")
    doctor_parser.add_argument("--supports-reference-images", action="store_true")
    doctor_parser.add_argument("--supports-dimensions", action="store_true")

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

    handoff_parser = subparsers.add_parser("handoff")
    handoff_subparsers = handoff_parser.add_subparsers(dest="handoff_command", required=True)

    for name in ("prepare", "inspect"):
        handoff_command = handoff_subparsers.add_parser(name)
        handoff_command.add_argument("project_dir", type=Path)
        handoff_command.add_argument("--json", action="store_true", dest="as_json")

    def add_executor_arguments(handoff_command: argparse.ArgumentParser) -> None:
        handoff_command.add_argument("project_dir", type=Path)
        handoff_command.add_argument("--job", required=True, dest="job_id")
        handoff_command.add_argument("--attempt", required=True, type=int)
        handoff_command.add_argument(
            "--executor-kind",
            required=True,
            choices=("native-tool", "external-tool"),
        )
        handoff_command.add_argument("--executor-id", required=True)
        handoff_command.add_argument("--provider")
        handoff_command.add_argument("--model")
        handoff_command.add_argument("--used-reference-images", action="store_true")
        handoff_command.add_argument("--used-dimensions", action="store_true")
        handoff_command.add_argument("--used-localized-edit", action="store_true")
        handoff_command.add_argument("--json", action="store_true", dest="as_json")

    accept_result_parser = handoff_subparsers.add_parser("accept-result")
    add_executor_arguments(accept_result_parser)
    accept_result_parser.add_argument("--path", required=True, type=Path, dest="raster_path")
    accept_result_parser.add_argument("--approve-reference", action="store_true")

    record_failure_parser = handoff_subparsers.add_parser("record-failure")
    add_executor_arguments(record_failure_parser)
    record_failure_parser.add_argument("--category", required=True)
    return parser


def _escape_cli_controls(value: object) -> str:
    """Render engine-owned values without emitting terminal control bytes."""
    escaped: list[str] = []
    for char in str(value):
        codepoint = ord(char)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, f"\\x{codepoint:02x}"))
        else:
            escaped.append(char)
    return "".join(escaped)


def _render_handoff_cli(command: str, result: object) -> str:
    """Return a compact terminal-safe source-CLI handoff summary."""
    if not isinstance(result, dict):
        return _escape_cli_controls(result)
    if command == "handoff.prepare":
        counts = result.get("job_counts")
        count_text = ""
        if isinstance(counts, dict):
            count_text = " ".join(
                f"{_escape_cli_controls(key)}={_escape_cli_controls(value)}"
                for key, value in counts.items()
            )
        lines = [
            f"{_escape_cli_controls(result.get('project_id'))}: "
            f"handoff prepared for {_escape_cli_controls(result.get('phase'))}"
        ]
        if count_text:
            lines.append(f"Jobs: {count_text}")
        lines.append(f"Next action: {_escape_cli_controls(result.get('next_action'))}")
        return "\n".join(lines)
    if command == "handoff.inspect":
        jobs = result.get("jobs")
        job_count = len(jobs) if isinstance(jobs, list) else 0
        return (
            f"Handoff: prepared={_escape_cli_controls(result.get('prepared'))} "
            f"phase={_escape_cli_controls(result.get('phase'))} "
            f"scope={_escape_cli_controls(result.get('scope_state'))} jobs={job_count}\n"
            f"Next action: {_escape_cli_controls(result.get('next_action'))}"
        )
    return (
        f"{_escape_cli_controls(result.get('job_id'))}: "
        f"{_escape_cli_controls(result.get('status'))}"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic Comic Sol lifecycle CLI."""
    from .command_service import CommandService

    service = CommandService(
        engine=sys.modules[__name__],
        validation=importlib.import_module(".validate_project", __package__),
    )
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            if arguments.starter is not None:
                if any(
                    value is not None
                    for value in (arguments.source, arguments.request_json, arguments.page_count)
                ):
                    raise ValueError(
                        "--starter cannot be combined with --source, --request-json, or --page-count"
                    )
                project = service.execute(
                    "init",
                    output_root=arguments.output_root,
                    title=arguments.title,
                    starter=arguments.starter,
                )
            else:
                if arguments.source is None or arguments.request_json is None:
                    raise ValueError("blank init requires --source and --request-json")
                source = arguments.source.read_bytes()
                project = service.execute(
                    "init",
                    output_root=arguments.output_root,
                    title=arguments.title,
                    source=source,
                    request=read_json(arguments.request_json),
                    suffix=arguments.source.suffix,
                    page_count=arguments.page_count or 2,
                )
            print(project.resolve())
        elif arguments.command == "transition":
            manifest = service.execute(
                "transition",
                project_dir=arguments.project_dir,
                target=arguments.target,
                warning=arguments.warning,
            )
            print(f"{manifest['project_id']}: {manifest['status']}")
        elif arguments.command == "status":
            manifest = service.execute("status", project_dir=arguments.project_dir)
            if arguments.as_json:
                print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"{manifest['project_id']}: {manifest['status']}")
        elif arguments.command == "doctor":
            image_capability = None
            if (
                arguments.image_capability_status is not None
                or arguments.image_capability_name is not None
                or arguments.supports_reference_images
                or arguments.supports_dimensions
            ):
                image_capability = {
                    "status": arguments.image_capability_status,
                    "name": arguments.image_capability_name,
                    "supports_reference_images": arguments.supports_reference_images,
                    "supports_dimensions": arguments.supports_dimensions,
                }
            healthy, messages = doctor(
                arguments.output_root,
                image_capability=image_capability,
            )
            print("\n".join(messages))
            return 0 if healthy else 1
        elif arguments.command == "resume-plan":
            actions = service.execute("resume-plan", project_dir=arguments.project_dir)
            if arguments.as_json:
                print(
                    json.dumps(
                        [asdict(action) for action in actions],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                for action in actions:
                    print(f"{action.stage}: {action.action} {action.artifact} — {action.reason}")
        elif arguments.command == "resume":
            result = service.execute("resume", project_dir=arguments.project_dir)
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
            if (
                read_project_manifest(Path(arguments.project_dir) / "project.json").get("status")
                == "BLOCKED"
            ):
                raise ValueError(
                    "project is BLOCKED; run `comic_sol.py resume PROJECT_DIR` "
                    "to clear the block before invalidating a stage"
                )
            removed = service.execute(
                "invalidate", project_dir=arguments.project_dir, stage=arguments.stage
            )
            print("\n".join(removed) if removed else "no manifest artifacts removed")
        elif arguments.command == "record-stage":
            recorded = service.execute(
                "record-stage", project_dir=arguments.project_dir, stage=arguments.stage
            )
            print(f"{recorded['stage']}: recorded {recorded['artifacts']} artifact(s)")
        elif arguments.command == "record-attempt":
            counts = service.execute(
                "record-attempt",
                project_dir=arguments.project_dir,
                panel_id=arguments.panel_id,
                kind=arguments.kind,
                path=arguments.path,
            )
            print(json.dumps(counts, sort_keys=True))
        elif arguments.command == "promote-attempt":
            print(
                service.execute(
                    "promote-attempt",
                    project_dir=arguments.project_dir,
                    panel_id=arguments.panel_id,
                    path=arguments.path,
                )
            )
        elif arguments.command == "override-panel":
            service.execute(
                "override-panel",
                project_dir=arguments.project_dir,
                panel_id=arguments.panel_id,
                reason=arguments.reason,
            )
            print(f"{arguments.panel_id}: accepted with warnings")
        elif arguments.command == "handoff":
            command = f"handoff.{arguments.handoff_command}"
            handoff_arguments: dict[str, object] = {"project_dir": arguments.project_dir}
            if command in {"handoff.accept-result", "handoff.record-failure"}:
                handoff_arguments.update(
                    {
                        "job_id": arguments.job_id,
                        "attempt": arguments.attempt,
                        "executor_kind": arguments.executor_kind,
                        "executor_id": arguments.executor_id,
                        "provider": arguments.provider,
                        "model": arguments.model,
                        "capabilities_used": {
                            "reference_images": arguments.used_reference_images,
                            "dimensions": arguments.used_dimensions,
                            "localized_edit": arguments.used_localized_edit,
                        },
                    }
                )
            if command == "handoff.accept-result":
                handoff_arguments.update(
                    {
                        "raster_path": arguments.raster_path,
                        "approve_reference": arguments.approve_reference,
                    }
                )
            elif command == "handoff.record-failure":
                handoff_arguments["category"] = arguments.category
            result = service.execute(command, **handoff_arguments)
            if arguments.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(_render_handoff_cli(command, result))
        elif arguments.command == "finalize":
            result = service.execute("finalize", project_dir=arguments.project_dir)
            if arguments.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"{result['status']}: {result['pdf']} | {result['report']}")
        return 0
    except ValueError as error:
        print(
            f"ERROR {type(error).__name__}: {_escape_cli_controls(error)}",
            file=sys.stderr,
        )
        return 2 if arguments.command == "handoff" else 1
    except (OSError, TypeError, RuntimeError) as error:
        print(
            f"ERROR {type(error).__name__}: {_escape_cli_controls(error)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
