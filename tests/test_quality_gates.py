from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SETUP = ROOT / "setup.py"
TYPED_FILES = [
    "scripts/project_io.py",
    "scripts/core_primitives.py",
    "comic_sol_product/cli.py",
    "comic_sol_product/setup.py",
    "comic_sol_product/mcp.py",
    "scripts/stage_registry.py",
    "comic_sol_product/release.py",
    "scripts/release_qualification.py",
    "scripts/release_evidence.py",
]


class QualityGateContractTests(unittest.TestCase):
    def test_pyproject_declares_enforced_quality_policy(self):
        config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

        ruff = config["tool"]["ruff"]
        self.assertEqual(ruff["target-version"], "py311")
        self.assertIn("line-length", ruff)
        self.assertIn("lint", ruff)
        self.assertIn("format", ruff)
        self.assertEqual(
            {
                "scripts/assemble_release.py": ["E402"],
                "scripts/container_sbom.py": ["E402"],
            },
            ruff["lint"]["per-file-ignores"],
        )

        mypy = config["tool"]["mypy"]
        self.assertEqual(TYPED_FILES, mypy["files"])
        self.assertEqual("skip", mypy["follow_imports"])
        self.assertTrue(mypy["ignore_missing_imports"])
        self.assertIn("python_version", mypy)

        coverage = config["tool"]["coverage"]
        self.assertTrue(coverage["run"]["branch"])
        self.assertEqual(["comic_sol_product", "scripts"], coverage["run"]["source"])
        self.assertNotIn("tests", coverage["run"]["source"])

    def test_workflow_runs_blocking_quality_tools_and_publishes_coverage(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", workflow)
        self.assertIn("requirements/locks/quality-linux-x86_64.txt", workflow)
        self.assertIn("name: Quality gates", workflow)
        self.assertIn("python -m ruff check", workflow)
        self.assertIn("python -m ruff format --check", workflow)
        self.assertIn("python -m mypy", workflow)
        self.assertIn("python -m coverage run", workflow)
        self.assertIn("python -m coverage json -o coverage.json", workflow)
        self.assertIn("python scripts/check_coverage.py coverage.json", workflow)
        self.assertIn("python -m coverage report", workflow)
        self.assertIn("coverage.xml", workflow)
        self.assertIn("actions/upload-artifact@", workflow)
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("blocking_quality", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn("quality-coverage", workflow)
        self.assertIn('"check_coverage.py"', SETUP.read_text(encoding="utf-8"))

    def test_release_caller_gates_the_exact_sha_without_a_relaxed_mode(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/tests.yml", release)
        self.assertIn("candidate_sha: ${{ needs.prepare.outputs.sha }}", release)
        self.assertNotIn("blocking_quality", release)


if __name__ == "__main__":
    unittest.main()
