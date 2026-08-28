"""Strict, immutable environment configuration for comic-sol-web.

Configuration is parsed from explicit environment values only. Secrets are
required and validated; no secret value is logged, echoed, serialized, or
included in an error message. Parsing performs no network operation and
creates no application, filesystem, or database state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

SESSION_SECRET_VAR = "COMIC_SOL_WEB_SESSION_SECRET"
ENCRYPTION_SECRET_VAR = "COMIC_SOL_WEB_ENCRYPTION_SECRET"
DATA_ROOT_VAR = "COMIC_SOL_WEB_DATA_ROOT"
HOSTED_SECRET_REFS_VAR = "COMIC_SOL_WEB_HOSTED_SECRET_REFS"
CREDENTIAL_KEY_REFS_VAR = "COMIC_SOL_WEB_CREDENTIAL_KEY_REFS"
CREDENTIAL_ACTIVE_KEY_ID_VAR = "COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID"

REQUIRED_VARIABLES = (SESSION_SECRET_VAR, ENCRYPTION_SECRET_VAR, DATA_ROOT_VAR)

# A secret must be long enough to be meaningful. It must not carry whitespace,
# newlines, or other control characters that would corrupt a header, cookie,
# log line, or shell invocation.
MINIMUM_SECRET_LENGTH = 32
_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f\s]")
_PATH_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_PROVIDER_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_KEY_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SECRET_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


class WebConfigError(ValueError):
    """Configuration is missing or invalid.

    The message always identifies the offending variable by name and never
    contains its (possibly secret) value.
    """


def _require(environ: Mapping[str, str], name: str) -> str:
    """Return a present, non-empty value that contains no control characters.

    Whitespace is permitted because configuration values such as absolute
    data-root paths may legitimately contain spaces (Windows profile or
    macOS user paths). The stricter secret-only check lives in
    `_require_secret`.
    """
    if name not in environ:
        raise WebConfigError(f"{name} is not set")
    value = environ[name]
    if not value.strip():
        raise WebConfigError(f"{name} is empty")
    if _PATH_UNSAFE_CHARACTERS.search(value):
        raise WebConfigError(f"{name} contains control characters")
    return value


def _require_secret(environ: Mapping[str, str], name: str) -> str:
    value = _require(environ, name)
    if _UNSAFE_CHARACTERS.search(value):
        raise WebConfigError(f"{name} contains whitespace or control characters")
    if len(value) < MINIMUM_SECRET_LENGTH:
        raise WebConfigError(f"{name} must be at least {MINIMUM_SECRET_LENGTH} characters")
    return value


def _parse_secret_references(
    environ: Mapping[str, str],
    variable: str,
    *,
    identifier_pattern: re.Pattern[str],
) -> Mapping[str, str]:
    """Parse comma-separated ``identifier=ENVIRONMENT_VARIABLE`` declarations."""
    raw = environ.get(variable, "")
    if not raw:
        return MappingProxyType({})
    references: dict[str, str] = {}
    for declaration in raw.split(","):
        identifier, separator, reference = declaration.partition("=")
        if (
            not separator
            or identifier_pattern.fullmatch(identifier) is None
            or _SECRET_REFERENCE.fullmatch(reference) is None
            or identifier in references
        ):
            raise WebConfigError(f"{variable} contains an invalid secret reference declaration")
        references[identifier] = reference
    return MappingProxyType(references)


@dataclass(frozen=True)
class WebConfig:
    """Immutable Web configuration.

    `session_secret` and `encryption_secret` are held only in memory for the
    Web distribution's own use; `repr` is suppressed with `field(repr=False)`
    so a traceback or log line can never render them.
    """

    session_secret: str = field(repr=False)
    encryption_secret: str = field(repr=False)
    data_root: Path
    hosted_secret_references: Mapping[str, str] = field(repr=False)
    master_key_references: Mapping[str, str] = field(repr=False)
    active_credential_key_id: str | None = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> WebConfig:
        """Build configuration from an explicit environment mapping.

        This parses environment values only and performs no network I/O and
        creates no application or database state. `environ` is assumed to be
        a `str`-valued mapping such as `os.environ`.
        """
        session_secret = _require_secret(environ, SESSION_SECRET_VAR)
        encryption_secret = _require_secret(environ, ENCRYPTION_SECRET_VAR)
        raw_data_root = _require(environ, DATA_ROOT_VAR)
        hosted_secret_references = _parse_secret_references(
            environ,
            HOSTED_SECRET_REFS_VAR,
            identifier_pattern=_PROVIDER_IDENTIFIER,
        )
        master_key_references = _parse_secret_references(
            environ,
            CREDENTIAL_KEY_REFS_VAR,
            identifier_pattern=_KEY_IDENTIFIER,
        )
        active_key_id = environ.get(CREDENTIAL_ACTIVE_KEY_ID_VAR)
        if active_key_id is not None and (
            _KEY_IDENTIFIER.fullmatch(active_key_id) is None
            or active_key_id not in master_key_references
        ):
            raise WebConfigError(
                f"{CREDENTIAL_ACTIVE_KEY_ID_VAR} does not name a declared credential key"
            )
        if master_key_references and active_key_id is None:
            raise WebConfigError(
                f"{CREDENTIAL_ACTIVE_KEY_ID_VAR} is required when credential keys are declared"
            )

        data_root = Path(raw_data_root)
        if not data_root.is_absolute():
            raise WebConfigError(f"{DATA_ROOT_VAR} must be an absolute path")

        return cls(
            session_secret=session_secret,
            encryption_secret=encryption_secret,
            data_root=data_root,
            hosted_secret_references=hosted_secret_references,
            master_key_references=master_key_references,
            active_credential_key_id=active_key_id,
        )
