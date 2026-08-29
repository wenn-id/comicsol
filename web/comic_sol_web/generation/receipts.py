"""Sanitized, append-only generation receipt values."""

from __future__ import annotations

import math
import re
from types import MappingProxyType
from typing import Mapping

AUTHORIZED_RECEIPT_FIELDS = frozenset({"provider", "model", "auth_mode", "usage", "checksum"})

# Provider usage is normalized to accounting metadata. Request/response bodies,
# prompts, credentials, account IDs, endpoints, and arbitrary provider fields
# are intentionally not eligible for persistence.
_AUTHORIZED_USAGE_FIELDS = frozenset(
    {
        "images",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "currency",
        "quantity",
        "unit",
        "duration_ms",
    }
)
_NUMERIC_USAGE_FIELDS = _AUTHORIZED_USAGE_FIELDS - {"currency", "unit"}
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_USAGE_UNITS = frozenset(
    {
        "image",
        "images",
        "token",
        "tokens",
        "second",
        "seconds",
        "millisecond",
        "milliseconds",
    }
)


def sanitize_usage(
    usage: Mapping[str, int | float | str],
) -> Mapping[str, int | float | str]:
    """Return only bounded, scalar accounting fields from provider usage."""
    sanitized: dict[str, int | float | str] = {}
    for key in sorted(set(usage) & _AUTHORIZED_USAGE_FIELDS):
        value = usage[key]
        if key in _NUMERIC_USAGE_FIELDS:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if 0 <= value <= 10**15:
                    sanitized[key] = value
                continue
            if isinstance(value, float) and math.isfinite(value) and 0 <= value <= 10**15:
                sanitized[key] = value
            continue
        if key == "currency" and isinstance(value, str) and _CURRENCY.fullmatch(value):
            sanitized[key] = value
            continue
        if key == "unit" and isinstance(value, str) and value in _USAGE_UNITS:
            sanitized[key] = value
    return MappingProxyType(sanitized)


def receipt_value(
    *,
    provider: str,
    model: str,
    auth_mode: str,
    usage: Mapping[str, int | float | str],
    checksum: str,
) -> Mapping[str, object]:
    """Build the exact public receipt shape; no extension fields are accepted."""
    return MappingProxyType(
        {
            "provider": provider,
            "model": model,
            "auth_mode": auth_mode,
            "usage": sanitize_usage(usage),
            "checksum": checksum,
        }
    )
