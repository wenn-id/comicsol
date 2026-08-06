import argparse
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# Import core business logic from scripts/
from comic_sol import (
    ALL_STATUSES,
    RESUME_STAGES,
    doctor,
    init_project,
    transition,
    build_resume_plan,
    resume_project,
    invalidate_from,
    record_stage,
    record_generation_attempt,
    promote_attempt,
    record_override,
    read_json,
    validate_request_settings,
    IDENTIFIER,
)
from validate_project import validate_project, ProjectValidationError, STAGES as VALIDATION_STAGES
from letter_panels import letter_project
from compose_pages import compose_project
from export_pdf import guarded_export
from render_report import render_report

# Root directory allowed for operations. All project IDs resolve relative to this.
OUTPUT_ROOT: Path

# Cache successful scans as (resolved path, root mtime, immediate child count).
# Full symlink inspection runs only after a visible root-tree mutation.
_SYMLINK_SCAN_CACHE: dict[str, tuple[Path, int, int]] = {}

_VALIDATION_STAGES = frozenset({"all", "plan", "storyboard", "panels", "final", "export-ready"})
_PANEL_ID = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
_ATTEMPT_KINDS = frozenset({"initial", "visual_retry", "transient_repeat"})
_RELATIVE_PATH = re.compile(r"^(?:[A-Za-z0-9._/-]+/)*[A-Za-z0-9._-]+$")


def _reject(message: str) -> None:
    raise ToolError(message)


def _validate_project_id(project_id: str) -> None:
    if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
        _reject("invalid project ID")


def _validate_stage(stage: str) -> None:
    if not isinstance(stage, str) or stage not in VALIDATION_STAGES:
        _reject("unknown validation stage")


def _validate_resume_stage(stage: str) -> None:
    if not isinstance(stage, str) or stage not in RESUME_STAGES:
        _reject("unknown resume stage")


def _validate_target(target: str) -> None:
    if not isinstance(target, str) or target not in ALL_STATUSES:
        _reject("invalid target status")


def _validate_panel_id(panel_id: str) -> None:
    if not isinstance(panel_id, str) or not _PANEL_ID.fullmatch(panel_id):
        _reject("invalid panel ID")


def _validate_kind(kind: str) -> None:
    if not isinstance(kind, str) or kind not in _ATTEMPT_KINDS:
        _reject("unknown generation attempt kind")


def _validate_relative_path(relative: str) -> None:
    normalized = relative.replace("\\", "/") if isinstance(relative, str) else ""
    if (
        not isinstance(relative, str)
        or not _RELATIVE_PATH.fullmatch(normalized)
        or normalized.startswith("/")
        or ".." in normalized.split("/")
        or re.match(r"^[A-Za-z]:/", normalized)
    ):
        _reject("attempt path must be a relative project path")


def _safe_message(error: Exception) -> str:
    """Return a message without local absolute paths or sensitive values."""
    message = str(error)
    if not message:
        return type(error).__name__

    def replace_quoted_path(match: re.Match[str]) -> str:
        quote, candidate = match.group(1), match.group(2)
        if PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
            return f"{quote}<path>{quote}"
        return match.group(0)

    message = re.sub(r"(['\"])([^'\"]+)\1", replace_quoted_path, message)
    message = re.sub(
        r"(?i)\b(api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(\s*[:=]\s*)[^\s,;]+",
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)\b(api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)\b(?!\s*[:=])",
        "<redacted>",
        message,
    )
    for token in message.split():
        candidate = token.strip("'\"(),:;")
        if candidate and (
            PurePosixPath(candidate).is_absolute()
            or PureWindowsPath(candidate).is_absolute()
        ):
            message = message.replace(candidate, "<path>")
    return message


def _tool_error(error: Exception) -> ToolError:
    return ToolError(_safe_message(error))


def _configure_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("output root must be an absolute path")
    global OUTPUT_ROOT
    OUTPUT_ROOT = path.resolve()
    if not OUTPUT_ROOT.is_dir():
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUTPUT_ROOT


def _project_fingerprint(project_dir: Path) -> tuple[Path, int, int]:
    """Return (resolved project path, root mtime ns, immediate child count)."""
    try:
        root_mtime = project_dir.resolve().stat().st_mtime_ns
    except OSError:
        root_mtime = 0
    try:
        child_count = sum(1 for _ in project_dir.iterdir())
    except OSError:
        child_count = 0
    return project_dir.resolve(), root_mtime, child_count


def _resolve_project(project_id: str) -> Path:
    """Resolve a project ID safely within OUTPUT_ROOT."""
    try:
        if not project_id or not IDENTIFIER.fullmatch(project_id):
            raise ValueError("invalid project ID format")
        resolved = (OUTPUT_ROOT / project_id).resolve()
        if not resolved.is_relative_to(OUTPUT_ROOT) or resolved == OUTPUT_ROOT:
            raise ValueError("project directory resolves outside output root")
        fingerprint = _project_fingerprint(resolved)
        if _SYMLINK_SCAN_CACHE.get(project_id) != fingerprint:
            for directory, dirnames, filenames in os.walk(resolved, followlinks=False):
                for name in (*dirnames, *filenames):
                    if (Path(directory) / name).is_symlink():
                        raise ValueError("project contains a symlink")
            _SYMLINK_SCAN_CACHE[project_id] = fingerprint
        return resolved
    except ValueError as e:
        raise ToolError(f"Security: {e}")


