"""Shared trust-boundary helpers for Comic Sol project input and paths."""

from __future__ import annotations

import base64
import errno
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, cast

from .core_primitives import canonical_json_bytes
from .input_limits import (
    MAX_JSON_BYTES,
    InputResourceLimitError,
    loads_bounded_json,
)
from .raster_limits import MAX_ENCODED_RASTER_BYTES


MAX_SOURCE_BYTES = 200 * 1024
SOURCE_SUFFIXES = {".txt", ".md"}
# No-follow read cap for any single contained file. JSON readers pass the
# smaller JSON limit explicitly; rasters and other binary artifacts are the
# only inputs that may approach this bound.
MAX_READ_BYTES = MAX_ENCODED_RASTER_BYTES
_DRIVE = re.compile(r"^[A-Za-z]:")
_LOCK_RETRY_SECONDS = 0.05
PROJECT_OPERATION_LOCK_TIMEOUT = 300.0
# Windows byte-range locks are mandatory, so the locked byte must sit past any
# region readers touch. The PID metadata occupies the first bytes of the file.
_LOCK_BYTE_OFFSET = 4096
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_HAS_NOFOLLOW = _O_NOFOLLOW != 0
_REPARSE_POINT = 0x400


class ProjectLock:
    """Cross-process advisory lock retained at ``.comic-sol.lock``."""

    _thread_state = threading.local()

    def __init__(self, project_dir: Path, timeout: float | None = 10.0):
        """Initialize lock state for a project directory."""
        self.project_dir = Path(project_dir)
        self.timeout = timeout
        self._handle: BinaryIO | None = None
        self._lock_key: Path | None = None
        self._acquisition_depth = 0

    @classmethod
    def _held_locks(cls) -> dict[Path, tuple[BinaryIO, int]]:
        held = getattr(cls._thread_state, "held", None)
        if held is None:
            held = {}
            cls._thread_state.held = held
        return held

    def __enter__(self) -> "ProjectLock":
        """Acquire the project lock and return it."""
        key = self.project_dir.resolve()
        held = self._held_locks()
        existing = held.get(key)
        if existing is not None:
            held[key] = (existing[0], existing[1] + 1)
            self._lock_key = key
            self._acquisition_depth += 1
            return self
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        path = self.project_dir / ".comic-sol.lock"
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            handle = self._open_retained(path)
        else:
            handle = os.fdopen(descriptor, "r+b")
            try:
                handle.write(b"\0")
                handle.flush()
            except BaseException:
                handle.close()
                raise
        self._handle = handle
        acquired = False
        try:
            while True:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    # No metadata means writer crashed between create+write
                    # or truncate+write. Try to acquire flock directly; if
                    # we succeed, no real holder exists (stale lock).
                    try:
                        self._lock(handle)
                        acquired = True
                        break
                    except OSError as error:
                        if not self._retryable(error):
                            raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("project is locked by another process")
                else:
                    try:
                        self._lock(handle)
                        acquired = True
                        break
                    except OSError as error:
                        if not self._retryable(error):
                            raise
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError("project is locked by another process") from error
                if deadline is None:
                    remaining = _LOCK_RETRY_SECONDS
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
            held[key] = (handle, 1)
            self._handle = handle
            self._lock_key = key
            self._acquisition_depth = 1
            return self
        except BaseException:
            try:
                if acquired:
                    try:
                        self._unlock(handle)
                    except BaseException:
                        pass
            finally:
                handle.close()
                self._handle = None
            raise

    @staticmethod
    def _open_retained(path: Path) -> BinaryIO:
        """Reopen an existing lock file without ever following a symlink.

        A symlinked lock path would otherwise be truncated and overwritten with
        PID metadata when the lock is acquired.
        """
        if not _HAS_NOFOLLOW and path.is_symlink():
            raise ValueError("lock path must not be a symlink")
        try:
            descriptor = os.open(path, os.O_RDWR | _O_NOFOLLOW)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise ValueError("lock path must not be a symlink") from error
            raise
        return os.fdopen(descriptor, "r+b")

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        """Acquire the platform-specific lock for an open handle."""
        if os.name == "nt":
            import msvcrt

            handle.seek(_LOCK_BYTE_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _retryable(error: OSError) -> bool:
        """Report whether a lock acquisition error may be retried."""
        return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
            error, "winerror", None
        ) in {33, 36}

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        """Release the platform-specific lock for an open handle."""
        if os.name == "nt":
            import msvcrt

            handle.seek(_LOCK_BYTE_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Release the project lock when leaving its context."""
        if self._lock_key is None or self._acquisition_depth == 0:
            return
        held = self._held_locks()
        existing = held.get(self._lock_key)
        if existing is None:
            self._handle = None
            self._lock_key = None
            self._acquisition_depth = 0
            return
        handle, depth = existing
        key = self._lock_key
        self._acquisition_depth -= 1
        if depth > 1:
            held[key] = (handle, depth - 1)
            if self._acquisition_depth == 0:
                self._handle = None
                self._lock_key = None
            return
        del held[key]
        self._handle = None
        self._lock_key = None
        try:
            self._unlock(handle)
        finally:
            handle.close()


def validate_source_bytes(source: bytes, suffix: str | None = None) -> str:
    """Validate and decode a supported UTF-8 source payload."""
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    if len(source) > MAX_SOURCE_BYTES:
        raise ValueError("source must be at most 200 KiB as UTF-8 bytes")
    if suffix is not None and suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("source file must use .txt or .md")
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("source must be valid UTF-8") from error


def contained_project_path(
    project_dir: Path,
    relative: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a safe relative path within a project directory."""
    text = os.fspath(relative).replace("\\", "/")
    if not text or text.startswith("/") or _DRIVE.match(text) or ".." in text.split("/"):
        raise ValueError("path must be a relative project path")
    root = Path(project_dir).resolve(strict=True)
    unresolved = root.joinpath(*PurePosixPath(text).parts)
    current = unresolved
    while current != root:
        if current.is_symlink():
            raise ValueError("project path must not contain symlinks")
        if os.name == "nt":
            try:
                attributes = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
            except FileNotFoundError:
                attributes = 0
            if attributes & _REPARSE_POINT:
                raise ValueError("project path must not contain symlinks or reparse points")
        current = current.parent
    candidate = unresolved.resolve(strict=must_exist)
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes the project directory")
    return candidate


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    """Return validated relative path components."""
    text = os.fspath(relative).replace("\\", "/")
    if not text or text.startswith("/") or _DRIVE.match(text):
        raise ValueError("path must be a relative project path")
    parts = PurePosixPath(text).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path must be a relative project path")
    return parts


def _stream_mode(flags: int) -> str:
    """Return the binary file-object mode implied by low-level open flags."""
    if flags & os.O_RDWR:
        return "r+b"
    if flags & os.O_WRONLY:
        return "wb"
    return "rb"


def _open_parent_fd(project_dir: Path, parts: tuple[str, ...], *, create: bool) -> tuple[int, str]:
    """Open the no-follow parent directory descriptor for a path."""
    root = Path(project_dir).resolve(strict=True)
    if os.name == "nt" or not _HAS_NOFOLLOW:
        raise NotImplementedError
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
    current = os.open(root, flags)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=current)
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


