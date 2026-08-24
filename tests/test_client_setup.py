import contextlib
import errno
import json
import os
import stat
import sys
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
        self.launcher = (
            self.home / "bin with spaces" / ("comic-sol.exe" if os.name == "nt" else "comic-sol")
        )
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b"launcher")
        if os.name != "nt":
            self.launcher.chmod(0o755)

        def verify_fixture_launcher(executable):
            if Path(executable).name.lower() not in {"comic-sol", "comic-sol.exe"}:
                raise RuntimeError("Comic Sol executable identity check failed")

        self.real_verify_launcher_identity = client_setup._verify_launcher_identity
        launcher_check = mock.patch.object(
            client_setup, "_verify_launcher_identity", side_effect=verify_fixture_launcher
        )
        launcher_check.start()
        self.addCleanup(launcher_check.stop)
        runtime_check = mock.patch.object(client_setup, "_mcp_runtime_available", return_value=True)
        runtime_check.start()
        self.addCleanup(runtime_check.stop)

    def adapter_paths(self, platform: str) -> dict[str, Path]:
        with mock.patch.object(client_setup.sys, "platform", platform):
            return {
                adapter.name: adapter.config_path
                for adapter in client_setup.default_adapters(self.home)
            }

    def test_default_adapter_paths_are_platform_native(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.home / "Roaming")}):
            windows = self.adapter_paths("win32")
        macos = self.adapter_paths("darwin")
        linux = self.adapter_paths("linux")

        self.assertEqual(
            self.home / "Roaming/Claude/claude_desktop_config.json",
            windows["claude-desktop"],
        )
        self.assertEqual(
            self.home / "Library/Application Support/Claude/claude_desktop_config.json",
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
        with mock.patch("comic_sol_product.setup.shutil.which", return_value=str(self.launcher)):
            first = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]
            first_bytes = config.read_bytes()
            second = setup_clients(self.output, adapters=[adapter], executable="comic-sol")[0]

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

    def test_unchanged_setup_does_not_require_config_lock(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        expected = {
            "command": str(self.launcher.resolve()),
            "args": ["mcp", "--root", str(self.output.resolve())],
        }
        config.write_text(
            json.dumps({"mcpServers": {"comic-sol": expected}}),
            encoding="utf-8",
        )
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(
            client_setup, "_ConfigLock", side_effect=PermissionError("read-only")
        ):
            result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("unchanged", result.status)
        self.assertIsNone(result.backup_path)

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

    @unittest.skipIf(
        os.name == "nt" or sys.platform == "darwin",
        "Windows and macOS have platform-specific publication paths",
    )
    def test_atomic_publish_fails_closed_when_exchange_is_unavailable(self):
        config = self.home / "config.json"
        original = b"original"
        config.write_bytes(original)
        snapshot = client_setup._read_snapshot(config)

        with mock.patch.object(
            client_setup,
            "_rename_exchange",
            side_effect=OSError(errno.ENOTSUP, "exchange unavailable"),
        ):
            with self.assertRaises(OSError):
                client_setup._atomic_write(config, b"candidate", expected=snapshot)

        self.assertEqual(original, config.read_bytes())

    def test_darwin_publish_fails_closed_when_exchange_is_unavailable(self):
        config = self.home / "config.json"
        original = b"original"
        config.write_bytes(original)
        snapshot = client_setup._read_snapshot(config)

        with (
            mock.patch.object(client_setup.sys, "platform", "darwin"),
            mock.patch.object(
                client_setup,
                "_rename_exchange",
                side_effect=OSError(errno.ENOTSUP, "exchange unavailable"),
            ),
        ):
            with self.assertRaises(OSError):
                client_setup._atomic_write(config, b"candidate", expected=snapshot)

        self.assertEqual(original, config.read_bytes())

    def test_darwin_publish_tolerates_unsupported_directory_fsync(self):
        config = self.home / "config.json"
        original = b"original"
        config.write_bytes(original)
        snapshot = client_setup._read_snapshot(config)
        real_fsync = os.fsync

        def fail_directory_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError(errno.ENOTSUP, "directory fsync unavailable")
            real_fsync(descriptor)

        def exchange(source, destination, *, directory=None):
            self.assertIsNone(directory)
            displaced = source.with_name(f"{source.name}.exchange")
            os.replace(destination, displaced)
            os.replace(source, destination)
            os.replace(displaced, source)

        with (
            mock.patch.object(client_setup.sys, "platform", "darwin"),
            mock.patch.object(
                client_setup,
                "_rename_exchange",
                side_effect=exchange,
            ),
            mock.patch.object(client_setup.os, "fsync", side_effect=fail_directory_fsync),
        ):
            client_setup._atomic_write(config, b"candidate", expected=snapshot)

        self.assertEqual(b"candidate", config.read_bytes())

    def test_darwin_config_directory_resolves_only_sanctioned_system_alias(self):
        with (
            mock.patch.object(client_setup.sys, "platform", "darwin"),
            mock.patch.object(Path, "resolve", return_value=Path("/private/var")) as resolve,
        ):
            directory = client_setup._ConfigDirectory(Path("/var/folders/example"))

        resolve.assert_called_once_with(strict=True)
        self.assertEqual(Path("/private/var/folders/example"), directory.path)

    def test_adapter_detect_failure_is_per_client(self):
        class DetectFailure:
            name = "detect-failure"
            config_path = self.home / "detect.conf"

            def detect(self):
                raise TypeError("detect failed")

        result = setup_clients(self.output, adapters=[DetectFailure()])[0]
        self.assertEqual("failed", result.status)
        self.assertIn("detect failed", result.message)

    def test_adapter_mutate_failure_does_not_abort_later_adapter(self):
        class MutateFailure:
            name = "mutate-failure"

            def __init__(self, path):
                self.config_path = path

            def detect(self):
                return True

            def load(self, raw):
                return raw

            def mutate(self, config, entry):
                raise TypeError("mutate failed")

            def remove(self, config):
                return config, False

            def dump(self, config):
                return config

            def verify(self, expected=None):
                return True

        first = self.home / "first.conf"
        first.write_bytes(b"first")
        second = self.home / ".cursor" / "mcp.json"
        second.parent.mkdir(parents=True)
        second.write_text("{}\n", encoding="utf-8")
        results = setup_clients(
            self.output,
            adapters=[
                MutateFailure(first),
                JsonClientAdapter("cursor", second, "mcpServers"),
            ],
            executable=self.launcher,
        )

        self.assertEqual("failed", results[0].status)
        self.assertIn("mutate failed", results[0].message)
        self.assertEqual("configured", results[1].status)

    def test_custom_stateful_adapter_runs_load_and_mutate_once_under_lock(self):
        class StatefulAdapter:
            name = "stateful"

            def __init__(self, path):
                self.config_path = path
                self.loads = 0
                self.mutations = 0

            def detect(self):
                return True

            def load(self, raw):
                self.loads += 1
                return raw

            def mutate(self, config, entry):
                self.mutations += 1
                return config + b"\ncomic-sol", True

            def remove(self, config):
                return config, False

            def dump(self, config):
                return config

            def verify(self, expected=None):
                return True

        config = self.home / "stateful.conf"
        config.write_bytes(b"original")
        adapter = StatefulAdapter(config)

        result = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]

        self.assertEqual("configured", result.status)
        self.assertEqual(1, adapter.loads)
        self.assertEqual(1, adapter.mutations)

        class DumpFailure:
            name = "dump-failure"

            def __init__(self, path):
                self.config_path = path

            def detect(self):
                return True

            def load(self, raw):
                return raw

            def mutate(self, config, entry):
                return b"changed", True

            def remove(self, config):
                return config, False

            def dump(self, config):
                raise KeyError("dump failed")

            def verify(self, expected=None):
                return True

        config = self.home / "dump.conf"
        config.write_bytes(b"original")
        result = setup_clients(
            self.output,
            adapters=[DumpFailure(config)],
            executable=self.launcher,
        )[0]

        self.assertEqual("failed", result.status)
        self.assertIn("dump failed", result.message)
        self.assertEqual(b"original", config.read_bytes())

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
            json.dumps(
                {
                    "mcpServers": {
                        "comic-sol": {
                            "command": "comic-sol",
                            "args": [
                                "mcp",
                                "--root",
                                str(self.output.resolve()),
                                "--root",
                                str(self.output.resolve()),
                            ],
                        }
                    }
                }
            ),
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

        self.assertFalse(
            JsonClientAdapter("cursor", config, "mcpServers", verify_hook=hook).verify({})
        )
        self.assertEqual([True], calls)

    def test_codex_toml_preserves_existing_config_and_is_idempotent(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8"
        )
        adapter = CodexAdapter(config)

        first = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        second = setup_clients(self.output, adapters=[adapter], executable=self.launcher)[0]
        text = config.read_text(encoding="utf-8")

        self.assertEqual(first.status, "configured")
        self.assertEqual(second.status, "unchanged")
        self.assertIn('model = "gpt-test"', text)
        self.assertIn("[mcp_servers.other]", text)
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
            "[other]\n"
            'note = """\n'
            "[mcp_servers.comic-sol]\n"
            '"""\n\n'
            "[kept_server]\n"
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
            "[mcp_servers.comic-sol]\n"
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
            "[mcp_servers.comic-sol]\n"
            'command = "comic-sol"\n'
            'args = ["mcp", "--root", "/tmp/comic-sol"]\n\n'
            "# user note after integration\n",
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
            "[mcp_servers.comic-sol]\n"
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
                return config[: -len(marker)], True

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

        with mock.patch("comic_sol_product.setup.shutil.which", return_value=None) as which:
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

        entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["comic-sol"]
        self.assertEqual("configured", result.status)
        self.assertEqual(str(native_launcher.resolve()), entry["command"])

    def test_repair_dry_run_previews_without_mutating_config(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"mcpServers":{"other":{"command":"other"}}}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
            dry_run=True,
        )[0]

        self.assertEqual("success", result.state)
        self.assertEqual("planned", result.status)
        self.assertEqual("set-comic-sol-entry", result.action)
        self.assertEqual(str(config), result.config_path)
        self.assertTrue(result.backup_required)
        self.assertEqual(
            {
                "command": str(self.launcher.resolve()),
                "args": ["mcp", "--root", str(self.output.resolve())],
            },
            result.planned_entry,
        )
        self.assertFalse(result.verified)
        self.assertIsNone(result.restored)
        self.assertIsNone(result.error)
        self.assertEqual(original, config.read_bytes())
        self.assertEqual([], list(config.parent.glob("*.bak-*")))

    def test_repair_missing_launcher_is_a_structured_failure_without_mutation(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{}\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch("comic_sol_product.setup.shutil.which", return_value=None):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable="missing-comic-sol",
            )[0]

        self.assertEqual(("failure", "failed"), (result.state, result.status))
        self.assertEqual("CS-INSTALL-002", result.error["code"])
        self.assertIn("doctor", result.error["recovery"])
        self.assertEqual(original, config.read_bytes())
        self.assertEqual([], list(config.parent.glob("*.bak-*")))

    def test_repair_twice_is_idempotent(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        first = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]
        repaired = config.read_bytes()
        second = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual(("success", "configured"), (first.state, first.status))
        self.assertTrue(first.verified)
        self.assertEqual(("no-op", "unchanged"), (second.state, second.status))
        self.assertIsNone(second.backup_path)
        self.assertEqual(repaired, config.read_bytes())

    def test_repair_restores_after_post_publication_failure(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")
        real_atomic_write = client_setup._atomic_write
        calls = 0

        def fail_after_publish(path, data, mode=None, *, expected=None, directory=None):
            nonlocal calls
            calls += 1
            real_atomic_write(path, data, mode, expected=expected, directory=directory)
            if calls == 1:
                raise OSError("durability failed after publish")

        with mock.patch.object(client_setup, "_atomic_write", side_effect=fail_after_publish):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual(("failure", "rolled-back"), (result.state, result.status))
        self.assertTrue(result.restored)
        self.assertEqual(original, config.read_bytes())

    def test_unchanged_repair_recomputes_under_lock(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        expected = {
            "command": str(self.launcher.resolve()),
            "args": ["mcp", "--root", str(self.output.resolve())],
        }
        config.write_text(json.dumps({"mcpServers": {"comic-sol": expected}}), encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(
            client_setup, "_ConfigLock", side_effect=PermissionError("lock denied")
        ):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual("failure", result.state)
        self.assertEqual("failed", result.status)
        self.assertIsNotNone(result.error)
        self.assertEqual("CS-INSTALL-002", result.error["code"])
        self.assertFalse(result.verified)
        self.assertIsNone(result.restored)

    def test_repair_failure_and_skipped_evidence_only_claims_completed_work(self):
        missing = JsonClientAdapter("missing", self.home / "missing.json", "mcpServers")

        class DetectFailure:
            name = "broken"
            config_path = self.home / "broken.json"

            def detect(self):
                raise OSError("detect failed")

        results = client_setup.repair_clients(
            self.output,
            selected=["missing", "broken"],
            adapters=[missing, DetectFailure()],
            executable=self.launcher,
        )

        self.assertFalse(results[0].verified)
        self.assertIsNone(results[0].restored)
        self.assertFalse(results[1].verified)
        self.assertIsNone(results[1].restored)

    def test_repair_missing_mcp_runtime_is_diagnostic_only(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{}\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(client_setup, "_mcp_runtime_available", return_value=False):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual("failure", result.state)
        self.assertEqual("CS-INSTALL-002", result.error["code"])
        self.assertIn("doctor", result.error["recovery"])
        self.assertEqual(original, config.read_bytes())

    def test_repair_rejects_unknown_selected_client(self):
        with self.assertRaisesRegex(ValueError, "unsupported client"):
            client_setup.repair_clients(
                self.output,
                selected=["curser"],
                adapters=[],
                executable=self.launcher,
            )

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics are unavailable on Windows")
    def test_repair_refuses_symlinked_config_parent(self):
        outside = self.home / "outside"
        outside.mkdir()
        config = outside / "mcp.json"
        original = b"{}\n"
        config.write_bytes(original)
        linked_parent = self.home / ".cursor"
        linked_parent.symlink_to(outside, target_is_directory=True)
        adapter = JsonClientAdapter("cursor", linked_parent / "mcp.json", "mcpServers")

        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual("failure", result.state)
        self.assertEqual(original, config.read_bytes())
        self.assertEqual([], list(outside.glob("*.bak-*")))

    def test_repair_parent_guard_failure_returns_structured_result(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(
            client_setup._ConfigDirectory,
            "__enter__",
            side_effect=OSError("parent is a reparse point"),
        ):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual(("failure", "failed"), (result.state, result.status))
        self.assertEqual("CS-INSTALL-002", result.error["code"])
        self.assertIsNone(result.restored)
        self.assertEqual("{}\n", config.read_text(encoding="utf-8"))

    def test_repair_pre_publish_failure_does_not_claim_rollback(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b"{}\n"
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        with mock.patch.object(
            client_setup, "_atomic_write", side_effect=OSError("temporary write failed")
        ):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual(("failure", "failed"), (result.state, result.status))
        self.assertIsNone(result.restored)
        self.assertEqual(original, config.read_bytes())

    def test_repair_rejects_fake_launcher_identity(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n", encoding="utf-8")
        fake = self.home / "not-comic-sol.exe"
        fake.write_text("fake", encoding="utf-8")
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        client_setup._verify_launcher_identity = self.real_verify_launcher_identity
        try:
            with mock.patch.object(
                client_setup,
                "_verify_launcher_identity",
                wraps=self.real_verify_launcher_identity,
            ) as identity:
                result = client_setup.repair_clients(
                    self.output,
                    adapters=[adapter],
                    executable=fake,
                )[0]

            identity.assert_called_once()

            self.assertEqual("failure", result.state)
            self.assertEqual("CS-INSTALL-002", result.error["code"])
            self.assertEqual("{}\n", config.read_text(encoding="utf-8"))
        finally:

            def verify_fixture_launcher(executable):
                if Path(executable).name.lower() not in {"comic-sol", "comic-sol.exe"}:
                    raise RuntimeError("Comic Sol executable identity check failed")

            client_setup._verify_launcher_identity = verify_fixture_launcher

    def test_repair_does_not_rollback_over_a_concurrent_third_party_edit(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)

        def edit_before_verification():
            config.write_bytes(b'{"thirdParty":true}\n')
            return False

        adapter = JsonClientAdapter(
            "cursor", config, "mcpServers", verify_hook=edit_before_verification
        )
        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual(("failure", "rollback-failed"), (result.state, result.status))
        self.assertFalse(result.restored)
        self.assertEqual("CS-INSTALL-003", result.error["code"])
        self.assertEqual(b'{"thirdParty":true}\n', config.read_bytes())

    def test_repair_preserves_unrelated_json_bytes(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = (
            "{\n"
            '  "z": [1, 2],\n'
            '  "mcpServers": {\n'
            '    "other": {"command":"other"}\n'
            "  },\n"
            '  "a": 1\n'
            "}\n"
        ).encode()
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual("configured", result.status)
        updated = config.read_bytes().decode()
        self.assertIn('  "z": [1, 2],\n', updated)
        self.assertIn('  "a": 1\n', updated)
        self.assertIn('    "other": {"command":"other"}\n', updated)
        self.assertEqual(1, updated.count('"comic-sol"'))

    def test_repair_replaces_existing_json_entry_without_losing_indentation(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        stale = {
            "command": str(self.launcher.resolve()),
            "args": ["mcp", "--root", str(self.home / "old-output")],
        }
        original = (
            f'{{\n  "mcpServers": {{\n    "comic-sol": {json.dumps(stale, indent=2)}\n  }}\n}}\n'
        ).encode()
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers")

        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual("configured", result.status)
        updated = config.read_text(encoding="utf-8")
        self.assertIn('    "comic-sol": {\n', updated)
        self.assertIn('      "args": [\n', updated)
        self.assertEqual(1, updated.count('"comic-sol"'))
        self.assertEqual(
            str(self.output.resolve()), json.loads(updated)["mcpServers"]["comic-sol"]["args"][2]
        )

    def test_json_publish_ignores_same_key_outside_servers_mapping(self):
        adapter = JsonClientAdapter("cursor", self.home / "mcp.json", "mcpServers")
        original = b'{"metadata":{"comic-sol":{"keep":true}},"mcpServers":{}}\n'
        published = adapter.publish(
            original,
            {
                "command": str(self.launcher.resolve()),
                "args": ["mcp", "--root", str(self.output.resolve())],
            },
        )
        document = json.loads(published)
        self.assertTrue(document["metadata"]["comic-sol"]["keep"])
        self.assertIn("comic-sol", document["mcpServers"])

        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers", verify_hook=lambda: False)

        result = client_setup.repair_clients(
            self.output,
            adapters=[adapter],
            executable=self.launcher,
        )[0]

        self.assertEqual(("failure", "rolled-back"), (result.state, result.status))
        self.assertFalse(result.verified)
        self.assertTrue(result.restored)
        self.assertEqual("CS-INSTALL-002", result.error["code"])
        self.assertEqual(original, config.read_bytes())
        self.assertEqual(original, Path(result.backup_path).read_bytes())

    def test_repair_reports_when_rollback_cannot_be_verified(self):
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        adapter = JsonClientAdapter("cursor", config, "mcpServers", verify_hook=lambda: False)
        real_atomic_write = client_setup._atomic_write
        calls = 0

        def corrupt_restoration(path, data, mode=None, *, expected=None, directory=None):
            nonlocal calls
            calls += 1
            real_atomic_write(path, data, mode, expected=expected, directory=directory)
            if calls == 2:
                path.write_bytes(b"corrupt restoration")

        with mock.patch.object(client_setup, "_atomic_write", side_effect=corrupt_restoration):
            result = client_setup.repair_clients(
                self.output,
                adapters=[adapter],
                executable=self.launcher,
            )[0]

        self.assertEqual("failure", result.state)
        self.assertEqual("rollback-failed", result.status)
        self.assertFalse(result.verified)
        self.assertFalse(result.restored)
        self.assertEqual("CS-INSTALL-003", result.error["code"])
        self.assertNotEqual(original, config.read_bytes())
        self.assertEqual(original, Path(result.backup_path).read_bytes())

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
