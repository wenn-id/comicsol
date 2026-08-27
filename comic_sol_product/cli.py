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

STARTER_IDS = (
    "minimal-one-page",
    "dialogue-two-page",
    "action-focused",
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

    init = subparsers.add_parser(
        "init",
        description="Create a project from explicit flags or a guided terminal session.",
    )
    init.add_argument(
        "--interactive",
        action="store_true",
        help="prompt for project settings (human terminals only)",
    )
    init.add_argument("--output-root", type=Path, help="parent directory for the project")
    init.add_argument("--title", help="project title")
    init.add_argument("--source", type=Path, help="UTF-8 .txt or .md story source")
    init.add_argument("--request-json", type=Path, help="optional request settings JSON")
    init.add_argument(
        "--starter",
        choices=STARTER_IDS,
        help="initialize from a bundled v1 storyboard starter",
    )
    init.add_argument(
        "--page-count",
        type=int,
        choices=range(1, 5),
        help="planned page count (default: 2)",
    )
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

    handoff = subparsers.add_parser("handoff")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)

    prepare_handoff = handoff_commands.add_parser("prepare")
    prepare_handoff.add_argument("project_dir", type=Path)

    inspect_handoff = handoff_commands.add_parser("inspect")
    inspect_handoff.add_argument("target", type=Path)

    export_handoff = handoff_commands.add_parser("export")
    export_handoff.add_argument("project_dir", type=Path)
    export_handoff.add_argument("--output", required=True, type=Path, dest="output_path")

    import_handoff = handoff_commands.add_parser("import")
    import_handoff.add_argument("archive_path", type=Path)
    import_handoff.add_argument("--output-root", required=True, type=Path)

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

    accept_result = handoff_commands.add_parser("accept-result")
    add_executor_arguments(accept_result)
    accept_result.add_argument("--path", required=True, type=Path, dest="raster_path")
    accept_result.add_argument("--approve-reference", action="store_true")

    record_failure = handoff_commands.add_parser("record-failure")
    add_executor_arguments(record_failure)
    record_failure.add_argument("--category", required=True)

    mcp = subparsers.add_parser("mcp")
    mcp.add_argument("--root", required=True, type=Path)

    for command in ("setup", "repair", "uninstall"):
        integration = subparsers.add_parser(command)
        integration.add_argument("--output-root", type=Path, default=default_output_root())
        client_options = None
        if command == "repair":
            from .setup import SUPPORTED_CLIENT_NAMES

            client_options = SUPPORTED_CLIENT_NAMES
        integration.add_argument(
            "--client", action="append", dest="clients", choices=client_options
        )
        if command == "repair":
            integration.add_argument("--dry-run", action="store_true")
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


