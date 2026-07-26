import argparse
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# Import core business logic from scripts/
from comic_sol import (
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
    IDENTIFIER,
)
from validate_project import validate_project, ProjectValidationError
from letter_panels import letter_project
from compose_pages import compose_project
from export_pdf import export_pdf
from render_report import render_report

# Root directory allowed for operations. All project IDs resolve relative to this.
OUTPUT_ROOT: Path


def _configure_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("output root must be an absolute path")
    global OUTPUT_ROOT
    OUTPUT_ROOT = path.resolve()
    if not OUTPUT_ROOT.is_dir():
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUTPUT_ROOT


def _resolve_project(project_id: str) -> Path:
    """Resolve a project ID safely within OUTPUT_ROOT."""
    try:
        if not project_id or not IDENTIFIER.fullmatch(project_id):
            raise ValueError("invalid project ID format")
        resolved = (OUTPUT_ROOT / project_id).resolve()
        if not resolved.is_relative_to(OUTPUT_ROOT) or resolved == OUTPUT_ROOT:
            raise ValueError("project directory resolves outside output root")
        for directory, dirnames, filenames in os.walk(resolved, followlinks=False):
            for name in (*dirnames, *filenames):
                if (Path(directory) / name).is_symlink():
                    raise ValueError("project contains a symlink")
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
        raise ToolError(str(e))


@mcp.tool()
def comic_init(title: str, source_text: str, request_settings: dict[str, Any]) -> str:
    """Initialize a new isolated project folder."""
    try:
        project_dir = init_project(
            OUTPUT_ROOT,
            title,
            source_text.encode("utf-8"),
            request_settings
        )
        return str(project_dir.name)
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_status(project_id: str) -> dict[str, Any]:
    """Read the current project manifest."""
    project_dir = _resolve_project(project_id)
    try:
        manifest = read_json(project_dir / "project.json")
        return manifest
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_transition(
    project_id: str, target: str, warning: str | None = None
) -> dict[str, Any]:
    """Move the project to the next state."""
    project_dir = _resolve_project(project_id)
    try:
        manifest = transition(project_dir, target, warning)
        return manifest
    except Exception as e:
        raise ToolError(str(e))


def _issue_payload(issue) -> dict[str, str]:
    """Include the file path so a caller can locate which artifact is at fault."""
    return {"path": issue.path, "field": issue.field, "message": issue.message}


@mcp.tool()
def comic_validate(project_id: str, stage: str = "all") -> list[dict[str, str]]:
    """Run integrity validators on the project."""
    project_dir = _resolve_project(project_id)
    try:
        issues = validate_project(project_dir, stage)
        return [_issue_payload(i) for i in issues]
    except ProjectValidationError as e:
        return [_issue_payload(i) for i in e.issues]
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_resume_plan(project_id: str) -> list[dict[str, str]]:
    """Get the deterministic resume plan."""
    project_dir = _resolve_project(project_id)
    try:
        actions = build_resume_plan(project_dir)
        return [
            {"stage": a.stage, "action": a.action, "artifact": a.artifact, "reason": a.reason}
            for a in actions
        ]
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_resume(project_id: str) -> dict[str, Any]:
    """Recover a BLOCKED project to its last valid state."""
    project_dir = _resolve_project(project_id)
    try:
        return resume_project(project_dir)
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_invalidate(project_id: str, stage: str) -> list[str]:
    """Invalidate a stage and its downstream dependents."""
    project_dir = _resolve_project(project_id)
    try:
        return invalidate_from(project_dir, stage)
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_record_stage(project_id: str, stage: str) -> dict[str, Any]:
    """Persist cache and hashes for a completed stage."""
    project_dir = _resolve_project(project_id)
    try:
        return record_stage(project_dir, stage)
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_record_attempt(
    project_id: str,
    panel_id: str,
    kind: Literal["initial", "visual_retry", "transient_repeat"],
    relative_path: str,
) -> dict[str, int]:
    """Record a generated panel attempt and manage the budget."""
    project_dir = _resolve_project(project_id)
    try:
        return record_generation_attempt(project_dir, panel_id, kind, Path(relative_path))
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_promote_attempt(project_id: str, panel_id: str, relative_path: str) -> str:
    """Promote an attempt to be the accepted raw panel."""
    project_dir = _resolve_project(project_id)
    try:
        dest = promote_attempt(project_dir, panel_id, Path(relative_path))
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_override_panel(project_id: str, panel_id: str, reason: str) -> str:
    """Force accept a panel with warnings."""
    project_dir = _resolve_project(project_id)
    try:
        record_override(project_dir, panel_id, reason)
        return f"{panel_id}: accepted with warnings"
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_letter(project_id: str) -> list[str]:
    """Run deterministic text lettering."""
    project_dir = _resolve_project(project_id)
    try:
        outputs = letter_project(project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_compose(project_id: str) -> list[str]:
    """Compose letters and panels onto full pages."""
    project_dir = _resolve_project(project_id)
    try:
        outputs = compose_project(project_dir)
        return [str(p.relative_to(project_dir).as_posix()) for p in outputs]
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_export(project_id: str) -> str:
    """Export the finished PDF."""
    project_dir = _resolve_project(project_id)
    try:
        dest = export_pdf(project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_render_report(project_id: str) -> str:
    """Render the QA report."""
    project_dir = _resolve_project(project_id)
    try:
        dest = render_report(project_dir)
        return str(dest.relative_to(project_dir).as_posix())
    except Exception as e:
        raise ToolError(str(e))


@mcp.tool()
def comic_finalize(project_id: str) -> dict[str, Any]:
    """Run all deterministic finalization steps: validate, letter, compose, export, report, transition."""
    project_dir = _resolve_project(project_id)
    try:
        from comic_sol import finalize_project
        return finalize_project(project_dir)
    except Exception as e:
        raise ToolError(str(e))


def main() -> None:
    parser = argparse.ArgumentParser(description="Comic Sol MCP Server")
    parser.add_argument("--root", type=Path, required=True, help="Absolute path to the output directory")
    args = parser.parse_args()
    _configure_root(args.root)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
