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
            output_root = self._required(kwargs, "output_root")
            image_capability = kwargs.get("image_capability")
            if image_capability is None:
                return engine.doctor_report(output_root)
            return engine.doctor_report(output_root, image_capability=image_capability)
        if command == "init":
            output_root = self._required(kwargs, "output_root")
            title = self._required(kwargs, "title")
            starter = kwargs.get("starter")
            image_capability = kwargs.get("image_capability")
            if starter is not None:
                conflicting = [
                    name
                    for name in ("source", "request", "page_count")
                    if name in kwargs and kwargs[name] is not None
                ]
                if conflicting:
                    raise ValueError(
                        "starter cannot be combined with explicit source, request, or page count"
                    )
                if image_capability is None:
                    return engine.init_project(output_root, title, starter=starter)
                return engine.init_project(
                    output_root,
                    title,
                    starter=starter,
                    image_capability=image_capability,
                )
            source = self._required(kwargs, "source")
            request = self._required(kwargs, "request")
            engine.validate_source_bytes(source, kwargs.get("suffix"))
            has_page_count = "page_count" in kwargs
            page_count = kwargs.get("page_count", 2)
            if image_capability is None:
                return engine.init_project(
                    output_root, title, source, request, page_count=page_count
                )
            if not has_page_count:
                return engine.init_project(
                    output_root, title, source, request, image_capability=image_capability
                )
            return engine.init_project(
                output_root,
                title,
                source,
                request,
                image_capability=image_capability,
                page_count=page_count,
            )
        if project_dir is None:
            raise TypeError(f"{command} requires project_dir")
        if command == "handoff.prepare":
            return engine.prepare_handoff(project_dir)
        if command == "handoff.inspect":
            return engine.inspect_handoff(project_dir)
        if command == "handoff.accept-result":
            return engine.accept_handoff_result(
                project_dir,
                job_id=self._required(kwargs, "job_id"),
                attempt=self._required(kwargs, "attempt"),
                raster_path=self._required(kwargs, "raster_path"),
                executor_kind=self._required(kwargs, "executor_kind"),
                executor_id=self._required(kwargs, "executor_id"),
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                capabilities_used=kwargs.get("capabilities_used"),
                approve_reference=kwargs.get("approve_reference", False),
            )
        if command == "handoff.record-failure":
            return engine.record_handoff_failure(
                project_dir,
                job_id=self._required(kwargs, "job_id"),
                attempt=self._required(kwargs, "attempt"),
                executor_kind=self._required(kwargs, "executor_kind"),
                executor_id=self._required(kwargs, "executor_id"),
                category=self._required(kwargs, "category"),
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                capabilities_used=kwargs.get("capabilities_used"),
            )
        if command == "status":
            reader = getattr(engine, "read_project_status", None)
            if reader is not None:
                return reader(project_dir)
            return engine.read_project_manifest(Path(project_dir) / "project.json")
        if command == "status-summary":
            summarizer = getattr(engine, "summarize_project_status", None)
            if summarizer is not None:
                return summarizer(project_dir)
            # Older engines without the visual summary still expose a status
            # reader; fall back to the manifest so the human surface degrades
            # to the stable one-line view instead of failing.
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
            return engine.record_generation_attempt(
                project_dir,
                self._required(kwargs, "panel_id"),
                self._required(kwargs, "kind"),
                self._attempt_path(kwargs),
            )
        if command == "promote-attempt":
            return engine.promote_attempt(
                project_dir,
                self._required(kwargs, "panel_id"),
                self._attempt_path(kwargs),
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

    @staticmethod
    def _attempt_path(arguments: dict[str, Any]) -> Path:
        path = arguments.get("path")
        if path:
            return Path(str(path))
        relative_path = arguments.get("relative_path")
        if relative_path:
            return Path(str(relative_path))
        raise TypeError("command requires path or relative_path")
