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

SESSION_SECRET_VAR = "COMIC_SOL_WEB_SESSION_SECRET"
ENCRYPTION_SECRET_VAR = "COMIC_SOL_WEB_ENCRYPTION_SECRET"
DATA_ROOT_VAR = "COMIC_SOL_WEB_DATA_ROOT"

REQUIRED_VARIABLES = (SESSION_SECRET_VAR, ENCRYPTION_SECRET_VAR, DATA_ROOT_VAR)

# A secret must be long enough to be meaningful. It must not carry whitespace,
# newlines, or other control characters that would corrupt a header, cookie,
# log line, or shell invocation.
MINIMUM_SECRET_LENGTH = 32
_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f\s]")


class WebConfigError(ValueError):
    """Configuration is missing or invalid.

    The message always identifies the offending variable by name and never
    contains its (possibly secret) value.
    """


def _require(environ: Mapping[str, str], name: str) -> str:
    """Return a present, non-empty, control-character-free value by name."""
    if name not in environ:
        raise WebConfigError(f"{name} is not set")
    value = environ[name]
    if not value.strip():
        raise WebConfigError(f"{name} is empty")
    if _UNSAFE_CHARACTERS.search(value):
        raise WebConfigError(f"{name} contains whitespace or control characters")
    return value


def _require_secret(environ: Mapping[str, str], name: str) -> str:
    value = _require(environ, name)
    if len(value) < MINIMUM_SECRET_LENGTH:
        raise WebConfigError(f"{name} must be at least {MINIMUM_SECRET_LENGTH} characters")
    return value


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

        data_root = Path(raw_data_root)
        if not data_root.is_absolute():
            raise WebConfigError(f"{DATA_ROOT_VAR} must be an absolute path")

        return cls(
            session_secret=session_secret,
            encryption_secret=encryption_secret,
            data_root=data_root,
        )
