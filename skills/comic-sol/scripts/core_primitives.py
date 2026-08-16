"""Small, dependency-free primitives shared across Comic Sol modules."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TypeAlias


PANEL_ID_PATTERN = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
PANEL_CHECK_IDS = (
    "character-identity",
    "anatomy",
    "action",
    "composition",
    "continuity",
    "text-free",
    "technical",
)
PAGE_CHECK_IDS = (
    "clipped-text",
    "text-overlap",
    "face-action-obstruction",
    "bubble-tail-direction",
    "reading-order",
    "accidental-text-watermark",
    "layout-border-integrity",
)

Rectangle: TypeAlias = Mapping[str, int] | Sequence[int]


def canonical_json_bytes(value: object) -> bytes:
    """Serialize compact, sorted UTF-8 JSON without a trailing newline."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_artifact_bytes(value: object) -> bytes:
    """Serialize sorted, two-space UTF-8 JSON with one trailing newline."""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _rectangle_values(rectangle: Rectangle) -> tuple[int, int, int, int]:
    if isinstance(rectangle, Mapping):
        values = tuple(rectangle.get(key) for key in ("x", "y", "width", "height"))
    else:
        values = tuple(rectangle)
    if len(values) != 4 or any(
        not isinstance(value, int) or isinstance(value, bool) for value in values
    ):
        raise ValueError("rectangle must contain four integers")
    return values  # type: ignore[return-value]


def rectangles_overlap(first: Rectangle, second: Rectangle) -> bool:
    """Return whether two positive-area rectangles overlap using half-open edges."""
    ax, ay, aw, ah = _rectangle_values(first)
    bx, by, bw, bh = _rectangle_values(second)
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)
