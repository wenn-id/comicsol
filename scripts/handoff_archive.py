"""Deterministic, verified portable archives for prepared handoff projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from .core_primitives import canonical_json_bytes
from .project_io import (
    ProjectLock,
    _atomic_rename_noreplace,
    cleanup_owned_directory,
    contained_project_path,
    fsync_directory,
    fsync_directory_tree,
    normalized_project_relative_path,
    open_contained,
    open_path_nofollow,
    publish_directory_noreplace,
    read_contained_bytes,
    read_contained_json,
    remove_contained,
)


ARCHIVE_FORMAT_VERSION = "1.0"
ARCHIVE_SUFFIX = ".comic-sol-handoff"
FORMAT_METADATA_MEMBER = "comic-sol-handoff.json"
CHECKSUM_MANIFEST_MEMBER = "checksums.json"
PROJECT_MEMBER_PREFIX = "project/"
FIXED_ZIP_DATETIME = (1980, 1, 1, 0, 0, 0)

# These bounds are deliberately independent of ZIP metadata and are enforced
# both during the central-directory pre-scan and while bytes are streamed.
MAX_ARCHIVE_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
_CONTROL_MEMBER_BYTES = 4 * 1024 * 1024
_WINDOWS_REPARSE_ATTRIBUTE = 0x0400
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HandoffArchiveError(ValueError):
    """Raised when a handoff archive cannot be safely exported or imported."""


@dataclass(frozen=True)
class _VerifiedArchive:
    project_id: str
    project_members: tuple[tuple[zipfile.ZipInfo, str], ...]
    member_count: int

    @property
    def checksum_count(self) -> int:
        return len(self.project_members)


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _require_existing_directory(path: Path, *, label: str) -> Path:
    directory = Path(path).expanduser().absolute()
    try:
        metadata = directory.stat(follow_symlinks=False)
        resolved = directory.resolve(strict=True)
    except OSError as error:
        raise HandoffArchiveError(f"{label} must be an existing directory: {error}") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise HandoffArchiveError(f"{label} must be a regular directory")
    if getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE:
        raise HandoffArchiveError(f"{label} must not be a reparse point")
    return resolved


def _safe_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise HandoffArchiveError("archive project ID must be a string")
    try:
        normalized = normalized_project_relative_path(value)
    except ValueError as error:
        raise HandoffArchiveError("archive project ID is unsafe") from error
    if "/" in normalized:
        raise HandoffArchiveError("archive project ID must identify one directory")
    return normalized


def _regular_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_DATETIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _is_excluded_project_path(relative: str) -> bool:
    return relative == ".comic-sol.lock" or relative.startswith("logs/transactions/")


def _iter_project_entries(project_dir: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Yield a deterministic walk bounded by the archive member limit."""
    remaining_entries = MAX_ARCHIVE_MEMBERS

    def walk(relative_dir: str) -> Iterator[tuple[str, os.stat_result]]:
        nonlocal remaining_entries
        directory = project_dir if not relative_dir else project_dir / relative_dir
        entries = []
        try:
            with os.scandir(directory) as scanner:
                for entry in scanner:
                    if remaining_entries <= 0:
                        raise HandoffArchiveError(
                            "archive member count exceeds the limit during source traversal"
                        )
                    entries.append(entry)
                    remaining_entries -= 1
            entries.sort(key=lambda entry: entry.name)
        except HandoffArchiveError:
            raise
        except OSError as error:
            raise HandoffArchiveError(f"handoff project cannot be scanned: {error}") from error
        for entry in entries:
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise HandoffArchiveError(f"handoff project cannot be scanned: {error}") from error
            yield relative, metadata
            if stat.S_ISDIR(metadata.st_mode) and relative != "logs/transactions":
                yield from walk(relative)

    yield from walk("")


def _format_metadata_payload(project_id: str) -> bytes:
    return canonical_json_bytes(
        {
            "format": "comic-sol-handoff",
            "project_id": project_id,
            "version": ARCHIVE_FORMAT_VERSION,
        }
    )


