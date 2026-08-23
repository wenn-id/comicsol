import argparse
from collections import OrderedDict
import os
import re
import stat
import sys
import json
import types
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError

from pydantic import WithJsonSchema

# Import core business logic from scripts/
from .core_primitives import PANEL_ID_PATTERN
from .project_io import validate_source_bytes
from comic_sol_product.errors import error_payload
from .input_limits import (
    MAX_OVERRIDE_REASON_CHARS,
    MAX_TITLE_CHARS,
    MAX_WARNING_CHARS,
    OVERRIDE_REASON_LIMIT_MESSAGE,
    TITLE_LIMIT_MESSAGE,
    WARNING_LIMIT_MESSAGE,
    validate_narrative,
)
from .comic_sol import (
    ALL_STATUSES,
    RESUME_STAGES,
    doctor_report,
    init_project,
    transition,
    build_resume_plan,
    resume_project,
    invalidate_from,
    record_stage,
    record_generation_attempt,
    promote_attempt,
    record_override,
    read_project_status,
    finalize_project,
    read_project_manifest,
    validate_request_settings,
    IDENTIFIER,
)
from .validate_project import validate_project, ProjectValidationError, STAGES as VALIDATION_STAGES
from .letter_panels import letter_project
from .compose_pages import compose_project
from .export_pdf import guarded_export
from .render_report import render_report
from .command_service import CommandService

# Root directory allowed for operations. All project IDs resolve relative to this.
OUTPUT_ROOT: Path

# Successful symlink scans are cached per project ID. Unchanged directories need
# only an lstat; changed and newly discovered directories are scanned again.
# This cache is advisory only. contained_project_path is the authority for every
# actual project read/write and must remain the load-bearing containment check.
_DirectorySnapshot = tuple[int, int, int, int, int, int, tuple[str, ...]]
_SYMLINK_SCAN_CACHE_MAX_ENTRIES = 128
_SYMLINK_SCAN_CACHE: OrderedDict[
    str,
    tuple[Path, dict[str, _DirectorySnapshot]],
] = OrderedDict()
_SYMLINK_SCAN_CACHE_LOCK = RLock()

_PANEL_ID = PANEL_ID_PATTERN
_ATTEMPT_KINDS = frozenset({"initial", "visual_retry", "transient_repeat"})
_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_REQUEST_ERROR_PREFIXES = (
    "title must be a non-empty string of at most 200 characters",
    "source must be at most 200 KiB as UTF-8 bytes",
    "request_settings must be a JSON object",
    "sensitive request setting is not allowed",
    "request setting keys must be strings",
    "unsupported request setting",
    "request mode must be one of short_prompt, pasted_story, source_file, or resume",
    "request language must be a non-empty language tag",
    "request title must be a non-empty string of at most 200 characters",
    "transition warning must be a non-empty string of at most 500 characters",
    "override reason must be a non-empty string of at most 1000 characters",
    "narrative field must not contain secrets or credentials",
)
_IMAGE_CAPABILITY_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "name",
        "supports_reference_images",
        "supports_dimensions",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["available", "unavailable"]},
        "name": {
            "anyOf": [
                {"type": "string", "const": "agent-image-generation"},
                {"type": "null"},
            ]
        },
        "supports_reference_images": {"type": "boolean"},
        "supports_dimensions": {"type": "boolean"},
    },
}
ImageCapabilityInput = Annotated[
    object,
    WithJsonSchema(_IMAGE_CAPABILITY_INPUT_SCHEMA),
]


def _reject(message: str) -> None:
    """Raise a tool error for an invalid request field."""
    request = message.startswith(
        (
            "invalid project ID",
            "unknown validation stage",
            "unknown resume stage",
            "invalid target status",
            "invalid panel ID",
            "unknown generation attempt kind",
            "attempt path must be a relative project path",
        )
    )
    raise _tool_error(ValueError(message), request=request)


def _validate_project_id(project_id: str) -> None:
    """Validate a project identifier supplied to an MCP tool."""
    if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
        _reject("invalid project ID")


def _validate_stage(stage: str) -> None:
    """Validate a workflow stage supplied to an MCP tool."""
    if not isinstance(stage, str) or stage not in VALIDATION_STAGES:
        _reject("unknown validation stage")


