import json
import tempfile
import unittest
from pathlib import Path

from comic_sol_product import cli
from comic_sol_product.clients import CodexAdapter, JsonClientAdapter
from comic_sol_product.setup import setup_clients, uninstall_clients


class ClientSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.output = self.home / "Comic Sol Projects"
        self.output.mkdir(parents=True)
        (self.output / "keep.txt").write_text("project", encoding="utf-8")

    def test_json_setup_creates_backup_and_exact_entry(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"mcpServers":{"other":{"command":"other"}}}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = setup_clients(
            self.output,
            home=self.home,
            adapters=[adapter],
            executable="/opt/comic-sol/bin/comic-sol",
        )[0]

        self.assertEqual(result.status, "configured")
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(Path(result.backup_path).read_bytes(), original)
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["mcpServers"]["other"], {"command": "other"})
        self.assertEqual(
            saved["mcpServers"]["comic-sol"],
            {
                "command": "/opt/comic-sol/bin/comic-sol",
                "args": ["mcp", "--root", str(self.output.resolve())],
            },
        )

    def test_repeated_setup_is_unchanged_and_does_not_duplicate(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        first = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]
        first_bytes = config.read_bytes()
        second = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]

        self.assertEqual(first.status, "configured")
        self.assertEqual(second.status, "unchanged")
        self.assertIsNone(second.backup_path)
        self.assertEqual(config.read_bytes(), first_bytes)
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(list(saved["mcpServers"]), ["comic-sol"])

    def test_malformed_config_is_refused_without_backup_or_write(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{ definitely not json\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = setup_clients(self.output, adapters=[adapter])[0]

        self.assertEqual(result.status, "failed")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNone(result.backup_path)
        self.assertEqual(list(config.parent.glob("*.bak-*")), [])

    def test_verification_failure_rolls_back_byte_for_byte(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers", verify_hook=lambda: False)

        result = setup_clients(self.output, adapters=[adapter])[0]

        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNotNone(result.backup_path)

    def test_json_verify_rejects_parseable_but_unsafe_comic_sol_entries(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        cases = (
            {"command": "evil", "args": []},
            {"command": "comic-sol", "args": ["run"]},
            {"command": "comic-sol", "args": ["mcp", "--root", "relative-root"]},
        )
        for entry in cases:
            with self.subTest(entry=entry):
                config.write_text(
                    json.dumps({"mcpServers": {"comic-sol": entry}}),
                    encoding="utf-8",
                )
                self.assertFalse(JsonClientAdapter("cursor", config, "mcpServers").verify())

    def test_codex_verify_rejects_parseable_but_unsafe_comic_sol_entry(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[mcp_servers.comic-sol]\ncommand = "evil"\nargs = []\n',
            encoding="utf-8",
        )
        self.assertFalse(CodexAdapter(config).verify())

    def test_verify_rejects_duplicate_root_argument(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"mcpServers": {"comic-sol": {
                "command": "comic-sol",
                "args": ["mcp", "--root", str(self.output.resolve()), "--root", "again"],
            }}}),
            encoding="utf-8",
        )
        self.assertFalse(JsonClientAdapter("cursor", config, "mcpServers").verify())

    def test_type_error_from_verify_hook_is_not_retried(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        calls = []

        def hook(expected):
            calls.append(expected)
            raise TypeError("hook failure")

        with self.assertRaisesRegex(TypeError, "hook failure"):
            JsonClientAdapter("cursor", config, "mcpServers", verify_hook=hook).verify({})
        self.assertEqual(1, len(calls))

    def test_zero_argument_verify_hook_is_supported(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        calls = []

        def hook():
            calls.append(True)
            return False

        self.assertFalse(JsonClientAdapter("cursor", config, "mcpServers", verify_hook=hook).verify({}))
        self.assertEqual([True], calls)

    def test_codex_toml_preserves_existing_config_and_is_idempotent(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
        adapter = CodexAdapter(config)

        first = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]
        second = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]
        text = config.read_text(encoding="utf-8")

        self.assertEqual(first.status, "configured")
        self.assertEqual(second.status, "unchanged")
        self.assertIn('model = "gpt-test"', text)
        self.assertIn('[mcp_servers.other]', text)
        self.assertEqual(text.count("[mcp_servers.comic-sol]"), 1)
        self.assertIn('args = ["mcp", "--root",', text)

    def test_uninstall_removes_only_integration_and_preserves_projects(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers":{"other":{"command":"other"}}}\n', encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        setup_clients(self.output, adapters=[adapter])

        result = uninstall_clients(self.output, adapters=[adapter])[0]
        saved = json.loads(config.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "removed")
        self.assertNotIn("comic-sol", saved["mcpServers"])
        self.assertEqual(saved["mcpServers"]["other"], {"command": "other"})
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "project")

    def test_missing_config_is_reported_skipped_without_creation(self):
        config = self.home / ".cursor" / "mcp.json"
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        result = setup_clients(self.output, adapters=[adapter])[0]
        self.assertEqual(result.status, "skipped")
        self.assertFalse(config.exists())

    def test_cli_exposes_transaction_commands(self):
        parser = cli.build_parser()
        for command in ("setup", "repair", "uninstall"):
            arguments = parser.parse_args(
                [command, "--output-root", str(self.output), "--client", "codex"]
            )
            self.assertEqual(arguments.command, command)
            self.assertEqual(arguments.clients, ["codex"])


if __name__ == "__main__":
    unittest.main()
