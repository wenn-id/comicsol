import ast
import tomllib
import unittest
from pathlib import Path

from scripts.stage_registry import RESUME_STAGES


class _OSReplaceCallVisitor(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.scope: list[str] = []
        self.calls: dict[str, int] = {}
        self.os_aliases = {"os"}
        self.replace_aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.os_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "os"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                self.replace_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "replace"
                )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        target = node.func
        os_replace = (
            isinstance(target, ast.Attribute)
            and target.attr == "replace"
            and isinstance(target.value, ast.Name)
            and target.value.id in self.os_aliases
        )
        imported_replace = isinstance(target, ast.Name) and target.id in self.replace_aliases
        if os_replace or imported_replace:
            scope = ".".join(self.scope) or "<module>"
            self.calls[scope] = self.calls.get(scope, 0) + 1
        self.generic_visit(node)


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

    def test_article_six_exceptions_are_scoped_and_behavior_tested(self):
        article_six = self.collapsed.split(
            "## Article 6 — Deterministic behavior and atomic writes", 1
        )[1].split("## Article 7 —", 1)[0]
        for phrase in (
            "`project.json` lifecycle fields `created_at` and `updated_at` may record the UTC time",
            "excluded from stage-cache identity",
            "deterministic stage artifacts must remain byte-identical across those updates",
            "Direct `os.replace()` calls are limited to these tested publication paths",
            "Every new direct call site requires a failure-path regression test",
            "comic_sol_product/setup.py",
            "comic_sol_product/skill_install.py",
            "comic_sol_product/portable.py",
        ):
            self.assertIn(phrase, article_six)

        expected_calls = {
            "comic_sol_product/portable.py": {"create_portable_archive": 1},
            "comic_sol_product/setup.py": {
                "_ConfigDirectory.replace": 2,
                "_atomic_write": 1,
                "_publish_expected": 2,
            },
            "comic_sol_product/skill_install.py": {
                "_publish_new": 1,
                "_exchange_paths": 7,
                "install_skill": 2,
                "uninstall_skill": 1,
            },
        }
        actual_calls = {}
        source_root = self.root / "comic_sol_product"
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            visitor = _OSReplaceCallVisitor(tree)
            visitor.visit(tree)
            if visitor.calls:
                relative = path.relative_to(self.root).as_posix()
                actual_calls[relative] = visitor.calls
        self.assertEqual(expected_calls, actual_calls)

        required_tests = {
            "tests/test_resume.py": {
                "test_lifecycle_timestamps_do_not_change_artifact_bytes_or_cache_identity",
            },
            "tests/test_client_setup.py": {
                "test_atomic_write_replace_failure_preserves_existing_config",
                "test_directory_relative_replace_failure_preserves_existing_config",
                "test_atomic_publish_rejects_concurrent_entry_and_preserves_it",
            },
            "tests/test_skill_install.py": {
                "test_new_publication_os_replace_failure_leaves_no_destination_or_staging",
                "test_windows_exchange_fallback_rolls_back_after_second_rename_failure",
                "test_failed_post_publish_verification_rolls_back_upgrade",
                "test_uninstall_detach_replace_failure_preserves_complete_install",
            },
            "tests/test_distribution.py": {
                "test_portable_archive_publish_failure_preserves_existing_archive",
            },
        }
        for relative, expected_methods in required_tests.items():
            tree = ast.parse((self.root / relative).read_text(encoding="utf-8"))
            actual_methods = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            missing = expected_methods - actual_methods
            self.assertFalse(missing, f"{relative} is missing {sorted(missing)}")

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
            "exactly 18",
        ):
            self.assertIn(phrase, self.collapsed)
        self.assertIn("exactly 18", " ".join(self.readme.split()))

    def test_mcp_surface_still_registers_exactly_eighteen_tools(self):
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

        self.assertEqual(18, len(tool_names), sorted(tool_names))
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

    def test_reference_integrations_are_in_lint_and_coverage_gates(self):
        project = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("integrations", project["tool"]["coverage"]["run"]["source"])
        selected_rules = project["tool"]["ruff"]["lint"]["select"]
        self.assertIn("B", selected_rules)
        self.assertIn("C90", selected_rules)

        workflow = (self.root / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        for command in (
            "python scripts/check_ruff_baseline.py",
            "python -m ruff format --check scripts comic_sol_product tests integrations",
        ):
            self.assertIn(command, workflow)

        baseline_checker = (self.root / "scripts/check_ruff_baseline.py").read_text(
            encoding="utf-8"
        )
        for target in ('"scripts"', '"comic_sol_product"', '"tests"', '"integrations"'):
            self.assertIn(target, baseline_checker)

        contributing = (self.root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn(
            ".venv/bin/python scripts/check_ruff_baseline.py",
            contributing,
        )
        self.assertIn(
            ".venv/bin/python -m ruff format --check scripts comic_sol_product tests integrations",
            contributing,
        )

    def test_constitution_defers_to_human_review_and_existing_policy(self):
        for phrase in (
            "does not replace",
            "SKILL.md",
            "the stricter policy wins",
        ):
            self.assertIn(phrase, self.collapsed)


if __name__ == "__main__":
    unittest.main()