def _checksum_manifest_payload(checksums: list[dict[str, str]]) -> bytes:
    return canonical_json_bytes(
        {
            "algorithm": "sha256",
            "files": sorted(checksums, key=lambda item: item["path"]),
            "format_version": ARCHIVE_FORMAT_VERSION,
        }
    )


def _enforce_export_total(
    project_bytes: int,
    format_payload: bytes,
    checksum_payload: bytes,
) -> None:
    controls = (format_payload, checksum_payload)
    if any(len(payload) > MAX_MEMBER_BYTES for payload in controls):
        raise HandoffArchiveError("archive member exceeds the member limit")
    if project_bytes + sum(map(len, controls)) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise HandoffArchiveError("archive aggregate uncompressed size exceeds the limit")


def _collect_project_payloads(project_dir: Path) -> tuple[str, dict[str, bytes]]:
    project_dir = Path(project_dir).expanduser().absolute()
    try:
        root_metadata = project_dir.lstat()
    except OSError as error:
        raise HandoffArchiveError(f"handoff project cannot be accessed: {error}") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise HandoffArchiveError("handoff project must be a regular directory")
    if getattr(root_metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE:
        raise HandoffArchiveError("handoff project must not be a reparse point")

    from .validate_project import require_valid_project

    payloads: dict[str, bytes] = {}
    checksums: list[dict[str, str]] = []
    project_bytes = 0
    with ProjectLock(project_dir):
        try:
            require_valid_project(project_dir, "storyboard")
            manifest = read_contained_json(project_dir, "project.json")
        except Exception as error:
            raise HandoffArchiveError(f"handoff project is not valid: {error}") from error
        if not isinstance(manifest, dict):
            raise HandoffArchiveError("handoff project manifest must be an object")
        project_id = _safe_project_id(manifest.get("project_id"))
        if project_id != project_dir.name:
            raise HandoffArchiveError("handoff project ID does not match its directory")
        handoff = manifest.get("handoff")
        if not isinstance(handoff, dict) or handoff.get("manifest_path") != "handoff/manifest.json":
            raise HandoffArchiveError("handoff project is not completely prepared")

        format_payload = _format_metadata_payload(project_id)
        if MAX_ARCHIVE_MEMBERS < 2:
            raise HandoffArchiveError("archive member count exceeds the limit")
        _enforce_export_total(
            project_bytes,
            format_payload,
            _checksum_manifest_payload(checksums),
        )
        for relative, metadata in _iter_project_entries(project_dir):
            if relative == "logs/transactions" or relative.startswith("logs/transactions/"):
                continue
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE
            ):
                raise HandoffArchiveError("handoff project contains a symlink or reparse point")
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise HandoffArchiveError("handoff project contains a non-regular file")
            if _is_excluded_project_path(relative):
                continue

            name = PROJECT_MEMBER_PREFIX + relative
            if len(payloads) + 3 > MAX_ARCHIVE_MEMBERS:
                raise HandoffArchiveError("archive member count exceeds the limit")
            if metadata.st_size < 0 or metadata.st_size > MAX_MEMBER_BYTES:
                raise HandoffArchiveError("archive member exceeds the member limit")
            predicted_checksums = [
                *checksums,
                {"path": name, "sha256": "0" * 64},
            ]
            _enforce_export_total(
                project_bytes + metadata.st_size,
                format_payload,
                _checksum_manifest_payload(predicted_checksums),
            )

            payload = read_contained_bytes(
                project_dir,
                relative,
                max_bytes=MAX_MEMBER_BYTES,
            )
            digest = hashlib.sha256(payload).hexdigest()
            next_checksums = [*checksums, {"path": name, "sha256": digest}]
            _enforce_export_total(
                project_bytes + len(payload),
                format_payload,
                _checksum_manifest_payload(next_checksums),
            )
            payloads[name] = payload
            checksums = next_checksums
            project_bytes += len(payload)

    if PROJECT_MEMBER_PREFIX + "handoff/manifest.json" not in payloads:
        raise HandoffArchiveError("handoff project is missing its handoff manifest")
    return project_id, payloads


