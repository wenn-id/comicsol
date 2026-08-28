import contextlib
import json
import re
import unittest
from pathlib import Path

from comic_sol_product import __version__
from comic_sol_product.distribution import ReleaseIdentity, native_payload_names
from scripts.release_qualification import ARTIFACT_PLATFORMS, DEFAULT_REQUIRED_TARGETS
from scripts.stage_registry import ARTIFACT_STAGE
from scripts.validate_project import (
    EXPORT_READY_ARTIFACT_PATHS,
    MANIFEST_ARTIFACT_KEYS,
    TERMINAL_ARTIFACT_KEYS,
    TERMINAL_ARTIFACT_PATHS,
)


class ReleaseDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")
        cls.install = (cls.root / "docs/install.md").read_text(encoding="utf-8")
        cls.install_manual = (cls.root / "docs/install-manual.md").read_text(encoding="utf-8")
        cls.changelog = (cls.root / "CHANGELOG.md").read_text(encoding="utf-8")
        cls.notes = (cls.root / f"docs/releases/v{__version__}.md").read_text(encoding="utf-8")
        cls.release_workflow = (cls.root / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        cls.stable_criteria = (cls.root / "docs/releases/v2.0-stable-criteria.md").read_text(
            encoding="utf-8"
        )
        cls.schemas = (cls.root / "references/schemas.md").read_text(encoding="utf-8")
        cls.manifest_template = json.loads(
            (cls.root / "templates/manifest.json").read_text(encoding="utf-8")
        )

    @classmethod
    def _native_targets(cls):
        """Derive platform/architecture pairs from the release workflow matrix."""
        native = cls.release_workflow.split("\n  native:\n", 1)[1].split("\n  container:\n", 1)[0]
        targets = {}
        for chunk in native.split("- os:")[1:]:
            fields = dict(
                re.findall(
                    r"^[ \t]*(platform|arch):[ \t]*'?([^'\n]+?)'?[ \t]*$",
                    chunk,
                    re.M,
                )
            )
            if "platform" in fields and "arch" in fields:
                targets[fields["platform"]] = fields["arch"]
        return targets

    @staticmethod
    def _collapsed(text):
        return " ".join(text.split())

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
        self.assertIn("docker compose", self.install_manual)

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

    def test_release_support_docs_follow_workflow_and_qualification_matrix(self):
        native_targets = self._native_targets()
        qualification_targets = {
            platform: architecture
            for platform, architecture in DEFAULT_REQUIRED_TARGETS
            if platform != "wsl"
        }
        self.assertEqual(qualification_targets, native_targets)

        display_names = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
        documents = {
            "README.md": self.readme,
            "docs/install.md": self.install,
            "CHANGELOG.md": self.changelog,
            "stable criteria": self.stable_criteria,
            "rc5 candidate notes": (self.root / "docs/releases/v2.0.0rc5.md").read_text(
                encoding="utf-8"
            ),
            "current candidate notes": self.notes,
        }
        wsl_architecture = dict(DEFAULT_REQUIRED_TARGETS)["wsl"]
        wsl_artifact_platform = ARTIFACT_PLATFORMS["wsl"]
        wsl_contract = (
            f"WSL2 uses the {display_names[wsl_artifact_platform]} "
            f"{wsl_architecture} archive; it has no separate native archive."
        )
        source_contract = (
            "Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+."
        )
        intel_contract = "Intel macOS is source-install-only; it has no native archive."

        for name, document in documents.items():
            collapsed = self._collapsed(document)
            with self.subTest(document=name):
                for platform, architecture in native_targets.items():
                    self.assertIn(f"{display_names[platform]} {architecture}", collapsed)
                self.assertIn(wsl_contract, collapsed)
                self.assertIn(source_contract, collapsed)
                self.assertIn(intel_contract, collapsed)

    def test_candidate_notes_name_exactly_every_matrix_payload(self):
        native_targets = self._native_targets()
        versions = ("2.0.0rc5", __version__)
        for version in versions:
            notes = (self.root / f"docs/releases/v{version}.md").read_text(encoding="utf-8")
            documented = set(
                re.findall(
                    rf"`(comic-sol-{re.escape(version)}-[a-z0-9_-]+-[a-z0-9_-]+\."
                    rf"(?:zip|metadata\.json|sbom\.json))`",
                    notes,
                )
            )
            expected = set()
            for platform, architecture in native_targets.items():
                identity = ReleaseIdentity(__version__, platform, architecture)
                for name in native_payload_names(identity):
                    expected.add(name.replace(__version__, version, 1))
            self.assertEqual(expected, documented, version)

    def test_schema_reference_follows_template_registry_and_validator(self):
        artifact_section = self.schemas.split("### `artifacts`", 1)[1].split(
            "### `stage_versions`", 1
        )[0]
        allowed = re.search(r"Allowed keys are (.+?)\. Each present", artifact_section, re.S)
        self.assertIsNotNone(allowed)
        documented_allowed = set(re.findall(r"`([a-z_]+)`", allowed.group(1)))
        self.assertEqual(set(MANIFEST_ARTIFACT_KEYS), documented_allowed)

        rows = re.findall(
            r"(?m)^\| `([a-z_]+)` \| `([a-z]+)` \| `([^`]+)` "
            r"\| `(export-ready|terminal)` \|$",
            artifact_section,
        )
        documented = {
            artifact: {"owner": owner, "path": path, "required": required}
            for artifact, owner, path, required in rows
        }
        self.assertEqual(set(MANIFEST_ARTIFACT_KEYS), set(documented))
        self.assertEqual(
            ARTIFACT_STAGE,
            {artifact: values["owner"] for artifact, values in documented.items()},
        )

        expected_paths = dict(TERMINAL_ARTIFACT_PATHS)
        expected_paths["pdf"] = "exports/{project_id}.pdf"
        self.assertEqual(
            expected_paths,
            {artifact: values["path"] for artifact, values in documented.items()},
        )
        expected_requirement = {
            artifact: "export-ready" if artifact in EXPORT_READY_ARTIFACT_PATHS else "terminal"
            for artifact in TERMINAL_ARTIFACT_KEYS
        }
        self.assertEqual(
            expected_requirement,
            {artifact: values["required"] for artifact, values in documented.items()},
        )

        versions_section = self.schemas.split("### `stage_versions`", 1)[1].split("\n## ", 1)[0]
        documented_versions = dict(
            re.findall(r"(?m)^\| `([a-z]+)` \| `([0-9]+)` \|$", versions_section)
        )
        self.assertEqual(self.manifest_template["stage_versions"], documented_versions)

        bundled = (self.root / "skills/comic-sol/references/schemas.md").read_text(encoding="utf-8")
        self.assertEqual(self.schemas, bundled)

    def test_release_gate_documents_orchestration_states_and_environment_limit(self):
        criteria = self.stable_criteria
        for phrase in (
            "Candidate prepared",
            "Candidate gated and built",
            "Candidate published",
            "Qualification",
            "Awaiting approval",
            "Promotion",
            "Failure / blocked",
            "Withdrawal / yank",
            "Rollback",
            "Disposable-candidate drill",
            "release-production",
            "tag ruleset",
            "actor_id: 5",
            "bypass_mode: always",
            "Every matching ruleset containing `creation`",
            "draft=false",
            "prerelease=true",
            "immutable=true",
            "restricts tag creation, updates, and deletions",
            "required_signatures",
            "signed annotated tag",
            "tag-object SHA",
            "protected `main`",
            "target_commitish",
            "ownership marker",
            "newest deployment status",
            "required reviewers",
            "YAML can name but cannot protect an Environment",
            "github.actor",
            "deployment audit",
            "private draft candidate",
            "delete only that draft",
            "run-attempt evidence artifact",
            "immutable release",
            "attests the JSON and Markdown digests",
            "final allowed Release mutation",
            "locked prerelease field",
            "82% line coverage",
            "72% branch coverage",
            "Quality gates",
            "supporting evidence outside that payload manifest",
            "actual reviewer remains `null`",
            "release-evidence.json",
            "release-evidence.md",
        ):
            self.assertIn(phrase, criteria)

    def test_install_guide_covers_recommended_and_manual_lifecycles(self):
        for phrase in (
            "--release v2.0.0rc6",
            "-Release v2.0.0rc6",
            "--uninstall",
            "-Uninstall",
            "active-version",
            "rollback",
            "SHA256SUMS",
            "SHA256SUMS.sigstore.json",
            "cosign",
            "absolute `doctor` command",
        ):
            self.assertIn(phrase, self.install)
        for phrase in (
            "install.sh",
            "install.ps1",
            "--sha256",
            "-SHA256",
            "-Checksums",
            "-Signature",
            "--checksums",
            "--signature",
            "comic-sol doctor",
            "CycloneDX",
        ):
            self.assertIn(phrase, self.install_manual)
        self.assertEqual(3, self.install.count("sh ./install.sh --release v2.0.0rc6"))
        self.assertEqual(1, self.install.count(".\\install.ps1 -Release v2.0.0rc6"))
        self.assertIn(
            '--certificate-identity "https://github.com/wenn-id/comicsol/.github/workflows/release.yml@refs/tags/${RELEASE}"',
            self.install_manual,
        )
        self.assertIn("not published yet", self._collapsed(self.install))
        self.assertIn(
            "cannot bind that identity to one caller-selected exact tag",
            self._collapsed(self.install_manual),
        )
        self.assertIn("$HOME/.local/share/comic-sol", self.install)
        self.assertIn("$HOME\\AppData\\Local\\ComicSol", self.install)
        self.assertIn("User projects", self.install)
        self.assertNotIn("curl | sh", (self.install + self.install_manual).lower())

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

    def test_historical_macos_architecture_mislabel_is_disclosed_per_release(self):
        """Keep the immutable rc1-rc4 correction visible in every release note."""
        for version in ("rc1", "rc2", "rc3", "rc4"):
            with self.subTest(version=version):
                document = (self.root / f"docs/releases/v2.0.0{version}.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("## Published archive correction", document)
                self.assertIn("contains **arm64** binaries, not x86_64 binaries", document)
                # The sentence is hard-wrapped in Markdown, so match across the wrap.
                self.assertRegex(document, r"Intel Macs cannot run this\s+archive natively")
                self.assertIn("immutable asset has not been replaced", document)
                self.assertIn("macos-arm64", document)
                self.assertNotIn("This RC publishes x86_64 archives only", document)
                self.assertNotIn("Linux, macOS, and Windows x86_64 portable archives", document)

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

    def test_draft_release_notes_use_conditional_wording_for_artifacts(self):
        """Draft release notes must use future/conditional wording, not present-tense claims.

        The draft callout at the top of v2.0.0rc6 states "This tag does not exist yet"
        and "Nothing here is a claim that a download exists." The artifact list must
        use future wording consistent with that draft status, not imply artifacts are
        already published.
        """
        self.assertIn("The prerelease will also include:", self.notes)
        self.assertNotIn("The prerelease also includes:", self.notes)


class SupplyChainProvenanceContractTests(unittest.TestCase):
    """Keep the #211 supply-chain deliverables from silently regressing.

    The trust chain document, the installer bootstrap verification section, the
    rollback runbook, and the qualification-side candidate-identity binding are
    release-gate properties; these checks keep them linked and named exactly.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.install = (cls.root / "docs/install.md").read_text(encoding="utf-8")
        cls.install_manual = (cls.root / "docs/install-manual.md").read_text(encoding="utf-8")
        cls.trust_chain = (cls.root / "docs/releases/release-trust-chain.md").read_text(
            encoding="utf-8"
        )
        cls.runbook = (cls.root / "docs/releases/rollback-runbook.md").read_text(encoding="utf-8")
        cls.criteria = (cls.root / "docs/releases/v2.0-stable-criteria.md").read_text(
            encoding="utf-8"
        )
        cls.qualification_workflow = (
            cls.root / ".github/workflows/release-qualification.yml"
        ).read_text(encoding="utf-8")
        cls.security = (cls.root / "SECURITY.md").read_text(encoding="utf-8")
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")

    def test_trust_chain_defines_the_complete_subject_set(self):
        from comic_sol_product import __version__
        from comic_sol_product.distribution import ReleaseIdentity, native_payload_names

        document = self.trust_chain
        collapsed = " ".join(document.split())
        for phrase in (
            "# Release subject set and trust chain",
            "authoritative provenance reference",
            "Nothing else is a release asset",
            "candidate-identity.json",
            "SHA256SUMS.sigstore.json",
            "build-provenance attestation",
            "token.actions.githubusercontent.com",
            "self-hosted runners denied",
            "signed annotated tag",
            "tag-object SHA",
            "protected `main`",
            "required_signatures",
            "actor_type=RepositoryRole",
            "actor_id=5",
            "bypass_mode=always",
            "Every matching ruleset containing `creation`",
            "target_commitish=main",
            "raw field is non-authoritative",
            "captured direct tag-object SHA and peeled candidate commit",
        ):
            self.assertIn(phrase, collapsed)
        # The table uses the `X` version placeholder; assert the exact subject
        # name patterns for every matrix target plus the shared payloads.
        for platform, architecture in (
            ("linux", "x86_64"),
            ("macos", "arm64"),
            ("windows", "x86_64"),
        ):
            identity = ReleaseIdentity(__version__, platform, architecture)
            for name in native_payload_names(identity):
                self.assertIn(name.replace(__version__, "X"), document)
        for payload in (
            "py3-none-any.whl",
            "container.tar",
            "install.sh",
            "install.ps1",
        ):
            self.assertIn(payload, document)

    def test_trust_chain_records_the_oci_decision_and_its_boundary(self):
        collapsed = " ".join(self.trust_chain.split())
        oci_section = collapsed.split("## OCI distribution decision", 1)[1].split(" ## ", 1)[0]
        for phrase in (
            "official distribution channel",
            "not (yet) as a registry image",
            "packages: write",
            "cosign sign",
            "no release may claim a registry image",
        ):
            self.assertIn(phrase, oci_section)
        # The install guide must agree with the decision.
        install_collapsed = " ".join(self.install.split())
        self.assertIn("official distribution channel", install_collapsed)
        self.assertIn("no `ghcr.io` image", install_collapsed)

    def test_install_guide_documents_pre_execution_installer_verification(self):
        section = " ".join(
            self.install_manual.split("## Verify installer bytes before first execution", 1)[1]
            .split("\n## ", 1)[0]
            .split()
        )
        for phrase in (
            "bootstrap gap",
            "cosign verify-blob",
            "gh attestation verify",
            "Get-FileHash .\\install.ps1 -Algorithm SHA256",
            "sha256sum -c -",
            "must not be executed",
            "release page",
            "not the `installers/` paths from a repository checkout",
        ):
            self.assertIn(phrase, section)
        # The attestation commands must target the downloaded release-asset
        # file names, which exist in the download directory.
        self.assertIn("gh attestation verify ./install.sh", section)
        self.assertIn(".\\install.ps1", section)
        # The manual cosign example must stay executable: no doubled backslashes.
        self.assertNotIn("\\\\", section)

    def test_rollback_runbook_preserves_immutable_evidence(self):
        collapsed = " ".join(self.runbook.split())
        for phrase in (
            "adding evidence, never replacing bytes",
            "never delete the signed annotated tag",
            "WITHDRAWN",
            "ROLLED BACK",
            "candidate-identity.json",
            "--method PATCH",
            "blocked",
            "must not imply those systems were yanked",
            "fresh version and tag",
            "never delete the release yourself",
            "removes GitHub's immutable-release binding",
            "administrator-only escalation",
            "rulesets?includes_parents=true",
            "signed annotated tag",
            "captured tag-object SHA",
            "required_signatures",
            "RepositoryRole",
            "actor ID `5`",
            "restrict creation, updates, and deletions",
            "scripts/release_identity.py rulesets",
            '--release-ref "refs/tags/vX"',
            "validation.json",
        ):
            self.assertIn(phrase, collapsed)
        self.assertIn('--rulesets-dir "$phase_dir/full"', self.runbook)
        self.assertIn("rulesets/${ruleset_id}", self.runbook)
        self.assertEqual(self.runbook.count("capture_and_validate_tag_rulesets before"), 1)
        self.assertEqual(self.runbook.count("capture_and_validate_tag_rulesets after"), 1)
        # Deleting an immutable release is an escalation path guarded by the tag
        # ruleset, never a documented standard withdrawal command.
        self.assertNotIn("--method DELETE", self.runbook)
        for document_text in (self.criteria, self.install, self.readme):
            self.assertIn("rollback-runbook.md", document_text)
        self.assertIn("release-trust-chain.md", self.criteria)
        self.assertIn("release-trust-chain.md", self.security)
        self.assertIn("release-trust-chain.md", self.readme)

    def test_qualification_binds_candidate_identity_on_every_leg(self):
        workflow = self.qualification_workflow
        # Every platform leg (native matrix, WSL, source) downloads and verifies the
        # published candidate identity together with its own inputs.
        self.assertEqual(workflow.count("Bind signed manifest and signature bundle"), 3)
        self.assertGreaterEqual(workflow.count("--pattern candidate-identity.json"), 4)
        for phrase in (
            "candidate identity sidecar digest mismatch",
            "candidate identity tag does not match qualification input",
            "candidate identity commit does not match qualification input",
            "candidate identity tag object does not match qualification input",
            "candidate identity protected main does not match qualification input",
            "published checksum manifest does not match candidate identity",
            "supporting evidence digest mismatch",
        ):
            self.assertIn(phrase, workflow)

    def test_source_payload_checksum_filter_excludes_manifest_and_identity_files(self):
        """SHA256SUMS names neither itself nor the identity files; the filter must match.

        Regression test: the source leg passes every downloaded file except the
        four excluded names to verify_payload_checksums, which fails coverage
        when a file absent from the manifest is included.
        """
        workflow = self.qualification_workflow
        filter_block = workflow.split("verify_payload_checksums", 1)[1].split("PY", 1)[0]
        for name in (
            "SHA256SUMS",
            "SHA256SUMS.sigstore.json",
            "candidate-identity.json",
            "candidate-identity.json.sha256",
        ):
            self.assertIn(f'"{name}"', filter_block)

    def test_stable_criteria_gate_the_new_provenance_requirements(self):
        criteria = self.criteria
        self.assertIn(
            "canonical input file with a reproducible regeneration command",
            criteria,
        )
        self.assertIn("pre-execution installer verification", criteria)
        self.assertIn("requirements/README.md", criteria)


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


class GoldenCreatorDocumentationHierarchyTests(unittest.TestCase):
    """WP3 creator outcomes stay first while release paths remain reachable."""

    @classmethod
    def setUpClass(cls):
        """Resolve the repository root used by every documentation assertion."""
        cls.root = Path(__file__).resolve().parents[1]

    def _read(self, relative: str) -> str:
        """Read one UTF-8 repository document."""
        return (self.root / relative).read_text(encoding="utf-8")

    def test_creator_documents_lead_with_goal_before_advanced_installation(self):
        """Keep creator outcomes ahead of advanced installation material."""
        expectations = {
            "docs/onboarding.md": ("Install the Agent Skill", "native archive"),
            "docs/install.md": ("Creator path", "Recommended installation"),
            "docs/surfaces.md": ("Creator path", "Source checkout (development)"),
            "docs/user/index.md": ("Start with your goal", "Advanced integrations"),
            "docs/user/getting-started.md": ("Install the Agent Skill", "Native core CLI"),
        }
        for relative, (creator, advanced) in expectations.items():
            document = self._read(relative)
            self.assertGreater(document.find(creator), -1, relative)
            self.assertGreater(document.find(advanced), -1, relative)
            self.assertLess(document.find(creator), document.find(advanced), relative)

    def test_advanced_entry_keeps_every_supported_surface_reachable(self):
        """Keep every retained advanced integration linked from one entry."""
        readme = self._read("README.md")
        section = readme.split("## Advanced integrations", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "CLI",
            "MCP",
            "OCI",
            "source",
            "wheel",
            "native archive",
            "security",
            "release",
        ):
            self.assertIn(phrase, section, phrase)
        for link in (
            "docs/surfaces.md",
            "docs/install.md",
            "docs/install-manual.md",
            "SECURITY.md",
            "docs/releases/release-trust-chain.md",
        ):
            self.assertIn(f"]({link})", section, link)

    def test_readme_sample_output_links_are_real_tracked_files(self):
        """Require each creator-facing sample output link to resolve to a file."""
        readme = self._read("README.md")
        advanced = readme.index("## Advanced integrations")
        creator = readme[:advanced]
        outputs = (
            "samples/sunlight-courier/exports/sunlight-courier.pdf",
            "samples/sunlight-courier/pages/page-001.png",
            "samples/sunlight-courier/pages/page-002.png",
            "samples/sunlight-courier/project.json",
            "samples/sunlight-courier/qa/report.md",
        )
        for relative in outputs:
            self.assertIn(f"]({relative})", creator, relative)
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_installer_docs_use_the_current_wp2_interface(self):
        """Keep all installation routes on the current target-and-scope CLI."""
        for relative in (
            "README.md",
            "docs/onboarding.md",
            "docs/install.md",
            "docs/user/getting-started.md",
            "docs/user/index.md",
        ):
            document = self._read(relative)
            self.assertIn("comic-sol skill-install --target", document, relative)
            self.assertIn("--scope", document, relative)
        self.assertIn(
            "comic-sol skill-install --target codex --scope user",
            self._read("docs/user/index.md"),
        )

    def test_installed_creator_readiness_uses_the_installed_launcher(self):
        """Use `comic-sol doctor` before source-checkout alternatives."""
        getting_started = self._read("docs/user/getting-started.md")
        readiness = getting_started.split("## 2. Check readiness", 1)[1].split("\n## ", 1)[0]
        installed = readiness.index('comic-sol doctor --output-root "$HOME/Comic Sol"')
        source = readiness.index('"$PYTHON" scripts/comic_sol.py doctor')
        self.assertLess(installed, source)

        surfaces = self._read("docs/surfaces.md")
        self.assertIn("## Codex Skill placement", surfaces)
        self.assertNotIn("## Codex Skill checkout", surfaces)
        summary = surfaces.split("## Surface summary", 1)[1]
        skill_row = next(line for line in summary.splitlines() if "Codex Skill" in line)
        self.assertIn("fresh agent session", skill_row)
        self.assertIn("`comic-sol doctor`", skill_row)
        self.assertNotIn("scripts/comic_sol.py", skill_row)

    def test_source_pillow_recovery_links_the_real_install_command(self):
        """Point source users from the Pillow failure row to executable instructions."""
        onboarding = self._read("docs/onboarding.md")
        failure_table = onboarding.split("## 5. If `doctor` reported a failure", 1)[1]
        pillow_row = next(line for line in failure_table.splitlines() if "`pillow`" in line)
        self.assertIn("install-manual.md#source-and-wheel-installation", pillow_row)
        self.assertNotIn("in step 1", pillow_row)

    def test_user_index_routes_to_blocked_only_when_no_execution_route_exists(self):
        """Describe native, external, handoff, and BLOCKED as ordered alternatives."""
        index = " ".join(self._read("docs/user/index.md").split())
        self.assertIn(
            "If no compatible native tool or external adapter is declared, portable "
            "handoff is next; if no route is available, the project remains safely `BLOCKED`",
            index,
        )
        self.assertNotIn("handoff is next; otherwise the project remains", index)

    def test_readme_getting_started_anchor_matches_the_numbered_heading(self):
        """Keep the README Skill-placement deep link aligned with GitHub anchors."""
        readme = self._read("README.md")
        getting_started = self._read("docs/user/getting-started.md")
        self.assertIn(
            "[getting started](docs/user/getting-started.md#1-install-the-agent-skill)",
            readme,
        )
        self.assertIn("## 1. Install the Agent Skill", getting_started)
