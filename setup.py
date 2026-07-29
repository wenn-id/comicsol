"""Build adapter that bundles the existing engine without duplicating source files."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent


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
            shutil.copy2(source, engine_root / source.name)
        for directory in ("assets", "templates"):
            destination = package_root / directory
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(ROOT / directory, destination)


setup(cmdclass={"build_py": build_py})
