"""Transactional installation of the canonical Comic Sol Agent Skill bundle."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .errors import CliUsageError, UnsupportedSkillPlacementError

MARKER_NAME = ".comic-sol-install.json"
MAX_MARKER_BYTES = 64 * 1024
MAX_MANAGED_FILES = 512
MAX_PAYLOAD_FILE_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
_REPARSE_POINT = 0x400
_MARKER_FIELDS = frozenset({"target", "scope", "version", "bundle_digest", "managed_paths"})
_SUPPORTED = frozenset(
    {
        ("codex", "user"),
        ("claude", "user"),
        ("claude", "project"),
        ("antigravity", "project"),
        ("zcode", "user"),
    }
)


class AutoDetectionError(CliUsageError):
    """Auto-selection found zero or multiple supported host locations."""

    def __init__(self, scope: str, candidates: tuple[str, ...], matches: tuple[str, ...]) -> None:
        self.scope = scope
        self.candidates = candidates
        self.matches = matches
        if matches:
            detail = "Detected multiple supported hosts: " + ", ".join(matches)
            recovery = "Choose one explicitly with --target."
        else:
            detail = "No supported host location was detected."
            recovery = "Create or select one candidate with --target: " + ", ".join(candidates)
        super().__init__(f"{detail} {recovery}")


class UnsafeSkillPathError(ValueError):
    """A requested Skill path crossed the installer trust boundary."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"security-error: unsafe Skill destination: {detail}")


class SkillVerificationError(RuntimeError):
    """A copied or published Skill payload did not match its manifest."""


class InvalidSkillMarkerError(ValueError):
    """An existing installation marker is absent, malformed, or mismatched."""


