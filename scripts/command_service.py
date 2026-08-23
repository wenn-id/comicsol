"""Canonical command/service contract shared by every Comic Sol adapter.

Adapters own transport concerns only (arg parsing, JSON envelopes, ToolError
mapping, human output). Command dispatch and engine-object semantics live here.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


class CommandService:
    """Dispatch each canonical command to exactly one engine function."""

    def __init__(
        self,
        *,
        engine: Any | None = None,
        validation: Any | None = None,
        lettering: Any | None = None,
        composition: Any | None = None,
        export: Any | None = None,
        report: Any | None = None,
    ) -> None:
        package = __package__ or "scripts"

        def load(name: str) -> Any:
            return importlib.import_module(f"{package}.{name}")

        self.engine = engine if engine is not None else load("comic_sol")
        self.validation = validation if validation is not None else load("validate_project")
        self.lettering = lettering if lettering is not None else load("letter_panels")
        self.composition = composition if composition is not None else load("compose_pages")
        self.export = export if export is not None else load("export_pdf")
        self.report = report if report is not None else load("render_report")

    def execute(self, command: str, **kwargs: Any) -> Any:
        """Run one transport-neutral command with its canonical arguments."""
        engine = self.engine
        project_dir = kwargs.get("project_dir")

        if command == "doctor":
            return engine.doctor_report(self._required(kwargs, "output_root"))
        if command == "init":
            output_root = self._required(kwargs, "output_root")
            title = self._required(kwargs, "title")
            source = self._required(kwargs, "source")
            request = self._required(kwargs, "request")
            engine.validate_source_bytes(source, kwargs.get("suffix"))
            return engine.init_project(output_root, title, source, request)
        if project_dir is None:
            raise TypeError(f"{command} requires project_dir")
        if command == "status":
            reader = getattr(engine, "read_project_status", None)
            if reader is not None:
                return reader(project_dir)
            return engine.read_project_manifest(Path(project_dir) / "project.json")
        if command == "transition":
            return engine.transition(
                project_dir,
                self._required(kwargs, "target"),
                kwargs.get("warning"),
            )
        if command == "validate":
            try:
                return self.validation.validate_project(project_dir, kwargs.get("stage", "all"))
            except self.validation.ProjectValidationError as error:
                return error.issues
        if command == "resume-plan":
            return engine.build_resume_plan(project_dir)
        if command == "resume":
            return engine.resume_project(project_dir, progress=kwargs.get("progress"))
        if command == "invalidate":
            return engine.invalidate_from(project_dir, self._required(kwargs, "stage"))
        if command == "record-stage":
            return engine.record_stage(project_dir, self._required(kwargs, "stage"))
        if command == "record-attempt":
            attempt_path = kwargs.get("path", kwargs.get("relative_path"))
            return engine.record_generation_attempt(
                project_dir,
                self._required(kwargs, "panel_id"),
                self._required(kwargs, "kind"),
                Path(str(attempt_path)),
            )
        if command == "promote-attempt":
            return engine.promote_attempt(
                project_dir,
                self._required(kwargs, "panel_id"),
                Path(str(kwargs.get("path", kwargs.get("relative_path")))),
            )
        if command == "override-panel":
            return engine.record_override(
                project_dir,
                self._required(kwargs, "panel_id"),
                self._required(kwargs, "reason"),
            )
        if command == "letter":
            return self.lettering.letter_project(project_dir)
        if command == "compose":
            return self.composition.compose_project(project_dir)
        if command == "export":
            return self.export.guarded_export(project_dir)
        if command == "render-report":
            return self.report.render_report(project_dir)
        if command == "finalize":
            return engine.finalize_project(project_dir, progress=kwargs.get("progress"))
        raise ValueError(f"unsupported command: {command}")

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> Any:
        value = arguments.get(name)
        if value is None:
            raise TypeError(f"command requires {name}")
        return value