def _validate_resume_stage(stage: str) -> None:
    """Validate a resumable workflow stage."""
    if not isinstance(stage, str) or stage not in RESUME_STAGES:
        _reject("unknown resume stage")


def _validate_target(target: str) -> None:
    """Validate a project target path supplied to an MCP tool."""
    if not isinstance(target, str) or target not in ALL_STATUSES:
        _reject("invalid target status")


def _validate_panel_id(panel_id: str) -> None:
    """Validate a panel identifier supplied to an MCP tool."""
    if not isinstance(panel_id, str) or not _PANEL_ID.fullmatch(panel_id):
        _reject("invalid panel ID")


def _validate_kind(kind: str) -> None:
    """Validate an artifact kind supplied to an MCP tool."""
    if not isinstance(kind, str) or kind not in _ATTEMPT_KINDS:
        _reject("unknown generation attempt kind")


def _validate_relative_path(relative: str) -> None:
    """Validate a relative project path supplied to an MCP tool."""
    normalized = relative.replace("\\", "/") if isinstance(relative, str) else ""
    if (
        not isinstance(relative, str)
        or not _RELATIVE_PATH.fullmatch(normalized)
        or normalized.startswith("/")
        or ".." in normalized.split("/")
        or re.match(r"^[A-Za-z]:/", normalized)
    ):
        _reject("attempt path must be a relative project path")


def _tool_error(error: Exception, *, request: bool = False) -> ToolError:
    """Map an internal exception to the canonical machine-readable MCP envelope."""
    payload = error_payload(error, surface="mcp", request=request)
    raw = str(error)
    if raw.startswith("security-error"):
        legacy_message = raw
    elif request:
        legacy_message = f"invalid-request: {raw}"
    elif isinstance(error, FileNotFoundError):
        legacy_message = "not-found: required project data was not found"
    elif isinstance(error, PermissionError):
        legacy_message = "permission-denied: project data could not be accessed"
    elif isinstance(error, OSError):
        legacy_message = "io-error: project data operation failed"
    elif isinstance(error, UnicodeError):
        legacy_message = "invalid-data: project data encoding is invalid"
    elif isinstance(error, (ValueError, TypeError)):
        legacy_message = "invalid-data: tool request or project data is invalid"
    else:
        legacy_message = "internal-error: tool operation failed"
    payload["legacy_message"] = legacy_message
    return ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _request_error(error: Exception) -> ToolError:
    """Allowlist known request errors without forwarding dynamic suffixes."""
    raw_message = str(error)
    for prefix in _REQUEST_ERROR_PREFIXES:
        if raw_message.startswith(prefix):
            return _tool_error(ValueError(prefix), request=True)
    return _tool_error(error)


def _configure_root(path: Path) -> Path:
    """Configure and return the Comic Sol projects root."""
    if not path.is_absolute():
        raise ValueError("output root must be an absolute path")
    global OUTPUT_ROOT
    OUTPUT_ROOT = path.resolve()
    if not OUTPUT_ROOT.is_dir():
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUTPUT_ROOT


def _directory_path(project_dir: Path, relative: str) -> Path:
    return project_dir if relative == "." else project_dir / relative


def _discard_snapshot_subtree(snapshots: dict[str, _DirectorySnapshot], relative: str) -> None:
    prefix = f"{relative}/"
    for cached in tuple(snapshots):
        if cached == relative or cached.startswith(prefix):
            snapshots.pop(cached)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_size,
        metadata.st_nlink,
    )


def _scan_directory(
    project_dir: Path,
    relative: str,
    snapshots: dict[str, _DirectorySnapshot],
) -> list[str]:
    """Scan one changed directory and return uncached child directories."""
    directory = _directory_path(project_dir, relative)
    before = directory.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _reject("security-error: project directory structure is invalid")
    children: list[str] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink():
                _reject("security-error: project contains a symlink")
            if entry.is_dir(follow_symlinks=False):
                children.append(entry.name if relative == "." else f"{relative}/{entry.name}")

    after = directory.lstat()
    if stat.S_ISLNK(after.st_mode) or not stat.S_ISDIR(after.st_mode):
        _reject("security-error: project directory structure is invalid")
    identity = _directory_identity(after)
    if _directory_identity(before) != identity:
        _reject("security-error: project changed during validation")
    current_children = tuple(sorted(children))
    previous_children = snapshots.get(relative, (0, 0, 0, 0, 0, 0, ()))[6]
    for removed in set(previous_children) - set(current_children):
        _discard_snapshot_subtree(snapshots, removed)
    snapshots[relative] = (*identity, current_children)
    return [child for child in current_children if child not in snapshots]