@contextmanager
def open_contained(
    project_dir: Path, relative: str | Path, *, flags: int = os.O_RDONLY, mode: int = 0
) -> Iterator[BinaryIO]:
    """Open project file without following symlinked path components on POSIX."""
    parts = _relative_parts(relative)
    if os.name == "nt" or not _HAS_NOFOLLOW:
        path = contained_project_path(project_dir, relative, must_exist=not (flags & os.O_CREAT))
        stream = open_path_nofollow(path, flags=flags, mode=mode)
        try:
            yield stream
        finally:
            stream.close()
        return
    else:
        parent_fd, name = _open_parent_fd(project_dir, parts, create=bool(flags & os.O_CREAT))
        try:
            descriptor = os.open(name, flags | _O_NOFOLLOW, mode, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    stream = cast(BinaryIO, os.fdopen(descriptor, _stream_mode(flags)))
    try:
        yield stream
    finally:
        stream.close()


def open_path_nofollow(path: Path, *, flags: int = os.O_RDONLY, mode: int = 0) -> BinaryIO:
    """Open absolute path while refusing symlink components on POSIX."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    absolute = path.absolute()
    parts = absolute.parts
    if not absolute.is_absolute() or len(parts) < 2:
        raise ValueError("path must be absolute")
    if sys.platform == "darwin" and parts[:2] == ("/", "var"):
        absolute = Path("/private", *parts[1:])
        parts = absolute.parts
    if os.name == "nt" or not _HAS_NOFOLLOW:
        current = Path(parts[0])
        for part in parts[1:]:
            current /= part
            if current.is_symlink():
                raise ValueError("path must not contain symlinks or reparse points")
            try:
                attributes = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
            except (AttributeError, FileNotFoundError):
                attributes = 0
            if attributes & _REPARSE_POINT:
                raise ValueError("path must not contain symlinks or reparse points")
        return cast(BinaryIO, os.fdopen(os.open(absolute, flags, mode), _stream_mode(flags)))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
    directory_fd = os.open(parts[0], directory_flags)
    try:
        for part in parts[1:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        descriptor = os.open(parts[-1], flags | _O_NOFOLLOW, mode, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    return cast(BinaryIO, os.fdopen(descriptor, _stream_mode(flags)))


def _read_bounded_stream(stream: BinaryIO, *, max_bytes: int) -> bytes:
    """Read one open stream while enforcing a documented byte ceiling.

    The size is checked through the descriptor before reading so a multi-
    gigabyte file is refused without loading it, and the read itself is capped
    one byte above the limit to catch a file that grows between the two.
    """
    st = os.fstat(stream.fileno())
    if not stat.S_ISREG(st.st_mode):
        raise InputResourceLimitError("the regular file requirement")
    size = st.st_size
    if size > max_bytes:
        raise InputResourceLimitError(f"the file size limit of {max_bytes} bytes")
    payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise InputResourceLimitError(f"the file size limit of {max_bytes} bytes")
    return payload


def read_bytes_nofollow(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> bytes:
    """Read bounded bytes from an absolute path without following symlinks."""
    with open_path_nofollow(Path(path)) as stream:
        return _read_bounded_stream(stream, max_bytes=max_bytes)


def read_json_nofollow(path: Path, *, require_object: bool = True) -> object:
    """Read and parse one bounded no-follow JSON document."""
    path = Path(path)
    payload = read_bytes_nofollow(path, max_bytes=MAX_JSON_BYTES)
    value = loads_bounded_json(payload, source=path.name)
    if require_object and not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def read_contained_bytes(
    project_dir: Path,
    relative: str | Path,
    *,
    max_bytes: int = MAX_READ_BYTES,
) -> bytes:
    """Read bounded bytes from a safe relative project path."""
    try:
        with open_contained(project_dir, relative) as stream:
            return _read_bounded_stream(stream, max_bytes=max_bytes)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError("project path must not contain symlinks") from error
        raise


def read_contained_json(project_dir: Path, relative: str | Path) -> object:
    """Read and parse one bounded contained JSON document."""
    payload = read_contained_bytes(project_dir, relative, max_bytes=MAX_JSON_BYTES)
    return loads_bounded_json(payload, source=os.fspath(relative).replace("\\", "/"))


def remove_contained(project_dir: Path, relative: str | Path) -> None:
    """Remove a safe relative project path if it exists."""
    parts = _relative_parts(relative)
    if os.name == "nt" or not _HAS_NOFOLLOW:
        path = contained_project_path(project_dir, relative)
        path.unlink(missing_ok=True)
        return
    parent_fd, name = _open_parent_fd(project_dir, parts, create=False)
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(parent_fd)


def replace_contained(project_dir: Path, source: str | Path, destination: str | Path) -> None:
    """Atomically replace destination from source with no-follow parent traversal."""
    source_parts = _relative_parts(source)
    destination_parts = _relative_parts(destination)
    if os.name == "nt" or not _HAS_NOFOLLOW:
        source_path = contained_project_path(project_dir, source, must_exist=True)
        destination_path = contained_project_path(project_dir, destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, destination_path)
        return
    source_fd, source_name = _open_parent_fd(project_dir, source_parts, create=False)
    try:
        destination_fd, destination_name = _open_parent_fd(
            project_dir, destination_parts, create=True
        )
        try:
            os.replace(
                source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd
            )
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def fsync_directory(path: Path) -> None:
    """Persist directory metadata; Windows has no stdlib directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_atomic_write(path: Path, payload: bytes) -> None:
    """Atomically publish bytes and durably persist file and directory metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _find_transaction_dir(transaction_dir: Path) -> int:
    """Return the next available numeric transaction ID."""
    biggest = 0
    if transaction_dir.is_dir():
        for entry in transaction_dir.iterdir():
            try:
                value = int(entry.name)
                if value > biggest:
                    biggest = value
            except (ValueError, OSError):
                pass
    return biggest + 1


class ProjectTransaction:
    """Durable journal-backed all-or-nothing batch of file replacements.

    Acquires ``ProjectLock`` on enter, creates a numbered transaction
    directory under ``logs/transactions/<id>/``, writes a durable canonical
    journal before the first replace, and either commits or rolls back on exit.
    """

    JOURNAL_SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        project_dir: Path,
        operation: str,
        *,
        lock_timeout: float | None = PROJECT_OPERATION_LOCK_TIMEOUT,
    ) -> None:
        """Initialize an unpublished project transaction."""
        self.project_dir = Path(project_dir)
        self.operation = operation
        self.lock_timeout = lock_timeout
        self._lock: ProjectLock | None = None
        self._dir: Path | None = None
        self._journal: list[dict] = []
        self._phase: str | None = None
        self._id: int | None = None

    def __enter__(self) -> "ProjectTransaction":
        """Start a transaction while holding the project lock."""
        self._lock = ProjectLock(self.project_dir, timeout=self.lock_timeout).__enter__()
        try:
            base = contained_project_path(self.project_dir, "logs/transactions")
            base.mkdir(parents=True, exist_ok=True)
            self._id = _find_transaction_dir(base)
            self._dir = base / str(self._id)
            self._dir.mkdir(parents=True)
            self._phase = "staging"
            return self
        except BaseException:
            self._lock.__exit__(*sys.exc_info())
            raise

    def stage_bytes(self, relative: str, payload: bytes) -> None:
        """Back up old destination (if any) and store staged payload under the
        transaction directory, recording an entry in the in-memory journal."""
        if self._dir is None:
            raise RuntimeError("transaction not started")
        path = Path(relative)
        if path.is_absolute():
            raise ValueError("stage_bytes requires a relative path")
        # Reject traversal and use one canonical path for validation and publication.
        resolved = contained_project_path(self.project_dir, relative)
        if resolved.resolve() == self.project_dir.resolve():
            raise ValueError(f"path '{relative}' resolves to the project root")
        relative = resolved.relative_to(self.project_dir.resolve()).as_posix()
        if any(
            entry.get("operation") == "append" and entry.get("path") == relative
            for entry in self._journal
        ):
            raise ValueError("cannot mix append and replacement for one path")
        dest = resolved
        index = len(self._journal) + 1
        backup_name = f"backup-{index:03d}-{path.name}"
        staged_name = f"staged-{index:03d}-{path.name}"
        backup_path = self._dir / backup_name
        staged_path = self._dir / staged_name
        if dest.is_file():
            durable_atomic_write(
                backup_path,
                read_contained_bytes(self.project_dir, relative),
            )
        durable_atomic_write(staged_path, payload)
        entry = {
            "path": relative,
            "backup": (f"logs/transactions/{self._id}/{backup_name}" if dest.is_file() else None),
            "staged": f"logs/transactions/{self._id}/{staged_name}",
        }
        self._journal.append(entry)

    @staticmethod
    def _jsonl_append_position(handle: BinaryIO) -> tuple[int, int, bytes]:
        """Return ``(original_size, safe_append_offset, torn_tail)``.

        Only bytes after the final newline are inspected. A valid unterminated
        final JSON object gets a separator; an invalid tail is discarded on
        publish. The common case reads one byte, not the whole log.
        """
        handle.seek(0, os.SEEK_END)
        original_size = handle.tell()
        if original_size == 0:
            return 0, 0, b""
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return original_size, original_size, b""
        cursor = original_size
        later_chunks: list[bytes] = []
        while cursor:
            size = min(64 * 1024, cursor)
            cursor -= size
            handle.seek(cursor)
            chunk = handle.read(size)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                tail = chunk[newline + 1 :] + b"".join(reversed(later_chunks))
                safe_offset = cursor + newline + 1
                try:
                    json.loads(tail)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return original_size, safe_offset, tail
                return original_size, original_size, b""
            later_chunks.append(chunk)
        tail = b"".join(reversed(later_chunks))
        try:
            json.loads(tail)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return original_size, 0, tail
        return original_size, original_size, b""

    def append_bytes(
        self,
        relative: str,
        payload: bytes,
        *,
        repair_torn_jsonl: bool = False,
    ) -> None:
        """Journal an append without copying the existing destination."""
        if self._dir is None:
            raise RuntimeError("transaction not started")
        if not isinstance(payload, bytes):
            raise TypeError("append_bytes payload must be bytes")
        path = Path(relative)
        if path.is_absolute():
            raise ValueError("append_bytes requires a relative path")
        resolved = contained_project_path(self.project_dir, relative)
        if resolved.resolve() == self.project_dir.resolve():
            raise ValueError(f"path '{relative}' resolves to the project root")
        relative = resolved.relative_to(self.project_dir.resolve()).as_posix()
        existing = next(
            (
                entry
                for entry in self._journal
                if entry.get("operation") == "append" and entry.get("path") == relative
            ),
            None,
        )
        if existing is not None:
            existing["payload"] = base64.b64encode(
                base64.b64decode(existing["payload"], validate=True) + payload
            ).decode("ascii")
            return
        if any(entry.get("path") == relative for entry in self._journal):
            raise ValueError("cannot mix append and replacement for one path")
        exists = resolved.is_file()
        original_size = 0
        repair_size = 0
        append_payload = payload
        original_tail = b""
        if exists:
            flags = os.O_RDONLY
            with open_contained(self.project_dir, relative, flags=flags) as handle:
                original_size = handle.seek(0, os.SEEK_END)
                repair_size = original_size
                if repair_torn_jsonl:
                    original_size, repair_size, original_tail = self._jsonl_append_position(handle)
                    if repair_size == original_size and original_size:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            append_payload = b"\n" + payload
        self._journal.append(
            {
                "operation": "append",
                "path": relative,
                "original_exists": exists,
                "original_size": original_size,
                "repair_size": repair_size,
                "original_tail": base64.b64encode(original_tail).decode("ascii"),
                "payload": base64.b64encode(append_payload).decode("ascii"),
            }
        )

    def _apply_append(self, entry: dict) -> None:
        """Apply one journalled append and persist its bytes."""
        destination = contained_project_path(self.project_dir, entry["path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = base64.b64decode(entry["payload"], validate=True)
        with open_contained(
            self.project_dir,
            entry["path"],
            flags=os.O_RDWR | os.O_CREAT,
            mode=0o600,
        ) as handle:
            handle.seek(entry["repair_size"])
            handle.truncate()
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(destination.parent)

    def _rollback_append(self, entry: dict) -> None:
        """Restore one append target to its exact pre-transaction bytes."""
        destination = contained_project_path(self.project_dir, entry["path"])
        if entry["original_exists"]:
            original_tail = base64.b64decode(entry.get("original_tail", ""), validate=True)
            with open_contained(
                self.project_dir,
                entry["path"],
                flags=os.O_RDWR | os.O_CREAT,
                mode=0o600,
            ) as handle:
                handle.truncate(entry["repair_size"])
                handle.seek(entry["repair_size"])
                handle.write(original_tail)
                handle.truncate(entry["original_size"])
                handle.flush()
                os.fsync(handle.fileno())
        else:
            remove_contained(self.project_dir, entry["path"])
        if destination.parent.is_dir():
            fsync_directory(destination.parent)

    def commit(self) -> None:
        """Durably write the canonical journal, then atomically replace each
        target. On any replace failure, restore backups in reverse order."""
        if self._dir is None:
            raise RuntimeError("transaction not started")
        if self._phase != "staging":
            raise RuntimeError("transaction already committed or rolling back")
        self._phase = "publishing"
        self._write_journal()
        published: list[tuple[Path, dict]] = []
        try:
            for entry in self._journal:
                if entry.get("operation") == "append":
                    dest = contained_project_path(self.project_dir, entry["path"])
                    published.append((dest, entry))
                    self._apply_append(entry)
                    continue
                dest = contained_project_path(self.project_dir, entry["path"])
                staged = contained_project_path(self.project_dir, entry["staged"], must_exist=True)
                if os.name == "nt" or not _HAS_NOFOLLOW:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, dest)
                else:
                    replace_contained(self.project_dir, entry["staged"], entry["path"])
                published.append((dest, entry))
                fsync_directory(dest.parent)
            self._phase = "committed"
            self._write_journal()
            self._cleanup()
        except BaseException:
            for dest, entry in reversed(published):
                if entry.get("operation") == "append":
                    self._rollback_append(entry)
                    continue
                if entry.get("backup"):
                    backup = contained_project_path(self.project_dir, entry["backup"])
                    if backup.is_file():
                        if os.name == "nt" or not _HAS_NOFOLLOW:
                            os.replace(backup, dest)
                        else:
                            replace_contained(self.project_dir, entry["backup"], entry["path"])
                        fsync_directory(dest.parent)
                else:
                    try:
                        remove_contained(self.project_dir, entry["path"])
                    except OSError:
                        pass
            self._phase = "rolled_back"
            self._write_journal()
            raise

    def _write_journal(self) -> None:
        """Persist the transaction journal in canonical JSON."""
        if self._dir is None:
            return
        journal = {
            "schema_version": self.JOURNAL_SCHEMA_VERSION,
            "operation": self.operation,
            "phase": self._phase,
            "targets": self._journal,
        }
        durable_atomic_write(self._dir / "journal.json", canonical_json_bytes(journal))

    def _cleanup(self) -> None:
        """Remove completed transaction staging artifacts."""
        if self._dir is None or not self._dir.is_dir():
            return
        for child in self._dir.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        parent = self._dir.parent
        try:
            self._dir.rmdir()
        except OSError:
            pass
        fsync_directory(parent)
        self._dir = None

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Commit or roll back the transaction on context exit."""
        try:
            if exc_type is None and self._phase == "staging":
                self.commit()
            elif exc_type is not None and self._phase in ("staging", "publishing"):
                self._phase = "rolled_back"
                self._write_journal()
            if self._phase in ("committed", "rolled_back"):
                self._cleanup()
        finally:
            lock = self._lock
            self._lock = None
            if lock is not None:
                lock.__exit__(exc_type, exc, traceback)

    @staticmethod
    def recover(project_dir: Path) -> None:
        """Roll back incomplete journals while holding the project lock."""
        project_dir = Path(project_dir)
        base = contained_project_path(project_dir, "logs/transactions")
        if not base.is_dir():
            return
        with ProjectLock(project_dir):
            ids: list[int] = []
            for entry in base.iterdir():
                try:
                    ids.append(int(entry.name))
                except (ValueError, OSError):
                    continue
            for tid in sorted(ids):
                tx_dir = base / str(tid)
                journal_path = tx_dir / "journal.json"
                if not journal_path.is_file():
                    continue
                try:
                    journal = loads_bounded_json(
                        read_bytes_nofollow(journal_path, max_bytes=MAX_JSON_BYTES),
                        source="journal.json",
                    )
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
                if not isinstance(journal, dict):
                    continue
                phase = journal.get("phase")
                targets = journal.get("targets")
                if not isinstance(targets, list):
                    continue
                if phase in ("staging", "publishing", "rolled_back"):
                    valid = True
                    for entry in targets:
                        if not isinstance(entry, dict):
                            valid = False
                            break
                        if not isinstance(entry.get("path"), str):
                            valid = False
                            break
                        operation = entry.get("operation", "write")
                        if operation not in {"write", "append"}:
                            valid = False
                            break
                        if operation == "write" and "backup" not in entry:
                            valid = False
                            break
                    if not valid:
                        continue
                    for entry in reversed(targets):
                        if entry.get("operation") == "append":
                            ProjectTransaction(project_dir, "recovery")._rollback_append(entry)
                            continue
                        dest = contained_project_path(project_dir, entry["path"])
                        backup_path = entry.get("backup")
                        if backup_path:
                            backup = contained_project_path(project_dir, backup_path)
                            if backup.is_file():
                                if os.name == "nt" or not _HAS_NOFOLLOW:
                                    os.replace(backup, dest)
                                else:
                                    replace_contained(project_dir, backup_path, entry["path"])
                                fsync_directory(dest.parent)
                        else:
                            try:
                                remove_contained(project_dir, entry["path"])
                                fsync_directory(dest.parent)
                            except OSError:
                                pass
                for child in tx_dir.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                try:
                    tx_dir.rmdir()
                except OSError:
                    pass
                fsync_directory(base)