@dataclass(frozen=True)
class SkillOperationResult:
    target: str
    scope: str
    status: str
    destination: str
    bundle_digest: str | None
    managed_paths: int
    message: str


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _validate_relative(value: str) -> str:
    if not value or _has_control(value) or "\\" in value:
        raise UnsafeSkillPathError("managed paths must be non-empty portable relative paths")
    path = Path(value)
    if (
        path.is_absolute()
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafeSkillPathError("managed paths must not contain aliases or traversal")
    normalized = path.as_posix()
    if normalized != value:
        raise UnsafeSkillPathError("managed paths must use canonical POSIX separators")
    return normalized


def _lexical_path(value: Path, *, label: str) -> Path:
    raw = os.fspath(value)
    if not raw or _has_control(raw):
        raise UnsafeSkillPathError(f"{label} contains a control character")
    raw_path = Path(raw).expanduser()
    if any(part == ".." for part in raw_path.parts):
        raise UnsafeSkillPathError(f"{label} contains traversal or a path alias")
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    return raw_path.absolute()


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_macos_system_alias(path: Path) -> bool:
    return sys.platform == "darwin" and path in {Path("/var"), Path("/tmp")}


def _assert_safe_components(path: Path) -> None:
    """Inspect every existing component with lstat and never follow a link."""
    absolute = _lexical_path(path, label="path")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise UnsafeSkillPathError(
                "an existing path component could not be inspected"
            ) from error
        if _is_macos_system_alias(current):
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise UnsafeSkillPathError("a path component is a symlink or reparse point")


def _assert_directory(path: Path, *, label: str) -> None:
    _assert_safe_components(path)
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise UnsafeSkillPathError(f"{label} does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeSkillPathError(f"{label} is not a directory")


def _assert_contained(path: Path, root: Path) -> None:
    try:
        common = Path(os.path.commonpath((os.fspath(path), os.fspath(root))))
    except ValueError as error:
        raise UnsafeSkillPathError("destination is on a different filesystem root") from error
    if common != root:
        raise UnsafeSkillPathError("destination escapes the authorized root")


def _mkdir_verified(path: Path) -> None:
    """Create missing directory components and inspect each without following links."""
    absolute = _lexical_path(path, label="directory")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            metadata = current.lstat()
        if _is_macos_system_alias(current):
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise UnsafeSkillPathError("a destination ancestor is not a safe directory")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(descriptor)


def _read_regular(path: Path, *, maximum: int = MAX_PAYLOAD_FILE_BYTES) -> bytes:
    _assert_safe_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before):
            raise UnsafeSkillPathError("a payload member is not a regular file")
        if before.st_size > maximum:
            raise UnsafeSkillPathError("a payload member exceeds the size limit")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise UnsafeSkillPathError("a payload member exceeds the size limit")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
        ):
            raise SkillVerificationError("Skill payload changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def _aggregate_digest(managed_paths: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in sorted(managed_paths.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
    return digest.hexdigest()


def _scan_payload(root: Path) -> tuple[dict[str, str], str]:
    root = _lexical_path(root, label="bundle root")
    _assert_directory(root, label="bundle root")
    managed: dict[str, str] = {}
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        _assert_safe_components(directory)
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                relative = (Path(entry.path).relative_to(root)).as_posix()
                _validate_relative(relative)
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(metadata):
                    raise UnsafeSkillPathError("the canonical bundle contains a link")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeSkillPathError("the canonical bundle contains a non-regular member")
                data = _read_regular(Path(entry.path))
                total += len(data)
                if total > MAX_PAYLOAD_BYTES:
                    raise UnsafeSkillPathError("the canonical bundle exceeds the size limit")
                managed[relative] = hashlib.sha256(data).hexdigest()
                if len(managed) > MAX_MANAGED_FILES:
                    raise UnsafeSkillPathError("the canonical bundle contains too many files")
    if "SKILL.md" not in managed:
        raise SkillVerificationError("canonical Skill payload is missing SKILL.md")
    managed = dict(sorted(managed.items()))
    return managed, _aggregate_digest(managed)


def bundle_digest(bundle_root: Path | None = None) -> str:
    root = bundle_root or Path(__file__).resolve().parent / "skill"
    return _scan_payload(root)[1]


def _destination_for(
    target: str,
    scope: str,
    *,
    project_root: Path | None,
    home: Path,
    codex_home: Path,
) -> tuple[Path, Path]:
    if (target, scope) not in _SUPPORTED:
        raise UnsupportedSkillPlacementError(target, scope)
    if scope == "user" and project_root is not None:
        raise UnsafeSkillPathError("--project-root is valid only with project scope")
    if scope == "project":
        if project_root is None:
            raise UnsafeSkillPathError("project scope requires --project-root")
        root = _lexical_path(project_root, label="project root")
        _assert_directory(root, label="project root")
        relative = {
            "claude": Path(".claude/skills/comic-sol"),
            "antigravity": Path(".agents/skills/comic-sol"),
        }[target]
        destination = root / relative
        authorized = root
    elif target == "codex":
        authorized = _lexical_path(codex_home, label="CODEX_HOME")
        destination = authorized / "skills/comic-sol"
    else:
        authorized = _lexical_path(home, label="home")
        _assert_directory(authorized, label="home")
        relative = {
            "claude": Path(".claude/skills/comic-sol"),
            "zcode": Path(".zcode/skills/comic-sol"),
        }[target]
        destination = authorized / relative
    _assert_safe_components(authorized)
    _assert_safe_components(destination)
    _assert_contained(destination, authorized)
    return destination, authorized


def _host_location(
    target: str,
    scope: str,
    *,
    project_root: Path | None,
    home: Path,
    codex_home: Path,
) -> Path:
    destination, _ = _destination_for(
        target,
        scope,
        project_root=project_root,
        home=home,
        codex_home=codex_home,
    )
    return destination.parents[1]


def _auto_target(
    scope: str,
    *,
    project_root: Path | None,
    home: Path,
    codex_home: Path,
) -> str:
    candidates = tuple(
        sorted(target for target, supported_scope in _SUPPORTED if supported_scope == scope)
    )
    matches: list[str] = []
    for candidate in candidates:
        location = _host_location(
            candidate,
            scope,
            project_root=project_root,
            home=home,
            codex_home=codex_home,
        )
        try:
            metadata = location.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode) and not _is_reparse_point(metadata):
            matches.append(candidate)
    if len(matches) != 1:
        raise AutoDetectionError(scope, candidates, tuple(matches))
    return matches[0]


