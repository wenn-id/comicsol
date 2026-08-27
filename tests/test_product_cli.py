import ast
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
from scripts.handoff import HandoffContractError, HandoffResultError, StaleLockedScopeError

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

    def test_guided_init_uses_sensible_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "projects"
            with (
                mock.patch.object(cli, "default_output_root", return_value=output),
                mock.patch(
                    "builtins.input",
                    side_effect=["", "", "", "", "A courier carries the last light.", ""],
                ),
            ):
                code, stdout, stderr = self.invoke(["init", "--interactive"])

            project = output / stdout.strip()
            manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
            request = json.loads((project / "source/request.json").read_text(encoding="utf-8"))
            persisted_source = (project / "source/input.txt").read_bytes()

        self.assertEqual(0, code)
        self.assertEqual("comic-sol-project", stdout.strip())
        self.assertIn("Comic Sol guided project initializer", stderr)
        self.assertIn("Project name", stderr)
        self.assertIn("Starter", stderr)
        self.assertIn("Page count", stderr)
        self.assertIn("Story source", stderr)
        self.assertIn("Output location", stderr)
        self.assertEqual("Comic Sol Project", manifest["title"])
        self.assertEqual(2, manifest["settings"]["page_count"])
        self.assertEqual({"language": "en", "mode": "short_prompt"}, request)
        self.assertEqual(b"A courier carries the last light.", persisted_source)

    def test_guided_and_noninteractive_init_have_identical_project_schema(self):
        def shape(value):
            if isinstance(value, dict):
                return {key: shape(nested) for key, nested in value.items()}
            if isinstance(value, list):
                return [shape(nested) for nested in value]
            return type(value).__name__

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "story.md"
            source.write_text("A lighthouse wakes beneath the city.", encoding="utf-8")
            guided_root = root / "guided"
            automated_root = root / "automated"
            with mock.patch(
                "builtins.input",
                side_effect=[
                    "Schema Parity",
                    "",
                    "4",
                    "file",
                    str(source),
                    str(guided_root),
                ],
            ):
                guided_code, guided_stdout, _ = self.invoke(["init", "--interactive"])
            with mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
                automated_code, automated_stdout, automated_stderr = self.invoke(
                    [
                        "--json",
                        "init",
                        "--output-root",
                        str(automated_root),
                        "--title",
                        "Schema Parity",
                        "--source",
                        str(source),
                        "--page-count",
                        "4",
                    ]
                )

            automated_payload = json.loads(automated_stdout)
            guided_project = guided_root / guided_stdout.strip()
            automated_project = automated_root / automated_payload["data"]["project_id"]
            guided_manifest = json.loads(
                (guided_project / "project.json").read_text(encoding="utf-8")
            )
            automated_manifest = json.loads(
                (automated_project / "project.json").read_text(encoding="utf-8")
            )

            self.assertEqual(0, guided_code)
            self.assertEqual(0, automated_code)
            self.assertEqual("", automated_stderr)
            self.assertEqual(shape(guided_manifest), shape(automated_manifest))
            self.assertEqual(
                (guided_project / "source/input.txt").read_bytes(),
                (automated_project / "source/input.txt").read_bytes(),
            )
            self.assertEqual(
                (guided_project / "source/request.json").read_bytes(),
                (automated_project / "source/request.json").read_bytes(),
            )
            self.assertEqual(4, automated_manifest["settings"]["page_count"])

    def test_guided_init_can_select_a_starter_without_story_prompts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "projects"
            with mock.patch(
                "builtins.input",
                side_effect=["Guided Starter", "minimal-one-page", str(output)],
            ):
                code, stdout, stderr = self.invoke(["init", "--interactive"])

            project = output / stdout.strip()
            manifest = json.loads((project / "project.json").read_text("utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("STORYBOARDED", manifest["status"])
        self.assertEqual(1, manifest["settings"]["page_count"])
        self.assertIn("Starter", stderr)
        self.assertNotIn("Story source", stderr)
        self.assertNotIn("Page count", stderr)

    def test_noninteractive_starter_rejects_explicit_blank_inputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "story.md"
            source.write_text("conflict", encoding="utf-8")
            for flag in (
                ["--source", str(source)],
                ["--request-json", str(root / "request.json")],
                ["--page-count", "1"],
            ):
                with self.subTest(flag=flag):
                    output = root / ("output-" + flag[0].removeprefix("--"))
                    code, stdout, stderr = self.invoke(
                        [
                            "--json",
                            "init",
                            "--output-root",
                            str(output),
                            "--title",
                            "Conflict",
                            "--starter",
                            "minimal-one-page",
                            *flag,
                        ]
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stderr)
                    self.assertFalse(json.loads(stdout)["ok"])
                    self.assertFalse(output.exists())

    def test_guided_init_reprompts_invalid_page_scope_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "projects"
            with (
                mock.patch.object(cli, "default_output_root", return_value=output),
                mock.patch("builtins.input", side_effect=["Project", "", "0", "5", EOFError()]),
            ):
                code, stdout, stderr = self.invoke(["init", "--interactive"])

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertGreaterEqual(stderr.count("Page count must be"), 2)
            self.assertIn("interactive initialization ended before completion", stderr)
            self.assertFalse(output.exists())

    def test_interactive_init_rejects_json_and_data_flags_before_prompting(self):
        cases = (
            (["--json", "init", "--interactive"], True),
            (["init", "--interactive", "--title", "Project"], False),
        )
        for argv, as_json in cases:
            with (
                self.subTest(argv=argv),
                mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
            ):
                code, stdout, stderr = self.invoke(argv)

            self.assertEqual(2, code)
            if as_json:
                self.assertEqual("", stderr)
                payload = json.loads(stdout)
                self.assertEqual("CS-CLI-001", payload["error"]["code"])
            else:
                self.assertEqual("", stdout)
                self.assertIn("CS-CLI-001", stderr)

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

    def test_setup_passes_the_current_console_launcher(self):
        arguments = cli.build_parser().parse_args(
            ["setup", "--output-root", "/tmp/projects", "--client", "codex"]
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

    def test_repair_routes_dry_run_to_the_repair_workflow(self):
        arguments = cli.build_parser().parse_args(
            [
                "repair",
                "--dry-run",
                "--output-root",
                "/tmp/projects",
                "--client",
                "codex",
            ]
        )
        with (
            mock.patch.object(cli.sys, "argv", ["/opt/Comic Sol/bin/comic-sol"]),
            mock.patch("comic_sol_product.setup.repair_clients", return_value=[]) as repair,
        ):
            self.assertEqual([], cli._run(arguments))

        repair.assert_called_once_with(
            arguments.output_root,
            selected=["codex"],
            executable="/opt/Comic Sol/bin/comic-sol",
            dry_run=True,
        )

    def test_unknown_repair_client_is_a_usage_error(self):
        code, stdout, stderr = self.invoke(["--json", "repair", "--client", "curser"])

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("CS-CLI-001", payload["error"]["code"])

    def test_json_repair_prioritizes_rollback_failure(self):
        safe_failure = {
            "client": "cursor",
            "state": "failure",
            "status": "failed",
            "action": "none",
            "config_path": None,
            "backup_path": None,
            "backup_required": False,
            "planned_entry": None,
            "verified": False,
            "restored": None,
            "message": "repair failed",
            "error": {"code": "CS-INSTALL-002"},
        }
        rollback_failure = {
            **safe_failure,
            "client": "codex",
            "status": "rollback-failed",
            "backup_path": "/private/config.toml.bak",
            "error": {"code": "CS-INSTALL-003"},
        }
        with mock.patch.object(cli, "_run", return_value=[safe_failure, rollback_failure]):
            code, stdout, stderr = self.invoke(["--json", "repair"])

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("CS-INSTALL-003", payload["error"]["code"])

    def test_json_repair_failure_returns_data_and_structured_error(self):
        result = {
            "client": "cursor",
            "state": "failure",
            "status": "rollback-failed",
            "action": "set-comic-sol-entry",
            "config_path": "/private/config.json",
            "backup_path": "/private/config.json.bak",
            "backup_required": True,
            "planned_entry": {
                "command": "/opt/comic-sol",
                "args": ["mcp", "--root", "/tmp/projects"],
            },
            "verified": False,
            "restored": False,
            "message": "rollback could not be verified",
            "error": {
                "code": "CS-INSTALL-003",
                "category": "rollback-failed",
                "message": "Comic Sol could not verify client configuration rollback.",
                "reason": "A failed repair could not prove restoration.",
                "recovery": "Restore the reported backup, then run comic-sol doctor.",
                "command": "repair",
            },
        }
        with mock.patch.object(cli, "_run", return_value=[result]):
            code, stdout, stderr = self.invoke(["--json", "repair"])

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("repair", payload["command"])
        self.assertEqual([result], payload["data"])
        self.assertEqual(result["error"], payload["error"])

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


class HandoffProductCliContractTests(unittest.TestCase):
    """Authoritative WP2 installed-CLI grammar and transport contracts."""

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_with_service(self, argv: list[str], service) -> tuple[int, str, str]:
        service_module = types.SimpleNamespace(
            CommandService=mock.Mock(return_value=service),
        )
        with (
            mock.patch.object(cli, "_load_engine", return_value=types.SimpleNamespace()),
            mock.patch.object(cli, "_load_engine_module", return_value=types.SimpleNamespace()),
            mock.patch.object(cli, "_load_command_service", return_value=service_module),
        ):
            return self.invoke(argv)

    @staticmethod
    def success_data() -> dict[str, dict[str, object]]:
        digest = "a" * 64
        return {
            "handoff.prepare": {
                "project_id": "demo",
                "phase": "reference",
                "locked_scope_sha256": digest,
                "changed": True,
                "migrated": False,
                "manifest_path": "handoff/manifest.json",
                "batch_count": 1,
                "job_counts": {
                    "missing": 0,
                    "ready": 1,
                    "completed": 0,
                    "failed": 0,
                    "stale": 0,
                },
                "next_action": "render-references",
            },
            "handoff.inspect": {
                "prepared": True,
                "phase": "reference",
                "project_stage": "STORYBOARDED",
                "scope_state": "current",
                "batches": [],
                "jobs": [],
                "next_action": "render-references",
            },
            "handoff.accept-result": {
                "job_id": "job-" + "a" * 40,
                "attempt_id": "attempt-" + "b" * 40,
                "receipt_path": "generation/receipts/attempt.json",
                "raster_path": "references/attempts/mira/initial-001.png",
                "raster_sha256": digest,
                "duplicate": False,
                "status": "completed",
                "counters": None,
                "activated_reference_path": "references/characters/mira.png",
                "activated_reference_sha256": digest,
            },
            "handoff.record-failure": {
                "job_id": "job-" + "b" * 40,
                "attempt_id": "attempt-" + "c" * 40,
                "receipt_path": "generation/receipts/attempt.json",
                "category": "provider-refusal",
                "duplicate": False,
                "attempts_used": 1,
                "attempts_remaining": 2,
                "next_attempt": 2,
                "status": "ready",
            },
        }

    def test_nested_routes_forward_exactly_and_use_canonical_envelope_names(self):
        project = Path("/shared/demo")
        raster = Path("/renderer/result.png")
        job_a = "job-" + "a" * 40
        job_b = "job-" + "b" * 40
        cases = (
            (
                ["--json", "handoff", "prepare", str(project)],
                "handoff.prepare",
                {"project_dir": project},
            ),
            (
                ["--json", "handoff", "inspect", str(project)],
                "handoff.inspect",
                {"project_dir": project},
            ),
            (
                [
                    "--json",
                    "handoff",
                    "accept-result",
                    str(project),
                    "--job",
                    job_a,
                    "--attempt",
                    "2",
                    "--path",
                    str(raster),
                    "--executor-kind",
                    "external-tool",
                    "--executor-id",
                    "renderer-a",
                    "--provider",
                    "provider-category",
                    "--model",
                    "model-category",
                    "--used-reference-images",
                    "--used-dimensions",
                    "--approve-reference",
                ],
                "handoff.accept-result",
                {
                    "project_dir": project,
                    "job_id": job_a,
                    "attempt": 2,
                    "raster_path": raster,
                    "executor_kind": "external-tool",
                    "executor_id": "renderer-a",
                    "provider": "provider-category",
                    "model": "model-category",
                    "capabilities_used": {
                        "reference_images": True,
                        "dimensions": True,
                        "localized_edit": False,
                    },
                    "approve_reference": True,
                },
            ),
            (
                [
                    "--json",
                    "handoff",
                    "record-failure",
                    str(project),
                    "--job",
                    job_b,
                    "--attempt",
                    "1",
                    "--executor-kind",
                    "native-tool",
                    "--executor-id",
                    "renderer-b",
                    "--category",
                    "provider-refusal",
                    "--used-localized-edit",
                ],
                "handoff.record-failure",
                {
                    "project_dir": project,
                    "job_id": job_b,
                    "attempt": 1,
                    "executor_kind": "native-tool",
                    "executor_id": "renderer-b",
                    "category": "provider-refusal",
                    "provider": None,
                    "model": None,
                    "capabilities_used": {
                        "reference_images": False,
                        "dimensions": False,
                        "localized_edit": True,
                    },
                },
            ),
        )
        results = self.success_data()
        for argv, route, expected_arguments in cases:
            service = types.SimpleNamespace(
                execute=mock.Mock(return_value=results[route]),
            )
            with self.subTest(route=route):
                code, stdout, stderr = self.invoke_with_service(argv, service)

                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                self.assertEqual(1, len(stdout.splitlines()))
                payload = json.loads(stdout)
                self.assertEqual({"ok", "command", "data", "error"}, set(payload))
                self.assertEqual(
                    {
                        "ok": True,
                        "command": route,
                        "data": results[route],
                        "error": None,
                    },
                    payload,
                )
                service.execute.assert_called_once_with(route, **expected_arguments)

    def test_human_summary_stays_on_stdout_and_advisory_stays_on_stderr(self):
        data = self.success_data()["handoff.prepare"]

        def execute(command, **kwargs):
            print("WORKING handoff.prepare", file=cli.sys.stderr)
            return data

        service = types.SimpleNamespace(execute=mock.Mock(side_effect=execute))
        code, stdout, stderr = self.invoke_with_service(
            ["handoff", "prepare", "/shared/demo"], service
        )

        self.assertEqual(0, code)
        self.assertIn("demo", stdout)
        self.assertIn("reference", stdout)
        self.assertIn("ready=1", stdout)
        self.assertIn("render-references", stdout)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertEqual("WORKING handoff.prepare\n", stderr)

    def test_successful_record_failure_is_a_zero_exit_state_transition(self):
        data = self.success_data()["handoff.record-failure"]
        service = types.SimpleNamespace(execute=mock.Mock(return_value=data))
        code, stdout, stderr = self.invoke_with_service(
            [
                "--json",
                "handoff",
                "record-failure",
                "/shared/demo",
                "--job",
                "job-" + "b" * 40,
                "--attempt",
                "1",
                "--executor-kind",
                "external-tool",
                "--executor-id",
                "renderer-b",
                "--category",
                "quota",
            ],
            service,
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertEqual("handoff.record-failure", json.loads(stdout)["command"])

    def test_usage_and_invalid_handoff_input_exit_two(self):
        cases = (
            ["--json", "handoff"],
            ["--json", "handoff", "prepare"],
            [
                "--json",
                "handoff",
                "accept-result",
                "/shared/demo",
                "--job",
                "job-" + "a" * 40,
                "--attempt",
                "not-an-integer",
                "--path",
                "/renderer/result.png",
                "--executor-kind",
                "external-tool",
                "--executor-id",
                "renderer-a",
            ],
            [
                "--json",
                "handoff",
                "record-failure",
                "/shared/demo",
                "--job",
                "job-" + "b" * 40,
                "--attempt",
                "1",
                "--executor-kind",
                "unknown-tool",
                "--executor-id",
                "renderer-b",
                "--category",
                "quota",
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke(argv)

                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual(1, len(stdout.splitlines()))
                payload = json.loads(stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual("CS-CLI-001", payload["error"]["code"])

    def test_approve_reference_is_accepted_only_by_accept_result(self):
        project = "/shared/demo"
        job = "job-" + "a" * 40
        base_accept = [
            "--json",
            "handoff",
            "accept-result",
            project,
            "--job",
            job,
            "--attempt",
            "1",
            "--path",
            "/renderer/result.png",
            "--executor-kind",
            "native-tool",
            "--executor-id",
            "renderer-a",
        ]
        service = types.SimpleNamespace(
            execute=mock.Mock(return_value=self.success_data()["handoff.accept-result"]),
        )
        for approve, expected in (([], False), (["--approve-reference"], True)):
            service.execute.reset_mock()
            with self.subTest(approve=expected):
                code, _, _ = self.invoke_with_service([*base_accept, *approve], service)
                self.assertEqual(0, code)
                self.assertEqual(expected, service.execute.call_args.kwargs["approve_reference"])

        invalid_routes = (
            ["--json", "handoff", "prepare", project, "--approve-reference"],
            ["--json", "handoff", "inspect", project, "--approve-reference"],
            [
                "--json",
                "handoff",
                "record-failure",
                project,
                "--job",
                job,
                "--attempt",
                "1",
                "--executor-kind",
                "native-tool",
                "--executor-id",
                "renderer-a",
                "--category",
                "quota",
                "--approve-reference",
            ],
        )
        for argv in invalid_routes:
            service.execute.reset_mock()
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke_with_service(argv, service)
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual("CS-CLI-001", json.loads(stdout)["error"]["code"])
                service.execute.assert_not_called()

    def test_contract_scope_and_job_errors_use_typed_handoff_001(self):
        cases = (
            HandoffContractError(["contract_version: unsupported"]),
            StaleLockedScopeError(["locked_scope_sha256: stale"]),
            HandoffContractError(["job_id: does not name a current job"]),
        )
        for error in cases:
            direct = cli.error_payload(
                error,
                command="handoff.inspect",
                surface="cli",
            )
            with self.subTest(error=type(error).__name__):
                self.assertEqual("CS-HANDOFF-001", direct["code"])
                service = types.SimpleNamespace(execute=mock.Mock(side_effect=error))
                code, stdout, stderr = self.invoke_with_service(
                    ["--json", "handoff", "inspect", "/shared/demo"], service
                )
                payload = json.loads(stdout)
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual("handoff.inspect", payload["command"])
                self.assertEqual("CS-HANDOFF-001", payload["error"]["code"])

    def test_result_metadata_ordinal_raster_and_activation_errors_use_typed_handoff_003(self):
        issues = (
            "executor metadata is invalid",
            "attempt ordinal conflicts with the next attempt",
            "result raster does not match requested dimensions",
            "reference activation was refused",
        )
        argv = [
            "--json",
            "handoff",
            "accept-result",
            "/shared/demo",
            "--job",
            "job-" + "a" * 40,
            "--attempt",
            "1",
            "--path",
            "/renderer/result.png",
            "--executor-kind",
            "native-tool",
            "--executor-id",
            "renderer-a",
        ]
        for issue in issues:
            error = HandoffResultError([issue])
            direct = cli.error_payload(
                error,
                command="handoff.accept-result",
                surface="cli",
            )
            with self.subTest(issue=issue):
                self.assertEqual("CS-HANDOFF-003", direct["code"])
                service = types.SimpleNamespace(
                    execute=mock.Mock(side_effect=error),
                )
                code, stdout, stderr = self.invoke_with_service(argv, service)
                payload = json.loads(stdout)
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual("handoff.accept-result", payload["command"])
                self.assertEqual("CS-HANDOFF-003", payload["error"]["code"])

    def test_handoff_error_detail_redacts_paths_and_terminal_controls(self):
        secret_path = "/var/Comic Sol/private/result.png"
        issue = f"result raster '{secret_path}' contains control \x1b[31m"
        typed_error = HandoffResultError([issue])
        direct_payload = cli._failure(
            "handoff.accept-result",
            typed_error,
            detail=cli.safe_error_detail(typed_error),
        )
        direct_serialized = json.dumps(direct_payload, ensure_ascii=False)
        self.assertNotIn(secret_path, direct_serialized)
        self.assertNotIn("private", direct_serialized)
        self.assertNotIn("\x1b", direct_payload["error"].get("detail", ""))
        direct_human = cli.format_human_error(typed_error, command="handoff.accept-result")
        self.assertNotIn(secret_path, direct_human)
        self.assertNotIn("private", direct_human)
        self.assertNotIn("\x1b", direct_human)

        argv = [
            "--json",
            "handoff",
            "accept-result",
            "/shared/demo",
            "--job",
            "job-" + "a" * 40,
            "--attempt",
            "1",
            "--path",
            "/renderer/result.png",
            "--executor-kind",
            "native-tool",
            "--executor-id",
            "renderer-a",
        ]
        service = types.SimpleNamespace(
            execute=mock.Mock(side_effect=HandoffResultError([issue])),
        )
        code, stdout, stderr = self.invoke_with_service(argv, service)
        payload = json.loads(stdout)

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret_path, serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("\x1b", payload["error"].get("detail", ""))

        service = types.SimpleNamespace(
            execute=mock.Mock(side_effect=HandoffResultError([issue])),
        )
        code, stdout, stderr = self.invoke_with_service(argv[1:], service)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertNotIn(secret_path, stderr)
        self.assertNotIn("private", stderr)
        self.assertNotIn("\x1b", stderr)

    def test_handoff_adds_no_mcp_route_or_tool(self):
        source = (Path(__file__).resolve().parents[1] / "scripts/mcp_server.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        registered = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        expected = {
            "comic_doctor",
            "comic_init",
            "comic_status",
            "comic_transition",
            "comic_validate",
            "comic_resume_plan",
            "comic_resume",
            "comic_invalidate",
            "comic_record_stage",
            "comic_record_attempt",
            "comic_promote_attempt",
            "comic_override_panel",
            "comic_letter",
            "comic_compose",
            "comic_render_report",
            "comic_export",
            "comic_finalize",
        }
        self.assertEqual(17, len(registered))
        self.assertEqual(expected, registered)
        self.assertFalse(any("handoff" in name for name in registered))


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


class DogfoodProductCliContractTests(unittest.TestCase):
    def invoke(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_with_service(self, argv, service):
        service_module = types.SimpleNamespace(CommandService=mock.Mock(return_value=service))
        with (
            mock.patch.object(cli, "_load_engine", return_value=types.SimpleNamespace()),
            mock.patch.object(cli, "_load_engine_module", return_value=types.SimpleNamespace()),
            mock.patch.object(cli, "_load_command_service", return_value=service_module),
        ):
            return self.invoke(argv)

    def test_dogfood_report_preview_and_validate_preserve_json_envelope_and_route_arguments(self):
        project = Path("/shared/private-project")
        output = Path("/shared/reports/report.json")
        report_data = {
            "kind": "comic-sol-dogfood-report",
            "schema_version": "1.0",
            "consent": {"share_report": True},
        }
        cases = (
            (
                [
                    "--json",
                    "dogfood",
                    "report",
                    str(project),
                    "--setup-minutes",
                    "12",
                    "--first-project-minutes",
                    "19",
                    "--pdf-minutes",
                    "47",
                    "--manual-intervention",
                    "no",
                    "--would-use-again",
                    "yes",
                    "--failed-resume-attempts",
                    "2",
                    "--friction",
                    "installation",
                    "--friction",
                    "handoff",
                    "--cohort-alias",
                    "cohort-7",
                    "--consent-to-share",
                    "--output",
                    str(output),
                ],
                "dogfood.report",
                {
                    "project_dir": project,
                    "output_path": output,
                    "creator_inputs": {
                        "setup_minutes": 12,
                        "first_project_minutes": 19,
                        "pdf_minutes": 47,
                        "manual_intervention": False,
                        "would_use_again": True,
                        "failed_resume_attempts": 2,
                        "friction_categories": ["installation", "handoff"],
                        "cohort_alias": "cohort-7",
                    },
                    "consent_to_share": True,
                    "comic_sol_version": __version__,
                },
            ),
            (
                [
                    "--json",
                    "dogfood",
                    "preview",
                    str(project),
                    "--setup-minutes",
                    "12",
                    "--first-project-minutes",
                    "19",
                    "--pdf-minutes",
                    "47",
                    "--manual-intervention",
                    "no",
                    "--would-use-again",
                    "yes",
                ],
                "dogfood.preview",
                {
                    "project_dir": project,
                    "creator_inputs": {
                        "setup_minutes": 12,
                        "first_project_minutes": 19,
                        "pdf_minutes": 47,
                        "manual_intervention": False,
                        "would_use_again": True,
                        "failed_resume_attempts": None,
                        "friction_categories": [],
                        "cohort_alias": None,
                    },
                    "consent_to_share": False,
                    "comic_sol_version": __version__,
                },
            ),
            (
                ["--json", "dogfood", "validate", str(output)],
                "dogfood.validate",
                {"report_path": output},
            ),
        )
        for argv, route, expected in cases:
            service = types.SimpleNamespace(execute=mock.Mock(return_value=report_data))
            with self.subTest(route=route):
                code, stdout, stderr = self.invoke_with_service(argv, service)
                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                payload = json.loads(stdout)
                self.assertEqual({"ok", "command", "data", "error"}, set(payload))
                self.assertEqual(route, payload["command"])
                self.assertEqual(report_data, payload["data"])
                service.execute.assert_called_once_with(route, **expected)

    def test_persisted_report_requires_consent_and_output_at_parse_boundary(self):
        base = [
            "--json",
            "dogfood",
            "report",
            "/shared/private-project",
            "--setup-minutes",
            "1",
            "--first-project-minutes",
            "2",
            "--pdf-minutes",
            "3",
            "--manual-intervention",
            "no",
            "--would-use-again",
            "yes",
        ]
        for omitted in ("--consent-to-share", "--output"):
            argv = [*base]
            if omitted != "--consent-to-share":
                argv.append("--consent-to-share")
            if omitted != "--output":
                argv.extend(["--output", "/shared/report.json"])
            with self.subTest(omitted=omitted):
                code, stdout, stderr = self.invoke(argv)
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                payload = json.loads(stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual("CS-CLI-001", payload["error"]["code"])

    def test_dogfood_boolean_and_bounded_values_fail_closed(self):
        cases = (
            ("--manual-intervention", "maybe"),
            ("--would-use-again", "true"),
            ("--setup-minutes", "-1"),
        )
        for flag, value in cases:
            argv = [
                "--json",
                "dogfood",
                "preview",
                "/shared/private-project",
                "--setup-minutes",
                "1",
                "--first-project-minutes",
                "2",
                "--pdf-minutes",
                "3",
                "--manual-intervention",
                "no",
                "--would-use-again",
                "yes",
            ]
            index = argv.index(flag)
            argv[index + 1] = value
            with self.subTest(flag=flag, value=value):
                code, stdout, stderr = self.invoke(argv)
                self.assertEqual(2, code)
                self.assertEqual("", stderr)
                self.assertEqual("CS-CLI-001", json.loads(stdout)["error"]["code"])

    def test_dogfood_validate_rejects_persisted_report_without_share_consent(self):
        fixture = Path(__file__).resolve().parent / "fixtures/dogfood/valid-report-v1.0.json"
        report = json.loads(fixture.read_text(encoding="utf-8"))
        report["consent"]["share_report"] = False
        with tempfile.TemporaryDirectory() as raw:
            report_path = Path(raw) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            before = report_path.read_bytes()

            code, stdout, stderr = self.invoke(["--json", "dogfood", "validate", str(report_path)])

            self.assertEqual(2, code)
            self.assertEqual("", stderr)
            payload = json.loads(stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("CS-PROJ-001", payload["error"]["code"])
            self.assertEqual(before, report_path.read_bytes())
