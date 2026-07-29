"""Build adapter that bundles the existing engine without duplicating source files."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent
BUILD_ONLY_SCRIPTS = {
    "build_portable.py",
    "clean_install_smoke.py",
    "installed_mcp_smoke.py",
}


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
        for source in sorted((ROOT / "scripts").glob("*.py")):
            if source.name in BUILD_ONLY_SCRIPTS:
                continue
            shutil.copy2(source, engine_root / source.name)
        for directory in ("assets", "templates"):
            destination = package_root / directory
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(ROOT / directory, destination)

        skill_root = package_root / "skill"
        skill_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "SKILL.md", skill_root / "SKILL.md")
        references = skill_root / "references"
        if references.exists():
            shutil.rmtree(references)
        shutil.copytree(ROOT / "references", references)


setup(cmdclass={"build_py": build_py})