def _scan_subtree(
    project_dir: Path,
    relative: str,
    snapshots: dict[str, _DirectorySnapshot],
) -> None:
    pending = [relative]
    while pending:
        pending.extend(_scan_directory(project_dir, pending.pop(), snapshots))


def _refresh_project_snapshot(
    project_dir: Path,
    cached: dict[str, _DirectorySnapshot],
) -> tuple[dict[str, _DirectorySnapshot], bool]:
    """Refresh only directories whose own metadata changed."""
    snapshots = dict(cached)
    changed = False
    for relative in sorted(tuple(snapshots), key=lambda item: (item.count("/"), item)):
        if relative not in snapshots:
            continue
        directory = _directory_path(project_dir, relative)
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            if relative == ".":
                raise
            _discard_snapshot_subtree(snapshots, relative)
            changed = True
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _reject("security-error: project directory structure is invalid")
        previous = snapshots[relative]
        if _directory_identity(metadata) != previous[:6]:
            _scan_subtree(project_dir, relative, snapshots)
            changed = True
    return snapshots, changed


def _refresh_windows_snapshot(
    project_dir: Path,
    cached: dict[str, _DirectorySnapshot],
) -> tuple[dict[str, _DirectorySnapshot], bool]:
    """Refresh Windows snapshots without recursively rescanning unchanged subtrees.

    Windows directory timestamps do not reliably change for new reparse points,
    so each cached directory is inspected directly.  A recursive scan is only
    needed when that directory's child-directory set changes.
    """
    snapshots = dict(cached)
    changed = False
    for relative in sorted(tuple(snapshots), key=lambda item: (item.count("/"), item)):
        if relative not in snapshots:
            continue
        directory = _directory_path(project_dir, relative)
        metadata = directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _reject("security-error: project directory structure is invalid")
        children: list[str] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    _reject("security-error: project contains a symlink")
                if entry.is_dir(follow_symlinks=False):
                    children.append(entry.name if relative == "." else f"{relative}/{entry.name}")
        current_children = tuple(sorted(children))
        previous = snapshots[relative]
        if current_children != previous[6]:
            for removed in set(previous[6]) - set(current_children):
                _discard_snapshot_subtree(snapshots, removed)
            _scan_subtree(project_dir, relative, snapshots)
            changed = True
        else:
            snapshots[relative] = (*_directory_identity(metadata), current_children)
    return snapshots, changed


def _cache_project_snapshot(
    project_id: str,
    resolved: Path,
    snapshots: dict[str, _DirectorySnapshot],
) -> None:
    """Store one successful scan while keeping project cache memory bounded."""
    with _SYMLINK_SCAN_CACHE_LOCK:
        _SYMLINK_SCAN_CACHE[project_id] = (resolved, snapshots)
        _SYMLINK_SCAN_CACHE.move_to_end(project_id)
        while len(_SYMLINK_SCAN_CACHE) > _SYMLINK_SCAN_CACHE_MAX_ENTRIES:
            _SYMLINK_SCAN_CACHE.popitem(last=False)


