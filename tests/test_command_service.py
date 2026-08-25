import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.command_service import CommandService


class CommandServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = types.SimpleNamespace(
            doctor_report=Mock(return_value={"healthy": True}),
            init_project=Mock(return_value=Path("demo")),
            read_project_manifest=Mock(return_value={"status": "INIT"}),
            transition=Mock(return_value={"status": "PLANNED"}),
            build_resume_plan=Mock(return_value=[]),
            resume_project=Mock(return_value={"status": "INIT"}),
            invalidate_from=Mock(return_value=["pdf"]),
            record_stage=Mock(return_value={"stage": "planning"}),
            record_generation_attempt=Mock(return_value={"initial": 1}),
            promote_attempt=Mock(return_value=Path("panels/raw/p01-01.png")),
            record_override=Mock(return_value=None),
            finalize_project=Mock(return_value={"status": "COMPLETE"}),
            validate_source_bytes=Mock(),
        )
        self.validation = types.SimpleNamespace(
            validate_project=Mock(return_value=[]),
            ProjectValidationError=ValueError,
        )
        self.lettering = types.SimpleNamespace(letter_project=Mock(return_value=[]))
        self.composition = types.SimpleNamespace(compose_project=Mock(return_value=[]))
        self.export = types.SimpleNamespace(guarded_export=Mock(return_value=Path("out.pdf")))
        self.report = types.SimpleNamespace(render_report=Mock(return_value=Path("report.md")))
        self.service = CommandService(
            engine=self.engine,
            validation=self.validation,
            lettering=self.lettering,
            composition=self.composition,
            export=self.export,
            report=self.report,
        )

    def test_doctor_forwards_the_supplied_image_capability(self):
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": False,
        }

        self.assertEqual(
            {"healthy": True},
            self.service.execute(
                "doctor",
                output_root=Path("out"),
                image_capability=capability,
            ),
        )
        self.engine.doctor_report.assert_called_once_with(Path("out"), image_capability=capability)

    def test_init_forwards_the_supplied_image_capability(self):
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": True,
        }

        self.assertEqual(
            Path("demo"),
            self.service.execute(
                "init",
                output_root=Path("out"),
                title="Demo",
                source=b"story",
                request={},
                image_capability=capability,
            ),
        )
        self.engine.init_project.assert_called_once_with(
            Path("out"), "Demo", b"story", {}, image_capability=capability
        )

    def test_lifecycle_commands_share_one_engine_dispatch(self):
        project = Path("project")
        self.assertEqual({"healthy": True}, self.service.execute("doctor", output_root=Path("out")))
        self.assertEqual(
            Path("demo"),
            self.service.execute(
                "init", output_root=Path("out"), title="Demo", source=b"story", request={}
            ),
        )
        self.engine.init_project.assert_called_once_with(
            Path("out"), "Demo", b"story", {}, page_count=2
        )
        self.assertEqual({"status": "INIT"}, self.service.execute("status", project_dir=project))
        self.assertEqual(
            {"status": "PLANNED"},
            self.service.execute("transition", project_dir=project, target="PLANNED"),
        )
        self.assertEqual([], self.service.execute("validate", project_dir=project, stage="all"))
        self.assertEqual([], self.service.execute("resume-plan", project_dir=project))
        self.assertEqual({"status": "INIT"}, self.service.execute("resume", project_dir=project))
        self.assertEqual(
            ["pdf"], self.service.execute("invalidate", project_dir=project, stage="export")
        )
        self.assertEqual(
            {"stage": "planning"},
            self.service.execute("record-stage", project_dir=project, stage="planning"),
        )
        self.assertEqual(
            {"initial": 1},
            self.service.execute(
                "record-attempt",
                project_dir=project,
                panel_id="p01-01",
                kind="initial",
                path=Path("attempt.png"),
            ),
        )
        self.assertEqual(
            Path("panels/raw/p01-01.png"),
            self.service.execute(
                "promote-attempt", project_dir=project, panel_id="p01-01", path=Path("attempt.png")
            ),
        )
        self.assertIsNone(
            self.service.execute(
                "override-panel", project_dir=project, panel_id="p01-01", reason="review"
            )
        )
        self.engine.record_override.assert_called_once_with(project, "p01-01", "review")
        self.assertEqual(
            {"status": "COMPLETE"}, self.service.execute("finalize", project_dir=project)
        )

    def test_init_forwards_selected_page_scope(self):
        self.assertEqual(
            Path("demo"),
            self.service.execute(
                "init",
                output_root=Path("out"),
                title="Demo",
                source=b"story",
                request={},
                page_count=4,
            ),
        )
        self.engine.init_project.assert_called_once_with(
            Path("out"), "Demo", b"story", {}, page_count=4
        )

    def test_init_forwards_image_capability_and_page_scope(self):
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": True,
        }

        self.assertEqual(
            Path("demo"),
            self.service.execute(
                "init",
                output_root=Path("out"),
                title="Demo",
                source=b"story",
                request={},
                image_capability=capability,
                page_count=4,
            ),
        )
        self.engine.init_project.assert_called_once_with(
            Path("out"),
            "Demo",
            b"story",
            {},
            image_capability=capability,
            page_count=4,
        )

    def test_artifact_commands_use_injected_modules(self):
        project = Path("project")
        self.assertEqual([], self.service.execute("letter", project_dir=project))
        self.assertEqual([], self.service.execute("compose", project_dir=project))
        self.assertEqual(Path("out.pdf"), self.service.execute("export", project_dir=project))
        self.assertEqual(
            Path("report.md"), self.service.execute("render-report", project_dir=project)
        )

    def test_missing_required_arguments_fail_before_engine_call(self):
        with self.assertRaises(TypeError):
            self.service.execute("transition", project_dir=Path("project"))
        self.engine.transition.assert_not_called()

    def test_override_panel_returns_record_override_result(self):
        self.engine.record_override.return_value = None
        result = self.service.execute(
            "override-panel", project_dir=Path("project"), panel_id="p01-01", reason="review"
        )
        self.engine.record_override.assert_called_once_with(Path("project"), "p01-01", "review")
        self.assertIsNone(result)

    def test_validate_returns_issues_when_validation_raises(self):
        issues = [object()]
        self.validation.validate_project = Mock(
            side_effect=self.validation.ProjectValidationError(issues)
        )
        self.validation.ProjectValidationError = type(
            "ProjectValidationError", (ValueError,), {"issues": property(lambda self: issues)}
        )
        raised = self.validation.ProjectValidationError(issues)
        self.validation.validate_project = Mock(side_effect=raised)
        result = self.service.execute("validate", project_dir=Path("project"), stage="all")
        self.assertEqual(issues, result)

    def test_unsupported_command_fails_closed(self):
        with self.assertRaises(ValueError):
            self.service.execute("bogus", project_dir=Path("project"))

    def test_record_attempt_missing_path_fails_before_engine_call(self):
        with self.assertRaises(TypeError) as cm:
            self.service.execute(
                "record-attempt", project_dir=Path("project"), panel_id="p01-01", kind="initial"
            )
        self.assertIn("path or relative_path", str(cm.exception))
        self.engine.record_generation_attempt.assert_not_called()

    def test_promote_attempt_missing_path_fails_before_engine_call(self):
        with self.assertRaises(TypeError) as cm:
            self.service.execute("promote-attempt", project_dir=Path("project"), panel_id="p01-01")
        self.assertIn("path or relative_path", str(cm.exception))
        self.engine.promote_attempt.assert_not_called()

    def test_record_attempt_resolves_relative_path(self):
        result = self.service.execute(
            "record-attempt",
            project_dir=Path("project"),
            panel_id="p01-01",
            kind="initial",
            relative_path=Path("attempt.png"),
        )
        self.assertEqual({"initial": 1}, result)
        self.engine.record_generation_attempt.assert_called_once_with(
            Path("project"), "p01-01", "initial", Path("attempt.png")
        )

    def test_promote_attempt_resolves_relative_path(self):
        result = self.service.execute(
            "promote-attempt",
            project_dir=Path("project"),
            panel_id="p01-01",
            relative_path=Path("attempt.png"),
        )
        self.assertEqual(Path("panels/raw/p01-01.png"), result)
        self.engine.promote_attempt.assert_called_once_with(
            Path("project"), "p01-01", Path("attempt.png")
        )