def _archive_payloads(project_id: str, project_payloads: dict[str, bytes]) -> dict[str, bytes]:
    checksums = [
        {"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in sorted(project_payloads.items())
    ]
    payloads = dict(project_payloads)
    payloads[FORMAT_METADATA_MEMBER] = _format_metadata_payload(project_id)
    payloads[CHECKSUM_MANIFEST_MEMBER] = _checksum_manifest_payload(checksums)
    if len(payloads) > MAX_ARCHIVE_MEMBERS:
        raise HandoffArchiveError("archive member count exceeds the limit")
    total = sum(len(payload) for payload in payloads.values())
    if any(len(payload) > MAX_MEMBER_BYTES for payload in payloads.values()):
        raise HandoffArchiveError("archive member exceeds the member limit")
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise HandoffArchiveError("archive aggregate uncompressed size exceeds the limit")
    return payloads


def _set_descriptor_mode(descriptor: int, mode: int) -> None:
    descriptor_chmod = getattr(os, "fchmod", None)
    if callable(descriptor_chmod):
        descriptor_chmod(descriptor, mode)


def _write_archive(handle: BinaryIO, payloads: dict[str, bytes]) -> None:
    with zipfile.ZipFile(
        handle,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=0,
    ) as writer:
        for name in sorted(payloads):
            writer.writestr(_regular_zip_info(name), payloads[name], compresslevel=0)


def _unlink_owned_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) == identity and stat.S_ISREG(metadata.st_mode):
        path.unlink()


def export_handoff_archive(project_dir: Path, output_path: Path) -> dict[str, object]:
    """Export a prepared project as a deterministic, durable no-clobber ZIP."""
    requested_destination = Path(output_path).expanduser().absolute()
    if not requested_destination.name:
        raise HandoffArchiveError("archive output path must name a file")
    if not requested_destination.name.endswith(ARCHIVE_SUFFIX):
        raise HandoffArchiveError(f"archive output path must use the {ARCHIVE_SUFFIX} suffix")
    output_parent = _require_existing_directory(
        requested_destination.parent,
        label="archive output parent",
    )
    destination = output_parent / requested_destination.name
    source = Path(project_dir).expanduser().absolute()
    try:
        source_root = source.resolve(strict=True)
    except OSError as error:
        raise HandoffArchiveError(f"handoff project cannot be accessed: {error}") from error
    if output_parent == source_root or source_root in output_parent.parents:
        raise HandoffArchiveError("archive output path must not be inside the source project")
    if _path_entry_exists(destination):
        raise HandoffArchiveError("archive destination exists; export would clobber it")

    project_id, project_payloads = _collect_project_payloads(source)
    payloads = _archive_payloads(project_id, project_payloads)

    temporary: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    descriptor = -1
    try:
        descriptor, name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        metadata = os.fstat(descriptor)
        temporary_identity = (metadata.st_dev, metadata.st_ino)
        _set_descriptor_mode(descriptor, 0o644)
        stream = os.fdopen(descriptor, "w+b")
        descriptor = -1
        with stream as handle:
            _write_archive(handle, payloads)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            with zipfile.ZipFile(handle, "r") as written:
                _verify_open_archive(written)
        current = temporary.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != temporary_identity:
            raise HandoffArchiveError("archive temporary identity changed before publication")
        _atomic_rename_noreplace(temporary, destination)
        temporary = None
        fsync_directory(destination.parent)
    except FileExistsError as error:
        raise HandoffArchiveError("archive destination exists; export would clobber it") from error
    except HandoffArchiveError:
        raise
    except OSError as error:
        raise HandoffArchiveError(f"archive publish failed after interruption: {error}") from error
    finally:
        try:
            if descriptor != -1 and temporary is not None and temporary_identity is None:
                try:
                    metadata = os.fstat(descriptor)
                except OSError:
                    pass
                else:
                    temporary_identity = (metadata.st_dev, metadata.st_ino)
        finally:
            try:
                if descriptor != -1:
                    os.close(descriptor)
            finally:
                if temporary is not None and temporary_identity is not None:
                    _unlink_owned_file(temporary, temporary_identity)
    return {"project_id": project_id, "archive_path": str(destination)}


