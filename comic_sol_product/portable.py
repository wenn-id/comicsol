"""Portable-runtime archive creation and validation contracts."""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable

REQUIRED_RUNTIME_SUFFIXES = frozenset(
    {
        "comic-sol/_internal/comic_sol_product/assets/fonts/ComicNeue-Regular.ttf",
        "comic-sol/_internal/comic_sol_product/assets/fonts/ComicNeue-Bold.ttf",
        "comic-sol/_internal/comic_sol_product/templates/manifest.json",
        "comic-sol/_internal/comic_sol_product/skill/SKILL.md",
        "comic-sol/_internal/comic_sol_product/skill/references/workflow.md",
    }
)


def validate_runtime_members(members: Iterable[str]) -> None:
    names = {name.replace("\\", "/").rstrip("/") for name in members}
    missing = sorted(
        suffix
        for suffix in REQUIRED_RUNTIME_SUFFIXES
        if not any(name.endswith(suffix) for name in names)
    )
    executable_present = any(
        name.endswith("comic-sol/comic-sol") or name.endswith("comic-sol/comic-sol.exe")
        for name in names
    )
    if not executable_present:
        missing.append("comic-sol/comic-sol[.exe]")
    if missing:
        raise ValueError(
            "portable runtime is missing required members: " + ", ".join(sorted(set(missing)))
        )


def create_portable_archive(runtime_dir: Path, archive: Path) -> Path:
    runtime_dir = runtime_dir.resolve(strict=True)
    members = [
        f"comic-sol/{path.relative_to(runtime_dir).as_posix()}"
        for path in runtime_dir.rglob("*")
        if path.is_file()
    ]
    validate_runtime_members(members)
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as writer:
            for source in sorted(path for path in runtime_dir.rglob("*") if path.is_file()):
                target = f"comic-sol/{source.relative_to(runtime_dir).as_posix()}"
                info = zipfile.ZipInfo(target, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if os.access(source, os.X_OK) else 0o644) << 16
                writer.writestr(info, source.read_bytes())
    elif archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as writer:
            writer.add(runtime_dir, arcname="comic-sol", recursive=True)
    else:
        raise ValueError("portable archive must use .zip or .tar.gz")
    return archive
