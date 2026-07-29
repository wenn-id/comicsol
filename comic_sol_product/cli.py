"""Stable installed command-line interface for Comic Sol."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .config import default_output_root


def _load_engine() -> Any:
    """Load the canonical engine from a checkout or its bundled wheel location."""
    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "engine",
        package_root.parent / "scripts",
    )
    engine_dir = next((path for path in candidates if (path / "comic_sol.py").is_file()), None)
    if engine_dir is None:
        raise RuntimeError("Comic Sol engine files are missing; reinstall the package")
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    import comic_sol  # type: ignore[import-not-found]

    return comic_sol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comic-sol")
    parser.add_argument("--version", action="version", version=f"comic-sol {__version__}")
    parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--output-root", type=Path, default=default_output_root())

    init = subparsers.add_parser("init")
    init.add_argument("--output-root", type=Path, default=default_output_root())
    init.add_argument("--title", required=True)
    init.add_argument("--source", required=True, type=Path)
    init.add_argument("--request-json", required=True, type=Path)

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


def _failure(command: str, category: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command,
        "data": None,
        "error": {"category": category, "message": message},
    }


def _safe_message(error: Exception) -> str:
    """Return an actionable message without local absolute paths."""
    message = str(error)
    if not message:
        return type(error).__name__
    for token in message.split():
        candidate = token.strip("'\"(),:;")
        if candidate and Path(candidate).is_absolute():
            message = message.replace(candidate, "<path>")
    return message


def _run(arguments: argparse.Namespace) -> Any:
    engine = _load_engine()
    if arguments.command == "doctor":
        healthy, messages = engine.doctor(arguments.output_root)
        return {"healthy": healthy, "messages": messages}
    if arguments.command == "init":
        source = arguments.source.read_bytes()
        engine.validate_source_bytes(source, arguments.source.suffix)
        request = engine.read_json(arguments.request_json)
        project = engine.init_project(arguments.output_root, arguments.title, source, request)
        return {"project_id": project.name, "project_dir": project.name}
    if arguments.command == "status":
        return engine.read_json(arguments.project_dir / "project.json")
    if arguments.command == "validate":
        from validate_project import ProjectValidationError, validate_project

        try:
            issues = validate_project(arguments.project_dir, arguments.stage)
        except ProjectValidationError as error:
            issues = error.issues
        return [asdict(issue) for issue in issues]
    if arguments.command == "resume":
        return engine.resume_project(arguments.project_dir)
    if arguments.command == "finalize":
        return engine.finalize_project(arguments.project_dir)
    if arguments.command in {"setup", "repair", "uninstall"}:
        from .setup import repair_clients, setup_clients, uninstall_clients

        operation = {
            "setup": setup_clients,
            "repair": repair_clients,
            "uninstall": uninstall_clients,
        }[arguments.command]
        return [asdict(result) for result in operation(arguments.output_root, selected=arguments.clients)]
    raise ValueError(f"unsupported command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = arguments.command
    try:
        if command == "mcp":
            from .mcp import run as run_mcp

            run_mcp(arguments.root)
            return 0
        data = _run(arguments)
        if arguments.as_json:
            print(json.dumps(_success(command, data), ensure_ascii=False, sort_keys=True))
        elif command == "doctor":
            print("\n".join(data["messages"]))
        elif command == "init":
            print(data["project_id"])
        elif command == "status":
            print(f"{data['project_id']}: {data['status']}")
        elif command in {"setup", "repair", "uninstall"}:
            for result in data:
                print(f"{result['client']}: {result['status']} — {result['message']}")
        else:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        if command == "doctor" and not data["healthy"]:
            return 1
        return 0
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        payload = _failure(command, "invalid-input", _safe_message(error))
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR invalid-input: {payload['error']['message']}", file=sys.stderr)
        return 2
    except OSError as error:
        payload = _failure(command, "io-error", _safe_message(error))
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR io-error: {payload['error']['message']}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        payload = _failure(command, "missing-extra", _safe_message(error))
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR missing-extra: {payload['error']['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
