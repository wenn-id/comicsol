"""Transactional client setup, repair, and integration-only uninstall."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
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


def _resolve_executable(executable: str | os.PathLike[str] | None) -> str:
    launcher = os.fspath(
        executable
        if executable is not None
        else (sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    )
    located = launcher if Path(launcher).is_absolute() else shutil.which(launcher)
    if located is None:
        raise FileNotFoundError("Comic Sol executable could not be resolved")
    resolved = Path(located).expanduser().resolve(strict=True)
    if not resolved.is_file() or (
        os.name != "nt" and not os.access(resolved, os.X_OK)
    ):
        raise FileNotFoundError("Comic Sol executable is not runnable")
    return str(resolved)


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
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
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def default_adapters(home: Path | None = None) -> list[ClientAdapter]:
    """Return only adapters whose native formats and locations are verified."""
    home = (home or Path.home()).expanduser()
    adapters: list[ClientAdapter] = [CodexAdapter(home / ".codex" / "config.toml")]

    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        adapters.extend(
            [
                JsonClientAdapter("claude-desktop", roaming / "Claude" / "claude_desktop_config.json"),
                JsonClientAdapter("cursor", home / ".cursor" / "mcp.json"),
                JsonClientAdapter("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
            ]
        )
    else:
        adapters.extend(
            [
                JsonClientAdapter("claude-desktop", home / ".config" / "Claude" / "claude_desktop_config.json"),
                JsonClientAdapter("cursor", home / ".cursor" / "mcp.json"),
                JsonClientAdapter("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
            ]
        )
    return adapters


def _configure_one(adapter: ClientAdapter, entry: dict[str, object], *, remove: bool) -> SetupResult:
    path = adapter.config_path
    if not adapter.detect():
        return SetupResult(adapter.name, "skipped", str(path), None, "client config not found")
    try:
        original = path.read_bytes()
        original_mode = stat.S_IMODE(path.stat().st_mode) if os.name != "nt" else None
        config = adapter.load(original)
    except (OSError, UnicodeError, ValueError) as error:
        return SetupResult(adapter.name, "failed", str(path), None, f"malformed or unreadable config: {error}")

    updated, changed = adapter.remove(config) if remove else adapter.mutate(config, entry)
    if not changed:
        status = "unchanged" if not remove else "not-configured"
        return SetupResult(adapter.name, status, str(path), None, "no config change required")

    backup = _backup_path(path)
    try:
        shutil.copy2(path, backup)
        _atomic_write(path, adapter.dump(updated), original_mode)
        if not remove and not adapter.verify(entry):
            _atomic_write(path, original, original_mode)
            return SetupResult(adapter.name, "rolled-back", str(path), str(backup), "verification failed; original restored")
    except (OSError, UnicodeError, ValueError) as error:
        try:
            if backup.exists():
                _atomic_write(path, original, original_mode)
        except OSError:
            pass
        return SetupResult(adapter.name, "failed", str(path), str(backup) if backup.exists() else None, str(error))

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
    entry = mcp_entry(_resolve_executable(executable), output_root)
    results: list[SetupResult] = []
    for adapter in candidates:
        if chosen and adapter.name not in chosen:
            results.append(SetupResult(adapter.name, "skipped", str(adapter.config_path), None, "not selected"))
        else:
            results.append(_configure_one(adapter, entry, remove=False))
    if using_defaults:
        present = {adapter.name for adapter in candidates}
        for name in SUPPORTED_CLIENT_NAMES:
            if name not in present and (not chosen or name in chosen):
                results.append(
                    SetupResult(name, "unsupported", None, None, "native config format or location is not verified")
                )
    return results


def repair_clients(*args: object, **kwargs: object) -> list[SetupResult]:
    return setup_clients(*args, **kwargs)  # type: ignore[arg-type]


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
