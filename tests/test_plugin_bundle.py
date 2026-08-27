import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comic_sol_product import __version__


ROOT = Path(__file__).resolve().parents[1]

from scripts import sync_plugin_bundle  # noqa: E402


class PluginBundleTests(unittest.TestCase):
    def test_plugin_version_matches_package_version(self):
        metadata = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(__version__, metadata["version"])

    def test_generated_plugin_bundle_matches_canonical_root(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/sync_plugin_bundle.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_every_managed_bundle_file_is_a_byte_equal_canonical_copy(self):
        synchronized = set(sync_plugin_bundle.synchronized_paths())

        self.assertFalse(hasattr(sync_plugin_bundle, "HOST_SPECIFIC_REFERENCES"))
        self.assertEqual(synchronized, sync_plugin_bundle.expected_bundle_paths())
        for relative in sorted(synchronized, key=Path.as_posix):
            canonical = ROOT / relative
            bundled = sync_plugin_bundle.BUNDLE / relative
            self.assertTrue(canonical.is_file(), relative.as_posix())
            self.assertTrue(bundled.is_file(), relative.as_posix())
            self.assertEqual(
                canonical.read_bytes(),
                bundled.read_bytes(),
                relative.as_posix(),
            )

    def test_normative_root_and_bundle_workflow_does_not_require_codex(self):
        normative_paths = (
            Path("SKILL.md"),
            Path("references/capability-detection.md"),
            Path("references/image-provider-setup.md"),
            Path("references/workflow.md"),
        )
        banned_requirements = (
            "use when codex",
            "current codex session",
            "codex must",
            "requires codex",
            "pure codex skill run",
        )

        for relative in normative_paths:
            for root in (ROOT, sync_plugin_bundle.BUNDLE):
                text = (root / relative).read_text(encoding="utf-8").lower()
                for phrase in banned_requirements:
                    self.assertNotIn(phrase, text, f"{root / relative}: {phrase}")

    def test_capability_availability_and_features_are_never_inferred_from_names(self):
        contract = (
            "Never infer capability availability or features from provider, model, or tool names."
        )
        normative_paths = (
            Path("SKILL.md"),
            Path("references/capability-detection.md"),
            Path("references/image-provider-setup.md"),
            Path("references/workflow.md"),
        )

        for relative in normative_paths:
            for root in (ROOT, sync_plugin_bundle.BUNDLE):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn(contract, " ".join(text.split()), str(root / relative))

    def test_executor_priority_is_identical_in_canonical_and_bundled_workflow(self):
        priority = (
            "compatible declared native image tool",
            "compatible declared external adapter",
            "portable handoff",
            "actionable `blocked` state",
        )

        for relative in (Path("SKILL.md"), Path("references/workflow.md")):
            for root in (ROOT, sync_plugin_bundle.BUNDLE):
                text = " ".join((root / relative).read_text(encoding="utf-8").split()).lower()
                positions = [text.find(phrase) for phrase in priority]
                self.assertTrue(all(position >= 0 for position in positions), root / relative)
                self.assertEqual(sorted(positions), positions, root / relative)

    def test_handoff_contract_module_is_a_managed_bundle_path(self):
        self.assertIn(Path("scripts/handoff.py"), sync_plugin_bundle.synchronized_paths())

    def test_starter_assets_are_managed_bundle_paths(self):
        synchronized = set(sync_plugin_bundle.synchronized_paths())
        for starter in sync_plugin_bundle.STARTER_IDS:
            for relative in sync_plugin_bundle.STARTER_FILES:
                self.assertIn(
                    Path("templates/starters/v1") / starter / relative,
                    synchronized,
                )
        self.assertIn(Path("references/starter-templates.md"), synchronized)
        self.assertIn(Path("scripts/starter_templates.py"), synchronized)

    def test_check_detects_a_deleted_bundled_script(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "skills/comic-sol"
            source = root / "scripts/comic_sol.py"
            source.parent.mkdir(parents=True)
            source.write_text("canonical\n", encoding="utf-8")

            with (
                patch.object(sync_plugin_bundle, "ROOT", root),
                patch.object(sync_plugin_bundle, "BUNDLE", bundle),
            ):
                self.assertIn(Path("scripts/comic_sol.py"), sync_plugin_bundle.check())

    def test_generated_bytecode_is_not_treated_as_bundle_drift(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / "skills/comic-sol"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text("skill\n", encoding="utf-8")
            cache = bundle / "scripts/__pycache__/comic_sol.cpython-311.pyc"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"generated bytecode")

            with patch.object(sync_plugin_bundle, "BUNDLE", bundle):
                self.assertEqual({Path("SKILL.md")}, sync_plugin_bundle.actual_bundle_paths())

    def test_check_detects_deleted_canonical_and_orphaned_bundle_references(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = root / "skills/comic-sol"
            references = bundle / "references"
            references.mkdir(parents=True)
            (references / "workflow.md").write_text("stale\n", encoding="utf-8")
            (references / "orphan.md").write_text("orphan\n", encoding="utf-8")

            with (
                patch.object(sync_plugin_bundle, "ROOT", root),
                patch.object(sync_plugin_bundle, "BUNDLE", bundle),
            ):
                drift = sync_plugin_bundle.check()

            self.assertIn(Path("references/workflow.md"), drift)
            self.assertIn(Path("references/orphan.md"), drift)


if __name__ == "__main__":
    unittest.main()
