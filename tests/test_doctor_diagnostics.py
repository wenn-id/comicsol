import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock

from comic_sol_product import cli
from scripts import comic_sol


class DoctorDiagnosticContractTests(unittest.TestCase):
    def test_doctor_report_is_machine_readable_and_actionable(self):
        with tempfile.TemporaryDirectory() as raw:
            report = comic_sol.doctor_report(Path(raw) / "output")

        self.assertEqual({"ready", "healthy", "checks", "messages"}, set(report))
        self.assertIsInstance(report["ready"], bool)
        self.assertIsInstance(report["checks"], list)
        self.assertIsInstance(report["messages"], list)
        self.assertGreaterEqual(len(report["checks"]), 8)
        ids = {check["id"] for check in report["checks"]}
        self.assertTrue({
            "runtime", "pillow", "fonts", "templates", "references",
            "output-root", "mcp", "image-capability",
        } <= ids)
        for check in report["checks"]:
            self.assertEqual({"id", "status", "message", "remediation"}, set(check))
            self.assertIn(check["status"], {"pass", "warn", "fail"})
            self.assertTrue(check["message"])
            self.assertTrue(check["remediation"])
        self.assertEqual(report["ready"], not any(c["status"] == "fail" for c in report["checks"]))

    def test_doctor_reports_incomplete_template_install_as_not_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            templates = Path(raw) / "templates"
            templates.mkdir()
            for name in ("manifest.json", "character-bible.json", "story-plan.json", "storyboard.json", "panel-record.json"):
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
            for name in ("manifest.json", "character-bible.json", "story-plan.json", "storyboard.json", "panel-record.json"):
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
            with mock.patch.object(comic_sol.importlib, "import_module", side_effect=ImportError("partial MCP")):
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
            self.assertEqual(len(payload["data"]["messages"]), len(set(payload["data"]["messages"])))


if __name__ == "__main__":
    unittest.main()
