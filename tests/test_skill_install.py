import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comic_sol_product import __version__
from comic_sol_product import skill_install
from comic_sol_product.errors import CliUsageError


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.project = self.root / "project"
        self.project.mkdir()
        self.bundle = self.root / "bundle"
        self._write_bundle(self.bundle, suffix="v1")

    @staticmethod
    def _write_bundle(root: Path, *, suffix: str) -> None:
        files = {
            "SKILL.md": f"# Comic Sol {suffix}\n",
            "references/workflow.md": f"workflow-{suffix}\n",
            "assets/fonts/font.txt": f"font-{suffix}\n",
            "scripts/tool.py": f"VALUE = {suffix!r}\n",
            "templates/manifest.json": json.dumps({"bundle": suffix}) + "\n",
        }
        for relative, content in files.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

    def install(self, target: str, scope: str, **overrides):
        arguments = {
            "target": target,
            "scope": scope,
            "project_root": self.project if scope == "project" else None,
            "home": self.home,
            "codex_home": self.codex_home,
            "bundle_root": self.bundle,
            "version": __version__,
        }
        arguments.update(overrides)
        return skill_install.install_skill(**arguments)

    def uninstall(self, target: str, scope: str, **overrides):
        arguments = {
            "target": target,
            "scope": scope,
            "project_root": self.project if scope == "project" else None,
            "home": self.home,
            "codex_home": self.codex_home,
        }
        arguments.update(overrides)
        return skill_install.uninstall_skill(**arguments)

    def test_supported_target_scope_destinations_are_exact(self):
        cases = (
            ("codex", "user", self.codex_home / "skills/comic-sol"),
            ("claude", "user", self.home / ".claude/skills/comic-sol"),
            ("claude", "project", self.project / ".claude/skills/comic-sol"),
            ("antigravity", "project", self.project / ".agents/skills/comic-sol"),
            ("zcode", "user", self.home / ".zcode/skills/comic-sol"),
        )
        for target, scope, expected in cases:
            with self.subTest(target=target, scope=scope):
                result = self.install(target, scope)
                self.assertEqual(expected, Path(result.destination))
                self.assertEqual(target, result.target)
                self.assertEqual(scope, result.scope)
                self.assertEqual("installed", result.status)
                self.assertTrue((expected / "SKILL.md").is_file())

    def test_bytecode_caches_never_affect_the_digest_or_the_payload(self):
        """Byte-compilation is environment-dependent, so it must never be managed.

        `pip` byte-compiles an installed wheel while other installers do not, so
        counting `__pycache__` would make the canonical digest differ between
        environments and break both the marker contract and no-op reinstall.
        """
        cache = self.bundle / "scripts/__pycache__"
        cache.mkdir(parents=True)
        (cache / "tool.cpython-311.pyc").write_bytes(b"\x00compiled")
        (self.bundle / "scripts/tool.pyo").write_bytes(b"\x00optimized")

        result = self.install("claude", "user")
        destination = Path(result.destination)

        self.assertEqual(skill_install.bundle_digest(self.bundle), result.bundle_digest)
        self.assertFalse((destination / "scripts/__pycache__").exists())
        self.assertFalse((destination / "scripts/tool.pyo").exists())
        marker = json.loads((destination / skill_install.MARKER_NAME).read_text())
        self.assertTrue(
            all(
                "__pycache__" not in path and not path.endswith((".pyc", ".pyo"))
                for path in marker["managed_paths"]
            )
        )

    def test_digest_is_identical_with_and_without_bytecode_caches(self):
        clean = skill_install.bundle_digest(self.bundle)
        cache = self.bundle / "__pycache__"
        cache.mkdir()
        (cache / "stale.cpython-311.pyc").write_bytes(b"\x00compiled")
        self.assertEqual(clean, skill_install.bundle_digest(self.bundle))

    def test_upgrade_succeeds_after_the_host_byte_compiles_the_install(self):
        first = self.install("claude", "user")
        destination = Path(first.destination)
        compiled = destination / "scripts/__pycache__"
        compiled.mkdir(parents=True)
        (compiled / "tool.cpython-311.pyc").write_bytes(b"\x00compiled")

        upgraded = self.root / "bundle-v2"
        self._write_bundle(upgraded, suffix="v2")
        second = self.install("claude", "user", bundle_root=upgraded)

        self.assertEqual("upgraded", second.status)
        self.assertEqual("# Comic Sol v2\n", (destination / "SKILL.md").read_text())

    def test_uninstall_preserves_host_generated_bytecode_caches(self):
        installed = self.install("claude", "user")
        destination = Path(installed.destination)
        compiled = destination / "scripts/__pycache__"
        compiled.mkdir(parents=True)
        (compiled / "tool.cpython-311.pyc").write_bytes(b"\x00compiled")

        result = self.uninstall("claude", "user")

        self.assertEqual("preserved", result.status)
        self.assertTrue((compiled / "tool.cpython-311.pyc").is_file())
        self.assertFalse((destination / "SKILL.md").exists())

    def test_installed_payload_is_byte_equal_to_complete_canonical_bundle(self):
        canonical = Path(__file__).resolve().parents[1] / "skills/comic-sol"
        result = self.install("claude", "user", bundle_root=canonical)
        destination = Path(result.destination)
        expected = {
            path.relative_to(canonical).as_posix(): path.read_bytes()
            for path in canonical.rglob("*")
            if path.is_file()
        }
        actual = {
            path.relative_to(destination).as_posix(): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file() and path.name != skill_install.MARKER_NAME
        }
        self.assertEqual(expected, actual)
        self.assertEqual(skill_install.bundle_digest(canonical), result.bundle_digest)

    def test_marker_contains_only_bounded_contract_fields(self):
        result = self.install("codex", "user")
        destination = Path(result.destination)
        marker_bytes = (destination / skill_install.MARKER_NAME).read_bytes()
        marker = json.loads(marker_bytes)
        self.assertLessEqual(len(marker_bytes), skill_install.MAX_MARKER_BYTES)
        self.assertEqual(
            {"target", "scope", "version", "bundle_digest", "managed_paths"},
            set(marker),
        )
        self.assertEqual("codex", marker["target"])
        self.assertEqual("user", marker["scope"])
        self.assertEqual(__version__, marker["version"])
        self.assertEqual(result.bundle_digest, marker["bundle_digest"])
        self.assertEqual(sorted(marker["managed_paths"]), list(marker["managed_paths"]))
        self.assertTrue(all(not Path(path).is_absolute() for path in marker["managed_paths"]))
        serialized = marker_bytes.decode("utf-8")
        self.assertNotIn(str(self.home), serialized)
        for forbidden in ("provider", "credential", "endpoint", "account"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_same_digest_repeat_install_is_noop(self):
        first = self.install("claude", "user")
        skill_file = Path(first.destination) / "SKILL.md"
        identity = skill_file.stat().st_ino
        second = self.install("claude", "user")
        self.assertEqual("unchanged", second.status)
        self.assertEqual(first.bundle_digest, second.bundle_digest)
        self.assertEqual(identity, skill_file.stat().st_ino)
        self.assertEqual([], list(skill_file.parent.parent.glob(".comic-sol.stage-*")))

    def test_upgrade_atomically_replaces_verified_managed_directory(self):
        first = self.install("claude", "project")
        destination = Path(first.destination)
        old_digest = first.bundle_digest
        upgraded = self.root / "bundle-v2"
        self._write_bundle(upgraded, suffix="v2")
        second = self.install("claude", "project", bundle_root=upgraded, version="2.0.1")
        self.assertEqual("upgraded", second.status)
        self.assertNotEqual(old_digest, second.bundle_digest)
        self.assertEqual("# Comic Sol v2\n", (destination / "SKILL.md").read_text())
        self.assertEqual(
            "2.0.1", json.loads((destination / skill_install.MARKER_NAME).read_text())["version"]
        )
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_uninstall_removes_only_verified_install(self):
        installed = self.install("antigravity", "project")
        result = self.uninstall("antigravity", "project")
        self.assertEqual("uninstalled", result.status)
        self.assertFalse(Path(installed.destination).exists())
        repeat = self.uninstall("antigravity", "project")
        self.assertEqual("not-installed", repeat.status)

    def test_uninstall_preserves_unknown_and_modified_files(self):
        installed = self.install("zcode", "user")
        destination = Path(installed.destination)
        modified = destination / "SKILL.md"
        modified.write_text("user-modified\n", encoding="utf-8")
        unknown = destination / "notes/private.txt"
        unknown.parent.mkdir()
        unknown.write_text("preserve me\n", encoding="utf-8")
        result = self.uninstall("zcode", "user")
        self.assertEqual("preserved", result.status)
        self.assertEqual("user-modified\n", modified.read_text())
        self.assertEqual("preserve me\n", unknown.read_text())
        self.assertFalse((destination / "references/workflow.md").exists())
        self.assertFalse((destination / skill_install.MARKER_NAME).exists())

    def test_install_and_uninstall_preserve_unrelated_skill_config_and_project(self):
        unrelated_skill = self.project / ".claude/skills/other/SKILL.md"
        unrelated_config = self.project / ".claude/settings.json"
        project_file = self.project / "project.json"
        unrelated_skill.parent.mkdir(parents=True)
        unrelated_skill.write_bytes(b"other skill")
        unrelated_config.write_bytes(b'{"provider":"unchanged"}')
        project_file.write_bytes(b'{"comic":"user work"}')
        snapshots = {
            path: path.read_bytes() for path in (unrelated_skill, unrelated_config, project_file)
        }
        self.install("claude", "project")
        self.uninstall("claude", "project")
        self.assertEqual(snapshots, {path: path.read_bytes() for path in snapshots})

    def test_auto_selects_exactly_one_supported_user_host(self):
        (self.home / ".claude").mkdir()
        result = self.install("auto", "user")
        self.assertEqual("claude", result.target)
        self.assertEqual(self.home / ".claude/skills/comic-sol", Path(result.destination))

    def test_auto_zero_matches_reports_candidates_without_writing(self):
        with self.assertRaises(skill_install.AutoDetectionError) as context:
            self.install("auto", "user")
        self.assertEqual(("claude", "codex", "zcode"), context.exception.candidates)
        self.assertEqual((), context.exception.matches)
        self.assertFalse(any(self.home.rglob("comic-sol")))

    def test_auto_multiple_matches_reports_choices_without_writing(self):
        self.codex_home.mkdir()
        (self.home / ".claude").mkdir()
        with self.assertRaises(skill_install.AutoDetectionError) as context:
            self.install("auto", "user")
        self.assertEqual(("claude", "codex"), context.exception.matches)
        self.assertFalse((self.codex_home / "skills/comic-sol").exists())
        self.assertFalse((self.home / ".claude/skills/comic-sol").exists())

    def test_auto_does_not_infer_image_generation_capability(self):
        capability = self.home / "image-generation-capable-agent"
        capability.mkdir()
        (capability / "provider.json").write_text('{"images":true}')
        with self.assertRaises(skill_install.AutoDetectionError) as context:
            self.install("auto", "user")
        self.assertEqual((), context.exception.matches)
        self.assertFalse(any(capability.rglob("comic-sol")))

    def test_every_unsupported_target_scope_pair_raises_typed_actionable_error(self):
        for target, scope in (("codex", "project"), ("antigravity", "user"), ("zcode", "project")):
            with self.subTest(target=target, scope=scope):
                with self.assertRaises(skill_install.UnsupportedSkillPlacementError) as context:
                    self.install(target, scope)
                self.assertIsInstance(context.exception, CliUsageError)
                self.assertIn(target, str(context.exception))
                self.assertIn(scope, str(context.exception))
                self.assertIn("Supported", str(context.exception))

    def test_project_scope_requires_project_root_and_user_scope_rejects_it(self):
        with self.assertRaises(skill_install.UnsafeSkillPathError):
            skill_install.install_skill(
                target="claude",
                scope="project",
                bundle_root=self.bundle,
                home=self.home,
                codex_home=self.codex_home,
            )
        with self.assertRaises(skill_install.UnsafeSkillPathError):
            self.install("claude", "user", project_root=self.project)

    def test_traversal_and_alias_paths_are_rejected_before_writing(self):
        for unsafe in (
            self.root / "parent/../escaped",
            self.home / ".codex/../.claude",
        ):
            with self.subTest(path=unsafe):
                with self.assertRaises(skill_install.UnsafeSkillPathError):
                    self.install(
                        "claude" if "parent" in str(unsafe) else "codex",
                        "project" if "parent" in str(unsafe) else "user",
                        project_root=unsafe if "parent" in str(unsafe) else None,
                        codex_home=unsafe if "parent" not in str(unsafe) else self.codex_home,
                    )
        self.assertFalse((self.root / "escaped/.claude/skills/comic-sol").exists())

    def test_control_characters_are_rejected_before_writing(self):
        for character in ("\n", "\r", "\x00", "\x1b"):
            with self.subTest(character=repr(character)):
                unsafe = self.root / f"project{character}alias"
                with self.assertRaises(skill_install.UnsafeSkillPathError):
                    self.install("claude", "project", project_root=unsafe)

    def test_symlink_ancestor_is_rejected_without_following(self):
        real = self.root / "real-home"
        real.mkdir()
        linked = self.root / "linked-home"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError as error:
            if os.name == "nt":
                self.skipTest(f"unprivileged Windows cannot create directory symlinks: {error}")
            self.fail(f"directory symlink creation unexpectedly failed: {error}")
        with self.assertRaises(skill_install.UnsafeSkillPathError):
            self.install("claude", "user", home=linked)
        self.assertFalse((real / ".claude/skills/comic-sol").exists())

    def test_reparse_ancestor_is_rejected(self):
        original = skill_install._is_reparse_point

        def fake_reparse(metadata):
            return True if metadata.st_ino == self.home.stat().st_ino else original(metadata)

        with mock.patch.object(skill_install, "_is_reparse_point", side_effect=fake_reparse):
            with self.assertRaises(skill_install.UnsafeSkillPathError):
                self.install("claude", "user")

    def test_partial_copy_failure_leaves_no_destination_or_staging(self):
        destination = self.home / ".claude/skills/comic-sol"
        original = skill_install._copy_file
        calls = 0

        def fail_after_one(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated partial copy")
            return original(source, target)

        with mock.patch.object(skill_install, "_copy_file", side_effect=fail_after_one):
            with self.assertRaises(OSError):
                self.install("claude", "user")
        self.assertFalse(destination.exists())
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_verification_failure_leaves_no_destination_or_staging(self):
        destination = self.home / ".claude/skills/comic-sol"
        with mock.patch.object(skill_install, "_verify_payload", return_value=False):
            with self.assertRaises(skill_install.SkillVerificationError):
                self.install("claude", "user")
        self.assertFalse(destination.exists())
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_new_publish_failure_leaves_no_destination_or_staging(self):
        destination = self.home / ".claude/skills/comic-sol"
        with mock.patch.object(
            skill_install, "_publish_new", side_effect=OSError("simulated publish failure")
        ):
            with self.assertRaises(OSError):
                self.install("claude", "user")
        self.assertFalse(destination.exists())
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_exchange_failure_preserves_previous_install(self):
        installed = self.install("claude", "user")
        destination = Path(installed.destination)
        original = (destination / "SKILL.md").read_bytes()
        upgraded = self.root / "bundle-v2"
        self._write_bundle(upgraded, suffix="v2")
        with mock.patch.object(
            skill_install, "_exchange_paths", side_effect=OSError("simulated exchange failure")
        ):
            with self.assertRaises(OSError):
                self.install("claude", "user", bundle_root=upgraded)
        self.assertEqual(original, (destination / "SKILL.md").read_bytes())
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_failed_post_publish_verification_rolls_back_upgrade(self):
        installed = self.install("claude", "user")
        destination = Path(installed.destination)
        original = {
            path.relative_to(destination).as_posix(): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }
        upgraded = self.root / "bundle-v2"
        self._write_bundle(upgraded, suffix="v2")
        with mock.patch.object(skill_install, "_verify_payload", side_effect=(True, False)):
            with self.assertRaises(skill_install.SkillVerificationError):
                self.install("claude", "user", bundle_root=upgraded)
        restored = {
            path.relative_to(destination).as_posix(): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(original, restored)
        self.assertEqual([], list(destination.parent.glob(".comic-sol.stage-*")))

    def test_uninstall_requires_valid_matching_marker_before_changes(self):
        destination = self.home / ".claude/skills/comic-sol"
        destination.mkdir(parents=True)
        protected = destination / "SKILL.md"
        protected.write_text("not managed\n")
        for marker in (None, {"target": "claude"}, {"target": "zcode"}):
            with self.subTest(marker=marker):
                marker_path = destination / skill_install.MARKER_NAME
                marker_path.unlink(missing_ok=True)
                if marker is not None:
                    marker_path.write_text(json.dumps(marker))
                with self.assertRaises(skill_install.InvalidSkillMarkerError):
                    self.uninstall("claude", "user")
                self.assertEqual("not managed\n", protected.read_text())

    def test_uninstall_rejects_symlinked_managed_member_before_changes(self):
        installed = self.install("claude", "user")
        destination = Path(installed.destination)
        linked = destination / "references/workflow.md"
        linked.unlink()
        outside = self.root / "outside"
        outside.write_text("outside\n")
        try:
            linked.symlink_to(outside)
        except OSError as error:
            if os.name == "nt":
                self.skipTest(f"unprivileged Windows cannot create file symlinks: {error}")
            self.fail(f"file symlink creation unexpectedly failed: {error}")
        with self.assertRaises(skill_install.UnsafeSkillPathError):
            self.uninstall("claude", "user")
        self.assertEqual("outside\n", outside.read_text())
        self.assertTrue((destination / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
