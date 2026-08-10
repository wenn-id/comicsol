"""Transactional user-local installation lifecycle for portable runtimes."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .portable import safe_extract_zip

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:rc[0-9]+)?$")


@dataclass(frozen=True, slots=True)
class InstallResult:
    status: str
    version: str | None
    previous_version: str | None
    executable: Path


def _executable_name() -> str:
    return "comic-sol.exe" if os.name == "nt" else "comic-sol"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_runtime(source: Path, destination: Path) -> Path | None:
    pending = destination.with_name(f"{destination.name}.new")
    rollback = destination.with_name(f"{destination.name}.rollback")
    shutil.rmtree(pending, ignore_errors=True)
    shutil.rmtree(rollback, ignore_errors=True)
    shutil.copytree(source, pending, copy_function=shutil.copy2)
    moved_previous = False
    try:
        if destination.exists():
            os.replace(destination, rollback)
            moved_previous = True
        os.replace(pending, destination)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        if moved_previous and rollback.exists():
            os.replace(rollback, destination)
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return rollback if moved_previous else None


def read_active_version(install_root: Path) -> str | None:
    pointer = Path(install_root) / "active-version"
    if not pointer.is_file():
        return None
    version = pointer.read_text(encoding="utf-8").strip()
    if not _VERSION.fullmatch(version):
        raise ValueError("invalid active installation version")
    return version


def install_archive(
    archive: Path,
    install_root: Path,
    version: str,
    *,
    verifier: Callable[[Path], bool],
) -> InstallResult:
    if not _VERSION.fullmatch(version):
        raise ValueError("invalid installation version")
    archive = Path(archive).resolve(strict=True)
    install_root = Path(install_root).expanduser().resolve()
    versions = install_root / "versions"
    target = versions / version
    executable_name = _executable_name()
    stable_runtime = install_root / "bin"
    stable_executable = stable_runtime / executable_name
    previous = read_active_version(install_root) if install_root.exists() else None

    if previous == version and target.is_dir() and stable_executable.is_file():
        if not verifier(stable_executable):
            raise RuntimeError("installation verification failed")
        return InstallResult("unchanged", version, previous, stable_executable)

    install_root.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=versions))
    extracted = staging_parent / "extracted"
    rollback_runtime: Path | None = None

    try:
        safe_extract_zip(archive, extracted)
        runtime = extracted / "comic-sol"
        source_executable = runtime / executable_name
        if not source_executable.is_file():
            raise ValueError("portable archive executable is missing")
        if target.exists():
            shutil.rmtree(target)
        os.replace(runtime, target)
        rollback_runtime = _publish_runtime(target, stable_runtime)
        if not verifier(stable_executable):
            raise RuntimeError("installation verification failed")
        _atomic_write(install_root / "active-version", f"{version}\n".encode("utf-8"))
        if rollback_runtime is not None:
            shutil.rmtree(rollback_runtime, ignore_errors=True)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(stable_runtime, ignore_errors=True)
        if rollback_runtime is not None and rollback_runtime.exists():
            os.replace(rollback_runtime, stable_runtime)
        if previous is None:
            (install_root / "active-version").unlink(missing_ok=True)
        else:
            _atomic_write(install_root / "active-version", f"{previous}\n".encode("utf-8"))
        raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    status = "installed" if previous is None else "upgraded"
    return InstallResult(status, version, previous, stable_executable)


def uninstall_runtime(install_root: Path) -> InstallResult:
    install_root = Path(install_root).expanduser().resolve()
    executable = install_root / "bin" / _executable_name()
    previous = read_active_version(install_root) if install_root.exists() else None
    if not install_root.exists():
        return InstallResult("not-installed", None, None, executable)
    shutil.rmtree(install_root)
    return InstallResult("removed", None, previous, executable)
