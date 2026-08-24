import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock

from comic_sol_product import cli
from scripts import comic_sol


class DoctorDiagnosticContractTests(unittest.TestCase):
    NOT_CHECKED_CAPABILITY = {
        "status": "not_checked",
        "name": None,
        "supports_reference_images": False,
        "supports_dimensions": False,
    }

    @staticmethod
    def _image_check(report: dict[str, object]) -> dict[str, object]:
        checks = cast(list[dict[str, object]], report["checks"])
        return next(check for check in checks if check["id"] == "image-capability")

    @staticmethod
    def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*"))
        }

    def test_doctor_report_is_machine_readable_and_actionable(self):
        with tempfile.TemporaryDirectory() as raw:
            report = comic_sol.doctor_report(Path(raw) / "output")

        self.assertEqual({"ready", "healthy", "checks", "messages"}, set(report))
        self.assertIsInstance(report["ready"], bool)
        self.assertIsInstance(report["checks"], list)
        self.assertIsInstance(report["messages"], list)
        self.assertGreaterEqual(len(report["checks"]), 8)
        ids = {check["id"] for check in report["checks"]}
        self.assertTrue(
            {
                "runtime",
                "pillow",
                "fonts",
                "templates",
                "starter-templates",
                "references",
                "output-root",
                "mcp",
                "image-capability",
            }
            <= ids
        )
        for check in report["checks"]:
            required = {"id", "status", "message", "remediation"}
            self.assertTrue(required <= set(check))
            self.assertTrue(set(check) <= required | {"details"})
            self.assertIn(check["status"], {"pass", "warn", "fail"})
            self.assertTrue(check["message"])
            self.assertTrue(check["remediation"])
        self.assertEqual(report["ready"], not any(c["status"] == "fail" for c in report["checks"]))

    def test_doctor_reports_a_fully_capable_image_environment_as_healthy(self):
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": True,
        }
        with tempfile.TemporaryDirectory() as raw:
            report = comic_sol.doctor_report(Path(raw) / "output", image_capability=capability)

        check = self._image_check(report)
        self.assertEqual("pass", check["status"])
        self.assertEqual(
            {"readiness": "healthy", "capability": capability},
            check["details"],
        )
        self.assertTrue(report["ready"])
        self.assertTrue(report["healthy"])

    def test_doctor_reports_usable_but_incomplete_image_features_as_partial(self):
        for reference_support, dimension_support in ((False, True), (True, False), (False, False)):
            capability = {
                "status": "available",
                "name": "agent-image-generation",
                "supports_reference_images": reference_support,
                "supports_dimensions": dimension_support,
            }
            with (
                self.subTest(
                    supports_reference_images=reference_support,
                    supports_dimensions=dimension_support,
                ),
                tempfile.TemporaryDirectory() as raw,
            ):
                report = comic_sol.doctor_report(Path(raw) / "output", image_capability=capability)

            check = self._image_check(report)
            self.assertEqual("warn", check["status"])
            self.assertEqual(
                {"readiness": "partial", "capability": capability},
                check["details"],
            )
            self.assertTrue(report["ready"])
            self.assertTrue(report["healthy"])

    def test_doctor_reports_an_explicitly_unavailable_image_capability_as_missing(self):
        capability = {
            "status": "unavailable",
            "name": None,
            "supports_reference_images": False,
            "supports_dimensions": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            report = comic_sol.doctor_report(Path(raw) / "output", image_capability=capability)

        check = self._image_check(report)
        self.assertEqual("warn", check["status"])
        self.assertEqual(
            {"readiness": "missing", "capability": capability},
            check["details"],
        )
        self.assertIn("image-generation", cast(str, check["remediation"]).lower())
        self.assertTrue(report["ready"])
        self.assertTrue(report["healthy"])

    def test_doctor_reports_an_unobserved_image_environment_as_unknown(self):
        with tempfile.TemporaryDirectory() as raw:
            report = comic_sol.doctor_report(Path(raw) / "output")

        check = self._image_check(report)
        self.assertEqual("warn", check["status"])
        self.assertEqual(
            {
                "readiness": "unknown",
                "capability": self.NOT_CHECKED_CAPABILITY,
            },
            check["details"],
        )
        self.assertTrue(report["ready"])
        self.assertTrue(report["healthy"])

    def test_malformed_or_secret_bearing_observations_fail_closed_without_project_writes(self):
        malformed = "definitely-not-a-boolean"
        secret = "super-secret-provider-token"
        secret_name = "sk-ABCDEFGHIJKLMNOP"
        scalar_secret = "scalar-secret-provider-token-XYZ123"
        list_secret = "list-secret-provider-token-XYZ123"
        observations = (
            (
                {
                    "status": "available",
                    "name": "agent-image-generation",
                    "supports_reference_images": malformed,
                    "supports_dimensions": True,
                },
                malformed,
            ),
            (
                {
                    "status": "available",
                    "name": "agent-image-generation",
                    "supports_reference_images": True,
                    "supports_dimensions": True,
                    "credential": secret,
                },
                secret,
            ),
            (
                {
                    "status": "available",
                    "name": secret_name,
                    "supports_reference_images": True,
                    "supports_dimensions": True,
                },
                secret_name,
            ),
            (scalar_secret, scalar_secret),
            ([list_secret], list_secret),
        )
        for observation, private_value in observations:
            with self.subTest(observation=observation), tempfile.TemporaryDirectory() as raw:
                output = Path(raw) / "output"
                project = output / "existing-project"
                (project / "logs").mkdir(parents=True)
                (project / "project.json").write_bytes(b'{"status":"STORYBOARDED"}\n')
                (project / "logs/events.jsonl").write_bytes(b'{"event":"preserved"}\n')
                before = self._tree_snapshot(output)

                report = comic_sol.doctor_report(output, image_capability=observation)

                self.assertEqual(before, self._tree_snapshot(output))
                check = self._image_check(report)
                self.assertEqual("warn", check["status"])
                self.assertEqual(
                    {
                        "readiness": "unknown",
                        "capability": self.NOT_CHECKED_CAPABILITY,
                    },
                    check["details"],
                )
                self.assertIn("could not interpret", cast(str, check["message"]).lower())
                self.assertNotIn(private_value, json.dumps(report, ensure_ascii=False))

    def test_unexpected_detection_failure_is_sanitized_without_project_writes(self):
        secret = "private-provider-response"
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": True,
        }
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "output"
            project = output / "existing-project"
            project.mkdir(parents=True)
            (project / "project.json").write_bytes(b'{"status":"STORYBOARDED"}\n')
            before = self._tree_snapshot(output)

            with mock.patch.object(
                comic_sol,
                "_image_capability_diagnostic",
                side_effect=RuntimeError(secret),
            ):
                report = comic_sol.doctor_report(output, image_capability=capability)

            self.assertEqual(before, self._tree_snapshot(output))
            check = self._image_check(report)
            self.assertEqual("warn", check["status"])
            self.assertEqual(
                {
                    "readiness": "unknown",
                    "capability": self.NOT_CHECKED_CAPABILITY,
                },
                check["details"],
            )
            self.assertIn("failed safely", cast(str, check["message"]).lower())
            self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))

    def test_doctor_reports_missing_starter_bundle_as_not_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            templates = Path(raw) / "templates"
            shutil.copytree(comic_sol.TEMPLATES, templates)
            missing = templates / "starters/v1/action-focused/plan/storyboard.json"
            missing.unlink()
            with mock.patch.object(comic_sol, "TEMPLATES", templates):
                report = comic_sol.doctor_report(Path(raw) / "output")

        check = next(check for check in report["checks"] if check["id"] == "starter-templates")
        self.assertEqual("fail", check["status"])
        self.assertIn("action-focused", cast(str, check["message"]))
        self.assertFalse(report["ready"])

    def test_doctor_reports_incomplete_template_install_as_not_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            templates = Path(raw) / "templates"
            templates.mkdir()
            for name in (
                "manifest.json",
                "character-bible.json",
                "story-plan.json",
                "storyboard.json",
                "panel-record.json",
            ):
                (templates / name).write_text("{}\n", encoding="utf-8")
            with mock.patch.object(comic_sol, "TEMPLATES", templates):
                report = comic_sol.doctor_report(Path(raw) / "output")
        checks = cast(list[dict[str, object]], report["checks"])
        templates_check = next(check for check in checks if check["id"] == "templates")
        self.assertEqual("fail", templates_check["status"])
        self.assertIn("qa-report.md.tmpl", cast(str, templates_check["message"]))
        self.assertIn("reinstall", cast(str, templates_check["remediation"]).lower())
        self.assertFalse(report["ready"])

    def test_doctor_rejects_malformed_or_empty_json_templates(self):
        with tempfile.TemporaryDirectory() as raw:
            templates = Path(raw) / "templates"
            templates.mkdir()
            for name in (
                "manifest.json",
                "character-bible.json",
                "story-plan.json",
                "storyboard.json",
                "panel-record.json",
            ):
                (templates / name).write_text('{"placeholder": true}\n', encoding="utf-8")
            (templates / "qa-report.md.tmpl").write_text("{{PROJECT_SUMMARY}}\n", encoding="utf-8")
            for content in ("{}\n", "{"):
                (templates / "manifest.json").write_text(content, encoding="utf-8")
                with mock.patch.object(comic_sol, "TEMPLATES", templates):
                    report = comic_sol.doctor_report(Path(raw) / "output")
                checks = cast(list[dict[str, object]], report["checks"])
                templates_check = next(check for check in checks if check["id"] == "templates")
                self.assertEqual("fail", templates_check["status"])
                self.assertFalse(report["ready"])

    def test_doctor_reports_unusable_mcp_installation(self):
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(
                comic_sol.importlib, "import_module", side_effect=ImportError("partial MCP")
            ):
                report = comic_sol.doctor_report(Path(raw) / "output")
        checks = cast(list[dict[str, object]], report["checks"])
        mcp_check = next(check for check in checks if check["id"] == "mcp")
        self.assertEqual("warn", mcp_check["status"])
        self.assertIn("unusable", cast(str, mcp_check["message"]))

    def test_doctor_reports_broken_output_as_failed_actionable_check(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "not-a-directory"
            output.write_text("occupied", encoding="utf-8")
            with mock.patch.object(comic_sol, "TEMPLATES", Path(raw) / "templates"):
                report = comic_sol.doctor_report(output)

        output_check = next(check for check in report["checks"] if check["id"] == "output-root")
        self.assertEqual("fail", output_check["status"])
        self.assertIn("output", output_check["remediation"].lower())
        self.assertFalse(report["ready"])

    def test_cli_json_and_human_doctor_share_readiness(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "output"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main(["--json", "doctor", "--output-root", str(output)])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, code)
            self.assertEqual(payload["data"]["ready"], payload["data"]["healthy"])
            self.assertIn("READY", "\n".join(payload["data"]["messages"]))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                human_code = cli.main(["doctor", "--output-root", str(output)])
            self.assertEqual(0, human_code)
            self.assertEqual("", stderr.getvalue())
            self.assertIn("READY", stdout.getvalue())
            self.assertEqual(
                len(payload["data"]["messages"]), len(set(payload["data"]["messages"]))
            )


if __name__ == "__main__":
    unittest.main()
