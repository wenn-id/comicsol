"""Verified client configuration adapters for Comic Sol.

Adapters only own native parsing and mutation. Transaction, backup, verification,
and rollback are centralized in :mod:`comic_sol_product.setup`.
"""

from __future__ import annotations

import inspect
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
    if Path(command).name not in {"comic-sol", "comic-sol.exe"} and PureWindowsPath(
        command
    ).name not in {
        "comic-sol",
        "comic-sol.exe",
    }:
        return False
    if (
        not isinstance(args, list)
        or len(args) != 3
        or not all(isinstance(item, str) for item in args)
    ):
        return False
    if args[0] != "mcp" or args[1] != "--root" or not isinstance(args[2], str):
        return False
    root = args[2]
    return Path(root).is_absolute() or PureWindowsPath(root).is_absolute()


def _call_verify_hook(hook: Callable[..., bool], expected: dict[str, Any] | None) -> bool:
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return bool(hook(expected))
    try:
        signature.bind(expected)
    except TypeError:
        signature.bind()
        return bool(hook())
    return bool(hook(expected))


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _json_member_spans(text: str, container_start: int, container_end: int):
    decoder = json.JSONDecoder()
    member_spans = []
    offset = container_start
    while offset < container_end:
        while offset < container_end and text[offset] in " \t\r\n,":
            offset += 1
        if offset >= container_end or text[offset] == "}":
            break
        member_start = offset
        try:
            key, _ = decoder.scan_once(text, offset)
        except StopIteration as error:
            raise ValueError("client config member key could not be decoded") from error
        offset = _end_of_value(text, offset)
        while offset < container_end and text[offset] in " \t\r\n":
            offset += 1
        if offset < container_end and text[offset] == ":":
            offset = _end_of_value(text, offset + 1)
        member_spans.append((key, member_start, offset))
    return member_spans


def _end_of_value(text: str, offset: int) -> int:
    while offset < len(text) and text[offset] in " \t\r\n":
        offset += 1
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.scan_once(text, offset)
    except StopIteration as error:
        raise ValueError("client config value could not be decoded") from error
    return end


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

    read_only_preflight = True

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
        if config.get(self.servers_key, {}).get(MCP_SERVER_NAME) == entry:
            return config, False
        updated = dict(config)
        servers = dict(updated.get(self.servers_key, {}))
        servers[MCP_SERVER_NAME] = dict(entry)
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
        return (json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )

    def publish(self, raw: bytes, entry: dict[str, Any]) -> bytes:
        """Return raw with only the product-owned member replaced byte-surgically."""
        text = raw.decode("utf-8")
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError("client config root must be an object")
        servers = document.get(self.servers_key)
        if servers is not None and not isinstance(servers, dict):
            raise ValueError(f"{self.servers_key} must be an object")
        entry_text = json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True)
        owned_key = json.dumps(MCP_SERVER_NAME, ensure_ascii=False)
        owned_pattern = re.compile(rf'"{re.escape(MCP_SERVER_NAME)}"\s*:')
        owned_match = owned_pattern.search(text)
        if owned_match is not None:
            value_start = _end_of_value(text, owned_match.end())
            depth = 0
            index = value_start - 1
            while index < len(text):
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            value_end = index + 1
            context = text[owned_match.start() : value_start]
            indent = _indent_of(context) if context.strip() else ""
            replacement = "\n".join(
                indent + line if line.strip() else line for line in entry_text.splitlines()
            )
            return (
                text[: owned_match.start()] + f"{owned_key}: " + replacement + text[value_end:]
            ).encode("utf-8")

        servers_pattern = re.compile(rf'"{re.escape(self.servers_key)}"\s*:\s*\{{')
        servers_match = servers_pattern.search(text)
        if servers_match is None:
            if servers is not None:
                raise ValueError(f"{self.servers_key} mapping could not be located")
            stripped = text.rstrip()
            if not stripped.endswith("}"):
                raise ValueError("client config root object could not be located")
            body = stripped[:-1].rstrip()
            member_indent = "  "
            member = f"{owned_key}: {entry_text}"
            padded = member.replace("\n", "\n" + member_indent * 2)
            addition = f"{member_indent}{json.dumps(self.servers_key)}: {{\n{member_indent * 2}{padded}\n{member_indent}}}"
            if body.endswith("{"):
                return (body + "\n" + addition + "\n}\n").encode("utf-8")
            return (body + ",\n" + addition + "\n}\n").encode("utf-8")
        open_brace = servers_match.end() - 1
        depth = 0
        index = open_brace
        while index < len(text):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        closing = index
        body = text[open_brace + 1 : closing].rstrip()
        member_indent = "    "
        member = f"{owned_key}: {entry_text}"
        padded = member.replace("\n", "\n" + member_indent)
        if body:
            return (text[:closing] + ",\n" + member_indent + padded + text[closing:]).encode(
                "utf-8"
            )
        return (text[: open_brace + 1] + "\n" + member_indent + padded + text[closing:]).encode(
            "utf-8"
        )

    def verify(self, expected: dict[str, Any] | None = None) -> bool:
        if self._verify_hook is not None:
            return _call_verify_hook(self._verify_hook, expected)
        try:
            value = self.load(self.config_path.read_bytes())
            entry = value.get(self.servers_key, {}).get(MCP_SERVER_NAME)
            return _valid_mcp_entry(entry, expected)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False

    def verify_removed(self) -> bool:
        if self._verify_hook is not None:
            return _call_verify_hook(self._verify_hook, None)
        try:
            value = self.load(self.config_path.read_bytes())
            return MCP_SERVER_NAME not in value.get(self.servers_key, {})
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False


