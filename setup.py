"""Build adapter that bundles the existing engine without duplicating source files."""

from __future__ import annotations

import shutil
import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

ROOT = Path(__file__).resolve().parent
REQUIRED_RUNTIME_SCRIPTS = runpy.run_path(ROOT / "runtime_contract.py")["REQUIRED_RUNTIME_SCRIPTS"]
BUILD_ONLY_SCRIPTS = {
    "assemble_release.py",
    "benchmark.py",
    "benchmark_summary.py",
    "build_portable.py",
    "check_coverage.py",
    "clean_install_smoke.py",
    "dogfood_summary.py",
    "installed_mcp_smoke.py",
    "live_visual_evidence.py",
    "portable_release_smoke.py",
    "release_identity.py",
}
# Byte-compilation output is environment-generated and must never enter the
# packaged Skill payload, or the packaged bundle stops matching its source.
_IGNORE_GENERATED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


class build_py(_build_py):
    """Copy canonical engine modules and runtime assets into the wheel build tree."""

    def run(self) -> None:
        super().run()
        package_root = Path(self.build_lib) / "comic_sol_product"
        engine_root = package_root / "engine"
        engine_root.mkdir(parents=True, exist_ok=True)
        (engine_root / "__init__.py").write_text(
            '"""Bundled deterministic Comic Sol engine."""\n', encoding="utf-8"
        )
        # Runtime modules, including portable handoff and local dogfood report
        # support, are discovered from canonical scripts rather than duplicated.
        script_names = {source.name for source in (ROOT / "scripts").glob("*.py")}
        missing_runtime = sorted(REQUIRED_RUNTIME_SCRIPTS - script_names)
        if missing_runtime:
            raise FileNotFoundError(
                "required runtime scripts are missing: " + ", ".join(missing_runtime)
            )
        for source in sorted((ROOT / "scripts").glob("*.py")):
            if source.name in BUILD_ONLY_SCRIPTS:
                continue
            shutil.copy2(source, engine_root / source.name)
        for directory in ("assets", "templates"):
            destination = package_root / directory
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(ROOT / directory, destination)

        # The Skill payload is the one already-synchronized canonical bundle,
        # copied verbatim. Never reassemble a second payload from independently
        # selected root files: that is how the two copies drift apart.
        skill_root = package_root / "skill"
        if skill_root.exists():
            shutil.rmtree(skill_root)
        canonical_skill = ROOT / "skills" / "comic-sol"
        if not (canonical_skill / "SKILL.md").is_file():
            raise FileNotFoundError(
                "canonical Agent Skill bundle is missing: skills/comic-sol/SKILL.md"
            )
        shutil.copytree(canonical_skill, skill_root, ignore=_IGNORE_GENERATED)


class sdist(_sdist):
    """Ship the canonical Skill bundle in the source distribution unchanged."""

    def make_release_tree(self, base_dir, files) -> None:
        super().make_release_tree(base_dir, files)
        canonical_skill = ROOT / "skills" / "comic-sol"
        if not (canonical_skill / "SKILL.md").is_file():
            raise FileNotFoundError(
                "canonical Agent Skill bundle is missing: skills/comic-sol/SKILL.md"
            )
        destination = Path(base_dir) / "skills" / "comic-sol"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(canonical_skill, destination, ignore=_IGNORE_GENERATED)


setup(cmdclass={"build_py": build_py, "sdist": sdist})
