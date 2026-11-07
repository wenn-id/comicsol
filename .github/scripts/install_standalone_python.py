"""Install a hash-pinned standalone CPython runtime for native release builds."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform as host_platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

RELEASE_TAG = "20261026"
PYTHON_VERSION = "3.11.18"
_RELEASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download"


@dataclass(frozen=True, slots=True)
class RuntimeAsset:
    filename: str
    sha256: str
    python_relative: Path
    path_relative: Path
    license_relative: Path


RUNTIME_ASSETS = {
    ("linux", "x86_64"): RuntimeAsset(
        "cpython-3.11.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "a87ee63646c8f8438b7af3954b2135ed2af91d033c631f12a8f20af903046d06",
        Path("python/bin/python3.11"),
        Path("python/bin"),
        Path("python/lib/python3.11/LICENSE.txt"),
    ),
    ("macos", "arm64"): RuntimeAsset(
        "cpython-3.11.18+20261026-aarch64-apple-darwin-install_only.tar.gz",
        "cba3455e18516fbe8668cff2820c9b6a53447c552d612443c2b2fb60d390a908",
        Path("python/bin/python3.11"),
        Path("python/bin"),
        Path("python/lib/python3.11/LICENSE.txt"),
    ),
    ("windows", "x86_64"): RuntimeAsset(
        "cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz",
        "75cb501dc11c7b55c6b447c58e4910bce5b1f609f1017328b4f3a858dfaf5428",
        Path("python/python.exe"),
        Path("python"),
        Path("python/LICENSE.txt"),
    ),
}


_MACHINE_ALIASES = {
    "x86_64": frozenset({"x86_64", "amd64"}),
    "arm64": frozenset({"arm64", "aarch64"}),
}
_SYSTEM_NAMES = {"linux": "linux", "darwin": "macos", "windows": "windows"}


def runtime_asset(platform_name: str, architecture: str) -> RuntimeAsset:
    """Return the pinned standalone runtime for a supported native target."""
    try:
        return RUNTIME_ASSETS[(platform_name, architecture)]
    except KeyError as error:
        raise ValueError(
            f"no pinned standalone CPython runtime for {platform_name}/{architecture}"
        ) from error


def asset_url(asset: RuntimeAsset) -> str:
    """Return the immutable release asset URL with its filename encoded."""
    filename = quote(asset.filename, safe="")
    return f"{_RELEASE_URL}/{RELEASE_TAG}/{filename}"


def sha256_file(path: Path) -> str:
    """Hash a file without loading the complete artifact into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> str:
    """Fail unless a downloaded asset matches its pinned SHA-256 digest."""
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch: expected {expected}, got {actual}")
    return actual


def _archive_member_path(name: str) -> PurePosixPath:
    """Accept only member paths rooted in the standalone runtime directory."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe archive member path: {name!r}")
    parts = name.split("/")
    if parts[-1] == "":
        parts.pop()
    if (
        not parts
        or parts[0] != "python"
        or any(part in ("", ".", "..") or ":" in part for part in parts)
    ):
        raise ValueError(f"unsafe archive member path: {name!r}")
    return PurePosixPath(*parts)


def _validate_link_target(
    member_path: PurePosixPath,
    linkname: str,
    *,
    is_hard_link: bool,
) -> None:
    """Require archive links to resolve inside the runtime's python directory."""
    if (
        not linkname
        or linkname.startswith("/")
        or "\\" in linkname
        or "\x00" in linkname
        or any(part == "" for part in linkname.split("/"))
    ):
        raise ValueError(f"unsafe archive link target: {linkname!r}")
    if is_hard_link:
        try:
            _archive_member_path(linkname)
        except ValueError as error:
            raise ValueError(f"unsafe archive link target: {linkname!r}") from error
        return

    resolved = list(member_path.parent.parts)
    for part in linkname.split("/"):
        if part == ".":
            continue
        if part == "..":
            if len(resolved) <= 1:
                raise ValueError(f"unsafe archive link target: {linkname!r}")
            resolved.pop()
        elif ":" in part:
            raise ValueError(f"unsafe archive link target: {linkname!r}")
        else:
            resolved.append(part)
    if not resolved or resolved[0] != "python":
        raise ValueError(f"unsafe archive link target: {linkname!r}")


