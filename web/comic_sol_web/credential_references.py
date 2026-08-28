"""Shared validation rules for deployment credential references."""

from __future__ import annotations

import re
from collections.abc import Mapping

PROVIDER_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
KEY_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,32}")
SECRET_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
MINIMUM_MASTER_KEY_LENGTH = 32


def active_key_is_valid(references: Mapping[str, str], active_key_id: str | None) -> bool:
    """Return whether an optional active key names a declared, valid key ID."""
    return active_key_id is None or (
        KEY_IDENTIFIER.fullmatch(active_key_id) is not None and active_key_id in references
    )
