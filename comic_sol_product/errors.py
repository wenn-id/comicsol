"""Canonical structured error taxonomy shared by CLI and MCP surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Literal


REQUIRED_NAMESPACES = frozenset({"IMG", "PROJ", "QA", "FONT", "MCP", "INSTALL", "EXPORT", "SEC"})


@dataclass(frozen=True)
class ErrorDefinition:
    """Stable public description of one recoverable error class."""

    code: str
    category: str
    message: str
    reason: str
    recovery: str


@dataclass(frozen=True)
class ClassifiedError:
    """A definition selected for one exception without exposing its raw text."""

    definition: ErrorDefinition

    @property
    def code(self) -> str:
        return self.definition.code

    @property
    def category(self) -> str:
        return self.definition.category

    @property
    def message(self) -> str:
        return self.definition.message

    @property
    def reason(self) -> str:
        return self.definition.reason

    @property
    def recovery(self) -> str:
        return self.definition.recovery


_DEFINITIONS = (
    ErrorDefinition(
        "CS-MCP-001",
        "invalid-request",
        "The MCP request is invalid.",
        "A tool argument did not satisfy the MCP request contract.",
        "Correct the highlighted argument and retry the tool.",
    ),
    ErrorDefinition(
        "CS-MCP-002",
        "internal-error",
        "The MCP tool could not complete.",
        "The tool encountered an unexpected internal failure.",
        "Retry once; if it persists, inspect the server diagnostic logs.",
    ),
    ErrorDefinition(
        "CS-PROJ-001",
        "invalid-data",
        "Project data is invalid.",
        "A project file or request value does not match the Comic Sol schema.",
        "Validate or restore the project data, then retry the command.",
    ),
    ErrorDefinition(
        "CS-PROJ-002",
        "not-found",
        "Required project data was not found.",
        "The requested project or project artifact does not exist.",
        "Check the project identifier/path and initialize or restore the missing data.",
    ),
    ErrorDefinition(
        "CS-PROJ-003",
        "permission-denied",
        "Project data could not be accessed.",
        "The current process lacks permission to read or write project data.",
        "Grant access to the project directory and retry.",
    ),
    ErrorDefinition(
        "CS-PROJ-004",
        "io-error",
        "A project data operation failed.",
        "The filesystem returned an I/O failure while handling project data.",
        "Check storage availability and filesystem permissions, then retry.",
    ),
    ErrorDefinition(
        "CS-PROJ-005",
        "internal-error",
        "The Comic Sol operation could not complete.",
        "The project pipeline encountered an unexpected runtime failure.",
        "Retry once; if it persists, inspect the diagnostic logs before retrying.",
    ),
    ErrorDefinition(
        "CS-INSTALL-001",
        "missing-extra",
        "A required Comic Sol component is unavailable.",
        "The installed distribution does not contain a required optional component.",
        "Reinstall Comic Sol with the required extra and retry.",
    ),
    ErrorDefinition(
        "CS-SEC-001",
        "security-error",
        "The project failed a security boundary check.",
        "A path, symlink, or other input crossed a protected project boundary.",
        "Remove the unsafe input and retry from a trusted project directory.",
    ),
    ErrorDefinition(
        "CS-SEC-002",
        "security-error",
        "The project input exceeded a resource limit.",
        "A project JSON document, raster, or narrative field exceeded a documented size, depth, or length limit.",
        "Shrink or simplify the input to the documented limit and retry.",
    ),
    ErrorDefinition(
        "CS-IMG-001",
        "image-error",
        "An image operation could not complete.",
        "An image asset was missing, unreadable, or outside supported limits.",
        "Use a supported image asset and retry the operation.",
    ),
    ErrorDefinition(
        "CS-QA-001",
        "quality-error",
        "Quality assurance did not pass.",
        "A required deterministic or visual quality check reported a failure.",
        "Review the QA evidence, correct the failing artifact, and rerun QA.",
    ),
    ErrorDefinition(
        "CS-FONT-001",
        "font-error",
        "The required font operation could not complete.",
        "A font was missing, invalid, or could not be loaded.",
        "Install or select a supported font and retry lettering.",
    ),
    ErrorDefinition(
        "CS-EXPORT-001",
        "export-error",
        "The comic export could not be created.",
        "The final artifact could not be rendered or published.",
        "Resolve the reported project or output issue and retry export.",
    ),
)

ERROR_DEFINITIONS = {definition.code: definition for definition in _DEFINITIONS}
_BY_CATEGORY = {definition.category: definition for definition in _DEFINITIONS}


def _safe_raw_message(error: Exception) -> str:
    """Return a bounded diagnostic signal without exposing paths or secrets."""
    message = str(error)
    if not message:
        return ""
    for token in message.replace("=", " ").replace(":", " ").split():
        candidate = token.strip("'\"(),;")
        if PurePosixPath(candidate).is_absolute():
            return "path"
        if PureWindowsPath(candidate).is_absolute():
            return "path"
    return message.lower()[:240]


def safe_error_detail(error: Exception) -> str:
    """Return actionable exception detail with absolute paths redacted."""
    message = str(error)
    if not message:
        return type(error).__name__

    def replace_quoted_path(match: re.Match[str]) -> str:
        quote, candidate = match.group(1), match.group(2)
        if PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
            return f"{quote}<path>{quote}"
        return match.group(0)

    message = re.sub(r"(['\"])([^'\"]+)\1", replace_quoted_path, message)
    for token in message.split():
        candidate = token.strip("'\"(),:;")
        if candidate and (
            PurePosixPath(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute()
        ):
            message = message.replace(candidate, "<path>")
    return message


def classify_exception(
    error: Exception,
    *,
    command: str | None = None,
    surface: Literal["cli", "mcp"] = "cli",
    request: bool = False,
) -> ClassifiedError:
    """Map an internal exception to one canonical public error definition."""
    raw = _safe_raw_message(error)
    if raw.startswith("security-error: input exceeds"):
        definition = ERROR_DEFINITIONS["CS-SEC-002"]
    elif raw.startswith("security-error"):
        definition = ERROR_DEFINITIONS["CS-SEC-001"]
    elif request or raw.startswith(
        ("invalid project id", "unknown validation stage", "attempt path")
    ):
        definition = ERROR_DEFINITIONS["CS-MCP-001"]
    elif isinstance(error, FileNotFoundError):
        definition = ERROR_DEFINITIONS["CS-PROJ-002"]
    elif isinstance(error, PermissionError):
        definition = ERROR_DEFINITIONS["CS-PROJ-003"]
    elif isinstance(error, UnicodeError):
        definition = ERROR_DEFINITIONS["CS-PROJ-001"]
    elif isinstance(error, (ValueError, TypeError)):
        definition = ERROR_DEFINITIONS["CS-PROJ-001"]
    elif isinstance(error, OSError):
        definition = ERROR_DEFINITIONS["CS-PROJ-004"]
    elif isinstance(error, RuntimeError):
        if surface == "mcp":
            definition = ERROR_DEFINITIONS["CS-MCP-002"]
        elif raw.startswith(
            (
                "mcp support is not installed",
                "comic sol engine files are missing",
                "install cyclonedx-bom",
            )
        ):
            definition = ERROR_DEFINITIONS["CS-INSTALL-001"]
        else:
            definition = ERROR_DEFINITIONS["CS-PROJ-005"]
    else:
        definition = (
            ERROR_DEFINITIONS["CS-MCP-002"]
            if surface == "mcp"
            else ERROR_DEFINITIONS["CS-PROJ-001"]
        )
    return ClassifiedError(definition)


def error_payload(
    error: Exception,
    *,
    command: str | None = None,
    surface: Literal["cli", "mcp"] = "cli",
    request: bool = False,
) -> dict[str, str | None]:
    """Serialize the stable machine-readable error fields."""
    classified = classify_exception(error, command=command, surface=surface, request=request)
    return {
        "code": classified.code,
        "category": classified.category,
        "message": classified.message,
        "reason": classified.reason,
        "recovery": classified.recovery,
        "command": command,
    }


def format_human_error(
    error: Exception,
    *,
    command: str | None = None,
    surface: Literal["cli", "mcp"] = "cli",
    request: bool = False,
) -> str:
    """Render a readable CLI/MCP diagnostic from the canonical payload."""
    payload = error_payload(error, command=command, surface=surface, request=request)
    detail = safe_error_detail(error)
    return (
        f"ERROR {payload['code']} [{payload['category']}]: {payload['message']}\n"
        f"Detail: {detail}\n"
        f"Reason: {payload['reason']}\n"
        f"Recovery: {payload['recovery']}"
    )