def _resolve_project(project_id: str) -> Path:
    """Resolve a project ID safely within OUTPUT_ROOT."""
    try:
        if not project_id or not IDENTIFIER.fullmatch(project_id):
            _reject("security-error: invalid project ID format")
        candidate = OUTPUT_ROOT / project_id
        if candidate.is_symlink():
            _reject("security-error: project directory resolves outside output root")
        if not candidate.exists():
            _reject("security-error: project directory is not an initialized Comic Sol project")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(OUTPUT_ROOT) or resolved == OUTPUT_ROOT:
            _reject("security-error: project directory resolves outside output root")
        if not resolved.is_dir():
            _reject("security-error: project directory is not an initialized Comic Sol project")
        with _SYMLINK_SCAN_CACHE_LOCK:
            cached = _SYMLINK_SCAN_CACHE.get(project_id)
            if cached is not None and cached[0] == resolved and "." in cached[1]:
                _SYMLINK_SCAN_CACHE.move_to_end(project_id)
        if cached is None or cached[0] != resolved or "." not in cached[1]:
            snapshots: dict[str, _DirectorySnapshot] = {}
            _scan_subtree(resolved, ".", snapshots)
            _cache_project_snapshot(project_id, resolved, snapshots)
        else:
            try:
                refresh = (
                    _refresh_windows_snapshot if os.name == "nt" else _refresh_project_snapshot
                )
                snapshots, changed = refresh(resolved, cached[1])
            except Exception:
                with _SYMLINK_SCAN_CACHE_LOCK:
                    _SYMLINK_SCAN_CACHE.pop(project_id, None)
                raise
            if changed:
                _cache_project_snapshot(project_id, resolved, snapshots)
        if not (resolved / "project.json").is_file():
            _reject("security-error: project directory is not an initialized Comic Sol project")
        return resolved
    except ToolError:
        raise
    except Exception as error:
        raise _tool_error(error) from None


mcp = FastMCP("Comic Sol", instructions="Deterministic Comic Sol project tools")


def _command_service() -> CommandService:
    """Bind the shared command contract to this module's live engine symbols.

    Referencing the imported functions keeps test monkeypatching effective
    while every tool dispatches through one canonical service.
    """
    return CommandService(
        engine=types.SimpleNamespace(
            doctor_report=doctor_report,
            init_project=init_project,
            validate_source_bytes=validate_source_bytes,
            read_project_status=read_project_status,
            read_project_manifest=read_project_manifest,
            transition=transition,
            build_resume_plan=build_resume_plan,
            resume_project=resume_project,
            invalidate_from=invalidate_from,
            record_stage=record_stage,
            record_generation_attempt=record_generation_attempt,
            promote_attempt=promote_attempt,
            record_override=record_override,
            finalize_project=finalize_project,
        ),
        validation=types.SimpleNamespace(
            validate_project=validate_project,
            ProjectValidationError=ProjectValidationError,
        ),
        lettering=types.SimpleNamespace(letter_project=letter_project),
        composition=types.SimpleNamespace(compose_project=compose_project),
        export=types.SimpleNamespace(guarded_export=guarded_export),
        report=types.SimpleNamespace(render_report=render_report),
    )


@mcp.tool()
def comic_doctor(
    image_capability: ImageCapabilityInput | None = None,
) -> dict[str, object]:
    """Check the runtime and report a provider-neutral agent capability observation."""
    try:
        return _command_service().execute(
            "doctor",
            output_root=OUTPUT_ROOT,
            image_capability=image_capability,
        )
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_init(title: str, source_text: str, request_settings: dict[str, Any]) -> str:
    """Initialize a new isolated project folder."""
    try:
        validate_narrative(title, message=TITLE_LIMIT_MESSAGE, max_chars=MAX_TITLE_CHARS)
        if not isinstance(source_text, str) or len(source_text.encode("utf-8")) > 200 * 1024:
            raise ValueError("source must be at most 200 KiB as UTF-8 bytes")
        if not isinstance(request_settings, dict):
            raise TypeError("request_settings must be a JSON object")
        validate_request_settings(request_settings)
        project_dir = _command_service().execute(
            "init",
            output_root=OUTPUT_ROOT,
            title=title,
            source=source_text.encode("utf-8"),
            request=request_settings,
        )
        return str(project_dir.name)
    except Exception as e:
        raise _request_error(e) from None


