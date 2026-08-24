"""Stable installed command-line interface for Comic Sol."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn, TextIO

from . import __version__
from .config import default_output_root
from .errors import (
    CliUsageError,
    ValidationFailureError,
    error_payload,
    format_human_error,
    safe_error_detail,
)


class _ArgumentParser(argparse.ArgumentParser):
    """Parser that raises instead of exiting so every surface stays fail-closed.

    ``--help`` and ``--version`` keep argparse's successful ``SystemExit``;
    only argument errors are rerouted through ``CliUsageError``.
    """

    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def _engine_package() -> str:
    """Return the explicit checkout or installed engine package name."""
    package_root = Path(__file__).resolve().parent
    if (package_root / "engine" / "comic_sol.py").is_file():
        return "comic_sol_product.engine"
    if (package_root.parent / "scripts" / "comic_sol.py").is_file():
        return "scripts"
    raise RuntimeError("Comic Sol engine files are missing; reinstall the package")


def _load_engine_module(name: str) -> Any:
    """Load one canonical engine module by package-qualified name."""
    return importlib.import_module(f"{_engine_package()}.{name}")


def _load_command_service() -> Any:
    """Load command service without adapter test injection."""
    return importlib.import_module(f"{_engine_package()}.command_service")


def _load_engine() -> Any:
    """Load the canonical engine from a checkout or its bundled wheel location."""
    return _load_engine_module("comic_sol")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="comic-sol")
    parser.add_argument("--version", action="version", version=f"comic-sol {__version__}")
    parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--output-root", type=Path, default=default_output_root())
    doctor.add_argument("--image-capability-status", choices=("available", "unavailable"))
    doctor.add_argument("--image-capability-name")
    doctor.add_argument("--supports-reference-images", action="store_true")
    doctor.add_argument("--supports-dimensions", action="store_true")

    init = subparsers.add_parser("init")
    init.add_argument("--output-root", type=Path, default=default_output_root())
    init.add_argument("--title", required=True)
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--request-json", required=True, type=Path)
    init.add_argument("--image-capability-status", choices=("available", "unavailable"))
    init.add_argument("--image-capability-name")
    init.add_argument("--supports-reference-images", action="store_true")
    init.add_argument("--supports-dimensions", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("project_dir", type=Path)

    validate = subparsers.add_parser("validate")
    validate.add_argument("project_dir", type=Path)
    validate.add_argument("--stage", default="all")

    resume = subparsers.add_parser("resume")
    resume.add_argument("project_dir", type=Path)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("project_dir", type=Path)

    mcp = subparsers.add_parser("mcp")
    mcp.add_argument("--root", required=True, type=Path)

    for command in ("setup", "repair", "uninstall"):
        integration = subparsers.add_parser(command)
        integration.add_argument("--output-root", type=Path, default=default_output_root())
        integration.add_argument("--client", action="append", dest="clients")
    return parser


def _success(command: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "command": command, "data": data, "error": None}


def _failure(
    command: str | None,
    error: Exception,
    *,
    legacy_category: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    payload = error_payload(error, command=command, surface="cli", detail=detail)
    if legacy_category is not None:
        payload["legacy_category"] = legacy_category
    return {
        "ok": False,
        "command": command,
        "data": None,
        "error": payload,
    }


def _safe_message(error: Exception) -> str:
    """Return an actionable message without local absolute paths."""
    return safe_error_detail(error)


_STAGE_STATE_GLYPH = {
    "complete": "[x]",
    "stale": "[~]",
    "blocked": "[!]",
    "pending": "[ ]",
}


def _escape_terminal_controls(value: object) -> str:
    """Return text with every terminal control character visibly escaped.

    Status values originate in project files. Rendering raw C0/C1 controls
    would allow ANSI/OSC sequences, line injection, or cursor movement. Escape
    the control byte itself instead of deleting it so diagnostics retain the
    original content without letting a terminal interpret it.
    """
    escaped: list[str] = []
    for char in str(value):
        codepoint = ord(char)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, f"\\x{codepoint:02x}"))
        else:
            escaped.append(char)
    return "".join(escaped)


def _format_next_action(next_action: object) -> str:
    """Render the single recommended next step as a stable human line.

    The summary reuses the resume plan's own vocabulary: ``command`` and
    ``agent_required`` come straight from ``_next_resume_action``, ``done`` marks
    a terminal project, and ``resume``/``required`` describe a blocked one. An
    unrecognized shape is shown verbatim so a diagnostic is never swallowed.
    """
    if not isinstance(next_action, dict) or not next_action:
        return "Next action: none — nothing to do."
    if "done" in next_action:
        return f"Next action: {_escape_terminal_controls(next_action['done'])}."
    if "command" in next_action:
        return f"Next action: run `{_escape_terminal_controls(next_action['command'])}`."
    if "agent_required" in next_action:
        stage = _escape_terminal_controls(next_action["agent_required"])
        return f"Next action: agent must produce the {stage} stage."
    if "resume" in next_action:
        reason = _escape_terminal_controls(next_action["resume"])
        return f"Next action: run `resume` to recover (blocked: {reason})."
    if "required" in next_action:
        requirement = _escape_terminal_controls(next_action["required"])
        return f"Next action: blocked until {requirement}; then run `resume`."
    key, value = next(iter(next_action.items()))
    return f"Next action: {_escape_terminal_controls(key)}={_escape_terminal_controls(value)}."


def _render_status_summary(summary: dict[str, Any]) -> str:
    """Render the visual project-status summary as plain, uncolored text.

    Errors are surfaced, never decorated away: unreadable panel QA records and
    active warnings each get their own line so a corrupt-but-diagnosable project
    stays legible. The first line keeps the stable ``<project_id>: <STATUS>``
    contract that scripts have parsed from the human surface.
    """
    project_id = _escape_terminal_controls(summary.get("project_id"))
    status = _escape_terminal_controls(summary.get("status"))
    lines = [f"{project_id}: {status}"]

    stages = summary.get("stages")
    if isinstance(stages, list) and stages:
        lines.append("Stages:")
        for entry in stages:
            if not isinstance(entry, dict):
                continue
            raw_state = str(entry.get("state", "pending"))
            state = _escape_terminal_controls(raw_state)
            stage = _escape_terminal_controls(entry.get("stage"))
            glyph = _STAGE_STATE_GLYPH.get(raw_state, "[ ]")
            lines.append(f"  {glyph} {stage}: {state}")

    panels = summary.get("panels")
    if isinstance(panels, dict) and panels:
        accepted = _escape_terminal_controls(panels.get("accepted", 0))
        failed = _escape_terminal_controls(panels.get("failed", 0))
        pending = _escape_terminal_controls(panels.get("pending", 0))
        lines.append(f"Panels: {accepted} accepted, {failed} failed, {pending} pending")
        unreadable = panels.get("unreadable", 0)
        if isinstance(unreadable, int) and unreadable > 0:
            lines.append(f"  WARNING: {unreadable} panel QA record(s) unreadable.")

    blocked_reason = summary.get("blocked_reason")
    if isinstance(blocked_reason, str) and blocked_reason:
        lines.append(f"Blocked reason: {_escape_terminal_controls(blocked_reason)}")

    warnings = summary.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            lines.append(f"  - {_escape_terminal_controls(warning)}")

    lines.append(_format_next_action(summary.get("next_action")))
    return "\n".join(lines)


def _render_status(arguments: argparse.Namespace) -> str:
    """Compute and render the human status view for one project.

    A rich summary is preferred, but an older engine that only knows the stable
    manifest reader falls back to the canonical one-line view so the human
    surface always prints something parseable.
    """
    service = _load_command_service().CommandService(engine=_load_engine())
    summary = service.execute("status-summary", project_dir=arguments.project_dir)
    if isinstance(summary, dict) and "stages" in summary:
        return _render_status_summary(summary)
    # Fallback: a plain manifest without the visual summary fields.
    project_id = (
        _escape_terminal_controls(summary.get("project_id"))
        if isinstance(summary, dict)
        else "None"
    )
    status = (
        _escape_terminal_controls(summary.get("status")) if isinstance(summary, dict) else "None"
    )
    return f"{project_id}: {status}"


class _ProgressReporter:
    """Render bounded lifecycle events without polluting machine output."""

    def __init__(self, *, as_json: bool, stream: TextIO | None = None) -> None:
        self.as_json = as_json
        self.stream = stream
        self.lines: list[str] = []
        self.current_stage = "lifecycle"

    def __call__(self, event: dict[str, object]) -> None:
        status = str(event.get("status", "working")).upper()
        stage = str(event.get("stage", self.current_stage))
        self.current_stage = stage
        fields = [f"{status} stage={stage}"]
        for key in ("completed", "remaining"):
            values = event.get(key)
            if isinstance(values, list):
                fields.append(f"{key}={len(values)}")
        line = " ".join(fields)
        self.lines.append(line)
        if not self.as_json and self.stream is not None:
            try:
                print(line, file=self.stream, flush=True)
            except OSError:
                # Progress is advisory; a closed stderr must never change the
                # lifecycle result or escape through the engine callback.
                self.stream = None

    def failure(self, *, blocked: bool = False) -> None:
        self({"status": "blocked" if blocked else "failed", "stage": self.current_stage})


def _run(
    arguments: argparse.Namespace,
    *,
    progress: _ProgressReporter | None = None,
) -> Any:
    engine = _load_engine()
    service = _load_command_service().CommandService(
        engine=engine,
        validation=_load_engine_module("validate_project"),
        lettering=_load_engine_module("letter_panels"),
        composition=_load_engine_module("compose_pages"),
        export=_load_engine_module("export_pdf"),
        report=_load_engine_module("render_report"),
    )
    if arguments.command == "doctor":
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
        return service.execute(
            "doctor",
            output_root=arguments.output_root,
            image_capability=image_capability,
        )
    if arguments.command == "init":
        source = arguments.source.read_bytes()
        engine.validate_source_bytes(source, arguments.source.suffix)
        request = engine.read_json(arguments.request_json)
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
        project = service.execute(
            "init",
            output_root=arguments.output_root,
            title=arguments.title,
            source=source,
            request=request,
            suffix=arguments.source.suffix,
            image_capability=image_capability,
        )
        return {"project_id": project.name, "project_dir": project.name}
    if arguments.command == "status":
        return service.execute("status", project_dir=arguments.project_dir)
    if arguments.command == "validate":
        issues = service.execute(
            "validate", project_dir=arguments.project_dir, stage=arguments.stage
        )
        return [asdict(issue) for issue in issues]
    if arguments.command == "resume":
        return service.execute("resume", project_dir=arguments.project_dir, progress=progress)
    if arguments.command == "finalize":
        return service.execute("finalize", project_dir=arguments.project_dir, progress=progress)
    if arguments.command in {"setup", "repair", "uninstall"}:
        from .setup import setup_clients, uninstall_clients

        operation = {
            "setup": setup_clients,
            "repair": setup_clients,
            "uninstall": uninstall_clients,
        }[arguments.command]
        operation_arguments: dict[str, Any] = {"selected": arguments.clients}
        if arguments.command != "uninstall":
            operation_arguments["executable"] = (
                sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
            )
        return [
            asdict(result) for result in operation(arguments.output_root, **operation_arguments)
        ]
    raise ValueError(f"unsupported command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    raw_arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        arguments = build_parser().parse_args(raw_arguments)
    except CliUsageError as error:
        # Parse failures never leak argparse usage text or SystemExit: JSON
        # mode still receives exactly one envelope, human mode the canonical
        # error block, and both keep diagnostics on their contract stream.
        payload = _failure(None, error, detail=error.message)
        if "--json" in raw_arguments:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(format_human_error(error), file=sys.stderr)
        return 2
    command = arguments.command
    reporter = _ProgressReporter(
        as_json=arguments.as_json,
        stream=None if arguments.as_json else sys.stderr,
    )
    try:
        if command == "mcp":
            from .mcp import run as run_mcp

            run_mcp(arguments.root)
            return 0
        data = _run(
            arguments,
            progress=reporter if command in {"resume", "finalize"} else None,
        )
        if command == "validate" and data:
            # Validation is fail-closed: a completed inspection that reports
            # issues is a failure result that still carries the issue list in
            # data for parity with the MCP inspection tool.
            failure = ValidationFailureError(len(data))
            if arguments.as_json:
                payload = _failure(command, failure)
                payload["data"] = data
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps(data, ensure_ascii=False, sort_keys=True))
                print(format_human_error(failure, command=command), file=sys.stderr)
            return 2
        if arguments.as_json:
            print(json.dumps(_success(command, data), ensure_ascii=False, sort_keys=True))
        elif command == "doctor":
            print("\n".join(data["messages"]))
        elif command == "init":
            print(data["project_id"])
        elif command == "status":
            print(_render_status(arguments))
        elif command in {"setup", "repair", "uninstall"}:
            for result in data:
                print(f"{result['client']}: {result['status']} — {result['message']}")
        else:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        if command == "doctor" and not data["healthy"]:
            return 1
        return 0
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        payload = _failure(
            command, error, legacy_category="invalid-input", detail=safe_error_detail(error)
        )
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            if command in {"resume", "finalize"}:
                reporter.failure(
                    blocked="blocked" in str(error).lower() or "capability" in str(error).lower()
                )
            print(format_human_error(error, command=command), file=sys.stderr)
        return 2
    except OSError as error:
        payload = _failure(command, error)
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            if command in {"resume", "finalize"}:
                reporter.failure()
            print(format_human_error(error, command=command), file=sys.stderr)
        return 1
    except RuntimeError as error:
        payload = _failure(command, error)
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            if command in {"resume", "finalize"}:
                reporter.failure(
                    blocked="blocked" in str(error).lower() or "capability" in str(error).lower()
                )
            print(format_human_error(error, command=command), file=sys.stderr)
        return 1
    except Exception as error:
        # Unexpected failures fail closed: one envelope with a redacted
        # diagnostic replaces the raw traceback on both surfaces.
        payload = _failure(command, error, detail=safe_error_detail(error))
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            if command in {"resume", "finalize"}:
                reporter.failure()
            print(format_human_error(error, command=command), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
