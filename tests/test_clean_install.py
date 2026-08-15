import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comic_sol_product.release import (
    FORBIDDEN_WHEEL_MEMBERS,
    REQUIRED_SDIST_SUFFIXES,
    REQUIRED_WHEEL_MEMBERS,
    validate_sdist_members,
    validate_wheel_members,
)
from scripts import clean_install_smoke, installed_mcp_smoke


class DistributionContractTests(unittest.TestCase):
    def test_required_wheel_members_cover_runtime_and_skill(self):
        self.assertIn("comic_sol_product/engine/comic_sol.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/quality_records.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/normalize_panels.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/typography.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/layouts.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/page_quality.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/pdf_quality.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/quality_sample.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/assets/fonts/ComicNeue-Regular.ttf", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/templates/manifest.json", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/skill/SKILL.md", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/skill/references/workflow.md", REQUIRED_WHEEL_MEMBERS)

    def test_distribution_validation_reports_every_missing_member(self):
        present = REQUIRED_WHEEL_MEMBERS - {
            "comic_sol_product/skill/SKILL.md",
            "comic_sol_product/skill/references/workflow.md",
        }
        with self.assertRaisesRegex(ValueError, "SKILL.md.*workflow.md"):
            validate_wheel_members(present)

    def test_distribution_validation_accepts_complete_archive(self):
        validate_wheel_members(REQUIRED_WHEEL_MEMBERS | {"extra.txt"})

    def test_distribution_rejects_build_only_scripts(self):
        with self.assertRaisesRegex(ValueError, "build-only"):
            validate_wheel_members(REQUIRED_WHEEL_MEMBERS | FORBIDDEN_WHEEL_MEMBERS)

    def test_distribution_forbids_test_fixture_content_from_runtime(self):
        forbidden = {
            "comic_sol_product/engine/test_quality_matrix.py",
            "comic_sol_product/engine/support.py",
            "comic_sol_product/engine/quality-matrix/README.md",
        }
        with self.assertRaisesRegex(ValueError, "build-only"):
            validate_wheel_members(REQUIRED_WHEEL_MEMBERS | forbidden)

    def test_sdist_contract_covers_source_skill_and_runtime_assets(self):
        members = {f"comic-sol-2.0.0.dev0{suffix}" for suffix in REQUIRED_SDIST_SUFFIXES}
        validate_sdist_members(members)
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            validate_sdist_members({name for name in members if not name.endswith("/SKILL.md")})


class CleanInstallSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "projects"
        self.launcher = self.root / "bin with spaces" / (
            "comic-sol.exe" if os.name == "nt" else "comic-sol"
        )
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b"launcher")
        self.config = self.root / "home" / ".codex" / "config.toml"
        self.config.parent.mkdir(parents=True)

    def write_codex_entry(self, command: str, arguments: list[str]) -> None:
        self.config.write_text(
            "[mcp_servers.comic-sol]\n"
            f"command = {json.dumps(command)}\n"
            f"args = {json.dumps(arguments)}\n",
            encoding="utf-8",
        )

    def test_reads_exact_persisted_codex_entry(self):
        expected_arguments = ["mcp", "--root", str(self.output.resolve())]
        self.write_codex_entry(str(self.launcher.resolve()), expected_arguments)

        entry = clean_install_smoke.read_codex_entry(
            self.config, self.output, self.launcher
        )

        self.assertEqual(
            {"command": str(self.launcher.resolve()), "args": expected_arguments},
            entry,
        )

    def test_rejects_relative_persisted_command(self):
        self.write_codex_entry(
            "comic-sol", ["mcp", "--root", str(self.output.resolve())]
        )

        with self.assertRaisesRegex(RuntimeError, "non-absolute"):
            clean_install_smoke.read_codex_entry(
                self.config, self.output, self.launcher
            )

    def test_rejects_absolute_command_other_than_installed_launcher(self):
        other = self.root / "other" / self.launcher.name
        other.parent.mkdir()
        other.write_bytes(b"unrelated launcher")
        self.write_codex_entry(
            str(other.resolve()),
            ["mcp", "--root", str(self.output.resolve())],
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected MCP command"):
            clean_install_smoke.read_codex_entry(
                self.config, self.output, self.launcher
            )

    def test_minimal_environment_excludes_the_installation_path(self):
        source = {
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "SYSTEMROOT": str(self.root / "Windows"),
            "WINDIR": str(self.root / "Windows"),
            "TEMP": str(self.root / "temp"),
            "PATH": str(self.launcher.parent),
        }

        minimal = clean_install_smoke.minimal_environment(source)

        self.assertNotIn(str(self.launcher.parent), minimal["PATH"])
        self.assertEqual(source["HOME"], minimal["HOME"])
        self.assertEqual(source["TEMP"], minimal["TEMP"])

    def test_darwin_client_prep_uses_native_path_and_preserves_other_entry(self):
        home = self.root / "home"
        with mock.patch.object(clean_install_smoke.sys, "platform", "darwin"):
            clients, claude = clean_install_smoke.prepare_client_configs(home)

        self.assertEqual(["codex", "claude-desktop"], clients)
        self.assertEqual(
            home
            / "Library/Application Support/Claude/claude_desktop_config.json",
            claude,
        )
        record = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual({"command": "other"}, record["mcpServers"]["other"])

    def test_installed_smoke_parses_exact_command_and_arguments(self):
        command = str(self.launcher.resolve())
        arguments = ["mcp", "--root", str(self.output.resolve())]

        parsed = installed_mcp_smoke.parse_server_entry(
            command, json.dumps(arguments)
        )

        self.assertEqual((command, arguments), parsed)
        with self.assertRaisesRegex(ValueError, "JSON string array"):
            installed_mcp_smoke.parse_server_entry(command, '{"bad": true}')


if __name__ == "__main__":
    unittest.main()
