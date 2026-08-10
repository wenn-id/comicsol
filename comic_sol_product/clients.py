"""Verified client configuration adapters for Comic Sol.

Adapters only own native parsing and mutation. Transaction, backup, verification,
and rollback are centralized in :mod:`comic_sol_product.setup`.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Protocol, runtime_checkable


MCP_SERVER_NAME = "comic-sol"


def mcp_entry(executable: str, output_root: Path) -> dict[str, Any]:
    return {
        "command": executable,
        "args": ["mcp", "--root", str(output_root.resolve())],
    }


def _valid_mcp_entry(entry: object, expected: dict[str, Any] | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    if expected is not None and entry != expected:
        return False
    command = entry.get("command")
    args = entry.get("args")
    if not isinstance(command, str) or not command.strip():
        return False
    if Path(command).name not in {"comic-sol", "comic-sol.exe"} and PureWindowsPath(command).name not in {
        "comic-sol",
        "comic-sol.exe",
    }:
        return False
    if not isinstance(args, list) or len(args) < 3 or not all(isinstance(item, str) for item in args):
        return False
    if args[0] != "mcp" or args[1] != "--root" or not isinstance(args[2], str):
        return False
    root = args[2]
    return Path(root).is_absolute() or PureWindowsPath(root).is_absolute()


@runtime_checkable
class ClientAdapter(Protocol):
    name: str
    config_path: Path

    def detect(self) -> bool: ...
    def load(self, raw: bytes) -> Any: ...
    def mutate(self, config: Any, entry: dict[str, Any]) -> tuple[Any, bool]: ...
    def remove(self, config: Any) -> tuple[Any, bool]: ...
    def dump(self, config: Any) -> bytes: ...
    def verify(self, expected: dict[str, Any] | None = None) -> bool: ...


class JsonClientAdapter:
    """Adapter for clients with a verified JSON MCP server mapping."""

    def __init__(
        self,
        name: str,
        config_path: Path,
        servers_key: str = "mcpServers",
        *,
        verify_hook: Callable[..., bool] | None = None,
    ) -> None:
        self.name = name
        self.config_path = Path(config_path)
        self.servers_key = servers_key
        self._verify_hook = verify_hook

    def detect(self) -> bool:
        return self.config_path.is_file()

    def load(self, raw: bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("client config root must be an object")
        servers = value.get(self.servers_key, {})
        if not isinstance(servers, dict):
            raise ValueError(f"{self.servers_key} must be an object")
        return value

    def mutate(self, config: dict[str, Any], entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        updated = dict(config)
        servers = dict(updated.get(self.servers_key, {}))
        if servers.get(MCP_SERVER_NAME) == entry:
            return config, False
        servers[MCP_SERVER_NAME] = entry
        updated[self.servers_key] = servers
        return updated, True

    def remove(self, config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        servers = dict(config.get(self.servers_key, {}))
        if MCP_SERVER_NAME not in servers:
            return config, False
        del servers[MCP_SERVER_NAME]
        updated = dict(config)
        updated[self.servers_key] = servers
        return updated, True

    def dump(self, config: dict[str, Any]) -> bytes:
        return (json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def verify(self, expected: dict[str, Any] | None = None) -> bool:
        if self._verify_hook is not None:
            try:
                return bool(self._verify_hook(expected))
            except TypeError:
                return bool(self._verify_hook())
        try:
            value = self.load(self.config_path.read_bytes())
            entry = value.get(self.servers_key, {}).get(MCP_SERVER_NAME)
            return _valid_mcp_entry(entry, expected)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False


_SECTION_RE = re.compile(r"(?m)^\[mcp_servers\.comic-sol\]\s*$")
_ANY_SECTION_RE = re.compile(r"(?m)^\[[^\n]+\]\s*$")


class CodexAdapter:
    """Preserving adapter for Codex ``~/.codex/config.toml``."""

    name = "codex"

    def __init__(self, config_path: Path, *, verify_hook: Callable[..., bool] | None = None):
        self.config_path = Path(config_path)
        self._verify_hook = verify_hook

    def detect(self) -> bool:
        return self.config_path.is_file()

    def load(self, raw: bytes) -> str:
        text = raw.decode("utf-8")
        parsed = tomllib.loads(text)
        servers = parsed.get("mcp_servers", {})
        if servers is not None and not isinstance(servers, dict):
            raise ValueError("mcp_servers must be a TOML table")
        return text

    @staticmethod
    def _section(entry: dict[str, Any]) -> str:
        command = json.dumps(entry["command"], ensure_ascii=False)
        args = ", ".join(json.dumps(item, ensure_ascii=False) for item in entry["args"])
        return f"[mcp_servers.{MCP_SERVER_NAME}]\ncommand = {command}\nargs = [{args}]\n"

    @staticmethod
    def _replace_or_remove(text: str, replacement: str) -> tuple[str, bool]:
        match = _SECTION_RE.search(text)
        if not match:
            if not replacement:
                return text, False
            separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            return text + separator + replacement, True
        next_section = _ANY_SECTION_RE.search(text, match.end())
        end = next_section.start() if next_section else len(text)
        old = text[match.start():end]
        if replacement and old.strip() == replacement.strip():
            return text, False
        result = text[:match.start()] + replacement
        if replacement and next_section and not replacement.endswith("\n\n"):
            result += "\n"
        result += text[end:]
        return result, True

    def mutate(self, config: str, entry: dict[str, Any]) -> tuple[str, bool]:
        return self._replace_or_remove(config, self._section(entry))

    def remove(self, config: str) -> tuple[str, bool]:
        return self._replace_or_remove(config, "")

    def dump(self, config: str) -> bytes:
        return config.encode("utf-8")

    def verify(self, expected: dict[str, Any] | None = None) -> bool:
        if self._verify_hook is not None:
            try:
                return bool(self._verify_hook(expected))
            except TypeError:
                return bool(self._verify_hook())
        try:
            parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            entry = parsed["mcp_servers"][MCP_SERVER_NAME]
            return _valid_mcp_entry(entry, expected)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return False
