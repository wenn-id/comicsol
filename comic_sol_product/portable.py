"""Portable-runtime archive creation and validation contracts."""

from __future__ import annotations

import gzip
import hashlib
import os
import stat
import struct
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

REQUIRED_RUNTIME_SUFFIXES = frozenset(
    {
        "comic-sol/_internal/comic_sol_product/assets/fonts/ComicNeue-Regular.ttf",
        "comic-sol/_internal/comic_sol_product/assets/fonts/ComicNeue-Bold.ttf",
        "comic-sol/_internal/comic_sol_product/templates/manifest.json",
        "comic-sol/_internal/comic_sol_product/skill/SKILL.md",
        "comic-sol/_internal/comic_sol_product/skill/references/workflow.md",
        "comic-sol/_internal/comic_sol_product/skill/references/starter-templates.md",
    }
    | {
        f"comic-sol/_internal/comic_sol_product/templates/starters/v1/{starter_id}/{relative}"
        for starter_id in (
            "minimal-one-page",
            "dialogue-two-page",
            "action-focused",
        )
        for relative in (
            "source/input.txt",
            "source/request.json",
            "plan/story-plan.json",
            "plan/character-bible.json",
            "plan/storyboard.json",
        )
    }
)

_ARCHITECTURES = frozenset({"x86_64", "arm64"})
_REPARSE_POINT = 0x400
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NATIVE_SUFFIXES = (".dll", ".dylib", ".exe", ".pyd", ".so")


@dataclass(frozen=True, slots=True)
class _Member:
    relative: Path
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    digest: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Directory:
    relative: Path
    device: int
    inode: int
    mode: int


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


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _validate_entry(path: Path, metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ValueError(f"portable runtime must not contain symlinks or reparse points: {path}")
    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise ValueError(f"portable runtime member must be a regular file or directory: {path}")


def _directory(relative: Path, metadata: os.stat_result) -> _Directory:
    return _Directory(relative, metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _member(relative: Path, metadata: os.stat_result) -> _Member:
    return _Member(
        relative,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _same_directory(metadata: os.stat_result, expected: _Directory) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not _is_reparse(metadata)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and metadata.st_mode == expected.mode
    )


def _same_member(metadata: os.stat_result, expected: _Member) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not _is_reparse(metadata)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and metadata.st_mode == expected.mode
        and metadata.st_size == expected.size
        and metadata.st_mtime_ns == expected.modified_ns
        and metadata.st_ctime_ns == expected.changed_ns
    )


def _resolved_within(root: Path, path: Path) -> None:
    resolved = path.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"portable runtime member escapes the runtime root: {path}")


