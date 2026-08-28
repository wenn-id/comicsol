"""Contract tests for the opt-in creator program and manual consent flow."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    path = ROOT / relative
    return path.read_text(encoding="utf-8") if path.exists() else ""


def collapsed(text: str) -> str:
    return " ".join(text.split())


def issue_form_items(document: str) -> dict[str, str]:
    """Return Issue Form body entries keyed by id without a YAML dependency."""
    starts = list(re.finditer(r"^  - type: [a-z_]+\s*$", document, flags=re.MULTILINE))
    items: dict[str, str] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(document)
        block = document[match.start() : end]
        item_id = re.search(r"^    id: ([a-z0-9_-]+)\s*$", block, flags=re.MULTILINE)
        if item_id:
            items[item_id.group(1)] = block
    return items


def field_is_required(block: str) -> bool:
    return bool(re.search(r"^    validations:\s*\n      required: true\s*$", block, re.MULTILINE))


def required_checkbox_labels(block: str) -> list[str]:
    return re.findall(
        r"^        - label: (.+?)\s*\n          required: true\s*$",
        block,
        flags=re.MULTILINE,
    )


class CreatorProgramGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guide = read("docs/dogfood.md")
        cls.normalized = collapsed(cls.guide)
        cls.lower = cls.normalized.lower()

    def test_targets_ten_external_creators(self):
        self.assertRegex(self.normalized, r"at least 10 external (?:comic )?creators")
        self.assertIn("real person creating comics", self.lower)

    def test_targets_twenty_to_fifty_real_project_reports(self):
        self.assertRegex(
            self.normalized,
            r"20[–-]50 valid, consented, non-duplicate real-project reports",
        )

    def test_current_status_is_insufficient_evidence(self):
        self.assertIn("insufficient-evidence", self.guide)
        self.assertIn("current status", self.lower)
        self.assertIn("no adoption or success claim", self.lower)

    def test_ineligible_identities_and_duplicates_are_excluded_independently(self):
        for exclusion in (
            "maintainer identities",
            "automated agents",
            "CI identities",
            "fixtures",
            "deterministic samples",
            "fabricated identities",
            "duplicate reports",
            "cohort alias",
        ):
            self.assertIn(exclusion.lower(), self.lower, exclusion)

    def test_creator_route_uses_normal_projects_and_supported_generation_routes(self):
        for step in (
            "normal story idea",
            "preferred native generator",
            "declared external adapter",
            "portable-handoff route",
            "first project creation",
            "verified PDF",
            "blocked recovery",
            "resume behavior",
        ):
            self.assertIn(step.lower(), self.lower, step)

    def test_preview_and_validation_are_local_and_precede_manual_submission(self):
        preview = self.guide.index("comic-sol dogfood preview")
        validate = self.guide.index("comic-sol dogfood validate")
        inspect = self.lower.index("inspect the report")
        submit = self.lower.index("submit manually")
        self.assertLess(preview, submit)
        self.assertLess(validate, submit)
        self.assertLess(inspect, submit)
        self.assertIn("local preview", self.lower)
        self.assertIn("local validation", self.lower)

    def test_counted_reports_require_a_stable_distinct_bounded_cohort_alias(self):
        self.assertIn("reports intended to count", self.lower)
        self.assertIn("must include `--cohort-alias`", self.lower)
        self.assertIn("stable and distinct for each creator", self.lower)
        self.assertIn("lowercase slug of at most 48 characters", self.lower)

        report_command = self.guide.split("comic-sol dogfood report", 1)[1].split("```", 1)[0]
        self.assertIn("--cohort-alias", report_command)

    def test_submission_is_manual_only(self):
        self.assertIn("manual submission only", self.lower)
        self.assertIn("dedicated github issue template", self.lower)
        for forbidden_service in ("no upload service", "no telemetry", "no network submission"):
            self.assertIn(forbidden_service, self.lower)

    def test_report_sharing_consent_is_explicit(self):
        self.assertIn("`consent.share_report`", self.guide)
        self.assertIn("explicit `share_report` consent", self.lower)
        self.assertIn("covers only the sanitized report", self.lower)

    def test_showcase_publication_consent_is_separate(self):
        for phrase in (
            "does not grant permission to publish the comic",
            "showcase publication requires a separate explicit consent",
            "rights-to-share confirmation",
            "consent for one purpose must never be inferred from the other",
        ):
            self.assertIn(phrase, self.lower, phrase)
        self.assertIn("showcase.md", self.guide)

    def test_every_prohibited_private_data_category_is_named(self):
        categories = (
            "comic sol project",
            "story/source text",
            "prompts or negative prompts",
            "images, page pngs, pdfs, or reference art",
            "credentials, api keys, cookies, tokens, account identifiers",
            "provider request/response bodies",
            "endpoints containing secrets",
            "filesystem paths or home directories",
            "raw logs, stack traces, exceptions, or unrelated diagnostics",
        )
        for category in categories:
            self.assertIn(category, self.lower, category)

    def test_sensitive_material_uses_support_or_private_security_instead(self):
        self.assertIn("[support](../support.md)", self.guide.lower())
        self.assertIn("[private security reporting](../security.md)", self.guide.lower())
        self.assertIn("do not submit it", self.lower)
        self.assertIn("public github issue", self.lower)

    def test_maintainer_validation_precedes_aggregation(self):
        for check in (
            "schema version",
            "explicit consent",
            "duplicate report digest",
            "creator/cohort eligibility",
            "bounded report contents",
            "absence of known privacy violations",
        ):
            self.assertIn(check, self.lower, check)
        validation = self.lower.index("maintainer validation")
        publication = self.lower.index("published aggregate json/markdown")
        self.assertLess(validation, publication)
        self.assertIn("no aggregate is published by this work package", self.lower)

    def test_aggregate_limitations_are_independent_and_explicit(self):
        for limitation in (
            "maintainer-supplied collection period",
            "collection method",
            "recruitment bias",
            "missing data",
            "host routes",
            "image-generation routes",
            "sample size",
            "explicit numerators and denominators",
            "deterministic mechanics",
            "retained live visual evidence",
            "opt-in creator adoption evidence",
        ):
            self.assertIn(limitation, self.lower, limitation)

    def test_does_not_claim_current_participation_or_a_satisfied_gate(self):
        for fabricated_claim in (
            r"(?:we|comic sol) (?:have|has) (?:already )?(?:recruited|collected)",
            r"10 external creators (?:have|already)",
            r"(?:20[–-]50|20 to 50) real projects (?:have|already)",
            r"(?:adoption|evidence) gate (?:is|has been) (?:met|satisfied)",
        ):
            self.assertNotRegex(self.lower, fabricated_claim)


class DogfoodIssueFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = read(".github/ISSUE_TEMPLATE/dogfood-report.yml")
        cls.lower = collapsed(cls.document).lower()
        cls.items = issue_form_items(cls.document)

    def test_has_supported_issue_form_metadata(self):
        self.assertRegex(self.document, r"(?m)^name: .+")
        self.assertRegex(self.document, r"(?m)^description: .+")
        self.assertRegex(self.document, r"(?m)^title: ['\"]?\[Dogfood report\]")
        self.assertRegex(self.document, r"(?m)^labels:\s*\n  - .+")
        self.assertIn("body:", self.document)

    def test_report_field_accepts_only_attachment_link_or_fenced_json(self):
        report = self.items["sanitized_report"]
        self.assertIn("attachment or stable link", report.lower())
        self.assertIn("fenced sanitized json", report.lower())
        self.assertTrue(field_is_required(report))

    def test_report_sharing_consent_is_a_required_checkbox(self):
        consent = self.items["share_report_consent"]
        labels = " ".join(required_checkbox_labels(consent)).lower()
        self.assertIn("consent.share_report", labels)
        self.assertIn("sanitized dogfood report", labels)
        self.assertTrue(field_is_required(consent))

    def test_privacy_acknowledgement_is_a_required_checkbox(self):
        privacy = self.items["privacy_acknowledgement"]
        labels = " ".join(required_checkbox_labels(privacy)).lower()
        self.assertIn("prohibited project or private materials", labels)
        self.assertTrue(field_is_required(privacy))

    def test_template_keeps_showcase_permission_separate(self):
        self.assertIn("showcase publication permission is separate", self.lower)
        self.assertNotIn("consent_to_showcase", self.document)
        self.assertNotIn("showcase_consent", self.document)
        consent = self.items["share_report_consent"].lower()
        self.assertNotIn("permission to publish", consent)

    def test_template_names_every_prohibited_private_data_category(self):
        for category in (
            "comic sol project",
            "story/source text",
            "prompts or negative prompts",
            "images, page pngs, pdfs, or reference art",
            "credentials, api keys, cookies, tokens, account identifiers",
            "provider request/response bodies",
            "endpoints containing secrets",
            "filesystem paths or home directories",
            "raw logs, stack traces, exceptions, or unrelated diagnostics",
        ):
            self.assertIn(category, self.lower, category)

    def test_template_links_support_and_private_security_reporting(self):
        self.assertIn(
            "https://github.com/wenn-id/comicsol/blob/main/support.md",
            self.document.lower(),
        )
        self.assertIn(
            "https://github.com/wenn-id/comicsol/blob/main/security.md",
            self.document.lower(),
        )
        self.assertIn("do not submit", self.lower)

    def test_template_does_not_request_sensitive_fields(self):
        requested_ids = set(self.items)
        prohibited_ids = {
            "story_title",
            "story",
            "source_text",
            "prompt",
            "negative_prompt",
            "character_name",
            "provider_credential",
            "api_key",
            "endpoint",
            "filesystem_path",
            "home_directory",
            "account_id",
            "raw_log",
            "stack_trace",
            "image_upload",
        }
        self.assertTrue(requested_ids.isdisjoint(prohibited_ids), requested_ids & prohibited_ids)
        for wording in (
            "enter your story title",
            "paste your prompt",
            "upload an image",
            "paste raw logs",
            "provider credential",
        ):
            self.assertNotIn(wording, self.lower)


class DogfoodDocumentationIntegrationTests(unittest.TestCase):
    def test_creator_guide_is_reachable_from_required_documents(self):
        expected_links = {
            "README.md": "docs/dogfood.md",
            "docs/onboarding.md": "dogfood.md",
            "docs/user/getting-started.md": "../dogfood.md",
            "samples/README.md": "../docs/dogfood.md",
            "CONTRIBUTING.md": "docs/dogfood.md",
        }
        for document, link in expected_links.items():
            self.assertIn(link, read(document), document)

    def test_privacy_defines_manual_opt_in_and_prohibited_data_boundary(self):
        privacy = collapsed(read("PRIVACY.md")).lower()
        for phrase in (
            "manual opt-in",
            "sanitized dogfood report",
            "no telemetry",
            "story/source text",
            "prompts",
            "images",
            "credentials",
            "provider request/response bodies",
            "filesystem paths",
            "raw logs",
        ):
            self.assertIn(phrase, privacy, phrase)

    def test_support_separates_ordinary_support_from_private_security(self):
        support = collapsed(read("SUPPORT.md")).lower()
        self.assertIn("ordinary support", support)
        self.assertIn("private security reporting", support)
        self.assertIn("do not submit", support)
        self.assertIn("dogfood", support)

    def test_samples_are_not_adoption_evidence(self):
        samples = collapsed(read("samples/README.md")).lower()
        self.assertIn("deterministic samples", samples)
        self.assertIn("maintainer samples", samples)
        self.assertIn("do not count as adoption evidence", samples)

    def test_showcase_cross_links_separate_dogfood_consent(self):
        showcase = collapsed(read("docs/showcase.md")).lower()
        self.assertIn("dogfood.md", showcase)
        self.assertIn("separate", showcase)
        self.assertIn("dogfood report-sharing consent", showcase)
        self.assertIn("must never be inferred", showcase)


if __name__ == "__main__":
    unittest.main()