def _render_handoff(command: str, data: object) -> str:
    """Render a compact terminal-safe summary for one handoff operation."""
    if not isinstance(data, dict):
        return _escape_terminal_controls(data)

    if command == "handoff.prepare":
        project_id = _escape_terminal_controls(data.get("project_id"))
        phase = _escape_terminal_controls(data.get("phase"))
        lines = [f"{project_id}: handoff prepared for {phase}"]
        counts = data.get("job_counts")
        if isinstance(counts, dict):
            rendered_counts = " ".join(
                f"{_escape_terminal_controls(key)}={_escape_terminal_controls(value)}"
                for key, value in counts.items()
            )
            if rendered_counts:
                lines.append(f"Jobs: {rendered_counts}")
        lines.append(f"Next action: {_escape_terminal_controls(data.get('next_action'))}")
        return "\n".join(lines)

    if command == "handoff.inspect":
        if "format_version" in data or "valid" in data:
            return (
                f"Handoff archive {_escape_terminal_controls(data.get('project_id'))}: "
                f"version={_escape_terminal_controls(data.get('format_version'))} "
                f"valid={_escape_terminal_controls(data.get('valid'))}"
            )
        prepared = _escape_terminal_controls(data.get("prepared"))
        phase = _escape_terminal_controls(data.get("phase"))
        scope = _escape_terminal_controls(data.get("scope_state"))
        jobs = data.get("jobs")
        job_count = len(jobs) if isinstance(jobs, list) else 0
        return "\n".join(
            (
                f"Handoff: prepared={prepared} phase={phase} scope={scope} jobs={job_count}",
                f"Next action: {_escape_terminal_controls(data.get('next_action'))}",
            )
        )

    if command == "handoff.export":
        return (
            f"{_escape_terminal_controls(data.get('project_id'))}: handoff archive exported "
            f"to {_escape_terminal_controls(data.get('archive_path'))}"
        )
    if command == "handoff.import":
        return (
            f"{_escape_terminal_controls(data.get('project_id'))}: handoff archive imported "
            f"to {_escape_terminal_controls(data.get('project_dir'))}"
        )

    job_id = _escape_terminal_controls(data.get("job_id"))
    status = _escape_terminal_controls(data.get("status"))
    duplicate = _escape_terminal_controls(data.get("duplicate"))
    if command == "handoff.accept-result":
        return f"{job_id}: {status} (duplicate={duplicate})"
    if command == "handoff.record-failure":
        remaining = _escape_terminal_controls(data.get("attempts_remaining"))
        category = _escape_terminal_controls(data.get("category"))
        return (
            f"{job_id}: {status} category={category} "
            f"attempts_remaining={remaining} duplicate={duplicate}"
        )
    return _escape_terminal_controls(data)


def _validate_init_arguments(arguments: argparse.Namespace) -> None:
    """Keep the wizard opt-in and the automation path fully non-interactive."""
    if arguments.command != "init":
        return
    if arguments.interactive:
        if arguments.as_json:
            raise CliUsageError("--interactive cannot be combined with --json")
        has_capability_flags = (
            arguments.image_capability_status is not None
            or arguments.image_capability_name is not None
            or arguments.supports_reference_images
            or arguments.supports_dimensions
        )
        if (
            any(
                value is not None
                for value in (
                    arguments.output_root,
                    arguments.title,
                    arguments.source,
                    arguments.request_json,
                    arguments.page_count,
                    arguments.starter,
                )
            )
            or has_capability_flags
        ):
            raise CliUsageError("--interactive cannot be combined with init data arguments")
        return
    if arguments.starter is not None:
        if any(
            value is not None
            for value in (arguments.source, arguments.request_json, arguments.page_count)
        ):
            raise CliUsageError(
                "--starter cannot be combined with --source, --request-json, or --page-count"
            )
        missing = ["--title"] if arguments.title is None else []
    else:
        missing = [
            name
            for name, value in (("--title", arguments.title), ("--source", arguments.source))
            if value is None
        ]
    if missing:
        raise CliUsageError("the following arguments are required: " + ", ".join(missing))


