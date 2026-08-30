"""Contracts for honest showcase publication and live agent-host evidence."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def collapsed(text: str) -> str:
    return " ".join(text.split())


def level_two_section(document: str, heading: str) -> str:
    marker = f"## {heading}"
    start = document.index(marker)
    end = document.find("\n## ", start + len(marker))
    return document[start:] if end == -1 else document[start:end]


def markdown_links(document: str) -> dict[str, str]:
    return {label: target for label, target in re.findall(r"\[([^]]+)]\(([^)]+)\)", document)}


class ShowcaseEvidenceTests(unittest.TestCase):
    """The showcase makes exactly one narrow, inspectable quality claim."""

    @classmethod
    def setUpClass(cls):
        cls.document = read("docs/showcase.md")
        cls.initial = level_two_section(cls.document, "Initial visual-quality proof")

    def test_exactly_one_visual_quality_sample_is_claimed(self):
        links = markdown_links(self.initial)
        sample_targets = [
            target for target in links.values() if re.fullmatch(r"\.\./samples/[^/]+", target)
        ]
        self.assertEqual(["../samples/sunlight-courier"], sample_targets)
        self.assertIn("only initial visual-quality sample", collapsed(self.initial))

    def test_sunlight_courier_has_honest_evidence_labels(self):
        section = collapsed(self.initial)
        for phrase in (
            "retained live visual evidence",
            "does not establish broad or universal illustration quality",
            "Provider/model provenance",
            "Reviewer and visual-QA evidence",
        ):
            self.assertIn(phrase, section, phrase)

    def test_sunlight_courier_links_every_required_artifact(self):
        targets = set(markdown_links(self.initial).values())
        expected = {
            "../samples/sunlight-courier/pages/page-001.png",
            "../samples/sunlight-courier/pages/page-002.png",
            "../samples/sunlight-courier/exports/sunlight-courier.pdf",
            "../samples/sunlight-courier/project.json",
            "../samples/sunlight-courier/qa/report.md",
        }
        self.assertTrue(expected.issubset(targets), expected - targets)

    def test_known_limitations_are_explicit(self):
        limitations = collapsed(level_two_section(self.document, "Known limitations"))
        for phrase in (
            "one two-page project",
            "The retained reviewer label does not identify a human reviewer.",
            "provider and model are not separately identified",
            "not a universal quality guarantee",
        ):
            self.assertIn(phrase, limitations, phrase)

    def test_deterministic_samples_are_mechanics_only(self):
        section = collapsed(level_two_section(self.document, "Evidence boundary"))
        self.assertIn("Deterministic samples are mechanics-only", section)
        self.assertIn("never proof of illustration quality", section)


class ShowcaseSubmissionContractTests(unittest.TestCase):
    """Both contributor entry points preserve the publication-consent boundary."""

    @classmethod
    def setUpClass(cls):
        cls.documents = {
            "showcase": read("docs/showcase.md"),
            "contributing": read("CONTRIBUTING.md"),
        }

    def test_publication_consent_is_explicit_and_separate_from_dogfood(self):
        for name, document in self.documents.items():
            with self.subTest(document=name):
                normalized = collapsed(document)
                self.assertIn("explicit consent to publish the comic", normalized)
                self.assertIn("issue #246", normalized)
                self.assertIn("separate", normalized)
                self.assertIn(
                    "A dogfood report never implies permission to publish the comic, story, images, or prompts.",
                    normalized,
                )

    def test_rights_to_share_are_required(self):
        for name, document in self.documents.items():
            with self.subTest(document=name):
                self.assertIn(
                    "owns the work or has permission to share it",
                    collapsed(document),
                )

    def test_provider_and_model_disclosure_is_conditional_on_availability(self):
        for name, document in self.documents.items():
            with self.subTest(document=name):
                normalized = collapsed(document)
                self.assertIn("provider and model", normalized)
                self.assertIn("when available", normalized)
                self.assertNotIn("guess a provider or model", normalized.lower())

    def test_attempt_provenance_and_visual_qa_are_retained(self):
        for name, document in self.documents.items():
            with self.subTest(document=name):
                normalized = collapsed(document)
                self.assertIn("generation attempts and provenance", normalized)
                self.assertIn("visual-QA evidence", normalized)

    def test_private_and_sensitive_material_is_excluded(self):
        exclusions = (
            "private source material",
            "credentials",
            "tokens",
            "account identifiers",
            "private endpoints",
            "raw provider responses",
            "unrelated logs",
        )
        for name, document in self.documents.items():
            with self.subTest(document=name):
                normalized = collapsed(document)
                for exclusion in exclusions:
                    self.assertIn(exclusion, normalized, exclusion)


class HostSmokeContractTests(unittest.TestCase):
    """Host support stays experimental until one complete live record is retained."""

    @classmethod
    def setUpClass(cls):
        cls.document = read("docs/agent-host-smoke.md")
        cls.format = level_two_section(cls.document, "Smoke-record format")
        cls.threshold = level_two_section(cls.document, "Verification threshold")
        cls.status = level_two_section(cls.document, "Host evidence status")

    def test_smoke_record_requires_each_field(self):
        required_fields = (
            "Agent host name and version",
            "Comic Sol commit or version",
            "Installation target and scope",
            "Filesystem capability",
            "Shell/tool-execution capability",
            "Image-generation route",
            "Portable-handoff route",
            "Output evidence",
            "Execution date supplied by the tester",
            "Known limitations",
        )
        for field in required_fields:
            self.assertRegex(self.format, rf"(?m)^- \*\*{re.escape(field)}:\*\*")

    # Per-host name → supported slug used to discover its retained record under
    # docs/agent-host-smoke/. The slug is the stem of the record Markdown file.
    _HOST_RECORD_SLUG = {
        "Codex": "codex",
        "Claude Code": "claude-code",
        "Google Antigravity": "google-antigravity",
        "ZCode": "zcode",
    }
    _HOST_RECORD_REQUIRED_FIELDS = (
        "Agent host name and version",
        "Comic Sol commit or version",
        "Installation target and scope",
        "Filesystem capability",
        "Shell/tool-execution capability",
        "Image-generation route",
        "Portable-handoff route",
        "Output evidence",
        "Execution date supplied by the tester",
        "Known limitations",
    )
    _DURABLE_EVIDENCE_PHRASES = (
        "durable",
        "no durable",
        "not available",
        "experimental",
    )

    @classmethod
    def _host_status_rows(cls):
        return {
            cells[0]: cells[1:]
            for line in cls.status.splitlines()
            if line.startswith("|")
            for cells in [[cell.strip() for cell in line.strip("|").split("|")]]
            if cells and cells[0] in cls._HOST_RECORD_SLUG
        }

    def test_all_named_hosts_are_explicitly_experimental(self):
        rows = self._host_status_rows()
        self.assertEqual(set(self._HOST_RECORD_SLUG), set(rows))
        smoke_dir = ROOT / "docs" / "agent-host-smoke"
        for host, cells in rows.items():
            with self.subTest(host=host):
                self.assertEqual("Experimental", cells[0], f"{host} status must be Experimental")
                evidence_cell = cells[1]
                # Two permitted states for an Experimental row:
                # 1. "No retained live smoke record" → there is no linked Markdown file.
                # 2. A repository-relative Markdown link to a retained record that
                #    is verified to exist, names this host, includes every required
                #    field, and states an explicit durable-evidence limitation.
                if evidence_cell == "No retained live smoke record":
                    # If a record for this host exists anyway, the cell is wrong.
                    candidate = smoke_dir / f"{self._HOST_RECORD_SLUG[host]}-*.md"
                    if any(candidate.parent.glob(candidate.name)):
                        self.fail(
                            f"{host} row says 'No retained live smoke record' but a "
                            f"record exists matching {candidate}"
                        )
                    # Also reject bogus file existence for the "no record" state.
                    if any(
                        (ROOT / "docs").glob(
                            f"agent-host-smoke/{self._HOST_RECORD_SLUG[host]}-*.md"
                        )
                    ):
                        self.fail(
                            f"{host} row says 'No retained live smoke record' but a "
                            f"record exists under docs/agent-host-smoke/"
                        )
                    continue
                # Otherwise the cell MUST be a repository-relative Markdown link.
                link_match = re.search(r"\]\(([^)]+\.md)\)", evidence_cell)
                self.assertIsNotNone(
                    link_match,
                    f"{host} evidence cell must link a .md record: {evidence_cell!r}",
                )
                assert link_match is not None  # mypy narrowing after assertIsNotNone
                link_target = link_match.group(1)
                self.assertFalse(
                    link_target.startswith(("http://", "https://")),
                    f"{host} evidence cell must link a repository-relative record",
                )
                record_path = (ROOT / "docs" / link_target).resolve()
                self.assertTrue(
                    record_path.is_file(),
                    f"{host} record not found at {record_path}",
                )
                # Reject the previously-loosened bare "Retained ..." prefix without
                # a valid link — keep this explicit so the assertion can never
                # silently regress to a meaningless prefix check.
                record_text = record_path.read_text(encoding="utf-8")
                for field in self._HOST_RECORD_REQUIRED_FIELDS:
                    # Accept an optional parenthetical annotation after the field
                    # name (e.g. "Known limitations (exact missing evidence):").
                    self.assertRegex(
                        record_text,
                        rf"(?m)^\s*-\s*\*\*{re.escape(field)}(?:\s*\([^)]*\))?:\*\*",
                        f"{host} record missing required field {field!r}",
                    )
                # The record must name the host.
                normalized_record = collapsed(record_text)
                self.assertIn(host, normalized_record, f"{host} record must name {host}")
                # The record must state a durable-evidence limitation explicitly.
                lowered = normalized_record.lower()
                self.assertTrue(
                    any(phrase in lowered for phrase in self._DURABLE_EVIDENCE_PHRASES),
                    f"{host} record must state an explicit durable-evidence limitation",
                )

    def test_retained_evidence_cells_cannot_be_arbitrary_prefixes(self):
        """The contract is the linked record plus its durable-evidence status, not a
        free-form string starting with "Retained ". Lock the cell to a Markdown link
        so a future copy-paste cannot re-loosen the check."""
        rows = self._host_status_rows()
        for host, cells in rows.items():
            with self.subTest(host=host):
                evidence_cell = cells[1]
                if evidence_cell == "No retained live smoke record":
                    continue
                # Must end in a markdown link to a .md file (with optional preceding
                # descriptive text). The check is exact: no plain "Retained ..." text
                # without a verifiable link.
                self.assertRegex(
                    evidence_cell,
                    r"\[.+\]\([^)]+\.md\)",
                    f"{host} cell must end in a Markdown link to a .md record",
                )

    def test_no_host_is_fabricated_as_verified(self):
        self.assertNotRegex(self.status, r"(?im)^\|[^\n]+\|\s*Verified\s*\|")
        normalized = collapsed(self.document)
        self.assertIn("No named host is currently verified", normalized)
        self.assertIn(
            "Path-copy tests and installer tests are not live host verification", normalized
        )

    def test_provider_and_host_support_are_separate(self):
        normalized = collapsed(self.document)
        self.assertIn("Provider support and host support are separate", normalized)
        self.assertIn("does not verify the agent host", normalized)

    def test_generated_output_can_be_retained_outside_the_repository(self):
        normalized = collapsed(self.format)
        self.assertIn("generated projects and build output must not be committed", normalized)
        self.assertIn("durable, access-controlled external location", normalized)
        self.assertIn("immutable artifact or record", normalized)

    def test_verification_requires_successful_live_observations(self):
        normalized = collapsed(self.threshold)
        for phrase in (
            "read and wrote the test project",
            "successfully invoked the documented shell or tool-execution route",
            "full portable handoff through result intake",
            "accepted the resulting local raster",
            "completed deterministic validation and export",
            "remain inspectable through durable links",
        ):
            self.assertIn(phrase, normalized, phrase)
        self.assertIn("Partial and blocked records", normalized)
        self.assertIn("remains **Experimental**", normalized)


class DocumentationReachabilityTests(unittest.TestCase):
    """The new contracts are reachable without disturbing the creator-path order."""

    def test_readme_links_both_contracts_before_advanced_integrations(self):
        readme = read("README.md")
        advanced = readme.index("## Advanced integrations")
        self.assertIn("docs/showcase.md", readme[:advanced])
        self.assertIn("docs/agent-host-smoke.md", readme[:advanced])

    def test_sample_catalog_links_the_showcase_contract(self):
        self.assertIn("../docs/showcase.md", read("samples/README.md"))


if __name__ == "__main__":
    unittest.main()