@mcp.tool()
def comic_status(project_id: str) -> dict[str, Any]:
    """Read the current project manifest."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute("status", project_dir=project_dir)
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_transition(project_id: str, target: str, warning: str | None = None) -> dict[str, Any]:
    """Move the project to the next state."""
    _validate_project_id(project_id)
    _validate_target(target)
    if warning is not None:
        try:
            validate_narrative(warning, message=WARNING_LIMIT_MESSAGE, max_chars=MAX_WARNING_CHARS)
        except Exception as e:
            raise _request_error(e) from None
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute(
            "transition", project_dir=project_dir, target=target, warning=warning
        )
    except Exception as e:
        raise _tool_error(e) from None


def _issue_payload(issue) -> dict[str, str]:
    """Include the file path so a caller can locate which artifact is at fault."""
    return {"path": issue.path, "field": issue.field, "message": issue.message}


@mcp.tool()
def comic_validate(project_id: str, stage: str = "all") -> list[dict[str, str]]:
    """Run integrity validators on the project."""
    _validate_project_id(project_id)
    _validate_stage(stage)
    project_dir = _resolve_project(project_id)
    try:
        issues = _command_service().execute("validate", project_dir=project_dir, stage=stage)
        return [_issue_payload(i) for i in issues]
    except ProjectValidationError as e:
        return [_issue_payload(i) for i in e.issues]
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_resume_plan(project_id: str) -> list[dict[str, str]]:
    """Get the deterministic resume plan."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        actions = _command_service().execute("resume-plan", project_dir=project_dir)
        return [
            {"stage": a.stage, "action": a.action, "artifact": a.artifact, "reason": a.reason}
            for a in actions
        ]
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_resume(project_id: str) -> dict[str, Any]:
    """Recover a BLOCKED project to its last valid state."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute("resume", project_dir=project_dir)
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_invalidate(project_id: str, stage: str) -> list[str]:
    """Invalidate a stage and its downstream dependents."""
    _validate_project_id(project_id)
    _validate_resume_stage(stage)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute("invalidate", project_dir=project_dir, stage=stage)
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_record_stage(project_id: str, stage: str) -> dict[str, Any]:
    """Persist cache and hashes for a completed stage."""
    _validate_project_id(project_id)
    _validate_resume_stage(stage)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute("record-stage", project_dir=project_dir, stage=stage)
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_record_attempt(
    project_id: str,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    relative_path: str,
) -> dict[str, int]:
    """Record a generated panel attempt and manage the budget."""
    _validate_project_id(project_id)
    _validate_panel_id(panel_id)
    _validate_kind(kind)
    _validate_relative_path(relative_path)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute(
            "record-attempt",
            project_dir=project_dir,
            panel_id=panel_id,
            kind=kind,
            relative_path=Path(relative_path),
        )
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_promote_attempt(project_id: str, panel_id: str, relative_path: str) -> str:
    """Promote an attempt to be the accepted raw panel."""
    _validate_project_id(project_id)
    _validate_panel_id(panel_id)
    _validate_relative_path(relative_path)
    project_dir = _resolve_project(project_id)
    try:
        dest = _command_service().execute(
            "promote-attempt",
            project_dir=project_dir,
            panel_id=panel_id,
            relative_path=Path(relative_path),
        )
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_override_panel(project_id: str, panel_id: str, reason: str) -> str:
    """Force accept a panel with warnings."""
    _validate_project_id(project_id)
    _validate_panel_id(panel_id)
    try:
        validate_narrative(
            reason,
            message=OVERRIDE_REASON_LIMIT_MESSAGE,
            max_chars=MAX_OVERRIDE_REASON_CHARS,
        )
    except Exception as e:
        raise _request_error(e) from None
    project_dir = _resolve_project(project_id)
    try:
        _command_service().execute(
            "override-panel", project_dir=project_dir, panel_id=panel_id, reason=reason
        )
        return f"{panel_id}: accepted with warnings"
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_letter(project_id: str) -> list[str]:
    """Run deterministic text lettering."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        outputs = _command_service().execute("letter", project_dir=project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_compose(project_id: str) -> list[str]:
    """Compose letters and panels onto full pages."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        outputs = _command_service().execute("compose", project_dir=project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_export(project_id: str) -> str:
    """Export the finished PDF."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        dest = _command_service().execute("export", project_dir=project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_render_report(project_id: str) -> str:
    """Render the QA report."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        dest = _command_service().execute("render-report", project_dir=project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e) from None


@mcp.tool()
def comic_finalize(project_id: str) -> dict[str, Any]:
    """Run all deterministic finalization steps: validate, letter, compose, export, report, transition."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        return _command_service().execute("finalize", project_dir=project_dir)
    except Exception as e:
        raise _tool_error(e) from None


def main() -> None:
    """Start the Comic Sol MCP server."""
    parser = argparse.ArgumentParser(description="Comic Sol MCP Server")
    parser.add_argument(
        "--root", type=Path, required=True, help="Absolute path to the output directory"
    )
    args = parser.parse_args()
    _configure_root(args.root)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