def _validated_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise HandoffArchiveError("archive contains an unsafe member path")
    if name.startswith("/") or name.startswith("//") or re.match(r"^[A-Za-z]:", name):
        raise HandoffArchiveError("archive contains an absolute member path")
    try:
        normalized = normalized_project_relative_path(name)
    except ValueError as error:
        raise HandoffArchiveError("archive contains an unsafe traversal member path") from error
    if normalized not in {
        FORMAT_METADATA_MEMBER,
        CHECKSUM_MANIFEST_MEMBER,
    } and not normalized.startswith(PROJECT_MEMBER_PREFIX):
        raise HandoffArchiveError("archive contains a member outside the project payload")
    if normalized == PROJECT_MEMBER_PREFIX:
        raise HandoffArchiveError("archive contains an invalid project member path")
    return normalized


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    if info.is_dir():
        raise HandoffArchiveError("archive members must be regular files")
    if info.create_system == 3:
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK:
            raise HandoffArchiveError("archive contains a symlink member")
        if kind != stat.S_IFREG:
            raise HandoffArchiveError("archive contains a non-regular member")
    elif info.create_system == 0:
        if info.external_attr & _WINDOWS_REPARSE_ATTRIBUTE:
            raise HandoffArchiveError("archive contains a Windows reparse member")
        if info.external_attr & 0x10:
            raise HandoffArchiveError("archive contains a non-regular member")
    else:
        raise HandoffArchiveError("archive member platform is unsupported")


