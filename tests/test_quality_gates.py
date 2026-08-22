from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


class QualityGateContractTests(unittest.TestCase):
    def test_pyproject_declares_ruff_and_mypy_baselines(self):
        config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

        ruff = config["tool"]["ruff"]
        self.assertEqual(ruff["target-version"], "py311")
        self.assertIn("line-length", ruff)
        self.assertIn("lint", ruff)
        self.assertIn("format", ruff)

        mypy = config["tool"]["mypy"]
        self.assertEqual(mypy["files"], ["scripts/project_io.py"])
        self.assertIn("python_version", mypy)

    def test_workflow_runs_quality_tools_and_publishes_coverage(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", workflow)
        self.assertIn("requirements/locks/quality-linux-x86_64.txt", workflow)
        self.assertIn("ruff check", workflow)
        self.assertIn("ruff format --check", workflow)
        self.assertIn("mypy", workflow)
        self.assertIn("coverage run", workflow)
        self.assertIn("coverage report", workflow)
        self.assertIn("coverage.xml", workflow)
        self.assertIn("actions/upload-artifact@", workflow)
        self.assertIn("workflow_call:", workflow)
        self.assertIn("blocking_quality:", workflow)
        self.assertIn("continue-on-error: ${{ !inputs.blocking_quality }}", workflow)
        self.assertIn("coverage-baseline", workflow)

    def test_release_caller_enables_blocking_quality_for_exact_sha(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("uses: ./.github/workflows/tests.yml", release)
        self.assertIn("candidate_sha: ${{ needs.prepare.outputs.sha }}", release)
        self.assertIn("blocking_quality: true", release)


if __name__ == "__main__":
    unittest.main()