def _validated_archive_members(
    members: list[tarfile.TarInfo],
) -> list[tarfile.TarInfo]:
    """Reject unsafe paths, duplicate members, links, and special files."""
    seen: set[PurePosixPath] = set()
    for member in members:
        path = _archive_member_path(member.name)
        if path in seen:
            raise ValueError(f"duplicate archive member path: {member.name!r}")
        seen.add(path)
        if member.issym():
            _validate_link_target(path, member.linkname, is_hard_link=False)
        elif member.islnk():
            _validate_link_target(path, member.linkname, is_hard_link=True)
        elif not (member.isfile() or member.isdir()):
            raise ValueError(f"unsupported archive member type: {member.name!r}")
        if path == PurePosixPath("python") and not member.isdir():
            raise ValueError("standalone runtime root must be a directory")
    if not members:
        raise ValueError("standalone runtime archive is empty")
    return members


def extract_archive(archive_path: Path, destination: Path) -> Path:
    """Validate and extract a runtime archive into an empty staging directory."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise FileExistsError(f"archive staging directory is not empty: {destination}")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = _validated_archive_members(archive.getmembers())
        archive.extractall(path=destination, members=members)
    runtime_root = destination / "python"
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise FileNotFoundError("standalone runtime archive has no python directory")
    return runtime_root


def _check_host(platform_name: str, architecture: str) -> None:
    system = _SYSTEM_NAMES.get(host_platform.system().casefold())
    machine = host_platform.machine().casefold()
    if system != platform_name:
        raise ValueError(f"requested {platform_name} runtime on {host_platform.system()}")
    if machine not in _MACHINE_ALIASES[architecture]:
        raise ValueError(f"requested {architecture} runtime on {host_platform.machine()}")


def _download(asset: RuntimeAsset, destination: Path) -> None:
    with urllib.request.urlopen(asset_url(asset), timeout=180) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    verify_sha256(destination, asset.sha256)


def install_runtime(platform_name: str, architecture: str, install_dir: Path) -> Path:
    """Download, verify, extract, and register the runtime for later CI steps."""
    asset = runtime_asset(platform_name, architecture)
    _check_host(platform_name, architecture)
    install_root = install_dir.resolve()
    install_root.mkdir(parents=True, exist_ok=True)
    if any(install_root.iterdir()):
        raise FileExistsError(f"runtime installation directory is not empty: {install_root}")

    with tempfile.TemporaryDirectory(prefix="comicsol-cpython-", dir=install_root.parent) as raw:
        archive = Path(raw) / asset.filename
        _download(asset, archive)
        staging = Path(raw) / "staging"
        extracted_runtime = extract_archive(archive, staging)
        shutil.move(str(extracted_runtime), str(install_root / "python"))

    python = install_root / asset.python_relative
    license_path = install_root / asset.license_relative
    if not python.is_file():
        raise FileNotFoundError(f"standalone CPython executable is missing: {python}")
    if not license_path.is_file():
        raise FileNotFoundError(f"standalone CPython license is missing: {license_path}")
    version = subprocess.run(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if version != PYTHON_VERSION:
        raise ValueError(f"expected CPython {PYTHON_VERSION}, found {version}")

    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with Path(github_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{install_root / asset.path_relative}\n")
    print(f"Verified CPython {version} for {platform_name}/{architecture}: {asset.sha256}")
    return python


def main() -> int:
    """Install the standalone runtime selected by the native runner matrix."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--architecture", choices=("x86_64", "arm64"), required=True)
    parser.add_argument("--install-dir", required=True, type=Path)
    arguments = parser.parse_args()
    install_runtime(arguments.platform, arguments.architecture, arguments.install_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
