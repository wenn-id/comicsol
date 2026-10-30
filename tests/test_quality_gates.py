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
    "comic_sol_product/clients.py",
    "comic_sol_product/setup.py",
    "comic_sol_product/mcp.py",
    "scripts/stage_registry.py",
    "comic_sol_product/release.py",
    "scripts/release_qualification.py",
    "scripts/release_evidence.py",
    "scripts/release_visual_gate.py",
    "comic_sol_product/version.py",
    "comic_sol_product/errors.py",
    "comic_sol_product/config.py",
    "comic_sol_product/distribution.py",
    "comic_sol_product/engine_api.py",
    "comic_sol_product/skill_install.py",
    "comic_sol_product/__init__.py",
    "comic_sol_product/cli_command_spec.py",
    "scripts/__init__.py",
    "scripts/assemble_release.py",
    "scripts/benchmark_summary.py",
    "scripts/build_portable.py",
    "scripts/character_identity.py",
    "scripts/check_coverage.py",
    "scripts/check_ruff_baseline.py",
    "scripts/clean_install_smoke.py",
    "scripts/command_service.py",
    "scripts/container_runtime_audit.py",
    "scripts/container_sbom.py",
    "scripts/dogfood_report.py",
    "scripts/dogfood_summary.py",
    "scripts/export_pdf.py",
    "scripts/font_cmap.py",
    "scripts/font_coverage.py",
    "scripts/handoff.py",
    "scripts/handoff_archive.py",
    "scripts/input_limits.py",
    "scripts/installed_mcp_smoke.py",
    "scripts/layouts.py",
    "scripts/letter_panels.py",
    "scripts/lifecycle_contracts.py",
    "scripts/live_visual_evidence.py",
    "scripts/materialize_sample.py",
    "scripts/mcp_server.py",
    "scripts/normalize_panels.py",
    "scripts/pdf_quality.py",
    "scripts/portable_release_smoke.py",
    "scripts/quality_records.py",
    "scripts/quality_sample.py",
    "scripts/raster_limits.py",
    "scripts/reference_strategy.py",
    "scripts/release_identity.py",
    "scripts/render_report.py",
    "scripts/repair_strategy.py",
    "scripts/schema.py",
    "scripts/sfx_repair.py",
    "scripts/sfx_verification.py",
    "scripts/starter_templates.py",
    "scripts/sync_plugin_bundle.py",
    "scripts/typography.py",
    "scripts/version.py",
]


class QualityGateContractTests(unittest.TestCase):
    def test_pyproject_declares_enforced_quality_policy(self):
        config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

        ruff = config["tool"]["ruff"]
        self.assertEqual(ruff["target-version"], "py311")
        self.assertIn("line-length", ruff)
        self.assertIn("lint", ruff)
        self.assertIn("format", ruff)
        self.assertIn("B", ruff["lint"]["select"])
        self.assertIn("C90", ruff["lint"]["select"])
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
        self.assertEqual(
            ["comic_sol_product", "integrations", "scripts"],
            coverage["run"]["source"],
        )
        self.assertNotIn("tests", coverage["run"]["source"])

    def test_workflow_runs_blocking_quality_tools_and_publishes_coverage(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", workflow)
        self.assertIn("requirements/locks/quality-linux-x86_64.txt", workflow)
        self.assertIn("name: Quality gates", workflow)
        self.assertIn("python scripts/check_ruff_baseline.py", workflow)
        self.assertIn("python -m ruff format --check", workflow)
        self.assertIn("python -m mypy", workflow)
        self.assertIn("python -m mypy --platform win32 --show-error-codes", workflow)
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
        self.assertIn('"check_ruff_baseline.py"', SETUP.read_text(encoding="utf-8"))

    def test_release_caller_gates_the_exact_sha_without_a_relaxed_mode(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/tests.yml", release)
        self.assertIn("candidate_sha: ${{ needs.prepare.outputs.sha }}", release)
        self.assertNotIn("blocking_quality", release)


if __name__ == "__main__":
    unittest.main()