def _prompt_value(label: str, default: str | None = None) -> str:
    """Read one required human answer while keeping prompts off stdout."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        print(f"{label}{suffix}: ", end="", file=sys.stderr, flush=True)
        try:
            value = input().strip()
        except EOFError as error:
            raise CliUsageError("interactive initialization ended before completion") from error
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.", file=sys.stderr)


def _prompt_page_count() -> int:
    while True:
        value = _prompt_value("Page count (1-4)", "2")
        try:
            page_count = int(value)
        except ValueError:
            page_count = 0
        if 1 <= page_count <= 4:
            return page_count
        print("Page count must be an integer from 1 to 4.", file=sys.stderr)


def _prompt_starter() -> str | None:
    choices = STARTER_IDS
    label = "Starter (blank/" + "/".join(choices) + ")"
    while True:
        value = _prompt_value(label, "blank").lower()
        if value in {"blank", "none"}:
            return None
        if value in choices:
            return value
        print("Starter must be blank or one of: " + ", ".join(choices) + ".", file=sys.stderr)


def _read_source(engine: Any, source_path: Path) -> tuple[bytes, str]:
    """Read one bounded source file through the engine trust boundary."""
    project_io = _load_engine_module("project_io")
    absolute = Path(source_path).expanduser().absolute()
    source = project_io.read_bytes_nofollow(
        absolute,
        max_bytes=project_io.MAX_SOURCE_BYTES,
    )
    engine.validate_source_bytes(source, absolute.suffix)
    return source, absolute.suffix


def _guided_init(
    engine: Any,
) -> tuple[
    Path,
    str,
    bytes | None,
    str | None,
    dict[str, object] | None,
    int | None,
    str | None,
]:
    """Collect and validate the small set of choices needed for initialization."""
    print("Comic Sol guided project initializer", file=sys.stderr)
    while True:
        title = _prompt_value("Project name", "Comic Sol Project")
        try:
            engine.validate_narrative(
                title,
                message=engine.TITLE_LIMIT_MESSAGE,
                max_chars=engine.MAX_TITLE_CHARS,
            )
        except ValueError as error:
            print(f"Invalid project name: {_safe_message(error)}", file=sys.stderr)
            continue
        break

    starter = _prompt_starter()
    source: bytes | None = None
    suffix: str | None = None
    request: dict[str, object] | None = None
    page_count: int | None = None
    if starter is None:
        page_count = _prompt_page_count()
        while True:
            source_kind = _prompt_value("Story source (prompt/file)", "prompt").lower()
            if source_kind in {"prompt", "p", "file", "f"}:
                break
            print("Story source must be prompt or file.", file=sys.stderr)

        if source_kind in {"prompt", "p"}:
            while True:
                source = _prompt_value("Story prompt").encode("utf-8")
                try:
                    engine.validate_source_bytes(source)
                except ValueError as error:
                    print(f"Invalid story prompt: {_safe_message(error)}", file=sys.stderr)
                    continue
                break
            request = {"language": "en", "mode": "short_prompt"}
        else:
            while True:
                source_path = Path(_prompt_value("Story file (.txt or .md)"))
                try:
                    source, suffix = _read_source(engine, source_path)
                except (OSError, ValueError) as error:
                    print(f"Invalid story file: {_safe_message(error)}", file=sys.stderr)
                    continue
                break
            request = {"language": "en", "mode": "source_file"}

    while True:
        output_root = Path(
            _prompt_value("Output location", str(default_output_root()))
        ).expanduser()
        try:
            if output_root.exists() and not output_root.is_dir():
                raise ValueError("output location must be a directory or a new path")
        except (OSError, ValueError) as error:
            print(f"Invalid output location: {_safe_message(error)}", file=sys.stderr)
            continue
        break
    if request is not None:
        engine.validate_request_settings(request)
    return output_root, title, source, suffix, request, page_count, starter


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
        if arguments.interactive:
            output_root, title, source, suffix, request, page_count, starter = _guided_init(engine)
            image_capability = None
        else:
            output_root = arguments.output_root or default_output_root()
            title = arguments.title
            starter = arguments.starter
            source = None
            suffix = None
            request = None
            page_count = None
            if starter is None:
                source, suffix = _read_source(engine, arguments.source)
                request = (
                    engine.read_json(arguments.request_json)
                    if arguments.request_json is not None
                    else {"language": "en", "mode": "source_file"}
                )
                page_count = arguments.page_count or 2
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
        init_arguments: dict[str, Any] = {
            "output_root": output_root,
            "title": title,
            "image_capability": image_capability,
        }
        if starter is not None:
            init_arguments["starter"] = starter
        else:
            init_arguments.update(
                {
                    "source": source,
                    "request": request,
                    "suffix": suffix,
                    "page_count": page_count,
                }
            )
        project = service.execute("init", **init_arguments)
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
    if arguments.command in {
        "handoff.prepare",
        "handoff.inspect",
        "handoff.export",
        "handoff.import",
        "handoff.accept-result",
        "handoff.record-failure",
    }:
        if arguments.command == "handoff.export":
            return service.execute(
                arguments.command,
                project_dir=arguments.project_dir,
                output_path=arguments.output_path,
            )
        if arguments.command == "handoff.import":
            return service.execute(
                arguments.command,
                archive_path=arguments.archive_path,
                output_root=arguments.output_root,
            )
        if arguments.command == "handoff.inspect":
            if not arguments.target.is_dir() and arguments.target.name.endswith(
                ".comic-sol-handoff"
            ):
                return service.execute(arguments.command, archive_path=arguments.target)
            return service.execute(arguments.command, project_dir=arguments.target)
        handoff_arguments: dict[str, Any] = {"project_dir": arguments.project_dir}
        if arguments.command in {"handoff.accept-result", "handoff.record-failure"}:
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
        if arguments.command == "handoff.accept-result":
            handoff_arguments.update(
                {
                    "raster_path": arguments.raster_path,
                    "approve_reference": arguments.approve_reference,
                }
            )
        elif arguments.command == "handoff.record-failure":
            handoff_arguments["category"] = arguments.category
        return service.execute(arguments.command, **handoff_arguments)
    if arguments.command in {"setup", "repair", "uninstall"}:
        from .setup import repair_clients, setup_clients, uninstall_clients

        operation_arguments: dict[str, Any] = {"selected": arguments.clients}
        results: Any
        if arguments.command == "uninstall":
            results = uninstall_clients(arguments.output_root, **operation_arguments)
        else:
            operation_arguments["executable"] = (
                sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
            )
            if arguments.command == "repair":
                operation_arguments["dry_run"] = arguments.dry_run
                results = repair_clients(arguments.output_root, **operation_arguments)
            else:
                results = setup_clients(arguments.output_root, **operation_arguments)
        return [asdict(result) for result in results]
    raise ValueError(f"unsupported command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    raw_arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        arguments = build_parser().parse_args(raw_arguments)
        _validate_init_arguments(arguments)
        if arguments.command == "handoff":
            arguments.command = f"handoff.{arguments.handoff_command}"
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
        if command == "repair":
            failures = [result for result in data if result["state"] == "failure"]
            if failures:
                aggregate = next(
                    (result for result in failures if result["error"]["code"] == "CS-INSTALL-003"),
                    failures[0],
                )
                if arguments.as_json:
                    payload = {
                        "ok": False,
                        "command": command,
                        "data": data,
                        "error": aggregate["error"],
                    }
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                else:
                    for result in data:
                        evidence = (
                            f"; backup={result['backup_path']}"
                            if result["backup_path"] is not None
                            else ""
                        )
                        print(
                            f"{result['client']}: {result['state']}/{result['status']} — "
                            f"{result['message']}{evidence}"
                        )
                    repair_error = aggregate["error"]
                    print(
                        f"ERROR {repair_error['code']} [{repair_error['category']}]: "
                        f"{repair_error['message']}\nReason: {repair_error['reason']}\n"
                        f"Recovery: {repair_error['recovery']}",
                        file=sys.stderr,
                    )
                return 1
        if arguments.as_json:
            print(json.dumps(_success(command, data), ensure_ascii=False, sort_keys=True))
        elif command == "doctor":
            print("\n".join(data["messages"]))
        elif command == "init":
            print(data["project_id"])
        elif command == "status":
            print(_render_status(arguments))
        elif command.startswith("handoff."):
            print(_render_handoff(command, data))
        elif command in {"setup", "repair", "uninstall"}:
            for result in data:
                print(f"{result['client']}: {result['status']} — {result['message']}")
        else:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        if command == "doctor" and not data["healthy"]:
            return 1
        return 0
    except CliUsageError as error:
        payload = _failure(command, error, detail=error.message)
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(format_human_error(error, command=command), file=sys.stderr)
        return 2
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        detail = None if type(error).__name__ == "HandoffArchiveError" else safe_error_detail(error)
        payload = _failure(command, error, legacy_category="invalid-input", detail=detail)
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