def _copy_file(source: Path, destination: Path) -> None:
    data = _read_regular(source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _copy_payload(source: Path, destination: Path, managed: dict[str, str]) -> None:
    for relative in managed:
        source_file = source / relative
        target = destination / relative
        _mkdir_verified(target.parent)
        _copy_file(source_file, target)
        _fsync_directory(target.parent)


def _actual_payload_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name == MARKER_NAME and Path(entry.path).parent == root:
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(metadata):
                    raise UnsafeSkillPathError("installed payload contains a link")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(metadata.st_mode):
                    relative = Path(entry.path).relative_to(root).as_posix()
                    hashes[_validate_relative(relative)] = hashlib.sha256(
                        _read_regular(Path(entry.path))
                    ).hexdigest()
                else:
                    raise UnsafeSkillPathError("installed payload contains a non-regular member")
    return dict(sorted(hashes.items()))


def _verify_payload(root: Path, expected: dict[str, str], *, allow_unknown: bool = False) -> bool:
    try:
        actual = _actual_payload_hashes(root)
    except (OSError, ValueError, RuntimeError):
        return False
    if allow_unknown:
        return all(actual.get(relative) == digest for relative, digest in expected.items())
    return actual == expected


def _marker_bytes(
    target: str,
    scope: str,
    version: str,
    digest: str,
    managed: dict[str, str],
) -> bytes:
    record = {
        "target": target,
        "scope": scope,
        "version": version,
        "bundle_digest": digest,
        "managed_paths": managed,
    }
    data = (
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_MARKER_BYTES:
        raise SkillVerificationError("Skill installation marker exceeds its size limit")
    return data


def _write_marker(
    destination: Path,
    *,
    target: str,
    scope: str,
    version: str,
    digest: str,
    managed: dict[str, str],
) -> None:
    marker = destination / MARKER_NAME
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_marker_bytes(target, scope, version, digest, managed))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    _fsync_directory(destination)


def _read_marker(destination: Path) -> dict[str, Any]:
    marker_path = destination / MARKER_NAME
    try:
        data = _read_regular(marker_path, maximum=MAX_MARKER_BYTES)
    except FileNotFoundError as error:
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker is missing; no files were changed"
        ) from error
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker is malformed; no files were changed"
        ) from error
    if not isinstance(record, dict) or set(record) != _MARKER_FIELDS:
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker has unexpected fields; no files were changed"
        )
    target = record.get("target")
    scope = record.get("scope")
    version = record.get("version")
    digest = record.get("bundle_digest")
    managed = record.get("managed_paths")
    if (
        (target, scope) not in _SUPPORTED
        or not isinstance(version, str)
        or not version
        or _has_control(version)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(managed, dict)
        or not managed
        or len(managed) > MAX_MANAGED_FILES
    ):
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker values are invalid; no files were changed"
        )
    normalized: dict[str, str] = {}
    for relative, file_digest in managed.items():
        if (
            not isinstance(relative, str)
            or not isinstance(file_digest, str)
            or len(file_digest) != 64
            or any(character not in "0123456789abcdef" for character in file_digest)
        ):
            raise InvalidSkillMarkerError(
                "Comic Sol Skill marker paths are invalid; no files were changed"
            )
        normalized[_validate_relative(relative)] = file_digest
    normalized = dict(sorted(normalized.items()))
    if list(managed) != list(normalized) or _aggregate_digest(normalized) != digest:
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker digest is invalid; no files were changed"
        )
    record["managed_paths"] = normalized
    return record


def _assert_safe_tree(root: Path) -> None:
    _assert_safe_components(root)
    if not root.exists():
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(metadata):
                    raise UnsafeSkillPathError("an existing destination member is a link")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeSkillPathError("an existing destination member is not regular")


def _publish_new(staging: Path, destination: Path) -> None:
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("Skill destination appeared during publication")
    os.replace(staging, destination)
    _fsync_directory(destination.parent)


def _exchange_paths(first: Path, second: Path) -> None:
    if os.name != "nt":
        from .setup import _rename_exchange

        _rename_exchange(first, second)
        _fsync_directory(first.parent)
        return

    # Windows has no general directory-exchange primitive. Retain the old tree
    # in a private sibling and restore it on either rename failure.
    backup = first.with_name(f".{second.name}.rollback-{secrets.token_hex(8)}")
    os.replace(second, backup)
    try:
        os.replace(first, second)
        os.replace(backup, first)
    except BaseException:
        if backup.exists() and not second.exists():
            os.replace(backup, second)
        raise
    _fsync_directory(first.parent)


def _safe_remove_private_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        path.unlink()
        return
    shutil.rmtree(path)


