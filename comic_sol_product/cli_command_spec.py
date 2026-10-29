"""Shared argument definitions for the installed and source CLI surfaces."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

from .config import default_output_root

STARTER_IDS = (
    "minimal-one-page",
    "dialogue-two-page",
    "action-focused",
)

SHARED_COMMAND_NAMES = (
    "doctor",
    "init",
    "status",
    "bind-plan",
    "resume",
    "finalize",
    "dogfood",
    "handoff",
)

SHARED_COMMAND_LEAVES = (
    "doctor",
    "init",
    "status",
    "bind-plan",
    "resume",
    "finalize",
    "dogfood.report",
    "dogfood.preview",
    "dogfood.validate",
    "handoff.prepare",
    "handoff.inspect",
    "handoff.export",
    "handoff.import",
    "handoff.accept-result",
    "handoff.record-failure",
)


def dogfood_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer from 0 through 10080") from error
    if not 0 <= minutes <= 10_080:
        raise argparse.ArgumentTypeError("must be an integer from 0 through 10080")
    return minutes


def dogfood_failed_attempts(value: str) -> int:
    try:
        attempts = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer from 0 through 1000") from error
    if not 0 <= attempts <= 1_000:
        raise argparse.ArgumentTypeError("must be an integer from 0 through 1000")
    return attempts


def dogfood_yes_no(value: str) -> bool:
    if value == "yes":
        return True
    if value == "no":
        return False
    raise argparse.ArgumentTypeError("must be yes or no")


def add_shared_command_parsers(
    subparsers: Any,
    commands: Sequence[str],
    *,
    source_cli: bool,
) -> None:
    for name in commands:
        if name == "doctor":
            _add_doctor(subparsers)
        elif name == "init":
            _add_init(subparsers, source_cli=source_cli)
        elif name == "status":
            parser = subparsers.add_parser("status")
            parser.add_argument("project_dir", type=Path)
            _add_source_json(parser, source_cli=source_cli)
        elif name == "bind-plan":
            parser = subparsers.add_parser(
                "bind-plan",
                description="Bind validated plan files to manifest descriptors and panel counts.",
            )
            parser.add_argument("project_dir", type=Path)
        elif name == "resume":
            parser = subparsers.add_parser("resume")
            parser.add_argument("project_dir", type=Path)
            _add_source_json(parser, source_cli=source_cli)
        elif name == "finalize":
            parser = subparsers.add_parser("finalize")
            parser.add_argument("project_dir", type=Path)
            _add_source_json(parser, source_cli=source_cli)
        elif name == "dogfood":
            _add_dogfood(subparsers, source_cli=source_cli)
        elif name == "handoff":
            _add_handoff(subparsers, source_cli=source_cli)
        else:
            raise ValueError(f"unknown shared CLI command: {name}")


def _add_source_json(parser: argparse.ArgumentParser, *, source_cli: bool) -> None:
    if source_cli:
        parser.add_argument("--json", action="store_true", dest="as_json")


def _add_doctor(subparsers: Any) -> None:
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--output-root", type=Path, default=default_output_root())
    doctor.add_argument("--image-capability-status", choices=("available", "unavailable"))
    doctor.add_argument("--image-capability-name")
    doctor.add_argument("--supports-reference-images", action="store_true")
    doctor.add_argument("--supports-dimensions", action="store_true")


def _add_init(subparsers: Any, *, source_cli: bool) -> None:
    init = subparsers.add_parser(
        "init",
        description="Create a project from explicit flags or a guided terminal session.",
    )
    if source_cli:
        init.add_argument("--output-root", required=True, type=Path)
        init.add_argument("--title", required=True)
        init.add_argument("--source", type=Path)
        init.add_argument("--request-json", type=Path)
        init.add_argument("--page-count", type=int, choices=range(1, 5))
        init.add_argument("--starter", choices=STARTER_IDS)
        return
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


def _add_dogfood(subparsers: Any, *, source_cli: bool) -> None:
    dogfood = subparsers.add_parser("dogfood")
    commands = dogfood.add_subparsers(dest="dogfood_command", required=True)

    def add_creator_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("project_dir", type=Path)
        parser.add_argument("--setup-minutes", required=True, type=dogfood_minutes)
        parser.add_argument("--first-project-minutes", required=True, type=dogfood_minutes)
        parser.add_argument("--pdf-minutes", required=True, type=dogfood_minutes)
        parser.add_argument("--manual-intervention", required=True, type=dogfood_yes_no)
        parser.add_argument("--would-use-again", required=True, type=dogfood_yes_no)
        parser.add_argument("--failed-resume-attempts", type=dogfood_failed_attempts)
        parser.add_argument("--friction", action="append", default=[])
        parser.add_argument("--cohort-alias")
        _add_source_json(parser, source_cli=source_cli)

    report = commands.add_parser("report")
    add_creator_arguments(report)
    report.add_argument("--consent-to-share", required=True, action="store_true")
    report.add_argument("--output", required=True, type=Path, dest="output_path")

    preview = commands.add_parser("preview")
    add_creator_arguments(preview)
    preview.add_argument("--consent-to-share", action="store_true")

    validate = commands.add_parser("validate")
    validate.add_argument("report_path", type=Path)
    _add_source_json(validate, source_cli=source_cli)


def _add_handoff(subparsers: Any, *, source_cli: bool) -> None:
    handoff = subparsers.add_parser("handoff")
    commands = handoff.add_subparsers(dest="handoff_command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("project_dir", type=Path)
    _add_source_json(prepare, source_cli=source_cli)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("target", type=Path)
    _add_source_json(inspect, source_cli=source_cli)

    export = commands.add_parser("export")
    export.add_argument("project_dir", type=Path)
    export.add_argument("--output", required=True, type=Path, dest="output_path")
    _add_source_json(export, source_cli=source_cli)

    import_command = commands.add_parser("import")
    import_command.add_argument("archive_path", type=Path)
    import_command.add_argument("--output-root", required=True, type=Path)
    _add_source_json(import_command, source_cli=source_cli)

    def add_executor_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("project_dir", type=Path)
        parser.add_argument("--job", required=True, dest="job_id")
        parser.add_argument("--attempt", required=True, type=int)
        parser.add_argument(
            "--executor-kind",
            required=True,
            choices=("native-tool", "external-tool"),
        )
        parser.add_argument("--executor-id", required=True)
        parser.add_argument("--provider")
        parser.add_argument("--model")
        parser.add_argument("--used-reference-images", action="store_true")
        parser.add_argument("--used-dimensions", action="store_true")
        parser.add_argument("--used-localized-edit", action="store_true")
        _add_source_json(parser, source_cli=source_cli)

    accept_result = commands.add_parser("accept-result")
    add_executor_arguments(accept_result)
    accept_result.add_argument("--path", required=True, type=Path, dest="raster_path")
    accept_result.add_argument("--approve-reference", action="store_true")

    record_failure = commands.add_parser("record-failure")
    add_executor_arguments(record_failure)
    record_failure.add_argument("--category", required=True)
