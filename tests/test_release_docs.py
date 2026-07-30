import unittest
from pathlib import Path


class ReleaseDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")
        cls.install = (cls.root / "docs/install.md").read_text(encoding="utf-8")
        cls.changelog = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.notes = (cls.root / "docs/releases/v2.0.0rc3.md").read_text(encoding="utf-8")

    def test_readme_links_native_install_and_release_security(self):
        self.assertIn("docs/install.md", self.readme)
        self.assertIn("v2.0.0rc3", self.readme)
        self.assertIn("SHA256SUMS", self.readme)
        self.assertIn("unsigned", self.readme.lower())
        self.assertIn("docker compose", self.install)

    def test_install_guide_covers_every_supported_lifecycle(self):
        for phrase in (
            "installers/install.sh",
            "installers/install.ps1",
            "--sha256",
            "-SHA256",
            "--uninstall",
            "-Uninstall",
            "active-version",
            "rollback",
            "comic-sol --version",
            "comic-sol doctor",
            "SHA256SUMS",
            "CycloneDX",
            "unsigned",
        ):
            self.assertIn(phrase, self.install)
        self.assertIn("$HOME/.local/share/comic-sol", self.install)
        self.assertIn("$HOME\\AppData\\Local\\ComicSol", self.install)
        self.assertIn("projects are preserved", self.install.lower())
        self.assertNotIn("curl | sh", self.install.lower())

    def test_release_notes_and_changelog_identify_rc_limitations(self):
        for document in (self.changelog, self.notes):
            self.assertIn("2.0.0rc3", document)
            self.assertIn("unsigned", document.lower())
            self.assertIn("Linux", document)
            self.assertIn("macOS", document)
            self.assertIn("Windows", document)
            self.assertIn("SBOM", document)
        self.assertIn("prerelease", self.notes.lower())
        self.assertIn("17", self.notes)
        for phrase in (
            "normalization", "typography", "four-grid", "page QA",
            "full-content PDF", "quality matrix", "mechanics only",
        ):
            self.assertIn(phrase.lower(), self.notes.lower())
        self.assertIn("2.0.0rc1", self.changelog)


if __name__ == "__main__":
    unittest.main()
