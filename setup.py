"""Build adapter that bundles the existing engine without duplicating source files."""

from __future__ import annotations

import runpy
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

ROOT = Path(__file__).resolve().parent
RUNTIME_SCRIPTS = runpy.run_path(ROOT / "comic_sol_product" / "runtime_contract.py")[
    "RUNTIME_SCRIPTS"
]
# Byte-compilation output is environment-generated and must never enter the
# packaged Skill payload, or the packaged bundle stops matching its source.
_IGNORE_GENERATED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


class build_py(_build_py):
    """Copy canonical engine modules and runtime assets into the wheel build tree."""

    def run(self) -> None:
        """Rebuild the engine package from the canonical runtime inventory."""
        super().run()
        package_root = Path(self.build_lib) / "comic_sol_product"
        engine_root = package_root / "engine"
        if engine_root.exists():
            shutil.rmtree(engine_root)
        engine_root.mkdir(parents=True, exist_ok=True)
        (engine_root / "__init__.py").write_text(
            '"""Bundled deterministic Comic Sol engine."""\n', encoding="utf-8"
        )
        script_root = ROOT / "scripts"
        missing_runtime = sorted(
            name for name in RUNTIME_SCRIPTS if not (script_root / name).is_file()
        )
        if missing_runtime:
            raise FileNotFoundError(
                "required runtime scripts are missing: " + ", ".join(missing_runtime)
            )
        for name in sorted(RUNTIME_SCRIPTS):
            shutil.copy2(script_root / name, engine_root / name)
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
