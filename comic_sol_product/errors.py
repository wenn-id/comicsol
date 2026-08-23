"""Canonical structured error taxonomy shared by CLI and MCP surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Literal


REQUIRED_NAMESPACES = frozenset(
    {"IMG", "PROJ", "QA", "FONT", "MCP", "INSTALL", "EXPORT", "SEC", "CLI"}
)


@dataclass(frozen=True)
class ErrorDefinition:
    """Stable public description of one recoverable error class."""

    code: str
    category: str
    message: str
    reason: str
    recovery: str


class CliUsageError(Exception):
    """Raised when argument parsing fails so the surfaces stay fail-closed.

    Replacing argparse's ``SystemExit`` with this signal lets every surface emit
    its canonical envelope for a parse failure instead of leaking usage text.
    """

    def __init__(self, message: str) -> None:
        """Initialize with argparse's bounded, path-free diagnostic message."""
        super().__init__(message)
        self.message = message


class ValidationFailureError(ValueError):
    """Signal that a completed project inspection reported validation issues."""

    def __init__(self, issue_count: int) -> None:
        """Initialize with the number of reported validation issues."""
        super().__init__(f"project validation reported {issue_count} issue(s)")
        self.issue_count = issue_count


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
    ErrorDefinition(
        "CS-CLI-001",
        "invalid-request",
        "The command line is invalid.",
        "An argument did not satisfy the CLI usage contract.",
        "Check the command usage with --help and retry.",
    ),
)

ERROR_DEFINITIONS = {definition.code: definition for definition in _DEFINITIONS}

# Classifier lookup surfaces. An owning boundary is reached either through its
# typed exception class name (the engine raises typed ValueError subclasses
# that must not be re-imported here) or through its stable message prefix.
_BOUNDARY_TYPE_NAMES = {
    "PdfExportError": "CS-EXPORT-001",
    "PdfQualityError": "CS-QA-001",
    "PageQualityMigrationError": "CS-QA-001",
    "ProjectValidationError": "CS-QA-001",
    "ValidationFailureError": "CS-QA-001",
    "TypographyPreflightError": "CS-FONT-001",
    "CliUsageError": "CS-CLI-001",
}

_BOUNDARY_MESSAGE_PREFIXES = (
    ("security-error: input exceeds", "CS-SEC-002"),
    ("security-error", "CS-SEC-001"),
    ("page_qa_required:", "CS-QA-001"),
    ("panel is not a readable image", "CS-IMG-001"),
    ("source is not a readable image", "CS-IMG-001"),
    ("source image format must be", "CS-IMG-001"),
    ("source image exceeds the decoded pixel limit", "CS-IMG-001"),
    ("missing required lettered panel image", "CS-IMG-001"),
    ("font policy", "CS-FONT-001"),
    ("font script override", "CS-FONT-001"),
    ("font is not a readable TrueType/OpenType file", "CS-FONT-001"),
)


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
    if any(
        separator in token.strip("'\"(),:;")
        for token in message.split()
        for separator in ("/", "\\")
    ):
        # A path containing spaces cannot be bounded token-wise, so a surviving
        # path separator means segments may have escaped redaction; drop the
        # raw text and keep only the exception type name.
        return type(error).__name__
    return message


def classify_exception(
    error: Exception,
    *,
    command: str | None = None,
    surface: Literal["cli", "mcp"] = "cli",
    request: bool = False,
) -> ClassifiedError:
    """Map an internal exception to one canonical public error definition.

    Boundary prefixes are matched against the raw lowercased message (not a
    redacted form) because owning boundaries append paths after their stable
    prefix; classification itself never re-emits that text.
    """
    raw = str(error).lower()[:240]
    boundary_code = _BOUNDARY_TYPE_NAMES.get(type(error).__name__)
    if boundary_code is None:
        for prefix, code in _BOUNDARY_MESSAGE_PREFIXES:
            if raw.startswith(prefix):
                boundary_code = code
                break
    if boundary_code is not None:
        definition = ERROR_DEFINITIONS[boundary_code]
    elif request or (
        surface == "mcp"
        and raw.startswith(("invalid project id", "unknown validation stage", "attempt path"))
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
            else ERROR_DEFINITIONS["CS-PROJ-005"]
        )
    return ClassifiedError(definition)


def error_payload(
    error: Exception,
    *,
    command: str | None = None,
    surface: Literal["cli", "mcp"] = "cli",
    request: bool = False,
    detail: str | None = None,
) -> dict[str, str | None]:
    """Serialize the stable machine-readable error fields.

    ``detail`` optionally carries a redacted diagnostic (for example from
    ``safe_error_detail``) so machine consumers see the same bounded evidence
    the human rendering shows.
    """
    classified = classify_exception(error, command=command, surface=surface, request=request)
    payload: dict[str, str | None] = {
        "code": classified.code,
        "category": classified.category,
        "message": classified.message,
        "reason": classified.reason,
        "recovery": classified.recovery,
        "command": command,
    }
    if detail is not None:
        payload["detail"] = detail
    return payload


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
