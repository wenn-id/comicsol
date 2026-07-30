"""Distribution acceptance contracts for Comic Sol release artifacts."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable


REQUIRED_WHEEL_MEMBERS = frozenset(
    {
        "comic_sol_product/engine/comic_sol.py",
        "comic_sol_product/engine/quality_records.py",
        "comic_sol_product/engine/normalize_panels.py",
        "comic_sol_product/engine/typography.py",
        "comic_sol_product/engine/layouts.py",
        "comic_sol_product/engine/page_quality.py",
        "comic_sol_product/engine/pdf_quality.py",
        "comic_sol_product/assets/fonts/ComicNeue-Regular.ttf",
        "comic_sol_product/assets/fonts/ComicNeue-Bold.ttf",
        "comic_sol_product/templates/manifest.json",
        "comic_sol_product/skill/SKILL.md",
        "comic_sol_product/skill/references/workflow.md",
        "comic_sol_product/skill/references/visual-qa.md",
    }
)

REQUIRED_SDIST_SUFFIXES = frozenset(
    {
        "/SKILL.md",
        "/scripts/comic_sol.py",
        "/assets/fonts/ComicNeue-Regular.ttf",
        "/templates/manifest.json",
        "/references/workflow.md",
        "/comic_sol_product/cli.py",
    }
)

FORBIDDEN_WHEEL_MEMBERS = frozenset(
    {
        "comic_sol_product/engine/assemble_release.py",
        "comic_sol_product/engine/build_portable.py",
        "comic_sol_product/engine/clean_install_smoke.py",
        "comic_sol_product/engine/installed_mcp_smoke.py",
        "comic_sol_product/engine/portable_release_smoke.py",
    }
)


def validate_wheel_members(members: Iterable[str]) -> None:
    member_set = set(members)
    missing = sorted(REQUIRED_WHEEL_MEMBERS - member_set)
    if missing:
        raise ValueError("wheel is missing required members: " + ", ".join(missing))
    forbidden = sorted(FORBIDDEN_WHEEL_MEMBERS & member_set)
    if forbidden:
        raise ValueError("wheel contains build-only members: " + ", ".join(forbidden))


def validate_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        validate_wheel_members(archive.namelist())


def validate_sdist_members(members: Iterable[str]) -> None:
    names = set(members)
    missing = sorted(
        suffix
        for suffix in REQUIRED_SDIST_SUFFIXES
        if not any(name.endswith(suffix) for name in names)
    )
    if missing:
        raise ValueError("sdist is missing required members: " + ", ".join(missing))


def validate_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        validate_sdist_members(archive.getnames())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m comic_sol_product.release")
    parser.add_argument("artifacts", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    for artifact in arguments.artifacts:
        if artifact.suffix == ".whl":
            validate_wheel(artifact)
        elif artifact.name.endswith(".tar.gz"):
            validate_sdist(artifact)
        else:
            raise ValueError(f"unsupported distribution artifact: {artifact.name}")
        print(f"distribution-ok: {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