mcp = FastMCP("Comic Sol", instructions="Deterministic Comic Sol project tools")


@mcp.tool()
def comic_doctor() -> dict[str, object]:
    """Check the local runtime environment (Python, Pillow, fonts, templates)."""
    try:
        healthy, messages = doctor(OUTPUT_ROOT)
        return {"healthy": healthy, "messages": messages}
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_init(title: str, source_text: str, request_settings: dict[str, Any]) -> str:
    """Initialize a new isolated project folder."""
    try:
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("title must be a non-empty string of at most 200 characters")
        if not isinstance(source_text, str) or len(source_text.encode("utf-8")) > 200 * 1024:
            raise ValueError("source must be at most 200 KiB as UTF-8 bytes")
        if not isinstance(request_settings, dict):
            raise TypeError("request_settings must be a JSON object")
        validate_request_settings(request_settings)
        project_dir = init_project(
            OUTPUT_ROOT,
            title,
            source_text.encode("utf-8"),
            request_settings
        )
        return str(project_dir.name)
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_status(project_id: str) -> dict[str, Any]:
    """Read the current project manifest."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        manifest = read_json(project_dir / "project.json")
        return manifest
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_transition(
    project_id: str, target: str, warning: str | None = None
) -> dict[str, Any]:
    """Move the project to the next state."""
    _validate_project_id(project_id)
    _validate_target(target)
    project_dir = _resolve_project(project_id)
    try:
        manifest = transition(project_dir, target, warning)
        return manifest
    except Exception as e:
        raise _tool_error(e)


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
        issues = validate_project(project_dir, stage)
        return [_issue_payload(i) for i in issues]
    except ProjectValidationError as e:
        return [_issue_payload(i) for i in e.issues]
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_resume_plan(project_id: str) -> list[dict[str, str]]:
    """Get the deterministic resume plan."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        actions = build_resume_plan(project_dir)
        return [
            {"stage": a.stage, "action": a.action, "artifact": a.artifact, "reason": a.reason}
            for a in actions
        ]
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_resume(project_id: str) -> dict[str, Any]:
    """Recover a BLOCKED project to its last valid state."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        return resume_project(project_dir)
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_invalidate(project_id: str, stage: str) -> list[str]:
    """Invalidate a stage and its downstream dependents."""
    _validate_project_id(project_id)
    _validate_resume_stage(stage)
    project_dir = _resolve_project(project_id)
    try:
        return invalidate_from(project_dir, stage)
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_record_stage(project_id: str, stage: str) -> dict[str, Any]:
    """Persist cache and hashes for a completed stage."""
    _validate_project_id(project_id)
    _validate_resume_stage(stage)
    project_dir = _resolve_project(project_id)
    try:
        return record_stage(project_dir, stage)
    except Exception as e:
        raise _tool_error(e)


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
        return record_generation_attempt(project_dir, panel_id, kind, Path(relative_path))
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_promote_attempt(project_id: str, panel_id: str, relative_path: str) -> str:
    """Promote an attempt to be the accepted raw panel."""
    _validate_project_id(project_id)
    _validate_panel_id(panel_id)
    _validate_relative_path(relative_path)
    project_dir = _resolve_project(project_id)
    try:
        dest = promote_attempt(project_dir, panel_id, Path(relative_path))
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_override_panel(project_id: str, panel_id: str, reason: str) -> str:
    """Force accept a panel with warnings."""
    _validate_project_id(project_id)
    _validate_panel_id(panel_id)
    project_dir = _resolve_project(project_id)
    try:
        record_override(project_dir, panel_id, reason)
        return f"{panel_id}: accepted with warnings"
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_letter(project_id: str) -> list[str]:
    """Run deterministic text lettering."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        outputs = letter_project(project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_compose(project_id: str) -> list[str]:
    """Compose letters and panels onto full pages."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        outputs = compose_project(project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_export(project_id: str) -> str:
    """Export the finished PDF."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        dest = guarded_export(project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_render_report(project_id: str) -> str:
    """Render the QA report."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        dest = render_report(project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise _tool_error(e)


@mcp.tool()
def comic_finalize(project_id: str) -> dict[str, Any]:
    """Run all deterministic finalization steps: validate, letter, compose, export, report, transition."""
    _validate_project_id(project_id)
    project_dir = _resolve_project(project_id)
    try:
        from comic_sol import finalize_project
        return finalize_project(project_dir)
    except Exception as e:
        raise _tool_error(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Comic Sol MCP Server")
    parser.add_argument("--root", type=Path, required=True, help="Absolute path to the output directory")
    args = parser.parse_args()
    _configure_root(args.root)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
