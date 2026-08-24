import io
import json
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from comic_sol_product import __version__

from comic_sol_product import cli
from comic_sol_product.config import default_output_root

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ProductCliTests(unittest.TestCase):
    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_uses_installed_command_name(self):
        self.assertEqual("comic-sol", cli.build_parser().prog)

    def test_version_flag_reports_release_identity(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as exit_context, redirect_stdout(stdout):
            cli.build_parser().parse_args(["--version"])
        self.assertEqual(0, exit_context.exception.code)
        self.assertEqual(f"comic-sol {__version__}\n", stdout.getvalue())

    def test_default_output_roots_are_platform_native(self):
        home = Path("/users/example")
        self.assertEqual(home / "Comic Sol", default_output_root("linux", home))
        self.assertEqual(home / "Documents/Comic Sol", default_output_root("darwin", home))
        self.assertEqual(home / "Documents/Comic Sol", default_output_root("win32", home))

    def test_json_doctor_returns_stable_success_envelope(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            code, stdout, stderr = self.invoke(
                [
                    "--json",
                    "doctor",
                    "--output-root",
                    str(Path(temporary_directory) / "output"),
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual({"ok", "command", "data", "error"}, set(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual("doctor", payload["command"])
        self.assertIsNone(payload["error"])
        self.assertTrue(payload["data"]["healthy"])
        self.assertTrue(payload["data"]["ready"])
        self.assertIsInstance(payload["data"]["messages"], list)
        image_check = next(
            check for check in payload["data"]["checks"] if check["id"] == "image-capability"
        )
        self.assertEqual("unknown", image_check["details"]["readiness"])

    def test_json_doctor_transports_supplied_image_capability(self):
        cases = (
            (
                [
                    "--image-capability-status",
                    "available",
                    "--image-capability-name",
                    "agent-image-generation",
                    "--supports-reference-images",
                    "--supports-dimensions",
                ],
                "pass",
                "healthy",
                {
                    "status": "available",
                    "name": "agent-image-generation",
                    "supports_reference_images": True,
                    "supports_dimensions": True,
                },
            ),
            (
                [
                    "--image-capability-status",
                    "available",
                    "--image-capability-name",
                    "agent-image-generation",
                ],
                "warn",
                "partial",
                {
                    "status": "available",
                    "name": "agent-image-generation",
                    "supports_reference_images": False,
                    "supports_dimensions": False,
                },
            ),
            (
                ["--image-capability-status", "unavailable"],
                "warn",
                "missing",
                {
                    "status": "unavailable",
                    "name": None,
                    "supports_reference_images": False,
                    "supports_dimensions": False,
                },
            ),
        )
        for flags, expected_status, expected_readiness, expected_capability in cases:
            with self.subTest(readiness=expected_readiness), tempfile.TemporaryDirectory() as raw:
                code, stdout, stderr = self.invoke(
                    [
                        "--json",
                        "doctor",
                        "--output-root",
                        str(Path(raw) / "output"),
                        *flags,
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            payload = json.loads(stdout)
            self.assertEqual({"ok", "command", "data", "error"}, set(payload))
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["data"]["ready"])
            self.assertTrue(payload["data"]["healthy"])
            image_check = next(
                check for check in payload["data"]["checks"] if check["id"] == "image-capability"
            )
            self.assertEqual(expected_status, image_check["status"])
            self.assertEqual(
                {
                    "readiness": expected_readiness,
                    "capability": expected_capability,
                },
                image_check["details"],
            )

    def test_safe_message_redacts_absolute_paths_with_spaces(self):
        for path in (
            "/tmp/Comic Sol/private source.txt",
            r"C:\Users\Comic Sol\private source.txt",
        ):
            with self.subTest(path=path):
                message = cli._safe_message(OSError(f"cannot read '{path}'"))

                self.assertEqual("cannot read '<path>'", message)
                self.assertNotIn(path, message)

    def test_invalid_source_extension_is_categorized_before_allocation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "story.png"
            source.write_bytes(b"not an image or text source")
            request = root / "request.json"
            request.write_text('{"language":"en","mode":"short_prompt"}', encoding="utf-8")
            output = root / "output"

            code, stdout, stderr = self.invoke(
                [
                    "--json",
                    "init",
                    "--output-root",
                    str(output),
                    "--title",
                    "Rejected Source",
                    "--source",
                    str(source),
                    "--request-json",
                    str(request),
                ]
            )

            self.assertEqual(2, code)
            self.assertEqual("", stderr)
            payload = json.loads(stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("init", payload["command"])
            self.assertIsNone(payload["data"])
            self.assertEqual("invalid-data", payload["error"]["category"])
            self.assertEqual("invalid-input", payload["error"]["legacy_category"])
            self.assertEqual("CS-PROJ-001", payload["error"]["code"])
            self.assertTrue(payload["error"]["reason"])
            self.assertTrue(payload["error"]["recovery"])
            self.assertNotIn(str(root), payload["error"]["message"])
            self.assertFalse(output.exists())

    def test_human_failure_includes_safe_actionable_detail(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "story.png"
            source.write_bytes(b"not an image or text source")
            request = root / "request.json"
            request.write_text('{"language":"en","mode":"short_prompt"}', encoding="utf-8")

            code, stdout, stderr = self.invoke(
                [
                    "init",
                    "--output-root",
                    str(root / "output"),
                    "--title",
                    "Rejected Source",
                    "--source",
                    str(source),
                    "--request-json",
                    str(request),
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("source file must use .txt or .md", stderr)
        self.assertNotIn(str(source), stderr)

    def test_setup_and_repair_pass_the_current_console_launcher(self):
        for command in ("setup", "repair"):
            with self.subTest(command=command):
                arguments = cli.build_parser().parse_args(
                    [command, "--output-root", "/tmp/projects", "--client", "codex"]
                )
                with (
                    mock.patch.object(cli.sys, "argv", ["/opt/Comic Sol/bin/comic-sol"]),
                    mock.patch("comic_sol_product.setup.setup_clients", return_value=[]) as setup,
                ):
                    self.assertEqual([], cli._run(arguments))

                setup.assert_called_once_with(
                    arguments.output_root,
                    selected=["codex"],
                    executable="/opt/Comic Sol/bin/comic-sol",
                )

    def test_setup_passes_the_frozen_executable(self):
        arguments = cli.build_parser().parse_args(
            ["setup", "--output-root", "/tmp/projects", "--client", "codex"]
        )
        launcher = "/Applications/Comic Sol.app/Contents/MacOS/comic-sol"
        with (
            mock.patch.object(cli.sys, "frozen", True, create=True),
            mock.patch.object(cli.sys, "executable", launcher),
            mock.patch("comic_sol_product.setup.setup_clients", return_value=[]) as setup,
        ):
            self.assertEqual([], cli._run(arguments))

        setup.assert_called_once_with(
            arguments.output_root,
            selected=["codex"],
            executable=launcher,
        )

    def test_status_routes_through_locked_recovery_without_changing_response(self):
        expected = {"project_id": "project", "status": "BLOCKED"}
        seen = []

        class FakeEngine:
            def read_project_status(self, project_dir):
                seen.append(project_dir)
                return expected

        arguments = cli.build_parser().parse_args(["status", "/tmp/project"])
        with mock.patch.object(cli, "_load_engine", return_value=FakeEngine()):
            self.assertIs(expected, cli._run(arguments))

        self.assertEqual([Path("/tmp/project")], seen)

    def test_human_status_renders_visual_summary_on_stdout(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "valid-one-page"
            shutil.copytree(_FIXTURES / "valid-one-page", project)

            code, stdout, stderr = self.invoke(["status", str(project)])

        self.assertEqual(0, code)
        # The stable one-line contract stays the first line for scripts.
        self.assertEqual("valid-one-page: QA_READY", stdout.splitlines()[0])
        # The visual summary adds per-stage, panel, and next-action sections.
        self.assertIn("Stages:", stdout)
        self.assertIn("planning:", stdout)
        self.assertIn("Panels: 3 accepted, 0 failed, 0 pending", stdout)
        self.assertIn("Next action:", stdout)
        # Human output stays plain: no ANSI colour escapes.
        self.assertNotIn("\x1b[", stdout)

    def test_human_status_surfaces_unreadable_panel_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "valid-one-page"
            shutil.copytree(_FIXTURES / "valid-one-page", project)
            (project / "qa/panels/p01-02.json").write_text("{ broken", encoding="utf-8")

            code, stdout, stderr = self.invoke(["status", str(project)])

        self.assertEqual(0, code)
        self.assertIn("Panels: 2 accepted, 0 failed, 0 pending", stdout)
        self.assertIn("unreadable", stdout)

    def test_human_status_escapes_terminal_controls_in_all_project_fields(self):
        """Project values stay visible without becoming terminal instructions."""
        expected = {"project_id": "project", "status": "COMPLETE"}
        malicious_warning = "warning \x1b[31mred\x1b[0m and \x1b[2J clear"
        expected_summary = {
            "project_id": "project\x1b]0;renamed\x07",
            "status": "COMPLETE\rINJECTED",
            "stages": [{"stage": "generation\nforged", "state": "blocked\tfake"}],
            "panels": {"accepted": "0\x1b[2J", "failed": 0, "pending": 1},
            "warnings": [malicious_warning],
            "blocked_reason": "unsafe\x1b[?25l",
            "next_action": {"required": "capability\x1b[5m available"},
        }

        class FakeEngine:
            def read_project_status(self, project_dir):
                return expected

        class FakeCommandService:
            def __init__(self, **kwargs):
                pass

            def execute(self, command, **kwargs):
                return expected if command == "status" else expected_summary

        fake_module = types.SimpleNamespace(CommandService=FakeCommandService)
        with (
            mock.patch.object(cli, "_load_engine", return_value=FakeEngine()),
            mock.patch.object(cli, "_load_command_service", return_value=fake_module),
        ):
            code, stdout, stderr = self.invoke(["status", "/tmp/project"])

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertNotIn("\x1b", stdout)
        self.assertNotIn("\r", stdout)
        self.assertNotIn("\t", stdout)
        self.assertIn("project\\x1b]0;renamed\\x07: COMPLETE\\rINJECTED", stdout)
        self.assertIn("generation\\nforged: blocked\\tfake", stdout)
        self.assertIn("0\\x1b[2J accepted", stdout)
        self.assertIn("warning \\x1b[31mred\\x1b[0m", stdout)
        self.assertIn("Blocked reason: unsafe\\x1b[?25l", stdout)
        self.assertIn("capability\\x1b[5m available", stdout)

    def test_json_status_envelope_stays_the_unchanged_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "valid-one-page"
            shutil.copytree(_FIXTURES / "valid-one-page", project)
            expected_manifest = json.loads((project / "project.json").read_text("utf-8"))

            code, stdout, stderr = self.invoke(["--json", "status", str(project)])

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual({"ok", "command", "data", "error"}, set(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual("status", payload["command"])
        # The machine-readable status is exactly the recovered manifest; the
        # visual summary never leaks into the JSON envelope.
        self.assertEqual(expected_manifest, payload["data"])

    def test_human_lifecycle_progress_is_stage_aware_and_plain(self):
        events = [
            {
                "status": "working",
                "stage": "lettering",
                "completed": [],
                "remaining": ["composition", "export"],
            },
            {
                "status": "working",
                "stage": "composition",
                "completed": ["lettering"],
                "remaining": ["export"],
            },
            {
                "status": "complete",
                "stage": "export",
                "completed": ["lettering", "composition", "export"],
                "remaining": [],
            },
        ]

        class FakeEngine:
            def finalize_project(self, project_dir, *, progress=None):
                for event in events:
                    progress(event)
                return {
                    "status": "COMPLETE",
                    "pdf": "exports/project.pdf",
                    "report": "qa/report.md",
                }

        with mock.patch.object(cli, "_load_engine", return_value=FakeEngine()):
            code, stdout, stderr = self.invoke(["finalize", "/tmp/project"])

        self.assertEqual(0, code)
        self.assertIn("WORKING stage=lettering", stderr)
        self.assertIn("WORKING stage=composition", stderr)
        self.assertIn("COMPLETE stage=export", stderr)
        self.assertNotIn("\\x1b[", stderr)
        self.assertIn('"status": "COMPLETE"', stdout)

    def test_json_lifecycle_progress_stays_out_of_stdout_and_stderr(self):
        seen = []

        class FakeEngine:
            def finalize_project(self, project_dir, *, progress=None):
                seen.append(progress)
                progress({"status": "working", "stage": "export", "completed": [], "remaining": []})
                return {
                    "status": "COMPLETE",
                    "pdf": "exports/project.pdf",
                    "report": "qa/report.md",
                }

        with mock.patch.object(cli, "_load_engine", return_value=FakeEngine()):
            code, stdout, stderr = self.invoke(["--json", "finalize", "/tmp/project"])

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertIsNotNone(seen[0])
        self.assertEqual({"ok", "command", "data", "error"}, set(json.loads(stdout)))
        self.assertEqual("COMPLETE", json.loads(stdout)["data"]["status"])

    def test_human_progress_renderer_distinguishes_blocked_failed_and_complete(self):
        reporter = cli._ProgressReporter(as_json=False)
        for event in (
            {"status": "blocked", "stage": "generation"},
            {"status": "failed", "stage": "export"},
            {"status": "complete", "stage": "export"},
        ):
            reporter(event)

        self.assertEqual(
            [
                "BLOCKED stage=generation",
                "FAILED stage=export",
                "COMPLETE stage=export",
            ],
            reporter.lines,
        )

    def test_broken_progress_stream_does_not_change_lifecycle_result(self):
        class BrokenStream:
            def write(self, value):
                raise OSError("closed stderr")

            def flush(self):
                raise OSError("closed stderr")

        reporter = cli._ProgressReporter(as_json=False, stream=BrokenStream())
        reporter({"status": "working", "stage": "export"})
        reporter.failure()
        self.assertEqual(["WORKING stage=export", "FAILED stage=export"], reporter.lines)

    def test_resume_intermediate_status_is_not_reported_as_complete(self):
        class FakeEngine:
            def resume_project(self, project_dir, *, progress=None):
                progress({"status": "working", "stage": "resume"})
                return {
                    "status": "INIT",
                    "preserved": [],
                    "invalidated": ["planning"],
                    "next_action": "run planning",
                }

        with mock.patch.object(cli, "_load_engine", return_value=FakeEngine()):
            code, stdout, stderr = self.invoke(["resume", "/tmp/project"])

        self.assertEqual(0, code)
        self.assertNotIn("COMPLETE stage=resume", stderr)
        self.assertIn('"status": "INIT"', stdout)


class FailClosedContractTests(unittest.TestCase):
    """The CLI must emit one canonical envelope for every failure path."""

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_parse_error_emits_exactly_one_envelope(self):
        for argv in (["--json", "validate"], ["validate", "--json"], ["--json"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke(argv)

                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual(1, len(stdout.splitlines()))
                payload = json.loads(stdout)
                self.assertEqual({"ok", "command", "data", "error"}, set(payload))
                self.assertFalse(payload["ok"])
                self.assertIsNone(payload["command"])
                self.assertIsNone(payload["data"])
                self.assertEqual("CS-CLI-001", payload["error"]["code"])
                self.assertEqual("invalid-request", payload["error"]["category"])
                self.assertTrue(payload["error"]["recovery"])
                self.assertTrue(payload["error"]["detail"])

    def test_human_parse_error_stays_on_stderr(self):
        code, stdout, stderr = self.invoke(["validate"])

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("ERROR CS-CLI-001 [invalid-request]", stderr)
        self.assertIn("Recovery:", stderr)

    def test_version_and_help_remain_successful_exits(self):
        for argv in (["--version"], ["--help"]):
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                with self.assertRaises(SystemExit) as exit_context, redirect_stdout(stdout):
                    cli.main(argv)
                self.assertEqual(0, exit_context.exception.code)
                self.assertTrue(stdout.getvalue())

    def test_json_unexpected_exception_fails_closed_with_redacted_detail(self):
        secret = r"C:\Users\acer\Comic Sol\private"
        with mock.patch.object(cli, "_load_engine", side_effect=KeyError(f"lost {secret}")):
            code, stdout, stderr = self.invoke(["--json", "status", "/tmp/project"])

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("status", payload["command"])
        self.assertEqual("CS-PROJ-005", payload["error"]["code"])
        self.assertEqual("internal-error", payload["error"]["category"])
        # A space-containing path cannot be bounded token-wise, so the whole
        # detail collapses to the exception type name instead of leaking it.
        self.assertEqual("KeyError", payload["error"]["detail"])
        self.assertNotIn("private", json.dumps(payload))
        self.assertNotIn("acer", json.dumps(payload))

    def test_json_unexpected_exception_redacts_quoted_paths_in_detail(self):
        secret = "/var/Comic Sol/private"
        with mock.patch.object(
            cli, "_load_engine", side_effect=KeyError(f"cannot stat '{secret}'")
        ):
            code, stdout, stderr = self.invoke(["--json", "status", "/tmp/project"])

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("CS-PROJ-005", payload["error"]["code"])
        self.assertIn("<path>", payload["error"]["detail"])
        self.assertNotIn("private", json.dumps(payload))

    def test_human_unexpected_exception_fails_closed_without_traceback(self):
        with mock.patch.object(cli, "_load_engine", side_effect=AttributeError("boom")):
            code, stdout, stderr = self.invoke(["status", "/tmp/project"])

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("ERROR CS-PROJ-005 [internal-error]", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_json_validate_reports_issues_as_failure_with_data_intact(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            (project / "project.json").write_text("{ malformed", encoding="utf-8")

            code, stdout, stderr = self.invoke(["--json", "validate", str(project)])

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("validate", payload["command"])
        self.assertEqual("CS-QA-001", payload["error"]["code"])
        self.assertEqual("quality-error", payload["error"]["category"])
        self.assertIsInstance(payload["data"], list)
        self.assertTrue(payload["data"])
        self.assertTrue(
            all({"path", "field", "message"} <= set(issue) for issue in payload["data"])
        )

    def test_human_validate_prints_issues_on_stdout_and_error_on_stderr(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()
            (project / "project.json").write_text("{ malformed", encoding="utf-8")

            code, stdout, stderr = self.invoke(["validate", str(project)])

        self.assertEqual(2, code)
        issues = json.loads(stdout)
        self.assertTrue(issues)
        self.assertIn("ERROR CS-QA-001 [quality-error]", stderr)

    def test_json_validate_without_issues_remains_a_success_envelope(self):
        class FakeValidation:
            class ProjectValidationError(ValueError):
                def __init__(self, issues):
                    self.issues = tuple(issues)

            @staticmethod
            def validate_project(project_dir, stage):
                return []

        with (
            mock.patch.object(cli, "_load_engine"),
            mock.patch.object(cli, "_load_engine_module", return_value=FakeValidation),
        ):
            code, stdout, stderr = self.invoke(["--json", "validate", "/tmp/project"])

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["data"])
        self.assertIsNone(payload["error"])

    def test_engine_validation_error_uses_the_same_fail_closed_envelope(self):
        from dataclasses import dataclass

        @dataclass
        class _Issue:
            path: str
            field: str
            message: str

        class FakeValidation:
            class ProjectValidationError(ValueError):
                def __init__(self, issues):
                    self.issues = tuple(issues)

            @staticmethod
            def validate_project(project_dir, stage):
                raise FakeValidation.ProjectValidationError(
                    [_Issue("project.json", "status", "unknown manifest status")]
                )

        with (
            mock.patch.object(cli, "_load_engine"),
            mock.patch.object(cli, "_load_engine_module", return_value=FakeValidation),
        ):
            code, stdout, stderr = self.invoke(["--json", "validate", "/tmp/project"])

        self.assertEqual(2, code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("CS-QA-001", payload["error"]["code"])
        self.assertEqual(
            [{"path": "project.json", "field": "status", "message": "unknown manifest status"}],
            payload["data"],
        )

    def test_json_unknown_stage_is_invalid_data_not_an_mcp_request_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            project.mkdir()

            code, stdout, stderr = self.invoke(
                ["--json", "validate", str(project), "--stage", "bogus"]
            )

        self.assertEqual(2, code)
        payload = json.loads(stdout)
        self.assertEqual("CS-PROJ-001", payload["error"]["code"])
        self.assertEqual("invalid-data", payload["error"]["category"])
        self.assertEqual("invalid-input", payload["error"]["legacy_category"])

    def test_json_parse_error_redacts_absolute_paths_in_detail(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = root / "request.json"
            request.write_text("{ malformed json", encoding="utf-8")
            source = root / "story.txt"
            source.write_text("A story.", encoding="utf-8")
            output = root / "output"

            class FakeEngine:
                def validate_source_bytes(self, source, suffix):
                    pass

                def read_json(self, path):
                    # Read the actual file to trigger JSON parse error with real path
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)

            with mock.patch.object(cli, "_load_engine", return_value=FakeEngine()):
                code, stdout, stderr = self.invoke(
                    [
                        "--json",
                        "init",
                        "--output-root",
                        str(output),
                        "--title",
                        "Test Project",
                        "--source",
                        str(source),
                        "--request-json",
                        str(request),
                    ]
                )

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("init", payload["command"])
        self.assertEqual("CS-PROJ-001", payload["error"]["code"])
        self.assertEqual("invalid-data", payload["error"]["category"])
        self.assertEqual("invalid-input", payload["error"]["legacy_category"])
        # Verify the error detail is redacted and doesn't leak the absolute path
        self.assertIn("detail", payload["error"])
        self.assertNotIn(str(request), json.dumps(payload))
        self.assertNotIn(str(root), json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