class HandoffCommandServiceContractTests(unittest.TestCase):
    """WP2 adapters share these dotted, transport-neutral handoff routes."""

    def setUp(self):
        self.engine = types.SimpleNamespace(
            prepare_handoff=Mock(return_value={"phase": "reference"}),
            inspect_handoff=Mock(return_value={"prepared": True}),
            accept_handoff_result=Mock(return_value={"status": "completed"}),
            record_handoff_failure=Mock(return_value={"status": "ready"}),
        )
        self.service = CommandService(engine=self.engine)

    def test_prepare_and_inspect_use_dotted_routes_with_only_the_project(self):
        project = Path("shared/project")
        cases = (
            (
                "handoff.prepare",
                {"phase": "reference"},
                self.engine.prepare_handoff,
            ),
            (
                "handoff.inspect",
                {"prepared": True},
                self.engine.inspect_handoff,
            ),
        )

        for route, expected, engine_call in cases:
            with self.subTest(route=route):
                self.assertEqual(
                    expected,
                    self.service.execute(route, project_dir=project),
                )
                engine_call.assert_called_once_with(project)

    def test_accept_result_forwards_every_argument_exactly(self):
        project = Path("shared/project")
        raster = Path("renderer/result.png")
        capabilities = {
            "reference_images": True,
            "dimensions": True,
            "localized_edit": False,
        }

        self.assertEqual(
            {"status": "completed"},
            self.service.execute(
                "handoff.accept-result",
                project_dir=project,
                job_id="job-" + "a" * 40,
                attempt=2,
                raster_path=raster,
                executor_kind="external-tool",
                executor_id="renderer-a",
                provider="provider-category",
                model="model-category",
                capabilities_used=capabilities,
                approve_reference=True,
            ),
        )

        self.engine.accept_handoff_result.assert_called_once_with(
            project,
            job_id="job-" + "a" * 40,
            attempt=2,
            raster_path=raster,
            executor_kind="external-tool",
            executor_id="renderer-a",
            provider="provider-category",
            model="model-category",
            capabilities_used=capabilities,
            approve_reference=True,
        )

    def test_record_failure_forwards_every_argument_exactly(self):
        project = Path("shared/project")
        capabilities = {
            "reference_images": False,
            "dimensions": True,
            "localized_edit": True,
        }

        self.assertEqual(
            {"status": "ready"},
            self.service.execute(
                "handoff.record-failure",
                project_dir=project,
                job_id="job-" + "b" * 40,
                attempt=1,
                executor_kind="native-tool",
                executor_id="renderer-b",
                category="provider-refusal",
                provider="provider-category",
                model="model-category",
                capabilities_used=capabilities,
            ),
        )

        self.engine.record_handoff_failure.assert_called_once_with(
            project,
            job_id="job-" + "b" * 40,
            attempt=1,
            executor_kind="native-tool",
            executor_id="renderer-b",
            category="provider-refusal",
            provider="provider-category",
            model="model-category",
            capabilities_used=capabilities,
        )

    def test_missing_handoff_arguments_fail_before_engine_calls(self):
        cases = (
            ("handoff.accept-result", self.engine.accept_handoff_result),
            ("handoff.record-failure", self.engine.record_handoff_failure),
        )
        for command, engine_call in cases:
            with self.subTest(command=command), self.assertRaises(TypeError):
                self.service.execute(command, project_dir=Path("shared/project"))
            engine_call.assert_not_called()


class StarterCommandServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = types.SimpleNamespace(
            init_project=Mock(return_value=Path("starter-demo")),
            validate_source_bytes=Mock(),
        )
        self.service = CommandService(engine=self.engine)

    def test_init_forwards_starter_without_blank_inputs(self):
        self.assertEqual(
            Path("starter-demo"),
            self.service.execute(
                "init",
                output_root=Path("out"),
                title="Demo",
                starter="minimal-one-page",
            ),
        )
        self.engine.validate_source_bytes.assert_not_called()
        self.engine.init_project.assert_called_once_with(
            Path("out"), "Demo", starter="minimal-one-page"
        )

    def test_init_rejects_each_starter_conflict_before_engine_call(self):
        conflicts = (
            {"source": b"story"},
            {"request": {}},
            {"page_count": 1},
        )
        for conflict in conflicts:
            with (
                self.subTest(conflict=conflict),
                self.assertRaisesRegex(ValueError, "cannot be combined"),
            ):
                self.service.execute(
                    "init",
                    output_root=Path("out"),
                    title="Demo",
                    starter="minimal-one-page",
                    **conflict,
                )
        self.engine.init_project.assert_not_called()
