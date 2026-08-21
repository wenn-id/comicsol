import re
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


class MilestoneDeliveryRecordTests(unittest.TestCase):
    """Keep the milestone delivery record from silently omitting a delivered change.

    The record exists because a completed milestone was previously auditable only by
    reading the commit history, and five delivered issues had reached `main` with no
    entry in `CHANGELOG.md` at all. These checks are what make "nothing was missed" a
    property the suite enforces rather than a claim someone made once.

    Everything here is derived from the document itself, so the suite stays offline.
    Whether an issue is genuinely closed on GitHub is not checkable here; what is
    checkable is that the record is internally consistent, cites a pull request for
    every delivered issue, and never lists one issue twice.
    """

    MILESTONES = (
        "v2.0 — Stability",
        "v2.1 — Reliability & DX",
        "v2.2 — Comic Quality",
        "v2.3 — User Experience",
    )
    RECORD = "docs/releases/milestone-delivery.md"

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.record = (cls.root / cls.RECORD).read_text(encoding="utf-8")
        cls.changelog = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.sections = cls._sections(cls.record)

    @staticmethod
    def _sections(text):
        """Split the record into `{milestone heading: body}`."""
        sections = {}
        heading = None
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                sections[heading] = []
            elif heading is not None:
                sections[heading].append(line)
        return {name: "\n".join(body) for name, body in sections.items()}

    @classmethod
    def _issue_rows(cls, body):
        """Return `[(issue, cs_id, pr_or_None)]` for one milestone table."""
        rows = []
        for line in body.splitlines():
            issue = re.match(r"\|\s*\[#(\d+)\]\(", line)
            if issue is None:
                continue
            identifier = re.search(r"`(CS-\d+)`", line)
            pull = re.findall(r"\[#(\d+)\]\(\S+/pull/\d+\)", line)
            rows.append((
                int(issue.group(1)),
                identifier.group(1) if identifier else None,
                int(pull[0]) if pull else None,
            ))
        return rows

    def test_every_milestone_has_a_section(self):
        for milestone in self.MILESTONES:
            self.assertIn(milestone, self.sections, milestone)

    def test_summary_counts_match_the_listed_issues(self):
        summary = dict(
            (name.strip(), int(count))
            for name, count in re.findall(
                r"\|\s*(v\d+\.\d+ — [^|]+?)\s*\|\s*(\d+)\s*\|", self.record
            )
        )
        self.assertEqual(set(self.MILESTONES), set(summary))
        for milestone in self.MILESTONES:
            listed = self._issue_rows(self.sections[milestone])
            self.assertEqual(
                summary[milestone],
                len(listed),
                f"{milestone}: summary says {summary[milestone]}, "
                f"table lists {len(listed)}",
            )

    def test_no_issue_is_recorded_twice(self):
        issues = [
            issue
            for milestone in self.MILESTONES
            for issue, _, _ in self._issue_rows(self.sections[milestone])
        ]
        duplicated = sorted({n for n in issues if issues.count(n) > 1})
        self.assertEqual([], duplicated, f"issues listed more than once: {duplicated}")
        self.assertEqual(len(issues), len(set(issues)))

    def test_every_delivered_issue_cites_a_closing_pull_request(self):
        for milestone in self.MILESTONES:
            delivered = "Delivered" in self.sections[milestone] or milestone.startswith(
                ("v2.0", "v2.1", "v2.2")
            )
            for issue, identifier, pull in self._issue_rows(self.sections[milestone]):
                with self.subTest(milestone=milestone, issue=issue):
                    self.assertIsNotNone(identifier, f"#{issue} has no CS identifier")
                    if delivered:
                        self.assertIsNotNone(
                            pull, f"#{issue} is delivered but cites no pull request"
                        )

    def test_identifiers_are_unique_and_well_formed(self):
        identifiers = [
            identifier
            for milestone in self.MILESTONES
            for _, identifier, _ in self._issue_rows(self.sections[milestone])
            if identifier
        ]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_record_is_not_mistaken_for_a_release_announcement(self):
        # The milestone labels are planning names. Presenting them as tags would imply
        # releases that do not exist, and the published version is still a prerelease.
        for phrase in (
            "not** version tags",
            "The published version is `2.0.0rc4`",
            "unreleased",
            "docs/releases/v2.0-stable-criteria.md",
        ):
            self.assertIn(phrase, self.record)

    def test_changelog_points_at_the_record_and_states_the_release_status(self):
        unreleased = self.changelog.split("## 2.0.0rc4", 1)[0]
        self.assertIn(self.RECORD, unreleased)
        self.assertIn("unreleased", unreleased.lower())
        for milestone in ("v2.1", "v2.2"):
            self.assertIn(milestone, unreleased)

    def test_delivered_v2_1_and_v2_2_work_is_described_in_the_changelog(self):
        """Each delivered issue's headline artifact must appear in the CHANGELOG.

        This is the check that would have caught the original omission: `CS-015`
        through `CS-019` were delivered and closed while the CHANGELOG never
        mentioned them. Each probe is an identifier the change introduced, so it
        cannot be satisfied by unrelated prose.
        """
        unreleased = self.changelog.split("## 2.0.0rc4", 1)[0].lower()
        evidence = {
            "CS-011": "scripts/benchmark.py",
            "CS-012": "benchmark corpus",
            "CS-013": "consistency_benchmark",
            "CS-014": "benchmark_summary.py",
            "CS-015": "`blocked`, `failed`, and `complete`",
            "CS-016": "docs/onboarding.md",
            "CS-017": "scripts/build_examples.py",
            "CS-018": "agents.md",
            "CS-019": "plan/character-identity-pack.json",
            "CS-020": "scripts/reference_strategy.py",
            "CS-021": "character-consistency qa",
            "CS-022": "scripts/repair_strategy.py",
            "CS-023": "balloon-crowding",
            "CS-024": "dialogue-attribution-ambiguous",
            "CS-025": "scripts/font_coverage.py",
            "CS-026": "scripts/sfx_verification.py",
            "CS-034": "dialogue_correctness",
            "CS-035": "page_qa_migrations",
        }
        recorded = {
            identifier
            for milestone in ("v2.1 — Reliability & DX", "v2.2 — Comic Quality")
            for _, identifier, _ in self._issue_rows(self.sections[milestone])
        }
        self.assertEqual(
            recorded,
            set(evidence),
            "the delivered set and this test's evidence map disagree",
        )
        for identifier, phrase in sorted(evidence.items()):
            with self.subTest(identifier=identifier):
                self.assertIn(
                    phrase.lower(),
                    unreleased,
                    f"{identifier} is recorded as delivered but the CHANGELOG never "
                    f"mentions {phrase!r}",
                )


if __name__ == "__main__":
    unittest.main()