def _scan_posix(
    root: Path,
    descriptor: int,
    relative: Path,
    members: list[_Member],
    directories: dict[Path, _Directory],
) -> None:
    with os.scandir(descriptor) as entries:
        ordered = sorted(entries, key=lambda item: item.name)
    for entry in ordered:
        child_relative = relative / entry.name
        child_path = root / child_relative
        metadata = entry.stat(follow_symlinks=False)
        _validate_entry(child_path, metadata)
        _resolved_within(root, child_path)
        if stat.S_ISREG(metadata.st_mode):
            members.append(_member(child_relative, metadata))
            continue
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        child_descriptor = os.open(entry.name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(child_descriptor)
            expected = _directory(child_relative, metadata)
            if not _same_directory(opened, expected):
                raise ValueError(f"portable runtime changed while being inspected: {child_path}")
            directories[child_relative] = expected
            _scan_posix(root, child_descriptor, child_relative, members, directories)
        finally:
            os.close(child_descriptor)


def _scan_fallback(
    root: Path,
    path: Path,
    relative: Path,
    members: list[_Member],
    directories: dict[Path, _Directory],
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(
            f"portable runtime directory changed while being inspected: {path}"
        ) from error
    if not _same_directory(metadata, directories[relative]):
        raise ValueError(f"portable runtime directory changed while being inspected: {path}")
    with os.scandir(path) as entries:
        ordered = sorted(entries, key=lambda item: item.name)
    for entry in ordered:
        child_relative = relative / entry.name
        child_path = root / child_relative
        metadata = child_path.lstat()
        _validate_entry(child_path, metadata)
        _resolved_within(root, child_path)
        if stat.S_ISREG(metadata.st_mode):
            members.append(_member(child_relative, metadata))
        else:
            directories[child_relative] = _directory(child_relative, metadata)
            _scan_fallback(root, child_path, child_relative, members, directories)


def _snapshot_digests(
    root: Path,
    root_descriptor: int | None,
    members: list[_Member],
    directories: dict[Path, _Directory],
) -> list[_Member]:
    snapshots: list[_Member] = []
    for member in members:
        payload = _read_member(root, root_descriptor, member, directories)
        snapshots.append(replace(member, digest=hashlib.sha256(payload).digest()))
    return snapshots


def _scan_runtime(
    runtime_dir: Path,
) -> tuple[Path, list[_Member], dict[Path, _Directory], int | None]:
    supplied = Path(runtime_dir).absolute()
    metadata = supplied.lstat()
    _validate_entry(supplied, metadata)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("portable runtime root must be a directory")
    supplied_directory = _directory(Path(), metadata)
    root = supplied.resolve(strict=True)
    root_metadata = root.lstat()
    _validate_entry(root, root_metadata)
    if not _same_directory(root_metadata, supplied_directory):
        raise ValueError("portable runtime root changed while being inspected")
    members: list[_Member] = []
    directories = {Path(): supplied_directory}

    if os.name != "nt" and _O_NOFOLLOW:
        descriptor = os.open(supplied, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        try:
            if not _same_directory(os.fstat(descriptor), supplied_directory):
                raise ValueError("portable runtime root changed while being inspected")
            _scan_posix(root, descriptor, Path(), members, directories)
            members = _snapshot_digests(root, descriptor, members, directories)
        except BaseException:
            os.close(descriptor)
            raise
        return root, members, directories, descriptor

    _scan_fallback(root, root, Path(), members, directories)
    members = _snapshot_digests(root, None, members, directories)
    return root, members, directories, None


def _open_member_posix(
    root_descriptor: int, member: _Member, directories: dict[Path, _Directory]
) -> BinaryIO:
    current = os.dup(root_descriptor)
    try:
        if not _same_directory(os.fstat(current), directories[Path()]):
            raise ValueError("portable runtime root changed before it could be read")
        parent = Path()
        for part in member.relative.parts[:-1]:
            parent /= part
            child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
            if not _same_directory(os.fstat(current), directories[parent]):
                raise ValueError(
                    f"portable runtime directory changed before it could be read: {parent}"
                )
        descriptor = os.open(
            member.relative.name,
            os.O_RDONLY | _O_BINARY | _O_NOFOLLOW,
            dir_fd=current,
        )
    finally:
        os.close(current)
    return os.fdopen(descriptor, "rb")


def _assert_root_unchanged(root: Path, expected: _Directory) -> None:
    try:
        metadata = root.lstat()
    except OSError as error:
        raise ValueError("portable runtime root changed before it could be read") from error
    if not _same_directory(metadata, expected):
        raise ValueError("portable runtime root changed before it could be read")


def _open_member_fallback(
    root: Path, member: _Member, directories: dict[Path, _Directory]
) -> BinaryIO:
    _assert_root_unchanged(root, directories[Path()])
    current = root
    for index, part in enumerate(member.relative.parts[:-1], start=1):
        current /= part
        metadata = current.lstat()
        expected = directories[Path(*member.relative.parts[:index])]
        if not _same_directory(metadata, expected):
            raise ValueError(
                f"portable runtime directory changed before it could be read: {current}"
            )
    path = root / member.relative
    metadata = path.lstat()
    if not _same_member(metadata, member):
        raise ValueError(f"portable runtime member changed before it could be read: {path}")
    return os.fdopen(os.open(path, os.O_RDONLY | _O_BINARY), "rb")


def _read_member(
    root: Path,
    root_descriptor: int | None,
    member: _Member,
    directories: dict[Path, _Directory],
) -> bytes:
    _assert_root_unchanged(root, directories[Path()])
    try:
        if root_descriptor is not None:
            stream = _open_member_posix(root_descriptor, member, directories)
        else:
            stream = _open_member_fallback(root, member, directories)
    except OSError as error:
        raise ValueError(
            f"portable runtime member changed before it could be read: {member.relative}"
        ) from error
    with stream:
        before = os.fstat(stream.fileno())
        if not _same_member(before, member):
            raise ValueError(
                f"portable runtime member changed before it could be read: {member.relative}"
            )
        payload = stream.read()
        after = os.fstat(stream.fileno())
    if not _same_member(after, member) or len(payload) != member.size:
        raise ValueError(f"portable runtime member changed while it was read: {member.relative}")
    if member.digest is not None and hashlib.sha256(payload).digest() != member.digest:
        raise ValueError(
            f"portable runtime member changed before it could be read: {member.relative}"
        )
    return payload


_MACH_ARCHITECTURES = {0x01000007: "x86_64", 0x0100000C: "arm64"}


def _machine_architecture(machine: int, mapping: dict[int, str]) -> frozenset[str]:
    architecture = mapping.get(machine)
    return frozenset({architecture}) if architecture else frozenset({f"unknown-0x{machine:x}"})


def _detect_elf(payload: bytes) -> frozenset[str]:
    if len(payload) < 20 or payload[4] not in {1, 2} or payload[5] not in {1, 2}:
        raise ValueError("invalid ELF binary header")
    if payload[6] != 1:
        raise ValueError("invalid ELF binary version")
    byteorder = "little" if payload[5] == 1 else "big"
    header_size = 52 if payload[4] == 1 else 64
    size_offset = 40 if payload[4] == 1 else 52
    if len(payload) < header_size:
        raise ValueError("invalid ELF binary header")
    version = int.from_bytes(payload[20:24], byteorder)
    declared_size = int.from_bytes(payload[size_offset : size_offset + 2], byteorder)
    if version != 1 or declared_size < header_size or declared_size > len(payload):
        raise ValueError("invalid ELF binary header")
    machine = int.from_bytes(payload[18:20], byteorder)
    if machine in {62, 183} and payload[4] != 2:
        raise ValueError("64-bit ELF architecture uses a non-64-bit header")

    if payload[4] == 1:
        program_offset = int.from_bytes(payload[28:32], byteorder)
        section_offset = int.from_bytes(payload[32:36], byteorder)
        program_entry_size = int.from_bytes(payload[42:44], byteorder)
        program_count = int.from_bytes(payload[44:46], byteorder)
        section_entry_size = int.from_bytes(payload[46:48], byteorder)
        section_count = int.from_bytes(payload[48:50], byteorder)
        section_name_index = int.from_bytes(payload[50:52], byteorder)
        minimum_program_size, minimum_section_size = 32, 40
    else:
        program_offset = int.from_bytes(payload[32:40], byteorder)
        section_offset = int.from_bytes(payload[40:48], byteorder)
        program_entry_size = int.from_bytes(payload[54:56], byteorder)
        program_count = int.from_bytes(payload[56:58], byteorder)
        section_entry_size = int.from_bytes(payload[58:60], byteorder)
        section_count = int.from_bytes(payload[60:62], byteorder)
        section_name_index = int.from_bytes(payload[62:64], byteorder)
        minimum_program_size, minimum_section_size = 56, 64
    if program_count == 0xFFFF or section_name_index == 0xFFFF:
        raise ValueError("unsupported extended ELF header counts")
    if program_count:
        if (
            program_offset < header_size
            or program_entry_size < minimum_program_size
            or program_offset + program_entry_size * program_count > len(payload)
        ):
            raise ValueError("invalid ELF program header table")
    elif program_offset:
        raise ValueError("ELF program header offset has no entries")
    if section_count:
        if (
            section_offset < header_size
            or section_entry_size < minimum_section_size
            or section_offset + section_entry_size * section_count > len(payload)
            or section_name_index >= section_count
        ):
            raise ValueError("invalid ELF section header table")
    elif section_offset or section_name_index:
        raise ValueError("unsupported extended ELF section header table")
    return _machine_architecture(machine, {62: "x86_64", 183: "arm64"})


def _detect_thin_macho(payload: bytes) -> tuple[int, frozenset[str]] | None:
    magic = payload[:4]
    formats = {
        b"\xfe\xed\xfa\xce": ("big", 28),
        b"\xce\xfa\xed\xfe": ("little", 28),
        b"\xfe\xed\xfa\xcf": ("big", 32),
        b"\xcf\xfa\xed\xfe": ("little", 32),
    }
    detected_format = formats.get(magic)
    if detected_format is None:
        return None
    byteorder, header_size = detected_format
    if len(payload) < header_size:
        raise ValueError("invalid Mach-O binary header")
    machine = int.from_bytes(payload[4:8], byteorder)
    file_type = int.from_bytes(payload[12:16], byteorder)
    command_count = int.from_bytes(payload[16:20], byteorder)
    command_size = int.from_bytes(payload[20:24], byteorder)
    command_end = header_size + command_size
    if (
        not 1 <= file_type <= 12
        or command_end > len(payload)
        or (command_count == 0) != (command_size == 0)
        or command_size < command_count * 8
    ):
        raise ValueError("invalid Mach-O binary header")
    cursor = header_size
    for _ in range(command_count):
        if cursor + 8 > command_end:
            raise ValueError("invalid Mach-O load command table")
        current_size = int.from_bytes(payload[cursor + 4 : cursor + 8], byteorder)
        if current_size < 8 or current_size % 4 or cursor + current_size > command_end:
            raise ValueError("invalid Mach-O load command table")
        cursor += current_size
    if cursor != command_end:
        raise ValueError("invalid Mach-O load command table")
    return machine, _machine_architecture(machine, _MACH_ARCHITECTURES)


def _detect_fat_macho(payload: bytes) -> frozenset[str] | None:
    magic = payload[:4]
    formats = {
        b"\xca\xfe\xba\xbe": ("big", 20, False),
        b"\xbe\xba\xfe\xca": ("little", 20, False),
        b"\xca\xfe\xba\xbf": ("big", 32, True),
        b"\xbf\xba\xfe\xca": ("little", 32, True),
    }
    detected_format = formats.get(magic)
    if detected_format is None:
        return None
    if len(payload) < 8:
        raise ValueError("invalid FAT Mach-O binary header")
    byteorder, width, is_64_bit = detected_format
    count = int.from_bytes(payload[4:8], byteorder)
    table_end = 8 + count * width
    if count == 0 or count > 64 or table_end > len(payload):
        raise ValueError("invalid FAT Mach-O architecture table")

    architectures: set[str] = set()
    ranges: list[tuple[int, int]] = []
    for entry_offset in range(8, table_end, width):
        machine = int.from_bytes(payload[entry_offset : entry_offset + 4], byteorder)
        if is_64_bit:
            slice_offset = int.from_bytes(payload[entry_offset + 8 : entry_offset + 16], byteorder)
            slice_size = int.from_bytes(payload[entry_offset + 16 : entry_offset + 24], byteorder)
            alignment = int.from_bytes(payload[entry_offset + 24 : entry_offset + 28], byteorder)
        else:
            slice_offset = int.from_bytes(payload[entry_offset + 8 : entry_offset + 12], byteorder)
            slice_size = int.from_bytes(payload[entry_offset + 12 : entry_offset + 16], byteorder)
            alignment = int.from_bytes(payload[entry_offset + 16 : entry_offset + 20], byteorder)
        slice_end = slice_offset + slice_size
        if (
            slice_offset < table_end
            or slice_size < 28
            or slice_end > len(payload)
            or alignment > 63
            or slice_offset % (1 << alignment)
        ):
            raise ValueError("invalid FAT Mach-O architecture slice")
        if any(slice_offset < end and start < slice_end for start, end in ranges):
            raise ValueError("overlapping FAT Mach-O architecture slices")
        ranges.append((slice_offset, slice_end))
        thin = _detect_thin_macho(payload[slice_offset:slice_end])
        if thin is None or thin[0] != machine:
            raise ValueError("FAT Mach-O architecture table does not match its slice")
        architectures.update(thin[1])
    return frozenset(architectures)


def _detect_pe(payload: bytes) -> frozenset[str]:
    if len(payload) < 0x40:
        raise ValueError("invalid PE binary header")
    pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
    coff_end = pe_offset + 24
    if pe_offset < 0x40 or coff_end > len(payload):
        raise ValueError("invalid PE binary header")
    if payload[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("invalid PE binary header")
    machine, section_count = struct.unpack_from("<HH", payload, pe_offset + 4)
    optional_size = struct.unpack_from("<H", payload, pe_offset + 20)[0]
    optional_end = coff_end + optional_size
    section_table_end = optional_end + section_count * 40
    if section_count == 0 or optional_size < 112 or section_table_end > len(payload):
        raise ValueError("invalid PE binary header")
    if struct.unpack_from("<H", payload, coff_end)[0] != 0x20B:
        raise ValueError("native PE member must use a PE32+ optional header")
    directory_count = struct.unpack_from("<I", payload, coff_end + 108)[0]
    if directory_count > 16 or 112 + directory_count * 8 > optional_size:
        raise ValueError("invalid PE data directory table")
    for section_offset in range(optional_end, section_table_end, 40):
        raw_size = struct.unpack_from("<I", payload, section_offset + 16)[0]
        raw_offset = struct.unpack_from("<I", payload, section_offset + 20)[0]
        if raw_size and (raw_offset < section_table_end or raw_offset + raw_size > len(payload)):
            raise ValueError("invalid PE section data range")
    return _machine_architecture(machine, {0x8664: "x86_64", 0xAA64: "arm64"})


def detect_binary_architectures(payload: bytes) -> frozenset[str] | None:
    """Return architectures encoded by a structurally valid native binary image."""
    if payload.startswith(b"\x7fELF"):
        return _detect_elf(payload)
    fat = _detect_fat_macho(payload)
    if fat is not None:
        return fat
    thin = _detect_thin_macho(payload)
    if thin is not None:
        return thin[1]
    if payload.startswith(b"MZ"):
        return _detect_pe(payload)
    return None


def _is_launcher(relative: Path) -> bool:
    return relative.as_posix() in {"comic-sol", "comic-sol.exe"}


def _is_native_candidate(relative: Path) -> bool:
    name = relative.name.casefold()
    return _is_launcher(relative) or name.endswith(_NATIVE_SUFFIXES) or ".so." in name


def _validate_architecture(relative: Path, payload: bytes, architecture: str | None) -> None:
    if architecture is None:
        return
    detected = detect_binary_architectures(payload)
    if detected is None:
        if _is_native_candidate(relative):
            raise ValueError(f"native runtime member has no recognized binary header: {relative}")
        return
    if architecture not in detected:
        actual = ", ".join(sorted(detected))
        raise ValueError(
            f"portable runtime architecture mismatch for {relative}: "
            f"requested {architecture}, detected {actual}"
        )


def _archive_target(member: _Member) -> str:
    return f"comic-sol/{member.relative.as_posix()}"


def _write_zip(
    destination: Path,
    root: Path,
    root_descriptor: int | None,
    members: list[_Member],
    directories: dict[Path, _Directory],
    architecture: str | None,
) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as writer:
        for member in members:
            payload = _read_member(root, root_descriptor, member, directories)
            _validate_architecture(member.relative, payload, architecture)
            info = zipfile.ZipInfo(_archive_target(member), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if member.mode & 0o111 else 0o644
            info.external_attr = mode << 16
            writer.writestr(info, payload)


def _write_tar_gz(
    destination: Path,
    root: Path,
    root_descriptor: int | None,
    members: list[_Member],
    directories: dict[Path, _Directory],
    architecture: str | None,
) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as writer:
                for member in members:
                    payload = _read_member(root, root_descriptor, member, directories)
                    _validate_architecture(member.relative, payload, architecture)
                    info = tarfile.TarInfo(_archive_target(member))
                    info.size = len(payload)
                    info.mode = 0o755 if member.mode & 0o111 else 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    writer.addfile(info, fileobj=BytesIO(payload))


@contextmanager
def _temporary_archive(archive: Path) -> Iterator[Path]:
    descriptor, name = tempfile.mkstemp(prefix=f".{archive.name}.", dir=archive.parent)
    os.close(descriptor)
    temporary = Path(name)
    os.chmod(temporary, 0o644)
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def create_portable_archive(
    runtime_dir: Path, archive: Path, architecture: str | None = None
) -> Path:
    """Create a deterministic archive from a verified, regular-file-only runtime tree."""
    archive = Path(archive)
    if not (archive.name.endswith(".zip") or archive.name.endswith(".tar.gz")):
        raise ValueError("portable archive must use .zip or .tar.gz")
    if architecture is not None and architecture not in _ARCHITECTURES:
        raise ValueError("unsupported portable runtime architecture")

    root, members, directories, root_descriptor = _scan_runtime(Path(runtime_dir))
    try:
        names = [_archive_target(member) for member in members]
        validate_runtime_members(names)
        archive.parent.mkdir(parents=True, exist_ok=True)

        with _temporary_archive(archive) as temporary:
            if archive.name.endswith(".zip"):
                _write_zip(
                    temporary,
                    root,
                    root_descriptor,
                    members,
                    directories,
                    architecture,
                )
            else:
                _write_tar_gz(
                    temporary,
                    root,
                    root_descriptor,
                    members,
                    directories,
                    architecture,
                )
            os.replace(temporary, archive)
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
    return archive
