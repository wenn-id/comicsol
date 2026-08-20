import contextlib
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import comic_sol_product.setup as client_setup
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
        self.launcher = self.home / "bin with spaces" / (
            "comic-sol.exe" if os.name == "nt" else "comic-sol"
        )
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b"launcher")
        if os.name != "nt":
            self.launcher.chmod(0o755)

    def adapter_paths(self, platform: str) -> dict[str, Path]:
        with mock.patch.object(client_setup.sys, "platform", platform):
            return {
                adapter.name: adapter.config_path
                for adapter in client_setup.default_adapters(self.home)
            }

    def test_default_adapter_paths_are_platform_native(self):
        with mock.patch.dict(
            os.environ, {"APPDATA": str(self.home / "Roaming")}
        ):
            windows = self.adapter_paths("win32")
        macos = self.adapter_paths("darwin")
        linux = self.adapter_paths("linux")

        self.assertEqual(
            self.home / "Roaming/Claude/claude_desktop_config.json",
            windows["claude-desktop"],
        )
        self.assertEqual(
            self.home
            / "Library/Application Support/Claude/claude_desktop_config.json",
            macos["claude-desktop"],
        )
        self.assertEqual(
            self.home / ".config/Claude/claude_desktop_config.json",
            linux["claude-desktop"],
        )
        shared = {
            "codex": self.home / ".codex/config.toml",
            "cursor": self.home / ".cursor/mcp.json",
            "windsurf": self.home / ".codeium/windsurf/mcp_config.json",
        }
        for paths in (windows, macos, linux):
            for name, expected in shared.items():
                self.assertEqual(expected, paths[name])

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
            executable=self.launcher,
        )[0]

        self.assertEqual(result.status, "configured")
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(Path(result.backup_path).read_bytes(), original)
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["mcpServers"]["other"], {"command": "other"})
        self.assertEqual(
            saved["mcpServers"]["comic-sol"],
            {
                "command": str(self.launcher.resolve()),
                "args": ["mcp", "--root", str(self.output.resolve())],
            },
        )

    def test_bare_launcher_is_resolved_and_repeated_setup_is_unchanged(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        with mock.patch(
            "comic_sol_product.setup.shutil.which", return_value=str(self.launcher)
        ):
            first = setup_clients(
                self.output, adapters=[adapter], executable="comic-sol"
            )[0]
            first_bytes = config.read_bytes()
            second = setup_clients(
                self.output, adapters=[adapter], executable="comic-sol"
            )[0]

        self.assertEqual(first.status, "configured")
        self.assertEqual(second.status, "unchanged")
        self.assertIsNone(second.backup_path)
        self.assertEqual(config.read_bytes(), first_bytes)
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(list(saved["mcpServers"]), ["comic-sol"])
        self.assertEqual(
            saved["mcpServers"]["comic-sol"]["command"],
            str(self.launcher.resolve()),
        )

    def test_malformed_config_is_refused_without_backup_or_write(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{ definitely not json\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

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

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNotNone(result.backup_path)

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_setup_preserves_restrictive_config_backup_and_parent_modes(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.parent.chmod(0o700)
        config.write_text("{}\n", encoding="utf-8")
        config.chmod(0o600)
        parent_mode = stat.S_IMODE(config.parent.stat().st_mode)

        result = setup_clients(
            self.output,
            adapters=[JsonClientAdapter("cursor", config, "mcpServers")],
            executable=self.launcher,
        )[0]

        self.assertEqual("configured", result.status)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(
            0o600,
            stat.S_IMODE(Path(result.backup_path).stat().st_mode),
        )
        self.assertEqual(parent_mode, stat.S_IMODE(config.parent.stat().st_mode))

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_rollback_preserves_restrictive_config_and_backup_modes(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.parent.chmod(0o700)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        config.chmod(0o600)
        parent_mode = stat.S_IMODE(config.parent.stat().st_mode)
        adapter = JsonClientAdapter(
            "cursor",
            config,
            "mcpServers",
            verify_hook=lambda: False,
        )

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("rolled-back", result.status)
        self.assertEqual(original, config.read_bytes())
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(
            0o600,
            stat.S_IMODE(Path(result.backup_path).stat().st_mode),
        )
        self.assertEqual(parent_mode, stat.S_IMODE(config.parent.stat().st_mode))

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_setup_preserves_group_read_mode(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        config.chmod(0o640)

        result = setup_clients(
            self.output,
            adapters=[JsonClientAdapter("cursor", config, "mcpServers")],
            executable=self.launcher,
        )[0]

        self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(Path(result.backup_path).stat().st_mode))

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_rollback_preserves_group_read_mode(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        config.chmod(0o640)
        adapter = JsonClientAdapter(
            "cursor",
            config,
            "mcpServers",
            verify_hook=lambda: False,
        )

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("rolled-back", result.status)
        self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(Path(result.backup_path).stat().st_mode))


    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics are unavailable on Windows")
    def test_backup_collision_with_symlink_fails_without_following_or_overwriting_it(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        outside = self.home / "outside-secret.txt"
        outside.write_bytes(b"must-survive")
        backup = config.with_name("mcp.json.bak-fixed")
        backup.symlink_to(outside)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(client_setup, "_backup_path", return_value=backup):
            result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(original, config.read_bytes())
        self.assertEqual(b"must-survive", outside.read_bytes())
        self.assertTrue(backup.is_symlink())
        self.assertIsNone(result.backup_path)

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_setup_prunes_old_backups_and_keeps_the_five_newest(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        for index in range(1, 6):
            backup = config.with_name(f"mcp.json.bak-2026010100000{index}Z")
            backup.write_bytes(f"old-{index}".encode())
            backup.chmod(0o600)
        oldest = config.with_name("mcp.json.bak-20260101000000Z")
        oldest.write_bytes(b"oldest")
        oldest.chmod(0o600)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("configured", result.status)
        backups = sorted(config.parent.glob("mcp.json.bak-*"))
        self.assertEqual(5, len(backups))
        self.assertNotIn(oldest, backups)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups))

    def test_atomic_publish_rejects_concurrent_entry_and_preserves_it(self):
        config = self.home / "config.json"
        original = b"original"
        config.write_bytes(original)
        snapshot = client_setup._read_snapshot(config)

        client_setup._atomic_write(config, b"candidate", expected=snapshot)
        self.assertEqual(b"candidate", config.read_bytes())

        concurrent = b"concurrent"
        config.write_bytes(concurrent)
        with self.assertRaises(client_setup._ConfigChangedError):
            client_setup._atomic_write(config, b"stale", expected=snapshot)
        self.assertEqual(concurrent, config.read_bytes())

    def test_setup_aborts_when_config_changes_before_publish(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"user":"before"}\n'
        concurrent = b'{"user":"concurrent"}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        real_backup = client_setup._write_backup

        def mutate_after_snapshot(backup_path, data):
            real_backup(backup_path, data)
            config.write_bytes(concurrent)

        with mock.patch.object(client_setup, "_write_backup", side_effect=mutate_after_snapshot):
            result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(concurrent, config.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics are unavailable on Windows")
    def test_config_symlink_is_refused_without_following_target(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        outside = self.home / "outside-config.json"
        original = b'{"user":"outside"}\n'
        outside.write_bytes(original)
        config.symlink_to(outside)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("failed", result.status)
        self.assertEqual(original, outside.read_bytes())
        self.assertTrue(config.is_symlink())

    def test_config_lock_covers_snapshot_mutation_and_publish(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        lock_path = config.with_name(f".{config.name}.lock")
        observed = []
        real_backup = client_setup._write_backup

        def observe_lock(backup_path, data):
            observed.append(lock_path.is_file())
            real_backup(backup_path, data)

        with mock.patch.object(client_setup, "_write_backup", side_effect=observe_lock):
            result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("configured", result.status)
        self.assertEqual([True], observed)

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
                "args": ["mcp", "--root", str(self.output.resolve()), "--root", str(self.output.resolve())],
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

        first = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        second = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        text = config.read_text(encoding="utf-8")

        self.assertEqual(first.status, "configured")
        self.assertEqual(second.status, "unchanged")
        self.assertIn('model = "gpt-test"', text)
        self.assertIn('[mcp_servers.other]', text)
        self.assertEqual(text.count("[mcp_servers.comic-sol]"), 1)
        self.assertIn('args = ["mcp", "--root",', text)

    def test_codex_setup_replaces_quoted_key_without_duplicate_table(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt-test"\n\n'
            '[mcp_servers."comic-sol"]\n'
            'command = "old-comic-sol"\n'
            'args = ["old"]\n',
            encoding="utf-8",
        )
        adapter = CodexAdapter(config)

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        saved = config.read_text(encoding="utf-8")
        parsed = tomllib.loads(saved)

        self.assertEqual(result.status, "configured")
        self.assertEqual(saved.count("[mcp_servers.comic-sol]"), 1)
        self.assertNotIn('[mcp_servers."comic-sol"]', saved)
        self.assertEqual(
            Path(parsed["mcp_servers"]["comic-sol"]["command"]).resolve(),
            self.launcher.resolve(),
        )
        self.assertEqual(parsed["mcp_servers"]["comic-sol"]["args"][:2], ["mcp", "--root"])
        self.assertEqual(parsed["model"], "gpt-test")

    def test_uninstall_removes_only_integration_and_preserves_projects(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers":{"other":{"command":"other"}}}\n', encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        setup_clients(self.output, adapters=[adapter], executable=self.launcher)

        result = uninstall_clients(self.output, adapters=[adapter])[0]
        saved = json.loads(config.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "removed")
        self.assertNotIn("comic-sol", saved["mcpServers"])
        self.assertEqual(saved["mcpServers"]["other"], {"command": "other"})
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "project")

    def test_codex_uninstall_ignores_section_like_text_in_multiline_string(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        original = (
            '[other]\n'
            'note = """\n'
            '[mcp_servers.comic-sol]\n'
            '"""\n\n'
            '[kept_server]\n'
            'command = "important-user-server"\n'
        ).encode("utf-8")
        config.write_bytes(original)
        adapter = CodexAdapter(config)

        result = uninstall_clients(self.output, adapters=[adapter])[0]

        self.assertEqual(result.status, "not-configured")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNone(result.backup_path)
        self.assertEqual(
            "important-user-server",
            tomllib.loads(config.read_text(encoding="utf-8"))["kept_server"]["command"],
        )

    def test_codex_uninstall_removes_last_section_and_keeps_document_valid(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt-test"\n\n'
            '[mcp_servers.comic-sol]\n'
            'command = "comic-sol"\n'
            'args = ["mcp", "--root", "/tmp/comic-sol"]\n',
            encoding="utf-8",
        )
        adapter = CodexAdapter(config)

        result = uninstall_clients(self.output, adapters=[adapter])[0]
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "removed")
        self.assertEqual(parsed, {"model": "gpt-test"})

    def test_codex_uninstall_preserves_trailing_comments_after_last_section(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[mcp_servers.comic-sol]\n'
            'command = "comic-sol"\n'
            'args = ["mcp", "--root", "/tmp/comic-sol"]\n\n'
            '# user note after integration\n',
            encoding="utf-8",
        )
        adapter = CodexAdapter(config)

        result = uninstall_clients(self.output, adapters=[adapter])[0]
        saved = config.read_text(encoding="utf-8")

        self.assertEqual(result.status, "removed")
        self.assertIn("# user note after integration", saved)
        self.assertEqual(tomllib.loads(saved), {})

    def test_codex_uninstall_removes_quoted_key_section(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt-test"\n\n'
            '[mcp_servers."comic-sol"]\n'
            'command = "comic-sol"\n'
            'args = ["mcp", "--root", "/tmp/comic-sol"]\n',
            encoding="utf-8",
        )
        adapter = CodexAdapter(config)

        result = uninstall_clients(self.output, adapters=[adapter])[0]

        self.assertEqual(result.status, "removed")
        self.assertEqual(
            tomllib.loads(config.read_text(encoding="utf-8")),
            {"model": "gpt-test"},
        )

    def test_codex_uninstall_removes_whitespace_and_quoted_dotted_key_sections(self):
        for header in (
            '[mcp_servers . "comic-sol"]',
            '["mcp_servers"."comic-sol"]',
        ):
            with self.subTest(header=header):
                config = self.home / ".codex" / "config.toml"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(
                    'model = "gpt-test"\n\n'
                    f"{header}\n"
                    'command = "comic-sol"\n'
                    'args = ["mcp", "--root", "/tmp/comic-sol"]\n',
                    encoding="utf-8",
                )
                result = uninstall_clients(self.output, adapters=[CodexAdapter(config)])[0]

                self.assertEqual(result.status, "removed")
                self.assertEqual(
                    tomllib.loads(config.read_text(encoding="utf-8")),
                    {"model": "gpt-test"},
                )

    def test_uninstall_verification_failure_rolls_back_byte_for_byte(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers":{"other":{"command":"other"}}}\n', encoding="utf-8")
        setup_adapter = JsonClientAdapter("cursor", config, "mcpServers")
        setup_clients(self.output, adapters=[setup_adapter], executable=self.launcher)
        original = config.read_bytes()
        uninstall_adapter = JsonClientAdapter(
            "cursor", config, "mcpServers", verify_hook=lambda: False
        )

        result = uninstall_clients(self.output, adapters=[uninstall_adapter])[0]

        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNotNone(result.backup_path)

    def test_uninstall_rolls_back_when_written_toml_is_malformed(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        original = (
            '[mcp_servers.comic-sol]\n'
            'command = "comic-sol"\n'
            'args = ["mcp", "--root", "/tmp/comic-sol"]\n'
        ).encode("utf-8")
        config.write_bytes(original)
        adapter = CodexAdapter(config)

        with mock.patch.object(adapter, "dump", return_value=b"[broken\n"):
            result = uninstall_clients(self.output, adapters=[adapter])[0]

        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNotNone(result.backup_path)

    def test_custom_adapter_without_removed_verifier_is_supported(self):
        class CustomAdapter:
            name = "custom"

            def __init__(self, path):
                self.config_path = path

            def detect(self):
                return self.config_path.is_file()

            def load(self, raw):
                return raw.decode("utf-8")

            def mutate(self, config, entry):
                return config + "comic-sol\n", True

            def remove(self, config):
                marker = "comic-sol\n"
                if not config.endswith(marker):
                    return config, False
                return config[:-len(marker)], True

            def dump(self, config):
                return config.encode("utf-8")

            def verify(self, expected=None):
                return True

        config = self.home / "custom.conf"
        config.write_bytes(b"existing\ncomic-sol\n")

        result = uninstall_clients(self.output, adapters=[CustomAdapter(config)])[0]

        self.assertEqual(result.status, "removed")
        self.assertEqual(config.read_text(encoding="utf-8"), "existing\n")

    def test_uninstall_rolls_back_when_custom_adapter_load_raises(self):
        class RaisingAdapter:
            name = "raising"

            def __init__(self, path):
                self.config_path = path

            def detect(self):
                return True

            def load(self, raw):
                if raw == b"original\n":
                    return raw
                raise TypeError("post-write validation failed")

            def mutate(self, config, entry):
                return config, False

            def remove(self, config):
                return b"changed\n", True

            def dump(self, config):
                return config

            def verify(self, expected=None):
                return True

        config = self.home / "raising.conf"
        config.write_bytes(b"original\n")

        result = uninstall_clients(self.output, adapters=[RaisingAdapter(config)])[0]

        self.assertEqual(result.status, "rolled-back")
        self.assertEqual(config.read_bytes(), b"original\n")

    def test_missing_config_is_reported_skipped_without_creation(self):
        config = self.home / ".cursor" / "mcp.json"
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        self.assertEqual(result.status, "skipped")
        self.assertFalse(config.exists())

    def test_missing_config_skips_before_launcher_resolution(self):
        config = self.home / ".cursor" / "mcp.json"
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch(
            "comic_sol_product.setup.shutil.which", return_value=None
        ) as which:
            result = setup_clients(
                self.output,
                adapters=[adapter],
                executable="missing-comic-sol",
            )[0]

        self.assertEqual("skipped", result.status)
        which.assert_not_called()

    def test_unresolvable_launcher_fails_before_config_mutation(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{}\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch("comic_sol_product.setup.shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                setup_clients(
                    self.output,
                    adapters=[adapter],
                    executable="missing-comic-sol",
                )

        self.assertEqual(original, config.read_bytes())
        self.assertEqual([], list(config.parent.glob("*.bak-*")))

    @unittest.skipUnless(os.name == "nt", "Windows launcher semantics")
    def test_absolute_console_stub_ignores_a_cwd_decoy(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        console_path = self.home / "console stub" / "comic-sol"
        console_path.parent.mkdir()
        native_launcher = console_path.with_suffix(".exe")
        native_launcher.write_bytes(b"native launcher")
        decoy_directory = self.home / "decoy"
        decoy_directory.mkdir()
        (decoy_directory / "comic-sol.exe").write_bytes(b"decoy launcher")

        with contextlib.chdir(decoy_directory):
            result = setup_clients(
                self.output,
                adapters=[adapter],
                executable=console_path,
            )[0]

        entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][
            "comic-sol"
        ]
        self.assertEqual("configured", result.status)
        self.assertEqual(str(native_launcher.resolve()), entry["command"])

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
