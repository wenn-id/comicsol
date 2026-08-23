import json
import os
import subprocess
import sys
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
    validate_wheel_metadata,
    wheel_metadata_member,
)
from scripts import clean_install_smoke


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
        self.assertIn(
            "comic_sol_product/assets/fonts/ComicNeue-Regular.ttf", REQUIRED_WHEEL_MEMBERS
        )
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


class WheelMetadataContractTests(unittest.TestCase):
    """The wheel METADATA must keep its discovery fields (issue #213).

    Project URLs, classifiers, keywords, and maintainer entries are what PyPI
    and package tooling read; a wheel that loses them still installs and still
    passes the member check above, so only this contract makes the loss
    visible before a release.
    """

    COMPLETE_METADATA = "\n".join(
        [
            "Metadata-Version: 2.4",
            "Name: comic-sol",
            "Version: 2.0.0rc6",
            "Summary: Local-first deterministic comic production pipeline",
            "Author: Alwan Juliawan",
            "Maintainer: Alwan Juliawan",
            "License-Expression: MIT",
            "Keywords: comic,manga,pdf-export",
            "Classifier: Development Status :: 4 - Beta",
            "Classifier: Intended Audience :: End Users/Desktop",
            "Classifier: Operating System :: MacOS",
            "Classifier: Programming Language :: Python :: 3.11",
            "Classifier: Topic :: Artistic Software",
            "Project-URL: Homepage, https://github.com/wenn-id/comicsol",
            "Project-URL: Repository, https://github.com/wenn-id/comicsol",
            "Project-URL: Documentation, https://github.com/wenn-id/comicsol/tree/main/docs",
            "Project-URL: Changelog, https://github.com/wenn-id/comicsol/blob/main/CHANGELOG.md",
            "Project-URL: Issue Tracker, https://github.com/wenn-id/comicsol/issues",
            "Project-URL: Security Policy, https://github.com/wenn-id/comicsol/blob/main/SECURITY.md",
            "Requires-Python: >=3.11",
            "Requires-Dist: Pillow==12.3.0",
            "",
        ]
    )

    def test_complete_metadata_passes(self):
        validate_wheel_metadata(self.COMPLETE_METADATA)

    def test_missing_project_urls_are_named(self):
        broken = self.COMPLETE_METADATA.replace(
            "Project-URL: Homepage, https://github.com/wenn-id/comicsol\n", ""
        ).replace("Project-URL: Security Policy, https://github.com/wenn-id/comicsol/blob/main/SECURITY.md\n", "")
        with self.assertRaisesRegex(ValueError, "Homepage.*Security Policy"):
            validate_wheel_metadata(broken)

    def test_missing_keywords_classifiers_and_maintainer_fail(self):
        with self.assertRaisesRegex(ValueError, "missing Keywords"):
            validate_wheel_metadata(
                self.COMPLETE_METADATA.replace("Keywords: comic,manga,pdf-export\n", "")
            )
        no_contact = self.COMPLETE_METADATA.replace("Author: Alwan Juliawan\n", "").replace(
            "Maintainer: Alwan Juliawan\n", ""
        )
        with self.assertRaisesRegex(ValueError, "neither Author nor Maintainer"):
            validate_wheel_metadata(no_contact)
        no_classifiers = "\n".join(
            line
            for line in self.COMPLETE_METADATA.splitlines()
            if not line.startswith("Classifier: ")
        )
        with self.assertRaisesRegex(ValueError, "missing Classifiers"):
            validate_wheel_metadata(no_classifiers)
        missing_prefix = "\n".join(
            line
            for line in self.COMPLETE_METADATA.splitlines()
            if not line.startswith("Classifier: Operating System")
        )
        with self.assertRaisesRegex(ValueError, "no Operating System classifier"):
            validate_wheel_metadata(missing_prefix)

    def test_deprecated_license_classifier_is_rejected(self):
        legacy = self.COMPLETE_METADATA.replace(
            "Classifier: Topic :: Artistic Software",
            "Classifier: License :: OSI Approved :: MIT License",
        )
        with self.assertRaisesRegex(ValueError, "deprecated License :: classifier"):
            validate_wheel_metadata(legacy)

    def test_requires_python_floor_is_enforced(self):
        for relaxed in ("", "Requires-Python: >=3.9\n"):
            broken = self.COMPLETE_METADATA.replace("Requires-Python: >=3.11\n", relaxed)
            with self.assertRaisesRegex(ValueError, "Requires-Python"):
                validate_wheel_metadata(broken)

    def test_wheel_must_carry_exactly_one_metadata_member(self):
        self.assertEqual(
            "comic_sol_product-2.0.0rc6.dist-info/METADATA",
            wheel_metadata_member(
                REQUIRED_WHEEL_MEMBERS | {"comic_sol_product-2.0.0rc6.dist-info/METADATA"}
            ),
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            wheel_metadata_member(set(REQUIRED_WHEEL_MEMBERS))

    def test_pyproject_declares_the_required_metadata(self):
        import tomllib

        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        project = pyproject["project"]
        self.assertEqual(["version"], project["dynamic"])
        self.assertTrue(project["keywords"], "keywords must not be empty")
        self.assertTrue(project["classifiers"], "classifiers must not be empty")
        self.assertTrue(project["maintainers"], "maintainers must not be empty")
        urls = set(project["urls"])
        for label in (
            "Homepage",
            "Repository",
            "Documentation",
            "Changelog",
            "Issue Tracker",
            "Security Policy",
        ):
            self.assertIn(label, urls)
        for classifier in project["classifiers"]:
            self.assertFalse(
                classifier.startswith("License ::"),
                "license is declared as SPDX; License :: classifiers are deprecated",
            )
        self.assertEqual("MIT", project["license"])
        self.assertEqual(">=3.11", project["requires-python"])


class CleanInstallSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "projects"
        self.launcher = (
            self.root / "bin with spaces" / ("comic-sol.exe" if os.name == "nt" else "comic-sol")
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

        entry = clean_install_smoke.read_codex_entry(self.config, self.output, self.launcher)

        self.assertEqual(
            {"command": str(self.launcher.resolve()), "args": expected_arguments},
            entry,
        )

    def test_rejects_relative_persisted_command(self):
        self.write_codex_entry("comic-sol", ["mcp", "--root", str(self.output.resolve())])

        with self.assertRaisesRegex(RuntimeError, "non-absolute"):
            clean_install_smoke.read_codex_entry(self.config, self.output, self.launcher)

    def test_rejects_absolute_command_other_than_installed_launcher(self):
        other = self.root / "other" / self.launcher.name
        other.parent.mkdir()
        other.write_bytes(b"unrelated launcher")
        self.write_codex_entry(
            str(other.resolve()),
            ["mcp", "--root", str(self.output.resolve())],
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected MCP command"):
            clean_install_smoke.read_codex_entry(self.config, self.output, self.launcher)

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
            home / "Library/Application Support/Claude/claude_desktop_config.json",
            claude,
        )
        record = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual({"command": "other"}, record["mcpServers"]["other"])

    def test_installed_smoke_parses_exact_command_and_arguments(self):
        from scripts import installed_mcp_smoke

        command = str(self.launcher.resolve())
        arguments = ["mcp", "--root", str(self.output.resolve())]

        parsed = installed_mcp_smoke.parse_server_entry(command, json.dumps(arguments))

        self.assertEqual((command, arguments), parsed)
        with self.assertRaisesRegex(ValueError, "JSON string array"):
            installed_mcp_smoke.parse_server_entry(command, '{"bad": true}')

    def test_argument_parser_imports_without_mcp_extra(self):
        root = Path(__file__).resolve().parents[1]
        program = (
            "import importlib.abc\n"
            "import sys\n"
            "class BlockMcp(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname == 'mcp' or fullname.startswith('mcp.'):\n"
            "            raise ModuleNotFoundError('mcp extra is unavailable')\n"
            "        return None\n"
            "sys.meta_path.insert(0, BlockMcp())\n"
            "from scripts.installed_mcp_smoke import parse_server_entry\n"
            "assert parse_server_entry('comic-sol', '[]') == ('comic-sol', [])\n"
        )

        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
