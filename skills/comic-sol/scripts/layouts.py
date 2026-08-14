#!/usr/bin/env python3
"""Immutable deterministic page-layout registry for Comic Sol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

PAGE_WIDTH = 1600
PAGE_HEIGHT = 2400
MARGIN = 64
GUTTER = 32
LAYOUT_VERSION = "1"

Rectangle = tuple[int, int, int, int]

FOUR_GRID_RECTS: tuple[Rectangle, ...] = (
    (64, 64, 720, 1120),
    (816, 64, 720, 1120),
    (64, 1216, 720, 1120),
    (816, 1216, 720, 1120),
)


@dataclass(frozen=True)
class LayoutDefinition:
    name: str
    version: str
    rectangles: tuple[Rectangle, ...]
    reading_order: tuple[int, ...]


def _legacy_layouts() -> dict[str, tuple[Rectangle, ...]]:
    """Return legacy layout definitions for backward compatibility."""
    inner_width = PAGE_WIDTH - 2 * MARGIN
    inner_height = PAGE_HEIGHT - 2 * MARGIN
    half_width = (inner_width - GUTTER) // 2
    half_height = (inner_height - GUTTER) // 2
    third_height = (inner_height - 2 * GUTTER) // 3
    hero_height = 1176
    support_height = inner_height - GUTTER - hero_height
    return {
        "full-page": ((MARGIN, MARGIN, inner_width, inner_height),),
        "two-horizontal": (
            (MARGIN, MARGIN, inner_width, half_height),
            (MARGIN, MARGIN + half_height + GUTTER, inner_width, half_height),
        ),
        "three-horizontal": tuple(
            (MARGIN, MARGIN + index * (third_height + GUTTER), inner_width, third_height)
            for index in range(3)
        ),
        "hero-top-two-bottom": (
            (MARGIN, MARGIN, inner_width, hero_height),
            (MARGIN, MARGIN + hero_height + GUTTER, half_width, support_height),
            (MARGIN + half_width + GUTTER, MARGIN + hero_height + GUTTER,
             half_width, support_height),
        ),
        "two-top-hero-bottom": (
            (MARGIN, MARGIN, half_width, support_height),
            (MARGIN + half_width + GUTTER, MARGIN, half_width, support_height),
            (MARGIN, MARGIN + support_height + GUTTER, inner_width, hero_height),
        ),
        "four-grid": FOUR_GRID_RECTS,
    }


def _rectangle(value: object) -> Rectangle:
    """Return a layout rectangle by normalized coordinates."""
    if isinstance(value, Mapping):
        values = tuple(value.get(key) for key in ("x", "y", "width", "height"))
    elif isinstance(value, (list, tuple)):
        values = tuple(value)
    else:
        raise ValueError("layout rectangle must be an object or four values")
    if (
        len(values) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in values)
    ):
        raise ValueError("layout rectangle must contain four integers")
    return values  # type: ignore[return-value]


def _overlap(first: Rectangle, second: Rectangle) -> bool:
    """Report whether two rectangles overlap."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return not (
        ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay
    )


def validate_custom_layout(
    rectangles: Iterable[object], reading_order: Sequence[int]
) -> tuple[Rectangle, ...]:
    """Validate and return canonical rectangles for a custom page layout."""
    canonical = tuple(_rectangle(value) for value in rectangles)
    if not canonical:
        raise ValueError("layout requires at least one rectangle")
    for x, y, width, height in canonical:
        if width <= 0 or height <= 0:
            raise ValueError("layout rectangles require positive dimensions")
        if x < 0 or y < 0 or x + width > PAGE_WIDTH or y + height > PAGE_HEIGHT:
            raise ValueError("layout rectangles must be contained by the canonical page")
    for index, first in enumerate(canonical):
        for second in canonical[index + 1:]:
            if _overlap(first, second):
                raise ValueError("layout rectangles overlap")
    expected_order = tuple(range(1, len(canonical) + 1))
    if tuple(reading_order) != expected_order:
        raise ValueError("layout reading order must be unique and complete")
    return canonical


_LAYOUTS = {
    name: LayoutDefinition(
        name=name,
        version=LAYOUT_VERSION,
        rectangles=validate_custom_layout(rectangles, tuple(range(1, len(rectangles) + 1))),
        reading_order=tuple(range(1, len(rectangles) + 1)),
    )
    for name, rectangles in _legacy_layouts().items()
}


def get_layout(name: str) -> LayoutDefinition:
    """Return one immutable named layout definition."""
    try:
        return _LAYOUTS[name]
    except KeyError as error:
        raise ValueError(f"unknown layout: {name}") from error


def match_layout(rectangles: Iterable[object]) -> str:
    """Return the matching registered layout name, otherwise ``custom``."""
    canonical = tuple(_rectangle(value) for value in rectangles)
    validate_custom_layout(canonical, tuple(range(1, len(canonical) + 1)))
    for name, definition in _LAYOUTS.items():
        if definition.rectangles == canonical:
            return name
    return "custom"
