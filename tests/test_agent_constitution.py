import ast
import unittest
from pathlib import Path

from scripts.stage_registry import RESUME_STAGES


class AgentConstitutionTests(unittest.TestCase):
    """Guard the agent development contract documented in AGENTS.md."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.path = cls.root / "AGENTS.md"
        cls.text = cls.path.read_text(encoding="utf-8")
        # Collapse wrapping so prose assertions survive reflowed paragraphs.
        cls.collapsed = " ".join(cls.text.split())
        cls.readme = (cls.root / "README.md").read_text(encoding="utf-8")

    def test_constitution_exists_at_repository_root(self):
        self.assertTrue(self.path.is_file(), self.path)

    def test_every_required_article_is_present(self):
        for heading in (
            "## Article 1 — Project schemas never change silently",
            "## Article 2 — User projects are never deleted",
            "## Article 3 — Provider credentials and SDKs stay outside the engine",
            "## Article 4 — Path containment and security invariants hold",
            "## Article 5 — Behavior changes require regression tests",
            "## Article 6 — Deterministic behavior and atomic writes",
            "## Article 7 — Resumability and public JSON compatibility survive",
            "## Article 8 — Full verification before claiming completion",
            "## Article 9 — New product surfaces require adoption evidence or a waiver",
        ):
            self.assertIn(heading, self.collapsed)

    def test_schema_compatibility_rules_name_the_real_gate(self):
        for phrase in (
            "scripts/schema.py",
            "PROJECT_MIGRATIONS",
            "SUPPORTED_PROJECT_SCHEMA_VERSIONS",
            "UnsupportedSchemaVersionError",
            "schema_version",
            "Reading a project never mutates it",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_project_preservation_rules_cover_uninstall_and_upgrade(self):
        for phrase in (
            "comic_sol_product/setup.py",
            "comic_sol_product/clients.py",
            "never delete, move, or rewrite a user project directory",
            "idempotent and safe to repeat",
            "unsupported",
            "docs/install.md",
            "docs/releases/v2.0-stable-criteria.md",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_provider_boundary_keeps_credentials_and_sdks_out(self):
        for phrase in (
            "must never import a provider SDK",
            "read a provider credential",
            "sanitized",
            "requirements/locks/",
            "BLOCKED",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_path_containment_rules_reference_the_trust_boundary(self):
        for phrase in (
            "scripts/project_io.py",
            "contained_project_path()",
            "open_path_nofollow()",
            "reparse point",
            "MAX_SOURCE_BYTES",
            "COMIC_SOL_REQUIRE_SYMLINK_TESTS",
            "Containment is not authentication",
            "SECURITY.md",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_regression_test_requirement_is_explicit(self):
        for phrase in (
            "fails before the fix and passes after it",
            "Never weaken a validation threshold",
            "before/after render",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_determinism_and_atomic_write_requirements_are_explicit(self):
        for phrase in (
            "Identical inputs produce identical bytes",
            "scripts/core_primitives.py",
            "durable_atomic_write()",
            "ProjectTransaction",
            "ProjectLock",
            "ProjectTransaction.recover()",
            "sampling stays disabled",
            "all-or-nothing",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_resume_stage_names_match_the_stage_registry(self):
        for stage in RESUME_STAGES:
            self.assertIn(f"`{stage}`", self.collapsed)
        self.assertIn("scripts/stage_registry.py", self.collapsed)

    def test_public_json_contract_matches_documented_surface(self):
        for phrase in (
            "`ok`, `command`, `data`, `error`",
            "data.ready",
            "data.healthy",
            "stderr",
            "comic_sol_product/errors.py",
            "CS-<NAMESPACE>-<NNN>",
            "exactly 17",
        ):
            self.assertIn(phrase, self.collapsed)
        self.assertIn("exactly 17", " ".join(self.readme.split()))

    def test_mcp_surface_still_registers_exactly_seventeen_tools(self):
        source = (self.root / "scripts/mcp_server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        tool_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }

        self.assertEqual(17, len(tool_names), sorted(tool_names))
        self.assertTrue(all(name.startswith("comic_") for name in tool_names))

    def test_verification_gates_match_contributing(self):
        for phrase in (
            "CONTRIBUTING.md",
            "unittest discover -s tests",
            "comic_sol_product.release",
            "sync_plugin_bundle.py --check",
            "Never describe an unrun check as passing",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_constitution_defers_to_human_review_and_existing_policy(self):
        for phrase in (
            "does not replace",
            "SKILL.md",
            "the stricter policy wins",
        ):
            self.assertIn(phrase, self.collapsed)

    def test_surface_freeze_cannot_be_waived_by_an_agent(self):
        for phrase in (
            "new distribution, installation, integration, or execution surface",
            "published adoption summary",
            "named maintainer",
            "recorded in both the relevant issue and pull request",
            "cannot infer a waiver",
            "cannot self-authorize a waiver",
            "cannot treat its own comment as maintainer approval",
        ):
            self.assertIn(phrase, self.collapsed)


if __name__ == "__main__":
    unittest.main()