def install_skill(
    target: str,
    scope: str,
    project_root: Path | None = None,
    *,
    home: Path | None = None,
    codex_home: Path | None = None,
    bundle_root: Path | None = None,
    version: str = __version__,
) -> SkillOperationResult:
    """Install one verified canonical Skill payload at one explicit host location."""
    home_path = _lexical_path(home or Path.home(), label="home")
    configured_codex = codex_home
    if configured_codex is None:
        configured_codex = Path(os.environ.get("CODEX_HOME", os.fspath(home_path / ".codex")))
    codex_path = _lexical_path(configured_codex, label="CODEX_HOME")
    if target == "auto":
        target = _auto_target(
            scope,
            project_root=project_root,
            home=home_path,
            codex_home=codex_path,
        )
    destination, authorized = _destination_for(
        target,
        scope,
        project_root=project_root,
        home=home_path,
        codex_home=codex_path,
    )
    source = _lexical_path(
        bundle_root or Path(__file__).resolve().parent / "skill", label="bundle root"
    )
    managed, digest = _scan_payload(source)

    if not authorized.exists():
        _mkdir_verified(authorized)
    _mkdir_verified(destination.parent)
    _assert_safe_components(destination)
    if destination.exists():
        _assert_safe_tree(destination)
        marker = _read_marker(destination)
        if marker["target"] != target or marker["scope"] != scope:
            raise InvalidSkillMarkerError(
                "existing Comic Sol Skill marker targets another host or scope; no files were changed"
            )
        installed = marker["managed_paths"]
        if marker["bundle_digest"] == digest:
            if installed != managed or not _verify_payload(
                destination, managed, allow_unknown=True
            ):
                raise SkillVerificationError(
                    "existing Skill claims this digest but its managed files do not match"
                )
            return SkillOperationResult(
                target,
                scope,
                "unchanged",
                os.fspath(destination),
                digest,
                len(managed),
                "Skill already matches the packaged bundle.",
            )
        if not _verify_payload(destination, installed):
            raise SkillVerificationError(
                "existing managed Skill files were modified or contain unknown files; upgrade refused"
            )

    staging = destination.with_name(f".{destination.name}.stage-{secrets.token_hex(8)}")
    published = False
    exchanged = False
    try:
        staging.mkdir(mode=0o700)
        _copy_payload(source, staging, managed)
        if not _verify_payload(staging, managed):
            raise SkillVerificationError("staged Skill payload digest verification failed")
        _write_marker(
            staging,
            target=target,
            scope=scope,
            version=version,
            digest=digest,
            managed=managed,
        )
        _fsync_directory(staging)
        if destination.exists():
            _exchange_paths(staging, destination)
            exchanged = True
        else:
            _publish_new(staging, destination)
            published = True
        if not _verify_payload(destination, managed):
            if exchanged:
                _exchange_paths(staging, destination)
                exchanged = False
            elif published:
                os.replace(destination, staging)
                _fsync_directory(destination.parent)
                published = False
            raise SkillVerificationError("published Skill payload digest verification failed")
        persisted_marker = _read_marker(destination)
        if (
            persisted_marker["target"] != target
            or persisted_marker["scope"] != scope
            or persisted_marker["version"] != version
            or persisted_marker["bundle_digest"] != digest
            or persisted_marker["managed_paths"] != managed
        ):
            if exchanged:
                _exchange_paths(staging, destination)
                exchanged = False
            elif published:
                os.replace(destination, staging)
                _fsync_directory(destination.parent)
                published = False
            raise SkillVerificationError("published Skill marker verification failed")
        status = "upgraded" if exchanged else "installed"
        message = "Skill bundle upgraded transactionally." if exchanged else "Skill installed."
        return SkillOperationResult(
            target,
            scope,
            status,
            os.fspath(destination),
            digest,
            len(managed),
            message,
        )
    finally:
        _safe_remove_private_tree(staging)


def uninstall_skill(
    target: str,
    scope: str,
    project_root: Path | None = None,
    *,
    home: Path | None = None,
    codex_home: Path | None = None,
) -> SkillOperationResult:
    """Remove unchanged marker-listed files and preserve everything else."""
    home_path = _lexical_path(home or Path.home(), label="home")
    configured_codex = codex_home
    if configured_codex is None:
        configured_codex = Path(os.environ.get("CODEX_HOME", os.fspath(home_path / ".codex")))
    codex_path = _lexical_path(configured_codex, label="CODEX_HOME")
    destination, _ = _destination_for(
        target,
        scope,
        project_root=project_root,
        home=home_path,
        codex_home=codex_path,
    )
    if not destination.exists():
        return SkillOperationResult(
            target,
            scope,
            "not-installed",
            os.fspath(destination),
            None,
            0,
            "No managed Comic Sol Skill installation was found.",
        )
    _assert_safe_tree(destination)
    marker = _read_marker(destination)
    if marker["target"] != target or marker["scope"] != scope:
        raise InvalidSkillMarkerError(
            "Comic Sol Skill marker does not match the requested host and scope; no files were changed"
        )
    managed: dict[str, str] = marker["managed_paths"]
    removable: list[Path] = []
    for relative, expected_digest in managed.items():
        candidate = destination / relative
        _assert_contained(candidate, destination)
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise UnsafeSkillPathError("a managed file is a link")
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeSkillPathError("a managed path is not a regular file")
        if hashlib.sha256(_read_regular(candidate)).hexdigest() == expected_digest:
            removable.append(candidate)

    for candidate in removable:
        candidate.unlink()
        _fsync_directory(candidate.parent)
    marker_path = destination / MARKER_NAME
    marker_path.unlink()
    _fsync_directory(destination)

    directories = {
        parent
        for relative in managed
        for parent in (destination / relative).parents
        if parent != destination and destination in parent.parents
    }
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass
    try:
        destination.rmdir()
    except OSError:
        status = "preserved"
        message = (
            "Managed unchanged files were removed; modified or unrelated files were preserved."
        )
    else:
        _fsync_directory(destination.parent)
        status = "uninstalled"
        message = "Managed Comic Sol Skill files were removed."
    return SkillOperationResult(
        target,
        scope,
        status,
        os.fspath(destination),
        marker["bundle_digest"],
        len(managed),
        message,
    )
