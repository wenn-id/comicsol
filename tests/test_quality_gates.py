from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from comic_sol_product.runtime_contract import RUNTIME_SCRIPTS


ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
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
    "comic_sol_product/portable.py",
    "comic_sol_product/runtime_contract.py",
    "comic_sol_product/skill_payload.py",
    "comic_sol_product/engine_api.py",
    "comic_sol_product/skill_install.py",
    "comic_sol_product/__init__.py",
    "comic_sol_product/cli_command_spec.py",
    "comic_sol_product/engine/__init__.py",
    "integrations/comfyui-local/comfyui_executor.py",
    "scripts/assemble_release.py",
    "scripts/benchmark.py",
    "scripts/benchmark_summary.py",
    "scripts/build_examples.py",
    "scripts/build_portable.py",
    "scripts/character_identity.py",
    "scripts/character_quality.py",
    "scripts/check_coverage.py",
    "scripts/check_ruff_baseline.py",
    "scripts/clean_install_smoke.py",
    "scripts/comic_sol.py",
    "scripts/command_service.py",
    "scripts/compose_pages.py",
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
    "scripts/page_quality.py",
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
    "scripts/validate_project.py",
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
        self.assertEqual("silent", mypy["follow_imports"])
        self.assertTrue(mypy["ignore_missing_imports"])
        self.assertIn("python_version", mypy)

        coverage = config["tool"]["coverage"]
        self.assertTrue(coverage["run"]["branch"])
        self.assertEqual(
            ["comic_sol_product", "integrations", "scripts"],
            coverage["run"]["source"],
        )
        self.assertNotIn("tests", coverage["run"]["source"])

    def test_cross_platform_inputs_have_explicit_lf_checkout_rules(self):
        """Require LF rules and LF checkouts for Windows-sensitive inputs."""
        attributes: dict[str, set[str]] = {}
        for raw_line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
            line = raw_line.partition("#")[0].strip()
            if not line:
                continue
            pattern, *values = line.split()
            attributes[pattern] = set(values)

        patterns = ("*.sh", "*.ps1", "*.tmpl", "Dockerfile", "*.toml", "*.in", "*.spec")
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertTrue({"text", "eol=lf"} <= attributes.get(pattern, set()))

        paths = (
            "Dockerfile",
            "MANIFEST.in",
            "installers/install.ps1",
            "installers/install.sh",
            "packaging/comic-sol.spec",
            "pyproject.toml",
            "requirements/audit.in",
            "requirements/base.in",
            "requirements/quality.in",
            "requirements/release.in",
            "requirements/runtime.in",
            "skills/comic-sol/templates/qa-report.md.tmpl",
            "templates/qa-report.md.tmpl",
        )
        for relative in paths:
            with self.subTest(path=relative):
                self.assertNotIn(b"\r\n", (ROOT / relative).read_bytes())

    def test_workflow_runs_blocking_quality_tools_and_publishes_coverage(self):
        """Require the blocking quality tools and retained coverage output."""
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", workflow)
        self.assertIn("requirements/locks/quality-linux-x86_64.txt", workflow)
        self.assertIn("name: Quality gates", workflow)
        self.assertIn("python scripts/check_ruff_baseline.py", workflow)
        self.assertIn("python -m ruff check .github/scripts", workflow)
        self.assertIn("python -m ruff format --check", workflow)
        self.assertIn(
            "python -m ruff format --check scripts comic_sol_product tests integrations .github/scripts",
            workflow,
        )
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
        self.assertNotIn("check_coverage.py", RUNTIME_SCRIPTS)
        self.assertNotIn("check_ruff_baseline.py", RUNTIME_SCRIPTS)

    def test_advertised_python_minors_run_full_suite_with_hash_locked_runtime(self):
        """Keep explicitly advertised interpreter minors in a locked full-suite job."""
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
        advertised_minors = {
            classifier.rsplit(" :: ", 1)[1]
            for classifier in project["classifiers"]
            if classifier.startswith("Programming Language :: Python :: 3.")
        }
        newer_minors = advertised_minors - {"3.11"}
        self.assertTrue(newer_minors)

        workflow = WORKFLOW.read_text(encoding="utf-8")
        job_start = workflow.index("  test-supported-python-minors:\n")
        job_end = workflow.index("\n  quality:\n", job_start)
        compatibility_job = workflow[job_start:job_end]

        self.assertIn("python-version: ${{ matrix.python-version }}", compatibility_job)
        self.assertIn(
            "python -m pip install --require-hashes -r requirements/locks/runtime-linux-x86_64.txt",
            compatibility_job,
        )
        self.assertIn('COMIC_SOL_REQUIRE_SYMLINK_TESTS: "1"', compatibility_job)
        self.assertIn("python -m unittest discover -s tests -v", compatibility_job)
        for minor in sorted(newer_minors):
            with self.subTest(python_minor=minor):
                self.assertIn(f'"{minor}"', compatibility_job)

    def test_workflow_checkout_refs_are_scoped_to_event_type(self):
        """Keep candidate pins separate from direct push and pull request refs."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        release_workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        checkout_steps = workflow.count("uses: actions/checkout@")
        candidate_steps = workflow.count("if: inputs.candidate_sha != ''")
        event_steps = workflow.count("if: inputs.candidate_sha == ''")

        self.assertEqual(2 * candidate_steps, checkout_steps)
        self.assertEqual(candidate_steps, event_steps)
        self.assertEqual(candidate_steps, workflow.count("ref: ${{ inputs.candidate_sha }}"))
        self.assertEqual(event_steps, workflow.count("ref: ${{ github.sha }}"))
        self.assertIn("  workflow_call:\n    inputs:\n      candidate_sha:\n", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("inputs.candidate_sha || github.sha", workflow)
        self.assertIn("candidate_sha: ${{ needs.prepare.outputs.sha }}", release_workflow)

    def test_release_caller_gates_the_exact_sha_without_a_relaxed_mode(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/tests.yml", release)
        self.assertIn("candidate_sha: ${{ needs.prepare.outputs.sha }}", release)
        self.assertNotIn("blocking_quality", release)


if __name__ == "__main__":
    unittest.main()
