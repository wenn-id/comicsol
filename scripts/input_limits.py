#!/usr/bin/env python3
"""Central resource limits for untrusted Comic Sol project inputs.

Every project-relative JSON document, raster, and persisted narrative field is
untrusted: a local process or an MCP caller can place arbitrarily large, deeply
nested, or sensitive content inside the configured project root. This module
owns the documented bounds those inputs must satisfy before the engine reads,
decodes, or persists them, plus the structured failure that a bound raises.

The limits are deliberately generous compared to every artifact the engine
itself writes, so a legitimate project never reaches them.
"""

from __future__ import annotations

import json
import re
from typing import Any


# Bounded JSON documents: a project artifact never approaches these values.
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ENTRIES = 4096
MAX_JSON_STRING_CHARS = 65536
# The append-only event log is many small JSON documents, not one, so it gets
# its own ceiling sized for a long-lived project.
MAX_EVENT_LOG_BYTES = 8 * 1024 * 1024

# Persisted narrative fields. Titles, transition warnings, and override
# reasons are operator notes, not story content: they must stay short and must
# never carry source text, PII, or credentials.
MAX_TITLE_CHARS = 200
MAX_WARNING_CHARS = 500
MAX_OVERRIDE_REASON_CHARS = 1000

TITLE_LIMIT_MESSAGE = "title must be a non-empty string of at most 200 characters"
REQUEST_TITLE_LIMIT_MESSAGE = (
    "request title must be a non-empty string of at most 200 characters"
)
WARNING_LIMIT_MESSAGE = (
    "transition warning must be a non-empty string of at most 500 characters"
)
OVERRIDE_REASON_LIMIT_MESSAGE = (
    "override reason must be a non-empty string of at most 1000 characters"
)
NARRATIVE_SECRET_MESSAGE = (
    "narrative field must not contain secrets or credentials"
)

# Obvious credential shapes. Narrative fields are operator notes, so anything
# token-shaped or key-shaped is rejected outright rather than redacted: a
# redacted note would still claim to be an accepted override.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?:api[_-]?key|secret|password|token|credential)[=:]\s*\S", re.IGNORECASE),
)


class InputResourceLimitError(ValueError):
    """Raised when untrusted input exceeds a documented resource limit.

    The message always starts with ``security-error: input exceeds`` so both
    public surfaces classify it as ``CS-SEC-002`` without inspecting paths or
    payload content.
    """

    def __init__(self, detail: str) -> None:
        """Initialize with one bounded, path-free description of the limit."""
        super().__init__(f"security-error: input exceeds {detail}")


def looks_like_secret(text: str) -> bool:
    """Report whether a narrative value carries an obvious credential."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def validate_narrative(value: object, *, message: str, max_chars: int) -> str:
    """Validate one persisted narrative field: type, length, secret hygiene.

    Returns the original value unchanged; callers decide whether to strip.
    Raises the caller's stable limit message for type, emptiness, or length
    problems, and the shared secret message for credential-shaped content.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise ValueError(message)
    if looks_like_secret(value):
        raise ValueError(NARRATIVE_SECRET_MESSAGE)
    return value


def _structural_depth_exceeds(text: str, limit: int) -> bool:
    """Scan bracket nesting iteratively, ignoring brackets inside strings.

    A tiny payload such as ten thousand ``[`` bytes would otherwise recurse
    inside the JSON parser before any post-parse walk could bound it, so the
    scan runs before parsing and stops at the first limit violation.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[" or char == "{":
            depth += 1
            if depth > limit:
                return True
        elif char == "]" or char == "}":
            if depth:
                depth -= 1
    return False


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Bound object size while the parser builds it, not afterwards."""
    if len(pairs) > MAX_JSON_ENTRIES:
        raise InputResourceLimitError(
            f"the JSON collection size limit of {MAX_JSON_ENTRIES} entries"
        )
    return dict(pairs)


def _validate_loaded_value(value: Any) -> None:
    """Walk a parsed document with an explicit stack, bounding every level."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            if depth > MAX_JSON_DEPTH:
                raise InputResourceLimitError(
                    f"the JSON nesting depth limit of {MAX_JSON_DEPTH}"
                )
            if len(current) > MAX_JSON_ENTRIES:
                raise InputResourceLimitError(
                    f"the JSON collection size limit of {MAX_JSON_ENTRIES} entries"
                )
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if depth > MAX_JSON_DEPTH:
                raise InputResourceLimitError(
                    f"the JSON nesting depth limit of {MAX_JSON_DEPTH}"
                )
            if len(current) > MAX_JSON_ENTRIES:
                raise InputResourceLimitError(
                    f"the JSON collection size limit of {MAX_JSON_ENTRIES} entries"
                )
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > MAX_JSON_STRING_CHARS:
            raise InputResourceLimitError(
                f"the JSON string length limit of {MAX_JSON_STRING_CHARS} characters"
            )


def loads_bounded_json(
    payload: bytes | str, *, source: str = "JSON document"
) -> Any:
    """Parse one JSON document under every documented input bound.

    Rejects payloads over ``MAX_JSON_BYTES``, nesting deeper than
    ``MAX_JSON_DEPTH``, collections larger than ``MAX_JSON_ENTRIES``, and
    strings longer than ``MAX_JSON_STRING_CHARS``. ``source`` must be a short
    relative label (never an absolute path) so error messages stay safe.
    """
    if isinstance(payload, bytes):
        text = payload.decode("utf-8")
        encoded_size = len(payload)
    elif isinstance(payload, str):
        text = payload
        encoded_size = len(payload.encode("utf-8"))
    else:
        raise TypeError("JSON payload must be bytes or str")
    if encoded_size > MAX_JSON_BYTES:
        raise InputResourceLimitError(
            f"the JSON size limit of {MAX_JSON_BYTES} bytes for {source}"
        )
    if _structural_depth_exceeds(text, MAX_JSON_DEPTH):
        raise InputResourceLimitError(
            f"the JSON nesting depth limit of {MAX_JSON_DEPTH} for {source}"
        )
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs)
    except RecursionError as error:
        raise InputResourceLimitError(
            f"the JSON nesting depth limit of {MAX_JSON_DEPTH} for {source}"
        ) from error
    _validate_loaded_value(value)
    return value
