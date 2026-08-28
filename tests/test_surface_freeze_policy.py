"""Structural contract tests for human-reviewed surface-freeze governance."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    path = ROOT / relative
    return path.read_text(encoding="utf-8") if path.exists() else ""


def collapsed(relative: str) -> str:
    return " ".join(read(relative).split())


class SurfaceFreezePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = collapsed("docs/surfaces.md")
        cls.policy_lower = cls.policy.lower()
        cls.contributing = collapsed("CONTRIBUTING.md")
        cls.contributing_lower = cls.contributing.lower()

    def test_defines_every_kind_of_new_surface(self):
        self.assertIn(
            "new distribution, installation, integration, or execution surface",
            self.policy_lower,
        )
        self.assertIn("requires exactly one", self.policy_lower)
        self.assertIn("published adoption summary", self.policy_lower)
        self.assertIn("explicit waiver", self.policy_lower)

    def test_existing_surface_work_keeps_all_four_exceptions(self):
        for exception in ("security", "correctness", "compatibility", "maintenance"):
            self.assertRegex(
                self.policy_lower,
                rf"{exception}[^.]*existing surfaces[^.]*allowed",
                exception,
            )
        self.assertIn("without adoption evidence or a waiver", self.policy_lower)

    def test_real_adoption_gate_has_both_numeric_targets(self):
        self.assertRegex(
            self.policy_lower,
            r"at least 10 verified external (?:comic )?creators",
        )
        self.assertRegex(
            self.policy_lower,
            r"20[–-]50 valid, consented, non-duplicate real-project reports",
        )

    def test_real_adoption_gate_excludes_non_evidence(self):
        for exclusion in (
            "current tooling",
            "fixtures",
            "ci runs",
            "maintainers",
            "automated identities",
            "fabricated submissions",
            "deterministic samples",
        ):
            self.assertIn(exclusion, self.policy_lower, exclusion)
        self.assertIn("do not satisfy the adoption gate", self.policy_lower)

    def test_waiver_requires_named_maintainer_and_both_review_records(self):
        self.assertIn("named maintainer", self.policy_lower)
        self.assertRegex(
            self.policy_lower,
            r"waiver[^.]*recorded in both the relevant issue and (?:the )?pull request",
        )

    def test_agents_cannot_infer_or_self_authorize_waivers(self):
        for prohibition in (
            "cannot infer a waiver",
            "cannot self-authorize a waiver",
            "cannot treat its own comment as maintainer approval",
        ):
            self.assertIn(prohibition, self.policy_lower, prohibition)
        for actor in ("agent", "bot", "reviewer", "implementation tool"):
            self.assertIn(actor, self.policy_lower, actor)

    def test_approved_issue_boundaries_are_not_new_standalone_surfaces(self):
        for phrase in (
            "#245",
            "one universal, host-neutral agent skills portability initiative",
            "not a separate surface for every ai host",
            "#244",
            "one reference executor under the existing `external-tool` contract",
            "not a new standalone product surface",
        ):
            self.assertIn(phrase, self.policy_lower, phrase)

    def test_contributing_requires_the_same_exactly_one_review_decision(self):
        for phrase in (
            "new distribution, installation, integration, or execution surface",
            "exactly one",
            "published adoption summary",
            "named maintainer waiver",
            "both the relevant issue and pull request",
        ):
            self.assertIn(phrase, self.contributing_lower, phrase)

    def test_policy_is_human_review_governance_not_automated_enforcement(self):
        self.assertIn("governance for human review", self.policy_lower)
        for forbidden_enforcement in (
            "keyword scanner",
            "blocking ci grep",
            "bot",
            "network check",
            "automated waiver inference",
        ):
            self.assertIn(forbidden_enforcement, self.policy_lower, forbidden_enforcement)
        self.assertIn("must not be implemented", self.policy_lower)


class PullRequestTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = read(".github/pull_request_template.md")
        cls.template_lower = cls.template.lower()

    def test_asks_whether_the_change_adds_a_new_surface(self):
        self.assertIn("does this change add a new surface?", self.template_lower)
        self.assertIn(
            "distribution, installation, integration, or execution",
            self.template_lower,
        )

    def test_has_exactly_three_mutually_exclusive_surface_responses(self):
        self.assertIn("select exactly one", self.template_lower)
        checkboxes = re.findall(r"(?m)^- \[ \] (.+)$", self.template)
        self.assertEqual(3, len(checkboxes), checkboxes)
        choices = " ".join(checkboxes).lower()
        self.assertIn("n/a", choices)
        self.assertIn("published adoption summary", choices)
        self.assertIn("named maintainer waiver", choices)

    def test_not_applicable_response_requires_a_concrete_reason(self):
        na_choice = next(
            line for line in self.template.splitlines() if line.startswith("- [ ] N/A")
        )
        self.assertIn("concrete reason", na_choice.lower())

    def test_waiver_response_requires_issue_and_pr_records(self):
        waiver_choice = next(
            line
            for line in self.template.splitlines()
            if line.startswith("- [ ]") and "waiver" in line.lower()
        )
        waiver_lower = waiver_choice.lower()
        self.assertIn("named maintainer", waiver_lower)
        self.assertIn("issue", waiver_lower)
        self.assertIn("pull request", waiver_lower)
        self.assertGreaterEqual(waiver_lower.count("link"), 2)


if __name__ == "__main__":
    unittest.main()