_SECTION_RE = re.compile(
    r"^[ \t]*\[\s*(?:mcp_servers|\"mcp_servers\"|'mcp_servers')\s*\.\s*"
    r"(?:comic-sol|\"comic-sol\"|'comic-sol')\s*\][ \t]*(?:#.*)?(?:\r?\n)?$"
)
_ANY_SECTION_RE = re.compile(r"^[ \t]*\[{1,2}[^\r\n]+\]{1,2}[ \t]*(?:#.*)?(?:\r?\n)?$")


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _scan_toml_line(line: str, state: str | None) -> str | None:
    index = 0
    while index < len(line):
        if state == "basic":
            if line[index] == "\\":
                index += 2
            elif line[index] == '"':
                state = None
                index += 1
            else:
                index += 1
            continue
        if state == "literal":
            if line[index] == "'":
                state = None
            index += 1
            continue
        if state == "multiline-basic":
            if line.startswith('"""', index) and not _is_escaped(line, index):
                state = None
                index += 3
            else:
                index += 1
            continue
        if state == "multiline-literal":
            if line.startswith("'''", index):
                state = None
                index += 3
            else:
                index += 1
            continue

        if line[index] == "#":
            break
        if line.startswith('"""', index):
            state = "multiline-basic"
            index += 3
        elif line.startswith("'''", index):
            state = "multiline-literal"
            index += 3
        elif line[index] == '"':
            state = "basic"
            index += 1
        elif line[index] == "'":
            state = "literal"
            index += 1
        else:
            index += 1
    return state


def _section_spans(text: str, pattern: re.Pattern[str]):
    state: str | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if state is None and pattern.fullmatch(line):
            yield offset, end
        state = _scan_toml_line(line, state)
        offset = end


def _table_end(text: str, start: int, end: int) -> int:
    """Keep trailing TOML comments and whitespace outside a final table."""
    state: str | None = None
    content_end = start
    offset = start
    for line in text[start:end].splitlines(keepends=True):
        line_end = offset + len(line)
        stripped = line.strip()
        if state is not None or (stripped and not stripped.startswith("#")):
            content_end = line_end
        state = _scan_toml_line(line, state)
        offset = line_end
    return content_end


class CodexAdapter:
    """Preserving adapter for Codex ``~/.codex/config.toml``."""

    name = "codex"
    read_only_preflight = True

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
        matches = list(_section_spans(text, _SECTION_RE))
        if not matches:
            if not replacement:
                return text, False
            separator = (
                ""
                if not text or text.endswith("\n\n")
                else ("\n" if text.endswith("\n") else "\n\n")
            )
            return text + separator + replacement, True
        start, _ = matches[0]
        next_sections = [span for span in _section_spans(text, _ANY_SECTION_RE) if span[0] > start]
        end = next_sections[0][0] if next_sections else _table_end(text, start, len(text))
        old = text[start:end]
        if replacement and old.strip() == replacement.strip():
            return text, False
        result = text[:start] + replacement
        if replacement and next_sections and not replacement.endswith("\n\n"):
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
            return _call_verify_hook(self._verify_hook, expected)
        try:
            parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            entry = parsed["mcp_servers"][MCP_SERVER_NAME]
            return _valid_mcp_entry(entry, expected)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return False

    def verify_removed(self) -> bool:
        if self._verify_hook is not None:
            return _call_verify_hook(self._verify_hook, None)
        try:
            parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
            return MCP_SERVER_NAME not in parsed.get("mcp_servers", {})
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return False