def _pre_scan(bundle: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = bundle.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise HandoffArchiveError("archive member count exceeds the limit")
    names: set[str] = set()
    folded: dict[str, str] = {}
    by_name: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _validated_member_name(info.filename)
        if name in names:
            raise HandoffArchiveError(f"archive contains duplicate member: {name}")
        case_key = name.casefold()
        if case_key in folded:
            raise HandoffArchiveError("archive member names have a case collision")
        names.add(name)
        folded[case_key] = name
        by_name[name] = info
        _validate_member_type(info)
        if info.flag_bits & 0x1:
            raise HandoffArchiveError("archive contains an encrypted member")
        if info.compress_type != zipfile.ZIP_DEFLATED:
            raise HandoffArchiveError("archive member compression is unsupported")
        if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            raise HandoffArchiveError("archive member exceeds the member limit")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise HandoffArchiveError("archive aggregate uncompressed size exceeds the limit")
        if info.file_size and (
            info.compress_size <= 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise HandoffArchiveError("archive member exceeds the compression ratio limit")
    if [info.filename for info in infos] != sorted(names):
        raise HandoffArchiveError("archive members are not in canonical sorted order")
    required = {FORMAT_METADATA_MEMBER, CHECKSUM_MANIFEST_MEMBER}
    missing = required - names
    if missing:
        raise HandoffArchiveError("archive is missing required metadata members")
    return by_name


def _read_member(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int) -> bytes:
    payload = bytearray()
    with bundle.open(info, "r") as source:
        while True:
            chunk = source.read(min(1024 * 1024, limit + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > limit or len(payload) > info.file_size:
                raise HandoffArchiveError("archive member exceeded its declared size while reading")
    if len(payload) != info.file_size:
        raise HandoffArchiveError("archive member size does not match the central directory")
    return bytes(payload)


def _canonical_json_member(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    label: str,
) -> dict[str, object]:
    payload = _read_member(bundle, info, limit=min(MAX_MEMBER_BYTES, _CONTROL_MEMBER_BYTES))
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffArchiveError(f"archive {label} is not valid JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise HandoffArchiveError(f"archive {label} is not canonical JSON")
    return value


def _validate_metadata(metadata: dict[str, object]) -> str:
    if set(metadata) != {"format", "project_id", "version"}:
        raise HandoffArchiveError("archive format metadata has unexpected fields")
    if metadata.get("format") != "comic-sol-handoff":
        raise HandoffArchiveError("archive format metadata is invalid")
    if metadata.get("version") != ARCHIVE_FORMAT_VERSION:
        raise HandoffArchiveError("archive uses an unsupported format version")
    return _safe_project_id(metadata.get("project_id"))


def _validate_checksum_manifest(
    manifest: dict[str, object],
    project_names: set[str],
) -> dict[str, str]:
    if set(manifest) != {"algorithm", "files", "format_version"}:
        raise HandoffArchiveError("archive checksum manifest has unexpected fields")
    if manifest.get("algorithm") != "sha256":
        raise HandoffArchiveError("archive checksum algorithm is unsupported")
    if manifest.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise HandoffArchiveError("archive checksum manifest has an unsupported version")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HandoffArchiveError("archive checksum files must be an array")
    checksums: dict[str, str] = {}
    ordered_paths: list[str] = []
    folded: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise HandoffArchiveError("archive checksum entry is invalid")
        path = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(path, str) or not path.startswith(PROJECT_MEMBER_PREFIX):
            raise HandoffArchiveError("archive checksum path is invalid")
        _validated_member_name(path)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise HandoffArchiveError("archive checksum digest is invalid")
        if path in checksums:
            raise HandoffArchiveError("archive checksum manifest contains a duplicate path")
        case_key = path.casefold()
        if case_key in folded:
            raise HandoffArchiveError("archive checksum paths have a case collision")
        folded.add(case_key)
        ordered_paths.append(path)
        checksums[path] = digest
    if ordered_paths != sorted(ordered_paths):
        raise HandoffArchiveError("archive checksum paths are not sorted")
    if set(checksums) != project_names:
        raise HandoffArchiveError("archive checksum manifest does not cover the project payload")
    return checksums


def _stream_member_digest(bundle: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    size = 0
    with bundle.open(info, "r") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > info.file_size or size > MAX_MEMBER_BYTES:
                raise HandoffArchiveError("archive member exceeded its declared size while reading")
            digest.update(chunk)
    if size != info.file_size:
        raise HandoffArchiveError("archive member size does not match the central directory")
    return digest.hexdigest()


def _verify_open_archive(bundle: zipfile.ZipFile) -> _VerifiedArchive:
    members = _pre_scan(bundle)
    metadata = _canonical_json_member(
        bundle,
        members[FORMAT_METADATA_MEMBER],
        label="format metadata",
    )
    project_id = _validate_metadata(metadata)
    checksum_manifest = _canonical_json_member(
        bundle,
        members[CHECKSUM_MANIFEST_MEMBER],
        label="checksum manifest",
    )
    project_names = {name for name in members if name.startswith(PROJECT_MEMBER_PREFIX)}
    checksums = _validate_checksum_manifest(checksum_manifest, project_names)
    if PROJECT_MEMBER_PREFIX + "project.json" not in project_names:
        raise HandoffArchiveError("archive project manifest is missing")
    if PROJECT_MEMBER_PREFIX + "handoff/manifest.json" not in project_names:
        raise HandoffArchiveError("archive handoff manifest is missing")

    verified: list[tuple[zipfile.ZipInfo, str]] = []
    for name in sorted(project_names):
        info = members[name]
        actual = _stream_member_digest(bundle, info)
        expected = checksums[name]
        if actual != expected:
            raise HandoffArchiveError(f"archive checksum mismatch for {name}")
        verified.append((info, expected))

    project_payload = _read_member(
        bundle,
        members[PROJECT_MEMBER_PREFIX + "project.json"],
        limit=MAX_MEMBER_BYTES,
    )
    try:
        project_manifest = json.loads(project_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffArchiveError("archive project manifest is invalid") from error
    if not isinstance(project_manifest, dict) or project_manifest.get("project_id") != project_id:
        raise HandoffArchiveError("archive project ID does not match the project manifest identity")

    handoff_payload = _read_member(
        bundle,
        members[PROJECT_MEMBER_PREFIX + "handoff/manifest.json"],
        limit=MAX_MEMBER_BYTES,
    )
    try:
        handoff_manifest = json.loads(handoff_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffArchiveError("archive handoff manifest is invalid") from error
    from .handoff import validate_handoff_manifest

    handoff_issues = validate_handoff_manifest(handoff_manifest)
    if handoff_issues:
        raise HandoffArchiveError(
            "archive handoff manifest is invalid: " + "; ".join(handoff_issues[:8])
        )
    project_binding = project_manifest.get("handoff")
    if (
        not isinstance(handoff_manifest, dict)
        or handoff_manifest.get("project_id") != project_id
        or not isinstance(project_binding, dict)
        or project_binding.get("manifest_path") != "handoff/manifest.json"
        or project_binding.get("locked_scope_sha256") != handoff_manifest.get("locked_scope_sha256")
    ):
        raise HandoffArchiveError("archive project and handoff identities do not match")
    return _VerifiedArchive(project_id, tuple(verified), len(members))


@contextmanager
def _verified_archive(path: Path) -> Iterator[tuple[zipfile.ZipFile, _VerifiedArchive]]:
    archive_path = Path(path).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        source = open_path_nofollow(archive_path, flags=flags)
    except (OSError, ValueError) as error:
        raise HandoffArchiveError(f"archive cannot be opened: {error}") from error
    try:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise HandoffArchiveError("archive input must be a regular file")
        try:
            with zipfile.ZipFile(source, "r") as bundle:
                yield bundle, _verify_open_archive(bundle)
        except HandoffArchiveError:
            raise
        except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise HandoffArchiveError(
                f"archive ZIP or central directory is corrupt: {error}"
            ) from error
    finally:
        source.close()


def inspect_handoff_archive(archive_path: Path) -> dict[str, object]:
    """Verify an archive without publishing it and report its trusted identity."""
    with _verified_archive(Path(archive_path)) as (bundle, verified):
        _validate_archive_project_without_publication(bundle, verified)
        return {
            "archive_path": str(Path(archive_path)),
            "checksum_count": verified.checksum_count,
            "format_version": ARCHIVE_FORMAT_VERSION,
            "member_count": verified.member_count,
            "project_id": verified.project_id,
            "valid": True,
        }


def _ensure_extraction_parents(staging: Path, relative: str) -> None:
    """Create and revalidate every extraction parent through project containment."""
    parts = normalized_project_relative_path(relative).split("/")[:-1]
    for depth in range(1, len(parts) + 1):
        parent_relative = "/".join(parts[:depth])
        parent = contained_project_path(staging, parent_relative, must_exist=False)
        try:
            parent.mkdir(mode=0o755)
        except FileExistsError:
            pass
        verified = contained_project_path(staging, parent_relative, must_exist=True)
        metadata = verified.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise HandoffArchiveError("archive extraction parent must be a regular directory")
        if getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_ATTRIBUTE:
            raise HandoffArchiveError("archive extraction parent must not be a reparse point")


def _extract_member(
    bundle: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    expected_digest: str,
    staging: Path,
) -> None:
    relative = info.filename[len(PROJECT_MEMBER_PREFIX) :]
    normalized_project_relative_path(relative)
    _ensure_extraction_parents(staging, relative)
    digest = hashlib.sha256()
    size = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with (
        bundle.open(info, "r") as source,
        open_contained(
            staging,
            relative,
            flags=flags,
            mode=0o644,
        ) as target,
    ):
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > info.file_size or size > MAX_MEMBER_BYTES:
                raise HandoffArchiveError(
                    "archive member exceeded its declared size during extraction"
                )
            digest.update(chunk)
            target.write(chunk)
        if size != info.file_size:
            raise HandoffArchiveError("archive member size changed during extraction")
        if digest.hexdigest() != expected_digest:
            raise HandoffArchiveError("archive checksum changed during extraction")
        target.flush()
        _set_descriptor_mode(target.fileno(), 0o644)
        os.fsync(target.fileno())


def _extract_verified_project(
    bundle: zipfile.ZipFile,
    verified: _VerifiedArchive,
    staging: Path,
) -> None:
    try:
        for info, expected_digest in verified.project_members:
            _extract_member(bundle, info, expected_digest, staging)
    except HandoffArchiveError:
        raise
    except (OSError, ValueError) as error:
        raise HandoffArchiveError(f"archive extraction failed: {error}") from error


def _validate_staged_project(staging: Path, verified: _VerifiedArchive) -> None:
    from .validate_project import require_valid_project

    try:
        manifest = read_contained_json(staging, "project.json")
        if not isinstance(manifest, dict) or manifest.get("project_id") != verified.project_id:
            raise HandoffArchiveError(
                "archive project ID does not match the extracted project identity"
            )
        require_valid_project(staging, "storyboard")
    except HandoffArchiveError:
        raise
    except Exception as error:
        issues = getattr(error, "issues", ())
        details = "; ".join(
            f"{getattr(issue, 'field', 'validation')}: "
            f"{getattr(issue, 'message', 'project is invalid')}"
            for issue in tuple(issues)[:8]
        )
        suffix = f": {details}" if details else f": {error}"
        raise HandoffArchiveError("archive project validation failed" + suffix) from error
    remove_contained(staging, ".comic-sol.lock")


def _validate_archive_project_without_publication(
    bundle: zipfile.ZipFile,
    verified: _VerifiedArchive,
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".comic-sol-handoff-inspect-", suffix=".tmp"))
    metadata = staging.stat(follow_symlinks=False)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        _extract_verified_project(bundle, verified, staging)
        _validate_staged_project(staging, verified)
    finally:
        try:
            removed = cleanup_owned_directory(staging, identity)
        except OSError as error:
            raise HandoffArchiveError("archive inspection temporary cleanup failed") from error
        if not removed:
            raise HandoffArchiveError("archive inspection temporary identity changed")


def import_handoff_archive(archive_path: Path, output_root: Path) -> dict[str, object]:
    """Verify, stage, validate, and atomically publish an archived project."""
    root = _require_existing_directory(
        Path(output_root),
        label="archive output root",
    )

    with _verified_archive(Path(archive_path)) as (bundle, verified):
        destination = root / verified.project_id
        if _path_entry_exists(destination):
            raise HandoffArchiveError("archive destination already exists")
        staging = Path(tempfile.mkdtemp(dir=root, prefix=".comic-sol-handoff-", suffix=".tmp"))
        metadata = staging.stat(follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino)
        published = False
        try:
            _extract_verified_project(bundle, verified, staging)
            _validate_staged_project(staging, verified)
            fsync_directory_tree(staging)
            try:
                publish_directory_noreplace(
                    staging,
                    destination,
                    expected_identity=identity,
                )
            except FileExistsError as error:
                raise HandoffArchiveError("archive destination already exists") from error
            except BaseException as error:
                if _path_entry_exists(destination):
                    try:
                        restored = cleanup_owned_directory(destination, identity)
                    except OSError as cleanup_error:
                        raise HandoffArchiveError(
                            "archive directory publish interruption could not be rolled back"
                        ) from cleanup_error
                    if not restored:
                        raise HandoffArchiveError(
                            "archive directory publish interruption changed destination identity"
                        ) from error
                if not isinstance(error, Exception):
                    raise
                raise HandoffArchiveError(
                    f"archive directory publish failed after interruption: {error}"
                ) from error
            published = True
        finally:
            if not published:
                try:
                    cleanup_owned_directory(staging, identity)
                except OSError:
                    pass
        return {"project_id": verified.project_id, "project_dir": str(destination)}
