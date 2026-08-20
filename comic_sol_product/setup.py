"""Transactional client setup, repair, and integration-only uninstall."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .clients import ClientAdapter, CodexAdapter, JsonClientAdapter, mcp_entry


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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        0xC0000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        4,  # OPEN_ALWAYS
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    handle_value = getattr(handle, "value", handle)
    if handle_value == invalid:
        error_number = ctypes.get_last_error()
        raise OSError(error_number, ctypes.FormatError(error_number))
    try:
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        information = _FileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            error_number = ctypes.get_last_error()
            raise OSError(error_number, ctypes.FormatError(error_number))
        if information.attributes & _REPARSE_POINT:
            raise OSError("client config lock must not be a symlink or reparse point")
        return msvcrt.open_osfhandle(handle_value, os.O_RDWR)
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


class _ConfigLock:
    """Cross-process advisory lock for one client config directory."""

    def __init__(self, config_path: Path, timeout: float = 10.0) -> None:
        self.path = Path(config_path).with_name(f".{Path(config_path).name}.lock")
        self.timeout = timeout
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow and os.name != "nt":
            raise OSError("client config lock requires no-follow open support")
        if os.name == "nt":
            descriptor = _open_lock_descriptor(self.path)
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

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES, errno.EDEADLK} or time.monotonic() >= deadline:
                    raise TimeoutError("client config is locked") from error
                time.sleep(0.05)

    def _acquire_windows(self) -> None:
        import msvcrt

        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
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
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
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


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.bak-{stamp}")


def _write_backup(path: Path, data: bytes) -> None:
    """Create one private backup without following a pre-existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _prune_backups(path: Path) -> None:
    """Keep only newest private backups for one client config."""
    backups = []
    for candidate in path.parent.glob(f"{path.name}.bak-*"):
        try:
            metadata = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            backups.append(candidate)
    for candidate in sorted(backups, key=lambda item: item.name, reverse=True)[MAX_CONFIG_BACKUPS:]:
        try:
            candidate.unlink()
        except OSError:
            pass


def _resolve_executable(executable: str | os.PathLike[str] | None) -> str:
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
        os.name != "nt" and not os.access(resolved, os.X_OK)
    ):
        raise FileNotFoundError("Comic Sol executable is not runnable")
    return str(resolved)


def _atomic_write(
    path: Path,
    data: bytes,
    mode: int | None = None,
    *,
    expected: _FileSnapshot | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if expected is None:
            os.replace(temporary, path)
        else:
            _publish_expected(path, temporary, expected)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _rename_exchange(source: Path, destination: Path) -> None:
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
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _publish_expected(path: Path, temporary: Path, expected: _FileSnapshot) -> None:
    """Publish a replacement while retaining and validating the displaced entry."""
    if os.name == "nt":
        import ctypes

        backup = temporary.with_name(f"{temporary.name}.old")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
            error_number = ctypes.get_last_error()
            raise OSError(error_number, ctypes.FormatError(error_number))
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
        _rename_exchange(temporary, path)
    except OSError as error:
        if error.errno not in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        raise OSError(errno.ENOTSUP, "atomic exchange is unavailable on this platform") from error
    exchanged = True
    try:
        displaced = _read_snapshot(temporary)
        if (
            displaced.data != expected.data
            or displaced.mode != expected.mode
            or displaced.device != expected.device
            or displaced.inode != expected.inode
            or displaced.mtime_ns != expected.mtime_ns
        ):
            _rename_exchange(temporary, path)
            exchanged = False
            raise _ConfigChangedError("client config changed during publish")
    except BaseException:
        if exchanged:
            try:
                _rename_exchange(temporary, path)
            except OSError:
                pass
        raise


def _open_snapshot_descriptor(path: Path) -> int:
    """Open a config without following symlinks or Windows reparse points."""
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

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
            error_number = ctypes.get_last_error()
            raise OSError(error_number, ctypes.FormatError(error_number))
        information = _ByHandleFileInformation()
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            error_number = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error_number, ctypes.FormatError(error_number))
        if information.attributes & _REPARSE_POINT:
            kernel32.CloseHandle(handle)
            raise OSError("client config must not be a symlink or reparse point")
        try:
            return msvcrt.open_osfhandle(handle_value, os.O_RDONLY)
        except BaseException:
            kernel32.CloseHandle(handle)
            raise
    raise OSError("client config requires no-follow open support")


def _read_snapshot(path: Path) -> _FileSnapshot:
    """Read bytes and metadata from one no-follow file descriptor."""
    descriptor = _open_snapshot_descriptor(path)
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


def _assert_snapshot(path: Path, snapshot: _FileSnapshot) -> None:
    """Fail closed if the target inode changed since the snapshot."""
    try:
        metadata = path.stat(follow_symlinks=False)
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
        claude = (
            home
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    else:
        claude = home / ".config" / "Claude" / "claude_desktop_config.json"

    adapters.extend(
        [
            JsonClientAdapter("claude-desktop", claude),
            JsonClientAdapter("cursor", home / ".cursor" / "mcp.json"),
            JsonClientAdapter(
                "windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"
            ),
        ]
    )
    return adapters


def _configure_one(adapter: ClientAdapter, entry: dict[str, object], *, remove: bool) -> SetupResult:
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
                updated, changed = adapter.remove(config) if remove else adapter.mutate(config, entry)
            except Exception as error:
                return SetupResult(adapter.name, "failed", str(path), None, str(error))
            if not changed:
                status = "unchanged" if not remove else "not-configured"
                return SetupResult(adapter.name, status, str(path), None, "no config change required")

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
                updated, changed = adapter.remove(config) if remove else adapter.mutate(config, entry)
            except Exception as error:
                return SetupResult(adapter.name, "failed", str(path), None, str(error))
            if not changed:
                status = "unchanged" if not remove else "not-configured"
                return SetupResult(adapter.name, status, str(path), None, "no config change required")

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
            results.append(SetupResult(adapter.name, "skipped", str(adapter.config_path), None, "not selected"))
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
                    SetupResult(name, "unsupported", None, None, "native config format or location is not verified")
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
            results.append(SetupResult(adapter.name, "skipped", str(adapter.config_path), None, "not selected"))
        else:
            results.append(_configure_one(adapter, {}, remove=True))
    if using_defaults:
        present = {adapter.name for adapter in candidates}
        for name in SUPPORTED_CLIENT_NAMES:
            if name not in present and (not chosen or name in chosen):
                results.append(
                    SetupResult(name, "unsupported", None, None, "native config format or location is not verified")
                )
    return results
