"""Exercise the standalone CLI's filesystem and MCP boundaries."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class StandaloneCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.output = self.root / "projects"
        self.exe = Path(os.environ["COMIC_SOL_NATIVE_TEST_EXE"]).resolve(strict=True)
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            APPDATA=str(self.home / "AppData/Roaming"),
            CODEX_HOME=str(self.home / ".codex"),
            PATH=str(self.root / "empty-path"),
            COMIC_SOL_PYTHON=str(self.root / "missing-interpreter"),
            COMIC_SOL_ENGINE_DIR=str(self.root / "missing-engine"),
        )

    def cli(self, *args, status=0):
        result = subprocess.run(
            [self.exe, "--json", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            timeout=25,
        )
        self.assertEqual(result.returncode, status, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def symlink(self, link: Path, target: Path, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            if os.name == "nt" and os.environ.get("COMIC_SOL_REQUIRE_SYMLINK_TESTS") != "1":
                self.skipTest("Windows session lacks symlink privileges")
            raise

    def test_client_setup_repair_and_uninstall_preserve_other_servers(self):
        config = self.home / ".cursor/mcp.json"
        config.parent.mkdir()
        original = b'{ "editor": "keep",\r\n  "mcpServers": {"other": {"command": "stay"}}\r\n}\r\n'
        config.write_bytes(original)
        preview = self.cli("repair", "--client", "cursor", "--output-root", self.output, "--dry-run")
        self.assertEqual(preview["data"][2]["status"], "planned")
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(list(config.parent.iterdir()), [config])
        self.cli("setup", "--client", "cursor", "--output-root", self.output)
        data = json.loads(config.read_bytes())
        self.assertEqual(data["mcpServers"]["other"], {"command": "stay"})
        self.assertEqual(data["mcpServers"]["comic-sol"]["args"], ["mcp", "--root", str(self.output)])
        self.assertTrue(config.read_bytes().startswith(original.split(b'"stay"')[0] + b'"stay"}'))
        backup = sorted(config.parent.glob("*.bak-*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_bytes(), original)
        self.output.mkdir()
        project = self.output / "existing.txt"
        project.write_text("Keep this project.")
        self.cli("uninstall", "--client", "cursor", "--output-root", self.output)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(project.read_text(), "Keep this project.")

    def test_client_config_symlink_cannot_write_through_to_unrelated_file(self):
        outside = self.root / "unrelated.json"
        outside.write_bytes(b'{"private":true}\n')
        config = self.home / ".cursor/mcp.json"
        config.parent.mkdir()
        self.symlink(config, outside)
        failure = self.cli("repair", "--client", "cursor", "--output-root", self.output, status=1)
        self.assertEqual(failure["error"]["code"], "CS-INSTALL-002")
        self.assertEqual(outside.read_bytes(), b'{"private":true}\n')
        self.assertTrue(config.is_symlink())
        self.assertEqual(list(config.parent.iterdir()), [config])

    def test_skill_uninstall_preserves_modified_and_unmanaged_files(self):
        installed = self.cli("skill-install", "--target", "codex", "--scope", "user")["data"]
        bundle = Path(installed["destination"])
        (bundle / "SKILL.md").write_bytes(b"Local edits.")
        (bundle / "mine.txt").write_bytes(b"Unrelated content.")
        result = self.cli("skill-uninstall", "--target", "codex", "--scope", "user")["data"]
        self.assertEqual(result["status"], "preserved")
        self.assertEqual({item.name for item in bundle.iterdir()}, {"SKILL.md", "mine.txt"})
        self.assertEqual((bundle / "SKILL.md").read_bytes(), b"Local edits.")
        self.assertEqual((bundle / "mine.txt").read_bytes(), b"Unrelated content.")

    def test_dogfood_rejects_linked_external_destination_without_writing(self):
        project = self.cli("init", "--output-root", self.output, "--title", "Report",
            "--starter", "minimal-one-page")["data"]["project_id"]
        project = self.output / project
        link = self.root / "linked"
        self.symlink(link, project, directory=True)
        result = self.cli("dogfood", "report", project, "--setup-minutes", "1", "--first-project-minutes",
            "2", "--pdf-minutes", "3", "--manual-intervention", "no", "--would-use-again", "yes",
            "--consent-to-share", "--output", link / "report.json", status=2)
        self.assertEqual(result["error"]["code"], "CS-SEC-001")
        self.assertFalse((project / "report.json").exists())

    def test_mcp_schema_and_open_directory_containment(self):
        self.output.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "project.json").write_bytes(b'{"status":"INIT"}\n')
        self.symlink(self.output / "escape", outside, directory=True)
        nested = self.output / "nested-project" / "existing-child"
        nested.mkdir(parents=True)
        (nested.parent / "project.json").write_bytes(b'{"status":"INIT"}\n')
        with subprocess.Popen(
            [self.exe, "mcp", "--root", self.output],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.env,
        ) as server:
            def request(method, parameters, request_id=None):
                value = {"jsonrpc": "2.0", "method": method, "params": parameters}
                if request_id is not None:
                    value["id"] = request_id
                server.stdin.write(json.dumps(value) + "\n")
                server.stdin.flush()
                return json.loads(server.stdout.readline())["result"] if request_id is not None else None

            request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"}}, 0)
            request("notifications/initialized", {})
            tools = request("tools/list", {}, 1)
            expected = json.loads((Path(__file__).resolve().parents[1] / "src/mcp-contract.json").read_text())
            self.assertEqual(tools["tools"], expected["tools"])
            self.assertEqual(len(tools["tools"]), 18)
            error = request("tools/call", {"name": "comic_status", "arguments": {"project_id": "escape"}}, 2)
            self.assertTrue(error["isError"])
            self.assertIn("CS-SEC-001", error["content"][0]["text"])
            tool = {"name": "comic_status", "arguments": {"project_id": "nested-project"}}
            self.assertFalse(request("tools/call", tool, 3)["isError"])
            self.symlink(nested / "late-link", outside / "project.json")
            late = request("tools/call", tool, 4)
            self.assertTrue(late["isError"])
            self.assertIn("project contains a symlink", late["content"][0]["text"])
            server.stdin.close()
            self.assertEqual(server.wait(timeout=10), 0)
        self.assertEqual((outside / "project.json").read_bytes(), b'{"status":"INIT"}\n')
        self.assertFalse((outside / ".comic-sol.lock").exists())

    def test_doctor_observes_native_runtime(self):
        data = self.cli("doctor", "--output-root", self.output)["data"]
        checks = {item["id"]: item for item in data["checks"]}
        for check in ("runtime", "raster", "mcp"):
            self.assertEqual(checks[check]["status"], "pass")
        self.assertIn("Native Rust", checks["runtime"]["message"])
        self.assertTrue(data["healthy"])
        blocked = self.root / "read-only"
        blocked.write_text("this is a file")
        refused = self.cli("doctor", "--output-root", blocked, status=1)["data"]
        self.assertFalse(refused["healthy"])
        if os.name == "posix" and os.geteuid() != 0:
            folder = self.root / "unwritable"
            folder.mkdir(mode=0o500)
            try:
                refused = self.cli("doctor", "--output-root", folder / "child", status=1)["data"]
                output = next(item for item in refused["checks"] if item["id"] == "output-root")
                self.assertIn("PermissionError", output["message"])
            finally:
                folder.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
