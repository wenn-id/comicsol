"""Transactional client setup, repair, and integration-only uninstall."""

from __future__ import annotations

import errno
import importlib
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Protocol

from .clients import (
    ClientAdapter,
    CodexAdapter,
    JsonClientAdapter,
    _call_verify_hook,
    mcp_entry,
)
from .errors import (
    IntegrationRepairError,
    IntegrationRollbackError,
    error_payload,
    safe_error_detail,
)


SUPPORTED_CLIENT_NAMES = (
    "codex",
    "hermes",
    "claude-desktop",
    "claude-code",
    "cursor",
    "vscode",
    "windsurf",
)
MAX_CONFIG_BACKUPS = 5
_REPARSE_POINT = 0x400


def _open_lock_descriptor(path: Path) -> int:
    """Open a Windows lock file without following reparse points."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        0xC0000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002,
        None,
        4,  # OPEN_ALWAYS
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    handle_value = getattr(handle, "value", handle)
    if handle_value == invalid:
        error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
    try:
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        information = _FileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
        if information.attributes & _REPARSE_POINT:
            raise OSError("client config lock must not be a symlink or reparse point")
        return msvcrt.open_osfhandle(handle_value, os.O_RDWR)  # type: ignore[attr-defined]
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


@dataclass(frozen=True)
class _FileSnapshot:
    data: bytes
    mode: int | None
    device: int
    inode: int
    mtime_ns: int
    size: int


class _ConfigChangedError(RuntimeError):
    """The client changed its config during our read-modify-write cycle."""


class _ConfigDirectory:
    """Retain a verified config directory while one repair transaction runs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self.descriptor: int | None = None
        self._windows_handles: list[int] = []
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> "_ConfigDirectory":
        _assert_safe_path_components(self.path)
        if os.name == "nt":
            self._open_windows_components()
        else:
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if not nofollow:
                raise OSError("client config repair requires no-follow directory support")
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
            parts = self.path.parts
            current = os.open(parts[0], flags)
            try:
                for part in parts[1:]:
                    child = os.open(part, flags, dir_fd=current)
                    os.close(current)
                    current = child
                self.descriptor = current
                metadata = os.fstat(current)
                self._identity = (metadata.st_dev, metadata.st_ino)
            except BaseException:
                os.close(current)
                raise
        return self

    def _open_windows_components(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        current = Path(self.path.anchor)
        for part in self.path.parts[1:]:
            current /= part
            handle = kernel32.CreateFileW(
                str(current),
                0x80000000,  # GENERIC_READ
                0x00000001 | 0x00000002,  # share read/write, but not delete
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            invalid = ctypes.c_void_p(-1).value
            value = getattr(handle, "value", handle)
            if value == invalid:
                error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
                self.close()
                raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
            self._windows_handles.append(value)
            try:
                attributes = getattr(current.lstat(), "st_file_attributes", 0)
            except BaseException:
                self.close()
                raise
            if attributes & _REPARSE_POINT:
                self.close()
                raise OSError("client config path must not contain reparse points")

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        if self.descriptor is None:
            if os.name == "nt" and not (flags & os.O_CREAT):
                return _open_snapshot_descriptor(self.path / name)
            return os.open(self.path / name, flags, mode)
        return os.open(name, flags, mode, dir_fd=self.descriptor)

    def replace(self, source: str, destination: str) -> None:
        if self.descriptor is None:
            os.replace(self.path / source, self.path / destination)
        else:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )

    def unlink(self, name: str, *, missing_ok: bool = False) -> None:
        try:
            if self.descriptor is None:
                (self.path / name).unlink()
            else:
                os.unlink(name, dir_fd=self.descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def assert_bound(self) -> None:
        _assert_safe_path_components(self.path)
        if self.descriptor is not None and self._identity is not None:
            metadata = self.path.stat(follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) != self._identity:
                raise _ConfigChangedError("client config directory changed during repair")

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self._windows_handles:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            for handle in reversed(self._windows_handles):
                kernel32.CloseHandle(handle)
            self._windows_handles.clear()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _ConfigLock:
    """Cross-process advisory lock for one client config directory."""

    def __init__(
        self,
        config_path: Path,
        timeout: float = 10.0,
        *,
        directory: _ConfigDirectory | None = None,
    ) -> None:
        self.path = Path(config_path).with_name(f".{Path(config_path).name}.lock")
        self.timeout = timeout
        self.directory = directory
        self._handle: BinaryIO | None = None

    def __enter__(self):
        if self.directory is None:
            _assert_safe_path_components(self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _assert_safe_path_components(self.path.parent)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow and os.name != "nt":
            raise OSError("client config lock requires no-follow open support")
        if os.name == "nt":
            descriptor = _open_lock_descriptor(self.path)
        elif self.directory is not None:
            descriptor = self.directory.open(
                self.path.name, os.O_RDWR | os.O_CREAT | nofollow, 0o600
            )
        else:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
        self._handle = os.fdopen(descriptor, "r+b")
        if os.name != "nt" and self._handle is not None:
            os.fchmod(self._handle.fileno(), 0o600)
        try:
            if os.name == "nt":
                self._acquire_windows()
            else:
                self._acquire_posix()
            if os.name != "nt":
                os.fchmod(self._handle.fileno(), 0o600)
            return self
        except BaseException:
            self._close()
            raise

    def _acquire_posix(self) -> None:
        import fcntl

        if self._handle is None:
            raise RuntimeError("client config lock handle is unavailable")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
                return
            except OSError as error:
                if (
                    error.errno not in {errno.EAGAIN, errno.EACCES, errno.EDEADLK}
                    or time.monotonic() >= deadline
                ):
                    raise TimeoutError("client config is locked") from error
                time.sleep(0.05)

    def _acquire_windows(self) -> None:
        import msvcrt

        if self._handle is None:
            raise RuntimeError("client config lock handle is unavailable")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self._handle.seek(0)
            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                    1,  # type: ignore[attr-defined]
                )
                return
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError("client config is locked") from error
                time.sleep(0.05)

    def _close(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                try:
                    msvcrt.locking(  # type: ignore[attr-defined]
                        self._handle.fileno(),
                        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                        1,  # type: ignore[attr-defined]
                    )
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        finally:
            self._handle.close()
            self._handle = None

    def __exit__(self, exc_type, exc_value, traceback):
        self._close()
        return False


@dataclass(frozen=True)
class SetupResult:
    client: str
    status: str
    config_path: str | None
    backup_path: str | None
    message: str


@dataclass(frozen=True)
class RepairResult:
    client: str
    state: str
    status: str
    action: str
    config_path: str | None
    backup_path: str | None
    backup_required: bool
    planned_entry: dict[str, object] | None
    verified: bool
    restored: bool | None
    message: str
    error: dict[str, str | None] | None


class _RepairTargetProtocol(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def config_path(self) -> Path | None: ...


@dataclass(frozen=True)
class _RepairTarget:
    name: str
    config_path: Path | None = None


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.bak-{stamp}")


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if sys.platform == "darwin" and error.errno in {
            errno.EINVAL,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            return
        raise


def _write_backup(path: Path, data: bytes, *, directory: _ConfigDirectory | None = None) -> None:
    """Create one private backup without following a pre-existing path."""
    if directory is None:
        _assert_safe_path_components(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path_components(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (
        directory.open(path.name, flags, 0o600)
        if directory is not None
        else os.open(path, flags, 0o600)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)  # type: ignore[attr-defined]
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if os.name != "nt":
        directory_descriptor = (
            os.dup(directory.descriptor)
            if directory is not None and directory.descriptor is not None
            else os.open(path.parent, os.O_RDONLY)
        )
        try:
            _fsync_directory(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def _prune_backups(path: Path, *, directory: _ConfigDirectory | None = None) -> None:
    """Keep only newest private backups for one client config."""
    backups = []
    entries = (
        os.scandir(directory.descriptor)
        if directory and directory.descriptor
        else os.scandir(path.parent)
    )
    with entries:
        for entry in entries:
            if not entry.name.startswith(f"{path.name}.bak-"):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                backups.append(path.with_name(entry.name))
    for candidate in sorted(backups, key=lambda item: item.name, reverse=True)[MAX_CONFIG_BACKUPS:]:
        try:
            if directory is None:
                candidate.unlink()
            else:
                directory.unlink(candidate.name)
        except OSError:
            pass


def _resolve_executable(
    executable: str | os.PathLike[str] | None, *, require_runnable: bool = True
) -> str:
    launcher = os.fspath(
        executable
        if executable is not None
        else (sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    )
    candidate = Path(launcher).expanduser()
    if candidate.is_absolute():
        located = str(candidate) if candidate.is_file() else None
        if located is None and os.name == "nt":
            pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
            for extension in filter(None, pathext.split(os.pathsep)):
                extended = candidate.with_name(candidate.name + extension)
                if extended.is_file():
                    located = str(extended)
                    break
    else:
        located = shutil.which(launcher)
    if located is None:
        raise FileNotFoundError("Comic Sol executable could not be resolved")
    resolved = Path(located).expanduser().resolve(strict=True)
    if not resolved.is_file() or (
        require_runnable and os.name != "nt" and not os.access(resolved, os.X_OK)
    ):
        raise FileNotFoundError("Comic Sol executable is not runnable")
    return str(resolved)


def _mcp_runtime_available() -> bool:
    try:
        try:
            server_module = importlib.import_module("mcp.server.fastmcp")
            exceptions_module = importlib.import_module("mcp.server.fastmcp.exceptions")
            api_name = "FastMCP"
        except ModuleNotFoundError:
            server_module = importlib.import_module("mcp.server.mcpserver")
            exceptions_module = importlib.import_module("mcp.server.mcpserver.exceptions")
            api_name = "MCPServer"
        return hasattr(server_module, api_name) and hasattr(exceptions_module, "ToolError")
    except Exception:
        return False


def _verify_launcher_identity(executable: str) -> None:
    if Path(executable).name.lower() not in {"comic-sol", "comic-sol.exe"}:
        raise RuntimeError("Comic Sol executable identity check failed")
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or not completed.stdout.startswith("comic-sol "):
        raise RuntimeError("Comic Sol executable identity check failed")
    version = completed.stdout.strip().split()
    if len(version) != 2 or not version[1][0].isdigit():
        raise RuntimeError("Comic Sol executable identity check failed")


def _atomic_write(
    path: Path,
    data: bytes,
    mode: int | None = None,
    *,
    expected: _FileSnapshot | None = None,
    directory: _ConfigDirectory | None = None,
) -> None:
    if directory is None:
        _assert_safe_path_components(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path_components(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
    else:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = directory.open(
            temporary.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if mode is not None:
                os.fchmod(stream.fileno(), mode)  # type: ignore[attr-defined]
            os.fsync(stream.fileno())
        if expected is None:
            if directory is None:
                os.replace(temporary, path)
            else:
                directory.replace(temporary.name, path.name)
        else:
            _publish_expected(path, temporary, expected, directory=directory)
        if os.name != "nt":
            directory_descriptor = (
                os.dup(directory.descriptor)
                if directory is not None and directory.descriptor is not None
                else os.open(path.parent, os.O_RDONLY)
            )
            try:
                _fsync_directory(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if directory is None:
            temporary.unlink(missing_ok=True)
        else:
            directory.unlink(temporary.name, missing_ok=True)


def _rename_exchange(
    source: Path,
    destination: Path,
    *,
    directory: _ConfigDirectory | None = None,
) -> None:
    """Atomically exchange two entries where the host exposes that primitive."""
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        flags = 0x2  # RENAME_EXCHANGE
        at_fdcwd = -100
    elif sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        flags = 0x2  # RENAME_SWAP
        at_fdcwd = -2
    else:
        function = None
        at_fdcwd = 0
        flags = 0
    if function is None:
        raise OSError(errno.ENOTSUP, "atomic exchange is unavailable on this platform")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    directory_descriptor = (
        directory.descriptor
        if directory is not None and directory.descriptor is not None
        else at_fdcwd
    )
    source_name = source.name if directory is not None else os.fspath(source)
    destination_name = destination.name if directory is not None else os.fspath(destination)
    result = function(
        directory_descriptor,
        os.fsencode(source_name),
        directory_descriptor,
        os.fsencode(destination_name),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _publish_expected(
    path: Path,
    temporary: Path,
    expected: _FileSnapshot,
    *,
    directory: _ConfigDirectory | None = None,
) -> None:
    """Publish a replacement while retaining and validating the displaced entry."""
    if os.name == "nt":
        import ctypes

        backup = temporary.with_name(f"{temporary.name}.old")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        replace_file.restype = ctypes.c_int
        if not replace_file(str(path), str(temporary), str(backup), 0, None, None):
            error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
        try:
            displaced = _read_snapshot(backup)
            if displaced.data != expected.data or displaced.size != expected.size:
                os.replace(backup, path)
                raise _ConfigChangedError("client config changed during publish")
            backup.unlink(missing_ok=True)
        except BaseException:
            if backup.exists():
                try:
                    os.replace(backup, path)
                except OSError:
                    pass
            raise
        return

    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise OSError(errno.ENOTSUP, "atomic exchange is unavailable on this platform")
    try:
        _rename_exchange(temporary, path, directory=directory)
    except OSError as error:
        if sys.platform == "darwin" and error.errno in {
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            _assert_snapshot(path, expected, directory=directory)
            if directory is None:
                os.replace(temporary, path)
            else:
                directory.replace(temporary.name, path.name)
            return
        if error.errno not in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        raise OSError(errno.ENOTSUP, "atomic exchange is unavailable on this platform") from error
    exchanged = True
    try:
        displaced = _read_snapshot(temporary, directory=directory)
        if (
            displaced.data != expected.data
            or displaced.mode != expected.mode
            or displaced.device != expected.device
            or displaced.inode != expected.inode
            or displaced.mtime_ns != expected.mtime_ns
        ):
            _rename_exchange(temporary, path, directory=directory)
            exchanged = False
            raise _ConfigChangedError("client config changed during publish")
    except BaseException:
        if exchanged:
            try:
                _rename_exchange(temporary, path, directory=directory)
            except OSError:
                pass
        raise


def _assert_safe_path_components(path: Path) -> None:
    """Refuse symlink or reparse-point components in an existing absolute path."""
    absolute = Path(path).absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if sys.platform == "darwin" and current in {Path("/var"), Path("/tmp")}:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise OSError("client config path must not contain symlinks or reparse points")


def _open_snapshot_descriptor(path: Path) -> int:
    """Open a config without following symlinks or Windows reparse points."""
    _assert_safe_path_components(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        return os.open(path, os.O_RDONLY | nofollow)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("access_time", wintypes.FILETIME),
                ("write_time", wintypes.FILETIME),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        handle_value = getattr(handle, "value", handle)
        if handle_value == invalid:
            error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
        information = _ByHandleFileInformation()
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            error_number = ctypes.get_last_error()  # type: ignore[attr-defined]
            kernel32.CloseHandle(handle)
            raise OSError(error_number, ctypes.FormatError(error_number))  # type: ignore[attr-defined]
        if information.attributes & _REPARSE_POINT:
            kernel32.CloseHandle(handle)
            raise OSError("client config must not be a symlink or reparse point")
        try:
            return msvcrt.open_osfhandle(handle_value, os.O_RDONLY)  # type: ignore[attr-defined]
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
    raise OSError("client config requires no-follow open support")


def _read_snapshot(path: Path, *, directory: _ConfigDirectory | None = None) -> _FileSnapshot:
    """Read bytes and metadata from one no-follow file descriptor."""
    descriptor = (
        directory.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if directory is not None
        else _open_snapshot_descriptor(path)
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("client config is not a regular file")
        data = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            data += chunk
        after = os.fstat(descriptor)
        if (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
            stat.S_IMODE(metadata.st_mode),
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_size,
            stat.S_IMODE(after.st_mode),
        ):
            raise _ConfigChangedError("client config changed while being read")
        return _FileSnapshot(
            data=data,
            mode=stat.S_IMODE(metadata.st_mode) if os.name != "nt" else None,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mtime_ns=metadata.st_mtime_ns,
            size=metadata.st_size,
        )
    finally:
        os.close(descriptor)


def _assert_snapshot(
    path: Path,
    snapshot: _FileSnapshot,
    *,
    directory: _ConfigDirectory | None = None,
) -> None:
    """Fail closed if the target inode changed since the snapshot."""
    try:
        if directory is None:
            metadata = path.stat(follow_symlinks=False)
        else:
            descriptor = directory.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise _ConfigChangedError("client config disappeared before publish") from error
    current = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_size,
        stat.S_IMODE(metadata.st_mode) if snapshot.mode is not None else None,
    )
    expected = (
        snapshot.device,
        snapshot.inode,
        snapshot.mtime_ns,
        snapshot.size,
        snapshot.mode,
    )
    if current != expected:
        raise _ConfigChangedError("client config changed before publish")


def _verify_persisted(
    adapter: ClientAdapter,
    path: Path,
    entry: dict[str, object],
    *,
    remove: bool,
) -> bool:
    """Validate bytes on disk without letting adapter errors escape."""
    try:
        if remove:
            verifier = getattr(adapter, "verify_removed", None)
            if callable(verifier):
                return bool(verifier())
            persisted = adapter.load(path.read_bytes())
            _, changed = adapter.remove(persisted)
            return not changed
        return bool(adapter.verify(entry))
    except Exception:
        return False


def default_adapters(home: Path | None = None) -> list[ClientAdapter]:
    """Return only adapters whose native formats and locations are verified."""
    home = (home or Path.home()).expanduser()
    adapters: list[ClientAdapter] = [CodexAdapter(home / ".codex" / "config.toml")]

    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        claude = roaming / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        claude = home / ".config" / "Claude" / "claude_desktop_config.json"

    adapters.extend(
        [
            JsonClientAdapter("claude-desktop", claude),
            JsonClientAdapter("cursor", home / ".cursor" / "mcp.json"),
            JsonClientAdapter("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
        ]
    )
    return adapters


def _configure_one(
    adapter: ClientAdapter, entry: dict[str, object], *, remove: bool
) -> SetupResult:
    path = adapter.config_path
    try:
        detected = adapter.detect()
    except Exception as error:
        return SetupResult(adapter.name, "failed", str(path), None, str(error))
    if not detected:
        return SetupResult(adapter.name, "skipped", str(path), None, "client config not found")

    try:
        if getattr(adapter, "read_only_preflight", False):
            try:
                snapshot = _read_snapshot(path)
                config = adapter.load(snapshot.data)
            except Exception as error:
                return SetupResult(
                    adapter.name,
                    "failed",
                    str(path),
                    None,
                    f"malformed or unreadable config: {error}",
                )
            try:
                updated, changed = (
                    adapter.remove(config) if remove else adapter.mutate(config, entry)
                )
            except Exception as error:
                return SetupResult(adapter.name, "failed", str(path), None, str(error))
            if not changed:
                status = "unchanged" if not remove else "not-configured"
                return SetupResult(
                    adapter.name, status, str(path), None, "no config change required"
                )

        with _ConfigLock(path):
            try:
                snapshot = _read_snapshot(path)
                config = adapter.load(snapshot.data)
            except Exception as error:
                return SetupResult(
                    adapter.name,
                    "failed",
                    str(path),
                    None,
                    f"malformed or unreadable config: {error}",
                )
            try:
                updated, changed = (
                    adapter.remove(config) if remove else adapter.mutate(config, entry)
                )
            except Exception as error:
                return SetupResult(adapter.name, "failed", str(path), None, str(error))
            if not changed:
                status = "unchanged" if not remove else "not-configured"
                return SetupResult(
                    adapter.name, status, str(path), None, "no config change required"
                )

            backup = _backup_path(path)
            backup_created = False
            published: _FileSnapshot | None = None
            try:
                _assert_snapshot(path, snapshot)
                _write_backup(backup, snapshot.data)
                backup_created = True
                _assert_snapshot(path, snapshot)
                _atomic_write(
                    path,
                    adapter.dump(updated),
                    snapshot.mode,
                    expected=snapshot,
                )
                published = _read_snapshot(path)
                if not _verify_persisted(adapter, path, entry, remove=remove):
                    _assert_snapshot(path, published)
                    _atomic_write(
                        path,
                        snapshot.data,
                        snapshot.mode,
                        expected=published,
                    )
                    _prune_backups(path)
                    return SetupResult(
                        adapter.name,
                        "rolled-back",
                        str(path),
                        str(backup),
                        "verification failed; original restored",
                    )
                _assert_snapshot(path, published)
                _prune_backups(path)
            except Exception as error:
                try:
                    if backup_created and published is not None:
                        _assert_snapshot(path, published)
                        _atomic_write(
                            path,
                            snapshot.data,
                            snapshot.mode,
                            expected=published,
                        )
                except (OSError, _ConfigChangedError):
                    pass
                return SetupResult(
                    adapter.name,
                    "failed",
                    str(path),
                    str(backup) if backup_created else None,
                    str(error),
                )
    except (OSError, UnicodeError, ValueError, TimeoutError, _ConfigChangedError) as error:
        return SetupResult(adapter.name, "failed", str(path), None, str(error))

    status = "removed" if remove else "configured"
    return SetupResult(adapter.name, status, str(path), str(backup), "integration updated")


def _repair_error_payload(
    error: Exception, *, rollback_failed: bool = False
) -> dict[str, str | None]:
    public_error = (
        IntegrationRollbackError("client integration rollback could not be verified")
        if rollback_failed
        else IntegrationRepairError("client integration repair failed")
    )
    return error_payload(
        public_error,
        command="repair",
        detail=safe_error_detail(error),
    )


def _repair_failure(
    adapter: _RepairTargetProtocol,
    error: Exception,
    *,
    status: str = "failed",
    action: str = "none",
    backup_path: Path | None = None,
    backup_required: bool = False,
    planned_entry: dict[str, object] | None = None,
    restored: bool | None = None,
    rollback_failed: bool = False,
) -> RepairResult:
    config_path = getattr(adapter, "config_path", None)
    return RepairResult(
        client=adapter.name,
        state="failure",
        status=status,
        action=action,
        config_path=str(config_path) if config_path is not None else None,
        backup_path=str(backup_path) if backup_path is not None else None,
        backup_required=backup_required,
        planned_entry=planned_entry,
        verified=False,
        restored=restored,
        message=(
            "rollback could not be verified; restore the backup and run comic-sol doctor"
            if rollback_failed
            else "repair failed; run comic-sol doctor and retry"
        ),
        error=_repair_error_payload(error, rollback_failed=rollback_failed),
    )


def _repair_noop(
    adapter: _RepairTargetProtocol,
    entry: dict[str, object] | None,
    *,
    status: str = "unchanged",
    message: str = "no repair required",
    verified: bool = True,
) -> RepairResult:
    config_path = getattr(adapter, "config_path", None)
    return RepairResult(
        client=adapter.name,
        state="no-op",
        status=status,
        action="none",
        config_path=str(config_path) if config_path is not None else None,
        backup_path=None,
        backup_required=False,
        planned_entry=entry,
        verified=verified,
        restored=None,
        message=message,
        error=None,
    )


def _restore_snapshot(
    path: Path,
    original: _FileSnapshot,
    current: _FileSnapshot,
    *,
    directory: _ConfigDirectory | None = None,
) -> bool:
    try:
        _assert_snapshot(path, current, directory=directory)
        _atomic_write(
            path,
            original.data,
            original.mode,
            expected=current,
            directory=directory,
        )
        restored = _read_snapshot(path, directory=directory)
        if restored.data != original.data or restored.mode != original.mode:
            return False
        return True
    except (OSError, _ConfigChangedError):
        return False


def _repair_one(
    adapter: ClientAdapter,
    entry: dict[str, object],
    *,
    dry_run: bool,
) -> RepairResult:
    path = adapter.config_path
    if dry_run:
        try:
            dry_snapshot = _read_snapshot(path)
            config = adapter.load(dry_snapshot.data)
            _, changed = adapter.mutate(config, entry)
        except Exception as error:
            return _repair_failure(adapter, error, planned_entry=entry)
        if not changed:
            return _repair_noop(adapter, entry, message="no config change required")
        return RepairResult(
            client=adapter.name,
            state="success",
            status="planned",
            action="set-comic-sol-entry",
            config_path=str(path),
            backup_path=None,
            backup_required=True,
            planned_entry=entry,
            verified=False,
            restored=None,
            message="would update the Comic Sol integration",
            error=None,
        )

    backup: Path | None = None
    backup_created = False
    publish_attempted = False
    candidate_data: bytes | None = None
    snapshot: _FileSnapshot | None = None
    with _ConfigDirectory(path.parent) as directory:
        try:
            with _ConfigLock(path, directory=directory):
                snapshot = _read_snapshot(path, directory=directory)
                config = adapter.load(snapshot.data)
                updated, changed = adapter.mutate(config, entry)
                if not changed:
                    _assert_snapshot(path, snapshot, directory=directory)
                    return _repair_noop(adapter, entry, message="no config change required")
                candidate_data = (
                    adapter.publish(snapshot.data, entry)
                    if hasattr(adapter, "publish")
                    else adapter.dump(updated)
                )
                backup = _backup_path(path)
                _assert_snapshot(path, snapshot, directory=directory)
                _write_backup(backup, snapshot.data, directory=directory)
                backup_created = True
                if _read_snapshot(backup, directory=directory).data != snapshot.data:
                    raise OSError("client config backup verification failed")
                _assert_snapshot(path, snapshot, directory=directory)
                _atomic_write(
                    path,
                    candidate_data,
                    snapshot.mode,
                    expected=snapshot,
                    directory=directory,
                )
                publish_attempted = True
                persisted = _read_snapshot(path, directory=directory)
                try:
                    persisted_config = adapter.load(persisted.data)
                    _, still_changed = adapter.mutate(persisted_config, entry)
                    verified = not still_changed
                    verify_hook = getattr(adapter, "_verify_hook", None)
                    if verify_hook is not None:
                        hook_verified = _call_verify_hook(verify_hook, entry)
                        verified = verified and hook_verified
                except Exception:
                    verified = False
                if not verified:
                    try:
                        if not _restore_snapshot(path, snapshot, persisted, directory=directory):
                            return _repair_failure(
                                adapter,
                                IntegrationRollbackError(
                                    "restored client config did not match the backup"
                                ),
                                status="rollback-failed",
                                action="set-comic-sol-entry",
                                backup_path=backup,
                                backup_required=True,
                                planned_entry=entry,
                                restored=False,
                                rollback_failed=True,
                            )
                        _prune_backups(path, directory=directory)
                        return _repair_failure(
                            adapter,
                            IntegrationRepairError("persisted repair verification failed"),
                            status="rolled-back",
                            action="set-comic-sol-entry",
                            backup_path=backup,
                            backup_required=True,
                            planned_entry=entry,
                            restored=True,
                        )
                    except Exception as rollback_error:
                        try:
                            final = _read_snapshot(path, directory=directory)
                            if (
                                snapshot is not None
                                and final.data == snapshot.data
                                and final.mode == snapshot.mode
                            ):
                                return _repair_failure(
                                    adapter,
                                    rollback_error,
                                    status="rolled-back",
                                    action="set-comic-sol-entry",
                                    backup_path=backup,
                                    backup_required=True,
                                    planned_entry=entry,
                                    restored=True,
                                )
                        except (OSError, _ConfigChangedError):
                            pass
                        return _repair_failure(
                            adapter,
                            rollback_error,
                            status="rollback-failed",
                            action="set-comic-sol-entry",
                            backup_path=backup,
                            backup_required=True,
                            planned_entry=entry,
                            restored=False,
                            rollback_failed=True,
                        )
                _assert_snapshot(path, persisted, directory=directory)
                directory.assert_bound()
                _prune_backups(path, directory=directory)
        except Exception as error:
            if not publish_attempted:
                if snapshot is None:
                    return _repair_failure(
                        adapter,
                        error,
                        action="set-comic-sol-entry",
                        backup_required=True,
                        planned_entry=entry,
                    )
                try:
                    current = _read_snapshot(path, directory=directory)
                    changed_after_failure = snapshot is None or current.data != snapshot.data
                except (OSError, _ConfigChangedError):
                    changed_after_failure = True
                if not changed_after_failure:
                    return _repair_failure(
                        adapter,
                        error,
                        action="set-comic-sol-entry",
                        backup_path=backup if backup_created else None,
                        backup_required=True,
                        planned_entry=entry,
                    )
            try:
                current = _read_snapshot(path, directory=directory)
                if (
                    snapshot is not None
                    and current.data == snapshot.data
                    and current.mode == snapshot.mode
                ):
                    restored = True
                elif (
                    snapshot is not None
                    and candidate_data is not None
                    and current.data == candidate_data
                ):
                    restored = _restore_snapshot(path, snapshot, current, directory=directory)
                else:
                    restored = False
            except (OSError, _ConfigChangedError):
                restored = False
            if not restored:
                return _repair_failure(
                    adapter,
                    error,
                    status="rollback-failed",
                    action="set-comic-sol-entry",
                    backup_path=backup if backup_created else None,
                    backup_required=True,
                    planned_entry=entry,
                    restored=False,
                    rollback_failed=True,
                )
            return _repair_failure(
                adapter,
                error,
                status="rolled-back",
                action="set-comic-sol-entry",
                backup_path=backup if backup_created else None,
                backup_required=True,
                planned_entry=entry,
                restored=True,
            )

    return RepairResult(
        client=adapter.name,
        state="success",
        status="configured",
        action="set-comic-sol-entry",
        config_path=str(path),
        backup_path=str(backup),
        backup_required=True,
        planned_entry=entry,
        verified=True,
        restored=None,
        message="integration updated",
        error=None,
    )


def repair_clients(
    output_root: Path,
    selected: Iterable[str] | None = None,
    home: Path | None = None,
    *,
    adapters: Iterable[ClientAdapter] | None = None,
    executable: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> list[RepairResult]:
    output_root = Path(output_root).expanduser().resolve()
    chosen = set(selected or ())
    using_defaults = adapters is None
    candidates = list(adapters if adapters is not None else default_adapters(home))
    known_clients = set(SUPPORTED_CLIENT_NAMES) | {adapter.name for adapter in candidates}
    unknown = sorted(chosen - known_clients)
    if unknown:
        raise ValueError(f"unsupported client: {unknown[0]}")
    entry: dict[str, object] | None = None
    results: list[RepairResult] = []
    for adapter in candidates:
        if chosen and adapter.name not in chosen:
            results.append(
                _repair_noop(
                    adapter,
                    None,
                    status="skipped",
                    message="not selected",
                    verified=False,
                )
            )
            continue
        try:
            detected = adapter.detect()
        except Exception as error:
            results.append(_repair_failure(adapter, error))
            continue
        if not detected:
            results.append(
                _repair_noop(
                    adapter,
                    None,
                    status="skipped",
                    message="client config not found; no third-party config was created",
                    verified=False,
                )
            )
            continue
        if entry is None:
            try:
                resolved_executable = _resolve_executable(executable, require_runnable=False)
                _verify_launcher_identity(resolved_executable)
                if not _mcp_runtime_available():
                    raise RuntimeError("MCP support is unavailable; run comic-sol doctor")
                entry = mcp_entry(resolved_executable, output_root)
            except Exception as error:
                results.append(_repair_failure(adapter, error))
                continue
        try:
            results.append(_repair_one(adapter, entry, dry_run=dry_run))
        except Exception as error:
            results.append(_repair_failure(adapter, error))
    if using_defaults:
        present = {adapter.name for adapter in candidates}
        for name in SUPPORTED_CLIENT_NAMES:
            if name in present or (chosen and name not in chosen):
                continue
            unsupported = _RepairTarget(name)
            if chosen:
                results.append(
                    _repair_failure(
                        unsupported,
                        IntegrationRepairError("native config format or location is not verified"),
                    )
                )
            else:
                results.append(
                    _repair_noop(
                        unsupported,
                        None,
                        status="unsupported",
                        message="native config format or location is not verified",
                        verified=False,
                    )
                )
    return results


def setup_clients(
    output_root: Path,
    selected: Iterable[str] | None = None,
    home: Path | None = None,
    *,
    adapters: Iterable[ClientAdapter] | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> list[SetupResult]:
    output_root = Path(output_root).expanduser().resolve()
    chosen = set(selected or ())
    using_defaults = adapters is None
    candidates = list(adapters if adapters is not None else default_adapters(home))
    entry: dict[str, object] | None = None
    results: list[SetupResult] = []
    for adapter in candidates:
        if chosen and adapter.name not in chosen:
            results.append(
                SetupResult(adapter.name, "skipped", str(adapter.config_path), None, "not selected")
            )
        else:
            try:
                detected = adapter.detect()
            except Exception as error:
                results.append(
                    SetupResult(adapter.name, "failed", str(adapter.config_path), None, str(error))
                )
                continue
            if not detected:
                results.append(
                    SetupResult(
                        adapter.name,
                        "skipped",
                        str(adapter.config_path),
                        None,
                        "client config not found",
                    )
                )
            else:
                if entry is None:
                    entry = mcp_entry(_resolve_executable(executable), output_root)
                results.append(_configure_one(adapter, entry, remove=False))
    if using_defaults:
        present = {adapter.name for adapter in candidates}
        for name in SUPPORTED_CLIENT_NAMES:
            if name not in present and (not chosen or name in chosen):
                results.append(
                    SetupResult(
                        name,
                        "unsupported",
                        None,
                        None,
                        "native config format or location is not verified",
                    )
                )
    return results


def uninstall_clients(
    output_root: Path,
    selected: Iterable[str] | None = None,
    home: Path | None = None,
    *,
    adapters: Iterable[ClientAdapter] | None = None,
) -> list[SetupResult]:
    del output_root  # Projects are deliberately outside integration removal.
    chosen = set(selected or ())
    using_defaults = adapters is None
    candidates = list(adapters if adapters is not None else default_adapters(home))
    results: list[SetupResult] = []
    for adapter in candidates:
        if chosen and adapter.name not in chosen:
            results.append(
                SetupResult(adapter.name, "skipped", str(adapter.config_path), None, "not selected")
            )
        else:
            results.append(_configure_one(adapter, {}, remove=True))
    if using_defaults:
        present = {adapter.name for adapter in candidates}
        for name in SUPPORTED_CLIENT_NAMES:
            if name not in present and (not chosen or name in chosen):
                results.append(
                    SetupResult(
                        name,
                        "unsupported",
                        None,
                        None,
                        "native config format or location is not verified",
                    )
                )
    return results
