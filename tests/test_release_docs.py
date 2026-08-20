import unittest
from pathlib import Path

from comic_sol_product import __version__


class ReleaseDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")
        cls.install = (cls.root / "docs/install.md").read_text(encoding="utf-8")
        cls.changelog = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.notes = (
            cls.root / f"docs/releases/v{__version__}.md"
        ).read_text(encoding="utf-8")
        cls.release_workflow = (
            cls.root / ".github/workflows/release.yml"
        ).read_text(encoding="utf-8")
        cls.stable_criteria = (
            cls.root / "docs/releases/v2.0-stable-criteria.md"
        ).read_text(encoding="utf-8")

    def test_release_workflow_resolves_tag_prefixed_notes_in_prepare(self):
        expected_notes = self.root / f"docs/releases/v{__version__}.md"
        self.assertTrue(expected_notes.is_file(), expected_notes)
        prepare_job = self.release_workflow.split("\n  native:\n", 1)[0]
        self.assertIn('NOTES="docs/releases/${RELEASE_TAG}.md"', prepare_job)
        self.assertIn('test -f "$NOTES"', prepare_job)
        self.assertNotIn(
            'NOTES="docs/releases/${RELEASE_VERSION}.md"',
            self.release_workflow,
        )

    def test_readme_links_native_install_and_release_security(self):
        self.assertIn("docs/install.md", self.readme)
        self.assertIn("docs/releases/v2.0-stable-criteria.md", self.readme)
        self.assertIn("MCP trust boundary", self.install)
        self.assertIn("MCP trust boundary", self.readme)
        self.assertIn("v2.0.0rc4", self.readme)
        self.assertIn("SHA256SUMS", self.readme)
        self.assertIn("sigstore", self.readme.lower())
        self.assertIn("docker compose", self.install)

    def test_v2_stable_criteria_is_authoritative_and_complete(self):
        criteria = self.stable_criteria
        for phrase in (
            "authoritative release gate",
            "Supported clean-install targets",
            "Required CLI and project lifecycle",
            "Interrupted generation and resume",
            "Project-data preservation and lifecycle safety",
            "Release artifacts, integrity, and provenance",
            "No open P0/P1 issue",
            "SHA256SUMS",
            "CycloneDX SBOM",
            "Build provenance attestations",
            "#109",
            "#110",
            "#111",
            "#112",
            "#113",
            "#114",
            "#115",
            "#116",
            "#117",
        ):
            self.assertIn(phrase, criteria)
        for platform in ("Linux x86_64", "macOS x86_64", "Windows x86_64", "WSL2"):
            self.assertIn(platform, criteria)
        for command in (
            "doctor",
            "init",
            "status",
            "validate",
            "resume",
            "finalize",
            "setup",
            "repair",
            "uninstall",
        ):
            self.assertIn(f"`{command}`", criteria)
        self.assertIn(
            "test -f docs/releases/v2.0-stable-criteria.md",
            self.release_workflow,
        )
        self.assertIn("authoritative release gate", self.release_workflow)

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
            "SHA256SUMS.sigstore.json",
            "-Checksums",
            "-Signature",
            "--checksums",
            "--signature",
            "CycloneDX",
            "sigstore",
        ):
            self.assertIn(phrase, self.install)
        self.assertIn("$HOME/.local/share/comic-sol", self.install)
        self.assertIn("$HOME\\AppData\\Local\\ComicSol", self.install)
        self.assertIn("projects are preserved", self.install.lower())
        self.assertNotIn("curl | sh", self.install.lower())

    def test_release_notes_and_changelog_identify_rc_limitations(self):
        for document in (self.changelog, self.notes):
            self.assertIn("2.0.0rc4", document)
            self.assertIn("sigstore", document.lower())
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

    def test_public_docs_describe_provider_and_pdf_contracts(self):
        provider_setup = (
            self.root / "references/image-provider-setup.md"
        ).read_text(encoding="utf-8")
        workflow = (
            self.root / "skills/comic-sol/references/workflow.md"
        ).read_text(encoding="utf-8")
        listing = (self.root / "submission/listing.md").read_text(encoding="utf-8")
        cases = (self.root / "submission/test-cases.md").read_text(encoding="utf-8")
        public_docs = " ".join("\n".join((self.readme, listing, cases)).split())

        self.assertIn("platform-specific", provider_setup)
        self.assertNotIn("fal_ge...mage", provider_setup)
        self.assertIn("`pdf_verification` at `exports/pdf-verification.json`", workflow)
        self.assertIn("no Comic Sol account or demo credentials", public_docs)


if __name__ == "__main__":
    unittest.main()
