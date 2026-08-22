import contextlib
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
        cls.notes = (cls.root / f"docs/releases/v{__version__}.md").read_text(encoding="utf-8")
        cls.release_workflow = (cls.root / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        cls.stable_criteria = (cls.root / "docs/releases/v2.0-stable-criteria.md").read_text(
            encoding="utf-8"
        )

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
        # Pinned to the current version rather than a literal, so a release bump
        # cannot leave the README advertising the previous distribution.
        self.assertIn(f"v{__version__}", self.readme)
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
            "normalization",
            "typography",
            "four-grid",
            "page QA",
            "full-content PDF",
            "quality matrix",
            "mechanics only",
        ):
            self.assertIn(phrase.lower(), self.notes.lower())
        self.assertIn("2.0.0rc1", self.changelog)

    def test_public_docs_describe_provider_and_pdf_contracts(self):
        provider_setup = (self.root / "references/image-provider-setup.md").read_text(
            encoding="utf-8"
        )
        workflow = (self.root / "skills/comic-sol/references/workflow.md").read_text(
            encoding="utf-8"
        )
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
    # Delivered milestones owe a CHANGELOG entry per issue. Naming them once here is
    # what keeps the evidence check from quietly covering a subset.
    DELIVERED = MILESTONES[:3]
    RECORD = "docs/releases/milestone-delivery.md"

    # A released heading that must remain below the Unreleased section. It is a
    # prefix rather than a whole line because the real heading carries a date
    # (`## 2.0.0rc4 — 2026-07-30`), and it is only an existence check: the slice
    # itself ends at the next `## ` heading, whatever it is called.
    RELEASE_BOUNDARY = "## 2.0.0rc4"

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.record = (cls.root / cls.RECORD).read_text(encoding="utf-8")
        cls.changelog = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")
        cls.sections = cls._sections(cls.record)

    def _changelog_sections(self):
        """Return `{heading: body}` for every `## ` section of the changelog."""
        headings = list(re.finditer(r"(?m)^##[ \t]+(?P<name>\S.*?)[ \t]*$", self.changelog))
        self.assertTrue(headings, "the changelog has no section headings")
        sections = {}
        for index, match in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(self.changelog)
            sections[match.group("name")] = self.changelog[match.end() : end]
        return sections

    def _carrying_section(self, released):
        """Return the changelog heading that must describe a milestone's work.

        Delivered work moves out of `Unreleased` and into a version section the
        moment a tag is prepared, so the evidence check follows it rather than
        assuming one location. The record's own `Released` cell decides where to
        look, which keeps the two documents from drifting apart: a milestone marked
        released in a tag whose section does not exist fails here.
        """
        sections = self._changelog_sections()
        if released.startswith("No"):
            self.assertIn("Unreleased", sections)
            return sections["Unreleased"]
        # `Pending — <tag>` names a section prepared for a tag that is not published
        # yet. The evidence lives there, so it resolves like a release; what must not
        # happen is the record calling that tag published, which is checked below.
        cited = re.findall(r"\d+\.\d+\.\d+rc\d+", released)
        self.assertTrue(cited, f"{released!r} names no tag")
        matches = [
            body
            for heading, body in sections.items()
            if any(heading.startswith(tag) for tag in cited)
        ]
        self.assertTrue(
            matches,
            f"no changelog section for {cited}; a milestone cannot be released by a "
            "tag the changelog never describes",
        )
        return "\n".join(matches)

    def _released_by_milestone(self):
        """Return `{milestone: released cell}` from the status summary."""
        return {
            name.strip(): released.strip()
            for name, _, released in re.findall(
                r"\|\s*(v\d+\.\d+ — [^|]+?)\s*\|\s*(\d+)\s*\|\s*[^|]+?\s*\|\s*([^|]+?)\s*\|",
                self.record,
            )
        }

    def _unreleased_changelog(self):
        """Return only the Unreleased section, refusing to guess its extent.

        Every assertion about unreleased work is scoped by this slice, so a loose
        slice fails open in exactly the direction this suite exists to prevent.
        Three ways it could leak, all closed here:

        - A substring test for `## Unreleased` also matches a demoted `###
          Unreleased`, so the heading is matched as a whole line.
        - Splitting on the first released heading returns *everything* above it. The
          moment the next tag is cut, a `## 2.1.0` section lands between Unreleased
          and that heading, and its released prose would satisfy assertions about
          unreleased work — silently, and precisely when this suite matters most.
          The slice therefore ends at the next `## ` heading of any kind rather than
          at one named release.
        - Deleting the released history entirely would leave a single section that
          looks self-consistent, so `RELEASE_BOUNDARY` is still required to appear
          *below* the slice.
        """
        headings = list(re.finditer(r"(?m)^##[ \t]+(?P<name>\S.*?)[ \t]*$", self.changelog))
        names = [match.group("name") for match in headings]
        self.assertIn(
            "Unreleased",
            names,
            f"no `## Unreleased` section heading; found {names[:5]}",
        )
        index = names.index("Unreleased")
        start = headings[index].end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(self.changelog)
        self.assertIn(
            self.RELEASE_BOUNDARY,
            self.changelog[end:],
            "the released history is missing below the Unreleased section, so an "
            "unreleased claim cannot be distinguished from a shipped one",
        )
        return self.changelog[start:end]

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
            rows.append(
                (
                    int(issue.group(1)),
                    identifier.group(1) if identifier else None,
                    int(pull[0]) if pull else None,
                )
            )
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
                f"{milestone}: summary says {summary[milestone]}, table lists {len(listed)}",
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
            for issue, identifier, pull in self._issue_rows(self.sections[milestone]):
                with self.subTest(milestone=milestone, issue=issue):
                    self.assertIsNotNone(identifier, f"#{issue} has no CS identifier")
                    if milestone in self.DELIVERED:
                        self.assertIsNotNone(
                            pull, f"#{issue} is delivered but cites no pull request"
                        )

    def _timeline(self):
        """Return `({milestone: last_merge_date}, {tag: publish_date})`."""
        merged = dict(
            re.findall(
                r"\|\s*(v\d+\.\d+ — [^|]+?)\s*\|\s*\d{4}-\d{2}-\d{2} … (\d{4}-\d{2}-\d{2})\s*\|",
                self.record,
            )
        )
        published = dict(
            re.findall(r"\|\s*`(\d+\.\d+\.\d+rc\d+)`\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|", self.record)
        )
        return {name.strip(): date for name, date in merged.items()}, published

    def test_delivery_timeline_is_recorded_for_every_delivered_milestone(self):
        merged, published = self._timeline()
        for milestone in self.DELIVERED:
            self.assertIn(milestone, merged, f"{milestone} has no recorded merge window")
        # Counted against the current version rather than a literal, so preparing a
        # release cannot leave the timeline a tag behind.
        self.assertIn(
            f"`{__version__}`",
            self.record,
            f"the tag table has no row for the current version {__version__}",
        )
        self.assertGreaterEqual(len(published), len(self.DELIVERED))

    def test_no_milestone_claims_a_release_it_merged_after(self):
        """A `Released` cell must be `No`, or name a tag published after the work.

        This is the check that catches the mistake this document was written to
        correct. The record first said the v2.0 milestone shipped in
        `2.0.0rc1 … rc4`. Those are real tags, so merely verifying that a cited tag
        exists passes — and did. What makes the claim false is the ordering: every
        v2.0 pull request merged on 2026-08-18/19, and the last tag was published on
        2026-07-30. Work cannot be inside a tag that was cut nineteen days before it.

        Both dates are recorded in the delivery timeline, so the contradiction is
        checkable offline from the document alone.
        """
        merged, published = self._timeline()
        summary = re.findall(
            r"\|\s*(v\d+\.\d+ — [^|]+?)\s*\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
            self.record,
        )
        self.assertEqual(len(self.MILESTONES), len(summary), summary)
        for milestone, status, released in summary:
            with self.subTest(milestone=milestone):
                # Without this, a delivered milestone could carry any status word and
                # skip every check below by simply not saying `Planned`.
                self.assertEqual(
                    "Delivered" if milestone in self.DELIVERED else "Planned",
                    status,
                )
                if status == "Planned":
                    self.assertEqual("—", released)
                    continue
                if released.startswith("No"):
                    continue
                cited = set(re.findall(r"\d+\.\d+\.\d+rc\d+", released))
                self.assertTrue(cited, f"{milestone}: {released!r} names no tag")
                if released.startswith("Pending"):
                    # A pending tag must not also be recorded as published; that
                    # contradiction is what this document was written to prevent.
                    claimed = sorted(cited & set(published))
                    self.assertFalse(
                        claimed,
                        f"{milestone} is Pending on {claimed} but the tag table "
                        "records those as published",
                    )
                    continue
                unknown = sorted(cited - set(published))
                self.assertFalse(unknown, f"{milestone} claims unpublished tags: {unknown}")
                last_merge = merged[milestone]
                latest_claimed = max(published[tag] for tag in cited)
                self.assertLessEqual(
                    last_merge,
                    latest_claimed,
                    f"{milestone} merged through {last_merge} but claims release in "
                    f"{sorted(cited)}, published by {latest_claimed}; work cannot be "
                    "inside a tag cut before it",
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
            "docs/releases/v2.0-stable-criteria.md",
        ):
            self.assertIn(phrase, self.record)
        self.assertIn("unreleased", self.record.lower())

    def test_changelog_points_at_the_record_and_names_its_milestones(self):
        """The section carrying the work must cite the record and name what it carries."""
        released = self._released_by_milestone()
        for milestone in self.DELIVERED:
            body = self._carrying_section(released[milestone])
            label = milestone.split(" — ", 1)[0]
            with self.subTest(milestone=milestone):
                self.assertIn(self.RECORD, body)
                self.assertIn(label, body)
        # Whatever it carries, the changelog must still say where the record lives.
        self.assertIn(self.RECORD, self._unreleased_changelog())

    def test_unreleased_slice_excludes_a_newly_cut_release_section(self):
        """The slice must not widen the moment the next tag is cut.

        This is the failure mode that matters. Eighteen delivered issues are waiting
        on a tag, so a `## 2.1.0` section landing above `## 2.0.0rc4` is the next
        expected edit to this file. A slice that ended at one named release would
        swallow it and let shipped prose answer for unreleased work.
        """
        released = (
            "\n## 2.1.0 — 2026-09-01\n\n### Added\n\n"
            "- A shipped entry that must not count as unreleased.\n\n"
        )
        future = self.changelog.replace(
            f"\n{self.RELEASE_BOUNDARY}", f"{released}\n{self.RELEASE_BOUNDARY}", 1
        )
        self.assertIn("## 2.1.0", future, "the fixture did not insert a release")

        original, type(self).changelog = self.changelog, future
        try:
            sliced = self._unreleased_changelog()
        finally:
            type(self).changelog = original

        self.assertNotIn("## 2.1.0", sliced)
        self.assertNotIn("must not count as unreleased", sliced)
        # The real Unreleased content is still there, so the slice narrowed rather
        # than collapsed.
        self.assertIn("milestone-delivery.md", sliced)

    def test_unreleased_slice_rejects_a_demoted_or_missing_heading(self):
        for broken, reason in (
            (self.changelog.replace("## Unreleased", "### Unreleased", 1), "demoted"),
            (self.changelog.replace("## Unreleased", "## Unreleased notes", 1), "renamed"),
        ):
            with self.subTest(reason=reason):
                original, type(self).changelog = self.changelog, broken
                try:
                    with self.assertRaises(AssertionError):
                        self._unreleased_changelog()
                finally:
                    type(self).changelog = original

    def test_unreleased_slice_requires_released_history_below_it(self):
        truncated = self.changelog.split(f"\n{self.RELEASE_BOUNDARY}", 1)[0] + "\n"
        original, type(self).changelog = self.changelog, truncated
        try:
            with self.assertRaisesRegex(AssertionError, "released history is missing"):
                self._unreleased_changelog()
        finally:
            type(self).changelog = original

    @contextlib.contextmanager
    def _changelog(self, text):
        """Drive the helpers against a fixture instead of the real changelog."""
        original = type(self).changelog
        type(self).changelog = text
        try:
            yield
        finally:
            type(self).changelog = original

    def test_carrying_section_resolves_a_released_milestone_to_its_tag(self):
        """The released branch is unreachable from the current documents.

        Every milestone is `Released = No` here, so that branch would ship untested
        until the first tag is prepared — exactly when it starts deciding where the
        evidence check looks. It is driven directly instead.
        """
        fixture = (
            "# Changelog\n\n"
            "## Unreleased\n\n- Pending work.\n\n"
            "## 9.9.9rc1 — 2026-01-01\n\n- Shipped work.\n\n"
            "## 2.0.0rc4 — 2026-07-30\n\n- Older work.\n"
        )
        with self._changelog(fixture):
            self.assertIn("Shipped work", self._carrying_section("Yes — `9.9.9rc1`"))
            self.assertNotIn("Pending work", self._carrying_section("Yes — `9.9.9rc1`"))
            self.assertIn("Pending work", self._carrying_section("No — under Unreleased"))
            # A milestone cannot be released by a tag the changelog never describes.
            with self.assertRaisesRegex(AssertionError, "no changelog section for"):
                self._carrying_section("Yes — `9.9.9rc2`")

    def test_distinct_entry_matching_rejects_two_issues_sharing_one_entry(self):
        """Pin the rejection path, not only the happy path.

        The real documents satisfy the matching, so without this the check could be
        weakened to always pass and the suite would stay green.
        """
        milestone = self.DELIVERED[0]
        evidence = {"CS-901": "alpha", "CS-902": "beta"}
        owner = dict.fromkeys(evidence, milestone)
        released = dict.fromkeys(self.DELIVERED, "No — under Unreleased")
        tail = "\n## 2.0.0rc4 — 2026-07-30\n\n- Older work.\n"

        shared = f"# Changelog\n\n## Unreleased\n\n- One entry naming alpha and beta.\n{tail}"
        with self._changelog(shared):
            unmatched = self._unmatched_issues(evidence, owner, released)
            self.assertEqual(1, len(unmatched), unmatched)
            self.assertIn(unmatched[0][0], evidence)
            with self.assertRaisesRegex(AssertionError, "has no CHANGELOG entry of its own"):
                self._assert_each_issue_owns_a_distinct_entry(evidence, owner, released)

        separate = (
            "# Changelog\n\n## Unreleased\n\n"
            f"- An entry naming alpha.\n- An entry naming beta.\n{tail}"
        )
        with self._changelog(separate):
            self.assertEqual([], self._unmatched_issues(evidence, owner, released))
            self._assert_each_issue_owns_a_distinct_entry(evidence, owner, released)

    def test_distinct_entry_matching_reassigns_rather_than_failing_greedily(self):
        """One shared entry plus one exclusive entry is satisfiable, and accepted.

        A greedy first-come assignment would fail this: `CS-901` matches both
        bullets, and taking the shared one first would starve `CS-902`. The
        augmenting-path matching reassigns instead, so the check rejects only
        genuinely unsatisfiable evidence rather than unlucky ordering.
        """
        milestone = self.DELIVERED[0]
        evidence = {"CS-901": "alpha", "CS-902": "beta"}
        owner = dict.fromkeys(evidence, milestone)
        released = dict.fromkeys(self.DELIVERED, "No — under Unreleased")
        fixture = (
            "# Changelog\n\n## Unreleased\n\n"
            "- An entry naming alpha and beta.\n"
            "- An entry naming alpha only.\n"
            "\n## 2.0.0rc4 — 2026-07-30\n\n- Older work.\n"
        )
        with self._changelog(fixture):
            self.assertEqual([], self._unmatched_issues(evidence, owner, released))

    def test_readme_points_at_the_record_and_states_the_release_status(self):
        """A record nobody can find from the README is a record nobody reads."""
        self.assertIn(self.RECORD, self.readme)
        self.assertIn("unreleased", self.readme.lower())
        for milestone in ("v2.1", "v2.2"):
            self.assertIn(milestone, self.readme)

    def test_every_delivered_issue_is_described_in_the_changelog(self):
        """Each delivered issue's headline artifact must appear in the CHANGELOG.

        This is the check that catches an omission, and its scope is the whole
        delivered set for a reason. An earlier version of this test covered v2.1 and
        v2.2 only, mirroring the assumption that the v2.0 milestone was already
        described by the `2.0.0rc*` release notes. It was not: those prereleases were
        published on 2026-07-30 and every v2.0 milestone issue merged on 2026-08-18/19,
        so seven of them had no entry anywhere and the test could not see it.

        Each probe is an identifier the change introduced, so it cannot be satisfied
        by unrelated prose, and `DELIVERED` is derived from the record rather than
        restated here — adding a delivered milestone without extending `evidence`
        fails instead of silently narrowing the check.
        """
        evidence = {
            "CS-001": "v2.0-stable-criteria",
            "CS-002": "tests/golden/mini-comic",
            "CS-003": "resume requires a blocked project",
            "CS-004": "lifecycle failure-injection suite",
            "CS-005": "clean-install verification across the supported platforms",
            "CS-006": "doctor_report()",
            "CS-007": "supported_project_schema_versions",
            "CS-008": "audited installer lifecycle safety",
            "CS-009": "comic_sol_product.errors",
            "CS-010": "release_qualification.py",
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
            for milestone in self.DELIVERED
            for _, identifier, _ in self._issue_rows(self.sections[milestone])
        }
        self.assertEqual(
            recorded,
            set(evidence),
            "the delivered set and this test's evidence map disagree; a milestone "
            "marked delivered must have one evidence probe per issue",
        )
        # Whitespace is collapsed so a probe cannot be defeated by where a line
        # happens to wrap. `clean-install verification` spanning two lines is a
        # formatting detail, not a missing entry.
        released = self._released_by_milestone()
        bodies = {
            milestone: " ".join(self._carrying_section(released[milestone]).lower().split())
            for milestone in self.DELIVERED
        }
        owner = {
            identifier: milestone
            for milestone in self.DELIVERED
            for _, identifier, _ in self._issue_rows(self.sections[milestone])
        }
        for identifier, phrase in sorted(evidence.items()):
            milestone = owner[identifier]
            with self.subTest(identifier=identifier, milestone=milestone):
                self.assertIn(
                    phrase.lower(),
                    bodies[milestone],
                    f"{identifier} is recorded as delivered by {milestone} "
                    f"({released[milestone]}) but that CHANGELOG section never "
                    f"mentions {phrase!r}",
                )
        self._assert_each_issue_owns_a_distinct_entry(evidence, owner, released)

    def _unmatched_issues(self, evidence, owner, released):
        """Return `[(identifier, milestone, probe)]` for issues with no own entry.

        Every delivered issue must be evidenced by an entry of its own.

        A phrase that appears somewhere in the section is not enough. Twice now a
        probe passed by matching *another* issue's entry: `CS-007` matched
        `UnsupportedSchemaVersionError` inside the page-QA migration bullet that
        belongs to `CS-035`, and `CS-008` matched the `failure-injection` bullet that
        belongs to `CS-004` — so both were undocumented while appearing covered.

        Requiring a distinct owning bullet per issue closes that whole class rather
        than the two instances. It is a bipartite matching: if no assignment of
        issues to distinct bullets exists, at least two issues are leaning on one
        entry.

        Returning the unmatched set rather than asserting keeps the rejection path
        testable — an assertion inside `subTest` is recorded rather than raised, so
        a caller could not observe it.
        """
        unmatched: list[tuple[str, str, str]] = []
        for milestone in self.DELIVERED:
            bullets = [
                " ".join(bullet.lower().split())
                for bullet in re.split(r"\n(?=- )", self._carrying_section(released[milestone]))
            ]
            candidates = {
                identifier: {
                    index
                    for index, bullet in enumerate(bullets)
                    if evidence[identifier].lower() in bullet
                }
                for identifier in sorted(evidence)
                if owner[identifier] == milestone
            }
            assigned: dict[int, str] = {}

            def claim(identifier, seen):
                for index in sorted(candidates[identifier]):
                    if index in seen:
                        continue
                    seen.add(index)
                    holder = assigned.get(index)
                    if holder is None or claim(holder, seen):
                        assigned[index] = identifier
                        return True
                return False

            for identifier in candidates:
                if not claim(identifier, set()):
                    unmatched.append((identifier, milestone, evidence[identifier]))
        return unmatched

    def _assert_each_issue_owns_a_distinct_entry(self, evidence, owner, released):
        """Fail when any delivered issue lacks an entry of its own."""
        unmatched = self._unmatched_issues(evidence, owner, released)
        self.assertEqual(
            [],
            unmatched,
            "; ".join(
                f"{identifier} has no CHANGELOG entry of its own in {milestone}: "
                f"its probe {probe!r} only matches an entry another issue already "
                "accounts for"
                for identifier, milestone, probe in unmatched
            ),
        )


if __name__ == "__main__":
    unittest.main()
