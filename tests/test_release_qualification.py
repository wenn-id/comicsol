import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-qualification.yml"
SCRIPT = ROOT / "scripts" / "release_qualification.py"
DOCS = ROOT / "docs" / "install.md"


class ReleaseQualificationContractTests(unittest.TestCase):
    def test_release_qualification_script_exists_with_required_interfaces(self):
        self.assertTrue(SCRIPT.is_file())
        source = SCRIPT.read_text(encoding="utf-8")
        for token in (
            "--archive",
            "--platform",
            "--sha256",
            "--installer",
            "--summary",
            "--version",
            "doctor",
            "init",
            "validate",
            "uninstall",
            "env=env",
            "HOME",
        ):
            self.assertIn(token, source)

    def test_release_qualification_workflow_uses_release_asset_not_checkout_build(self):
        self.assertTrue(WORKFLOW.is_file())
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for token in (
            "workflow_dispatch",
            "inputs:",
            "tag:",
            "gh release download",
            "linux",
            "macos",
            "windows",
            "wsl",
            "release_qualification.py",
            "qualification-summary",
            "if-no-files-found: error",
        ):
            self.assertIn(token, workflow)
        self.assertNotIn("python -m build", workflow)
        self.assertNotIn("actions/checkout", workflow)

    def test_workflow_records_platform_specific_exceptions_in_summary(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Platform-specific exceptions", workflow)
        self.assertIn("WSL2", workflow)
        self.assertIn("not available", workflow)
        self.assertIn("upload-artifact", workflow)
        self.assertIn("$qualificationRoot", workflow)
        self.assertIn("qualification/summary-wsl.json", workflow)

    def test_install_docs_describe_release_qualification_and_wsl(self):
        docs = DOCS.read_text(encoding="utf-8")
        self.assertIn("release qualification", docs.lower())
        self.assertIn("WSL2", docs)
        self.assertIn("intended release artifact", docs)
        self.assertIn("comic-sol --version", docs)
        self.assertIn("comic-sol doctor", docs)
        self.assertIn("user projects", docs.lower())


if __name__ == "__main__":
    unittest.main()
