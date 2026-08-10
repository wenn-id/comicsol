#!/usr/bin/env python3
"""Deterministic panel lettering for Comic Sol."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import struct
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from comic_sol import atomic_write_bytes, canonical_artifact_bytes, read_json, sha256_file
from project_io import ProjectTransaction, contained_project_path, open_path_nofollow, read_contained_bytes
from typography import (
    lettering_geometry_hash,
    preflight_text_items,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FONT_PATH = ROOT / "assets/fonts/ComicNeue-Regular.ttf"
FONT_PATH = DEFAULT_FONT_PATH
FONT_PATH_BOLD = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
FONT_PATH_FALLBACK = ROOT / "assets/fonts/NotoSans-Regular.ttf"
ANCHORS = (
    "top-left",
    "top-center",
    "top-right",
    "middle-right",
    "bottom-right",
    "bottom-center",
    "bottom-left",
    "middle-left",
)
# A w x h text block only fits inside an ellipse whose axes are at least
# sqrt(2) times w and h, so balloons budget and circumscribe text with it.
ELLIPSE_TEXT_RATIO = math.sqrt(2.0)
BALLOON_PADDING = 19
CAPTION_PADDING = 20
# Panels are page-sized at most (1600x2400); sixteen page areas leaves room for
# oversampled source art while rejecting decompression bombs.
MAX_DECODED_PIXELS = 1600 * 2400 * 16
BALLOON_SUPERSAMPLE = 6


def normalize_content(text: str) -> str:
    """Normalize authored text without changing punctuation, emoji, or newlines."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFC", text)
    normalized = "".join(
        " " if unicodedata.category(character) == "Cc" and character != "\n" else character
        for character in normalized
    )
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def _display_content(kind: object, text: str) -> str:
    """Return display text without modifying the authored storyboard value."""
    normalized = normalize_content(text)
    return normalized.upper() if kind == "dialogue" else normalized


def normalized_word_count(text: str) -> int:
    """Count whitespace-separated words after deterministic normalization."""
    return len(normalize_content(text).split())


def _parse_emphasis(text: str) -> list[tuple[str, bool]]:
    """Parse complete ``**bold**`` spans into text and emphasis chunks."""
    parts = text.split("**")
    if len(parts) % 2 == 0 or any(
        not parts[index].strip() for index in range(1, len(parts), 2)
    ):
        return [(text, False)]
    return [(part, index % 2 != 0) for index, part in enumerate(parts) if part]


@lru_cache(maxsize=None)
def _unicode_cmap_subtables(path: str) -> tuple[bytes, ...]:
    """Return the Unicode cmap subtables from one deterministic SFNT face."""
    data = Path(path).read_bytes()
    sfnt_offset = 0
    if data[:4] == b"ttcf":
        if len(data) < 16 or struct.unpack_from(">I", data, 8)[0] < 1:
            raise OSError(f"invalid TrueType collection: {path}")
        sfnt_offset = struct.unpack_from(">I", data, 12)[0]
    if sfnt_offset + 12 > len(data):
        raise OSError(f"invalid font header: {path}")

    table_count = struct.unpack_from(">H", data, sfnt_offset + 4)[0]
    cmap: bytes | None = None
    for index in range(table_count):
        record_offset = sfnt_offset + 12 + index * 16
        if record_offset + 16 > len(data):
            raise OSError(f"invalid font table directory: {path}")
        tag, _, offset, length = struct.unpack_from(">4sIII", data, record_offset)
        if tag == b"cmap":
            if offset + length > len(data):
                raise OSError(f"invalid cmap table: {path}")
            cmap = data[offset:offset + length]
            break
    if cmap is None or len(cmap) < 4:
        return ()

    record_count = struct.unpack_from(">H", cmap, 2)[0]
    subtables: list[bytes] = []
    seen_offsets: set[int] = set()
    for index in range(record_count):
        record_offset = 4 + index * 8
        if record_offset + 8 > len(cmap):
            raise OSError(f"invalid cmap encoding records: {path}")
        platform, encoding, offset = struct.unpack_from(">HHI", cmap, record_offset)
        if not (platform == 0 or platform == 3 and encoding in {1, 10}):
            continue
        if offset in seen_offsets or offset + 4 > len(cmap):
            continue
        seen_offsets.add(offset)
        format_number = struct.unpack_from(">H", cmap, offset)[0]
        if format_number in {8, 10, 12, 13}:
            if offset + 8 > len(cmap):
                continue
            length = struct.unpack_from(">I", cmap, offset + 4)[0]
        else:
            length = struct.unpack_from(">H", cmap, offset + 2)[0]
        if length >= 4 and offset + length <= len(cmap):
            subtables.append(cmap[offset:offset + length])
    return tuple(subtables)


def _cmap_glyph_id(table: bytes, codepoint: int) -> int:
    """Return a cmap glyph id, with zero meaning that no glyph is mapped."""
    format_number = struct.unpack_from(">H", table, 0)[0]
    if format_number == 0:
        return table[6 + codepoint] if codepoint <= 0xFF and 6 + codepoint < len(table) else 0
    if format_number == 4:
        if codepoint > 0xFFFF or len(table) < 16:
            return 0
        segment_count = struct.unpack_from(">H", table, 6)[0] // 2
        end_codes = 14
        start_codes = end_codes + segment_count * 2 + 2
        deltas = start_codes + segment_count * 2
        range_offsets = deltas + segment_count * 2
        if range_offsets + segment_count * 2 > len(table):
            return 0
        for index in range(segment_count):
            end = struct.unpack_from(">H", table, end_codes + index * 2)[0]
            if codepoint > end:
                continue
            start = struct.unpack_from(">H", table, start_codes + index * 2)[0]
            if codepoint < start:
                return 0
            delta = struct.unpack_from(">h", table, deltas + index * 2)[0]
            range_offset_position = range_offsets + index * 2
            range_offset = struct.unpack_from(">H", table, range_offset_position)[0]
            if range_offset == 0:
                return (codepoint + delta) & 0xFFFF
            glyph_position = range_offset_position + range_offset + (codepoint - start) * 2
            if glyph_position + 2 > len(table):
                return 0
            glyph_id = struct.unpack_from(">H", table, glyph_position)[0]
            return (glyph_id + delta) & 0xFFFF if glyph_id else 0
        return 0
    if format_number == 6:
        if len(table) < 10:
            return 0
        first, count = struct.unpack_from(">HH", table, 6)
        index = codepoint - first
        position = 10 + index * 2
        return struct.unpack_from(">H", table, position)[0] if 0 <= index < count and position + 2 <= len(table) else 0
    if format_number == 10:
        if len(table) < 20:
            return 0
        first, count = struct.unpack_from(">II", table, 12)
        index = codepoint - first
        position = 20 + index * 2
        return struct.unpack_from(">H", table, position)[0] if 0 <= index < count and position + 2 <= len(table) else 0
    if format_number in {12, 13}:
        if len(table) < 16:
            return 0
        group_count = struct.unpack_from(">I", table, 12)[0]
        for index in range(group_count):
            position = 16 + index * 12
            if position + 12 > len(table):
                return 0
            start, end, start_glyph = struct.unpack_from(">III", table, position)
            if codepoint < start:
                return 0
            if codepoint <= end:
                return start_glyph if format_number == 13 else start_glyph + codepoint - start
        return 0
    return 0


def _font_supports(path: Path, character: str) -> bool:
    """Return whether a font's Unicode cmap maps one character to a glyph."""
    if len(character) != 1:
        raise ValueError("glyph coverage requires exactly one character")
    codepoint = ord(character)
    return any(_cmap_glyph_id(table, codepoint) != 0 for table in _unicode_cmap_subtables(str(path)))


@lru_cache(maxsize=None)
def _load_font_path(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a dialogue face, falling back when the requested face is unavailable."""
    path = FONT_PATH_BOLD if bold else FONT_PATH
    try:
        return _load_font_path(str(path), size)
    except OSError:
        return _load_font_path(str(FONT_PATH_FALLBACK), size)


def _font_runs(
    text: str,
    size: int,
    bold: bool = False,
    primary: ImageFont.FreeTypeFont | None = None,
) -> tuple[tuple[str, ImageFont.FreeTypeFont], ...]:
    """Group text into adjacent runs using exact per-character font fallback."""
    primary = primary or _load_font(size, bold)
    fallback = _load_font_path(str(FONT_PATH_FALLBACK), size)
    primary_path = Path(primary.path)
    runs: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for character in text:
        selected = primary if character == "\n" or _font_supports(primary_path, character) else fallback
        if runs and Path(runs[-1][1].path) == Path(selected.path):
            runs[-1] = (runs[-1][0] + character, selected)
        else:
            runs.append((character, selected))
    return tuple(runs)


def _styled_font_runs(
    text: str,
    regular_font: ImageFont.FreeTypeFont,
) -> tuple[tuple[str, ImageFont.FreeTypeFont], ...]:
    """Compose emphasis chunks and per-character fallback into drawable runs."""
    runs: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for chunk, bold in _parse_emphasis(text):
        primary = _load_font(regular_font.size, True) if bold else regular_font
        for run_text, run_font in _font_runs(chunk, regular_font.size, bold, primary):
            if runs and Path(runs[-1][1].path) == Path(run_font.path):
                runs[-1] = (runs[-1][0] + run_text, run_font)
            else:
                runs.append((run_text, run_font))
    return tuple(runs)


@dataclass(frozen=True)
class _StyledLine:
    runs: tuple[tuple[str, ImageFont.FreeTypeFont], ...]
    width: float
    top: int
    bottom: int

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class _StyledLayout:
    lines: tuple[_StyledLine, ...]
    spacing: int

    @property
    def width(self) -> float:
        return max((line.width for line in self.lines), default=0.0)

    @property
    def height(self) -> int:
        return sum(line.height for line in self.lines) + self.spacing * max(0, len(self.lines) - 1)


def _merge_font_tokens(
    tokens: list[tuple[str, ImageFont.FreeTypeFont]],
) -> tuple[tuple[str, ImageFont.FreeTypeFont], ...]:
    """Merge adjacent character tokens that use the same font face."""
    runs: list[tuple[str, ImageFont.FreeTypeFont]] = []
    for character, font in tokens:
        if runs and Path(runs[-1][1].path) == Path(font.path):
            runs[-1] = (runs[-1][0] + character, font)
        else:
            runs.append((character, font))
    return tuple(runs)


def _measure_styled_line(
    draw: ImageDraw.ImageDraw,
    tokens: list[tuple[str, ImageFont.FreeTypeFont]],
    regular_font: ImageFont.FreeTypeFont,
) -> _StyledLine:
    """Measure one mixed-font line from the baseline used to draw it."""
    runs = _merge_font_tokens(tokens)
    width = sum(draw.textlength(text, font=run_font) for text, run_font in runs)
    if runs:
        boxes = [
            draw.textbbox((0, 0), text, font=run_font, anchor="ls")
            for text, run_font in runs
        ]
    else:
        boxes = [draw.textbbox((0, 0), "Ag", font=regular_font, anchor="ls")]
    return _StyledLine(
        runs=runs,
        width=width,
        top=min(box[1] for box in boxes),
        bottom=max(box[3] for box in boxes),
    )


def _layout_styled_text(
    draw: ImageDraw.ImageDraw,
    content: str,
    regular_font: ImageFont.FreeTypeFont,
    maximum_width: float,
    spacing: int = 6,
    emphasis: bool = True,
) -> _StyledLayout | None:
    """Wrap parsed dialogue runs using the exact fonts that will draw them."""
    if maximum_width <= 0:
        return None

    paragraphs: list[list[tuple[str, ImageFont.FreeTypeFont]]] = [[]]
    source_runs = (
        _styled_font_runs(content, regular_font)
        if emphasis
        else _font_runs(content, regular_font.size, primary=regular_font)
    )
    for text, run_font in source_runs:
        for character in text:
            if character == "\n":
                paragraphs.append([])
            else:
                paragraphs[-1].append((character, run_font))

    lines: list[_StyledLine] = []
    for paragraph in paragraphs:
        words: list[
            tuple[
                list[tuple[str, ImageFont.FreeTypeFont]],
                list[tuple[str, ImageFont.FreeTypeFont]],
            ]
        ] = []
        separator: list[tuple[str, ImageFont.FreeTypeFont]] = []
        word: list[tuple[str, ImageFont.FreeTypeFont]] = []
        for token in paragraph:
            if token[0].isspace():
                if word:
                    words.append((separator, word))
                    separator = []
                    word = []
                separator.append(token)
            else:
                word.append(token)
        if word:
            words.append((separator, word))

        if not words:
            lines.append(_measure_styled_line(draw, [], regular_font))
            continue

        current = words[0][0] + words[0][1]
        first_line = _measure_styled_line(draw, current, regular_font)
        if first_line.width > maximum_width:
            return None
        for between, next_word in words[1:]:
            candidate = current + between + next_word
            candidate_line = _measure_styled_line(draw, candidate, regular_font)
            if candidate_line.width <= maximum_width:
                current = candidate
                continue
            lines.append(_measure_styled_line(draw, current, regular_font))
            current = list(next_word)
            next_line = _measure_styled_line(draw, current, regular_font)
            if next_line.width > maximum_width:
                return None
        lines.append(_measure_styled_line(draw, current, regular_font))

    return _StyledLayout(tuple(lines), spacing)


def _draw_font_runs(
    draw: ImageDraw.ImageDraw,
    runs: tuple[tuple[str, ImageFont.FreeTypeFont], ...],
    position: tuple[float, float],
    fill: tuple[int, ...],
) -> None:
    """Draw one line of mixed-font runs from a shared baseline."""
    x, y = position
    for text, run_font in runs:
        # One-pixel keyline gives Comic Neue the confident ink weight used by
        # print-comic dialogue without changing measured glyph advances.
        draw.text(
            (x, y), text, font=run_font, fill=fill, anchor="ls",
            stroke_width=1, stroke_fill=fill,
        )
        x += draw.textlength(text, font=run_font)


def _draw_styled_layout(
    draw: ImageDraw.ImageDraw,
    layout: _StyledLayout,
    center_x: float,
    top_y: float,
    fill: tuple[int, ...],
) -> None:
    """Draw a measured layout with every line centered independently."""
    line_top = top_y
    for line in layout.lines:
        _draw_font_runs(
            draw,
            line.runs,
            (center_x - line.width / 2, line_top - line.top),
            fill,
        )
        line_top += line.height + layout.spacing


def _known_character(character_bible: list[dict], speaker: object) -> bool:
    if not isinstance(speaker, str) or not speaker:
        return False
    return any(
        isinstance(character, dict)
        and speaker in {character.get("id"), character.get("name")}
        for character in character_bible
    )


def _anchor_rect(anchor: str, width: int, height: int) -> dict[str, int]:
    inset_x = max(4, round(width * 0.04))
    inset_y = max(4, round(height * 0.04))
    box_width = min(width - 2 * inset_x, max(1, round(width * 0.42)))
    box_height = min(height - 2 * inset_y, max(1, round(height * 0.30)))
    horizontal = {
        "left": inset_x,
        "center": (width - box_width) // 2,
        "right": width - inset_x - box_width,
    }
    vertical = {
        "top": inset_y,
        "middle": (height - box_height) // 2,
        "bottom": height - inset_y - box_height,
    }
    vertical_name, horizontal_name = anchor.split("-", 1)
    return {
        "x": horizontal[horizontal_name],
        "y": vertical[vertical_name],
        "width": box_width,
        "height": box_height,
    }


def _overlap(first: dict[str, int], second: dict[str, int]) -> bool:
    return not (
        first["x"] + first["width"] <= second["x"]
        or second["x"] + second["width"] <= first["x"]
        or first["y"] + first["height"] <= second["y"]
        or second["y"] + second["height"] <= first["y"]
    )


def _text_padding(kind: object) -> int:
    """Return the padding between a text block and the shape drawn around it."""
    return BALLOON_PADDING if kind == "dialogue" else CAPTION_PADDING


def _text_wrap_width(kind: object, box_width: float) -> float:
    """Return the wrapping width a text block may use inside its drawn shape."""
    inner = max(1.0, box_width - 2 * _text_padding(kind))
    # Balloon text is inscribed in the ellipse rather than in the box bounding
    # it, so only inner / sqrt(2) of that box is usable text width.
    return inner / ELLIPSE_TEXT_RATIO if kind == "dialogue" else inner


def _balloon_box(layout: _StyledLayout) -> tuple[int, int]:
    """Return the ellipse box that circumscribes one centered text block."""
    return (
        math.ceil(layout.width * ELLIPSE_TEXT_RATIO) + 2 * BALLOON_PADDING,
        math.ceil(layout.height * ELLIPSE_TEXT_RATIO) + 2 * BALLOON_PADDING,
    )


def _fitted_item_rect(
    draw: ImageDraw.ImageDraw,
    item: dict,
    maximum: dict[str, int],
    font: ImageFont.FreeTypeFont,
) -> dict[str, int]:
    """Fit a rendered text shape inside its anchor's maximum placement area."""
    kind = item.get("kind")
    if kind not in {"caption", "dialogue"}:
        return dict(maximum)
    layout = _layout_styled_text(
        draw,
        _display_content(kind, item.get("content", "")),
        font,
        _text_wrap_width(kind, maximum["width"]),
        emphasis=kind == "dialogue",
    )
    if layout is None:
        raise ValueError(f"text item {item.get('id', 'unknown')} cannot be wrapped")
    if kind == "dialogue":
        box_width, box_height = _balloon_box(layout)
    else:
        box_width = math.ceil(layout.width) + 2 * CAPTION_PADDING
        box_height = layout.height + CAPTION_PADDING
    fitted_width = min(maximum["width"], box_width)
    fitted_height = min(maximum["height"], box_height)

    anchor = item.get("anchor", "top-left")
    vertical, horizontal = anchor.split("-", 1) if anchor in ANCHORS else ("top", "left")
    x = {
        "left": maximum["x"],
        "center": maximum["x"] + (maximum["width"] - fitted_width) // 2,
        "right": maximum["x"] + maximum["width"] - fitted_width,
    }[horizontal]
    y = {
        "top": maximum["y"],
        "middle": maximum["y"] + (maximum["height"] - fitted_height) // 2,
        "bottom": maximum["y"] + maximum["height"] - fitted_height,
    }[vertical]
    return {"x": x, "y": y, "width": fitted_width, "height": fitted_height}


def _organic_tail_geometry(
    rect: dict[str, int],
    speaker_anchor: list[float],
    image_width: int,
    image_height: int,
    voice_source: str,
) -> dict[str, object]:
    """Resolve one compact, tapered cubic tail toward an explicit voice source."""
    if voice_source not in {"human", "device"}:
        raise ValueError("dialogue voice source must be human or device")
    if (
        not isinstance(speaker_anchor, list)
        or len(speaker_anchor) != 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= value <= 1
            for value in speaker_anchor
        )
    ):
        raise ValueError("speaker anchor must contain finite normalized coordinates")
    target_x = round(float(speaker_anchor[0]) * image_width)
    target_y = round(float(speaker_anchor[1]) * image_height)
    center_x = rect["x"] + rect["width"] / 2
    center_y = rect["y"] + rect["height"] / 2
    radius_x = max(0.5, rect["width"] / 2)
    radius_y = max(0.5, rect["height"] / 2)
    delta_x = target_x - center_x
    delta_y = target_y - center_y
    normalized_distance = math.sqrt(
        (delta_x / radius_x) ** 2 + (delta_y / radius_y) ** 2
    )
    if normalized_distance <= 1.08:
        raise ValueError("speaker anchor must remain outside the balloon outline")
    scale = 1 / normalized_distance
    attachment_x = center_x + delta_x * scale
    attachment_y = center_y + delta_y * scale
    source_distance = math.hypot(
        target_x - attachment_x, target_y - attachment_y
    )
    minimum_source_gap = max(
        8.0, min(24.0, min(image_width, image_height) * 0.025)
    )
    available_length = source_distance - minimum_source_gap
    maximum_length = min(
        min(radius_x, radius_y) * 0.9,
        min(image_width, image_height) * 0.12,
    )
    if available_length < 12.0:
        raise ValueError("speaker anchor is too close to the balloon for a readable tail")
    tail_length = min(available_length, maximum_length)
    source_gap = source_distance - tail_length
    unit_x = (target_x - attachment_x) / source_distance
    unit_y = (target_y - attachment_y) / source_distance
    half_base = max(5.0, min(14.0, tail_length * 0.18))

    # Begin both cubic sides inside the balloon body. The merged mask then
    # computes the visible ellipse/tail intersection naturally, avoiding the
    # cusp and shoulder produced by forcing roots onto the ellipse outline.
    normal_x, normal_y = -unit_y, unit_x
    root_depth = min(10.0, half_base * 0.7)
    base_one = (
        attachment_x - unit_x * root_depth + normal_x * half_base,
        attachment_y - unit_y * root_depth + normal_y * half_base,
    )
    base_two = (
        attachment_x - unit_x * root_depth - normal_x * half_base,
        attachment_y - unit_y * root_depth - normal_y * half_base,
    )
    tip = (
        attachment_x + unit_x * tail_length,
        attachment_y + unit_y * tail_length,
    )
    shoulder_run = min(22.0, tail_length * 0.30)
    near_tip_run = min(24.0, tail_length * 0.30)
    near_tip_half_width = max(2.6, half_base * 0.30)
    first_controls = (
        (
            attachment_x + unit_x * shoulder_run + normal_x * half_base * 0.92,
            attachment_y + unit_y * shoulder_run + normal_y * half_base * 0.92,
        ),
        (
            tip[0] - unit_x * near_tip_run + normal_x * near_tip_half_width,
            tip[1] - unit_y * near_tip_run + normal_y * near_tip_half_width,
        ),
    )
    second_controls = (
        (
            attachment_x + unit_x * shoulder_run - normal_x * half_base * 0.92,
            attachment_y + unit_y * shoulder_run - normal_y * half_base * 0.92,
        ),
        (
            tip[0] - unit_x * near_tip_run - normal_x * near_tip_half_width,
            tip[1] - unit_y * near_tip_run - normal_y * near_tip_half_width,
        ),
    )

    def rounded(point: tuple[float, float]) -> list[float]:
        return [round(point[0], 4), round(point[1], 4)]

    return {
        "attachment": rounded((attachment_x, attachment_y)),
        "base": [rounded(base_one), rounded(base_two)],
        "control": [
            [rounded(point) for point in first_controls],
            [rounded(point) for point in second_controls],
        ],
        "length": round(tail_length, 4),
        "policy_version": "organic-cubic-v1",
        "source_gap": round(source_gap, 4),
        "speaker_anchor": [float(value) for value in speaker_anchor],
        "tip": rounded(tip),
        "voice_source": voice_source,
        "width": round(half_base * 2, 4),
    }


def _draw_antialiased_balloon(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    tail: dict[str, object] | None,
) -> None:
    """Draw one seamless supersampled balloon and composite it onto the panel."""
    image = draw._image
    scale = BALLOON_SUPERSAMPLE
    image_width, image_height = image.size
    x0, y0, x1, y1 = bounds
    points = [(float(x0), float(y0)), (float(x1), float(y1))]
    if tail is not None:
        base = tail.get("base")
        control = tail.get("control")
        tip_value = tail.get("tip")
        if not isinstance(base, list) or not isinstance(control, list) or not isinstance(tip_value, list):
            raise ValueError("tail geometry has an invalid point record")
        points.extend(tuple(point) for point in base)
        points.extend(tuple(point) for side in control for point in side)
        points.append(tuple(tip_value))
    margin = 5
    left = max(0, math.floor(min(x for x, _ in points)) - margin)
    top = max(0, math.floor(min(y for _, y in points)) - margin)
    right = min(image_width, math.ceil(max(x for x, _ in points)) + margin + 1)
    bottom = min(image_height, math.ceil(max(y for _, y in points)) + margin + 1)
    mask = Image.new("L", ((right - left) * scale, (bottom - top) * scale), 0)
    mask_draw = ImageDraw.Draw(mask)

    def scaled_point(point: tuple[float, float]) -> tuple[int, int]:
        return round((point[0] - left) * scale), round((point[1] - top) * scale)

    if tail is not None:
        base = tail.get("base")
        control = tail.get("control")
        tip_value = tail.get("tip")
        if not isinstance(base, list) or not isinstance(control, list) or not isinstance(tip_value, list):
            raise ValueError("tail geometry has an invalid point record")
        base_one, base_two = (tuple(point) for point in base)
        first_control, second_control = (
            tuple(tuple(point) for point in side) for side in control
        )
        tip = tuple(tip_value)

        def cubic_points(start, controls, end):
            first, second = controls
            result = []
            for index in range(25):
                t = index / 24
                inverse = 1 - t
                result.append((
                    inverse ** 3 * start[0]
                    + 3 * inverse ** 2 * t * first[0]
                    + 3 * inverse * t ** 2 * second[0]
                    + t ** 3 * end[0],
                    inverse ** 3 * start[1]
                    + 3 * inverse ** 2 * t * first[1]
                    + 3 * inverse * t ** 2 * second[1]
                    + t ** 3 * end[1],
                ))
            return result

        first_side = cubic_points(base_one, first_control, tip)
        second_side = cubic_points(base_two, second_control, tip)
        silhouette = first_side + list(reversed(second_side))
        mask_draw.polygon(tuple(scaled_point(point) for point in silhouette), fill=255)
    mask_draw.ellipse(
        ((x0 - left) * scale, (y0 - top) * scale,
         (x1 - left) * scale, (y1 - top) * scale),
        fill=255,
    )

    # Expand the merged silhouette before downsampling. This avoids the seam,
    # doubled edge, and jagged notch produced by outlining two separate shapes.
    outline = mask.filter(ImageFilter.MaxFilter(25))
    resample = Image.Resampling.LANCZOS
    local_size = (right - left, bottom - top)
    mask = mask.resize(local_size, resample)
    outline = outline.resize(local_size, resample)
    image.paste((15, 15, 15, 255), (left, top), outline)
    image.paste((255, 255, 255, 255), (left, top), mask)


def _item_font(
    draw: ImageDraw.ImageDraw,
    item: dict,
    rect: dict[str, int],
) -> ImageFont.FreeTypeFont:
    kind = item.get("kind")
    content = _display_content(kind, item.get("content", ""))
    for size in range(42, 23, -2):
        font = _load_font(size)
        layout = _layout_styled_text(
            draw,
            content,
            font,
            _text_wrap_width(kind, rect["width"]),
            emphasis=kind == "dialogue",
        )
        if layout is None:
            continue
        if kind == "dialogue":
            box_width, box_height = _balloon_box(layout)
            if box_width <= rect["width"] and box_height <= rect["height"]:
                return font
        elif layout.height <= rect["height"] - 2 * _text_padding(kind):
            return font
    item_id = item.get("id", "unknown")
    raise ValueError(f"text item {item_id} does not fit inside the panel")


def render_text_item(
    draw: ImageDraw.ImageDraw,
    item: dict,
    rect: dict,
    font: ImageFont.FreeTypeFont,
    character_bible: list[dict],
) -> None:
    """Draw one validated text item inside an explicit bounded rectangle."""
    kind = item.get("kind")
    content = _display_content(kind, item.get("content", ""))
    if not content:
        raise ValueError(f"text item {item.get('id', 'unknown')} has empty content")
    if kind not in {"dialogue", "caption", "sfx"}:
        raise ValueError(f"text item {item.get('id', 'unknown')} has unknown kind")
    if kind == "dialogue" and not _known_character(character_bible, item.get("speaker")):
        raise ValueError(f"unknown dialogue character: {item.get('speaker')}")
    if kind == "sfx":
        return

    image_width, image_height = draw._image.size
    x0 = max(0, int(rect["x"]))
    y0 = max(0, int(rect["y"]))
    x1 = min(image_width - 1, x0 + max(1, int(rect["width"])))
    y1 = min(image_height - 1, y0 + max(1, int(rect["height"])))
    bounded = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    padding = _text_padding(kind)
    layout = _layout_styled_text(
        draw,
        content,
        font,
        _text_wrap_width(kind, bounded["width"]),
        emphasis=kind == "dialogue",
    )
    if layout is None:
        raise ValueError(f"text item {item.get('id', 'unknown')} cannot be wrapped")
    text_height = layout.height
    text_y = y0 + max(padding, (bounded["height"] - text_height) / 2)
    if kind == "caption":
        text_y = y0 + max(0, (bounded["height"] - text_height) / 2)

    if kind == "dialogue":
        tail = item.get("speaker_anchor")
        voice_source = item.get("voice_source")
        if isinstance(tail, list) and len(tail) == 2 and all(isinstance(value, (int, float)) for value in tail):
            if not isinstance(voice_source, str):
                raise ValueError("dialogue voice source must be human or device")
            tail_geometry = _organic_tail_geometry(
                bounded | {"x": x0, "y": y0},
                tail,
                image_width,
                image_height,
                voice_source,
            )
            _draw_antialiased_balloon(draw, (x0, y0, x1, y1), tail_geometry)
        else:
            _draw_antialiased_balloon(draw, (x0, y0, x1, y1), None)
        assert layout is not None
        _draw_styled_layout(
            draw,
            layout,
            x0 + bounded["width"] / 2,
            text_y,
            (10, 10, 10, 255),
        )
    elif kind == "caption":
        draw.rectangle(
            (x0, y0, x1, y1),
            fill=(255, 255, 255, 200), outline=(15, 15, 15, 200), width=2,
        )
        assert layout is not None
        _draw_styled_layout(
            draw,
            layout,
            x0 + bounded["width"] / 2,
            text_y,
            (15, 15, 15, 255),
        )


def _validate_decoded_pixels(size: tuple[int, int], path: Path) -> None:
    """Reject panel images whose decoded pixel count exceeds the project cap."""
    width, height = size
    if width * height > MAX_DECODED_PIXELS:
        raise ValueError(
            f"panel exceeds the {MAX_DECODED_PIXELS} pixel decode limit: {path}"
        )


def letter_panel(
    output_path: str,
    panel_width: int,
    panel_height: int,
    text_items: list[dict],
    character_bible: list[dict],
    *,
    source_bytes: bytes | None = None,
) -> dict:
    """Letter a panel atomically and return a compact output summary."""
    if not isinstance(panel_width, int) or not isinstance(panel_height, int) or panel_width <= 0 or panel_height <= 0:
        raise ValueError("panel dimensions must be positive integers")
    if not isinstance(text_items, list) or not isinstance(character_bible, list):
        raise TypeError("text_items and character_bible must be lists")
    path = Path(output_path)
    try:
        stream = io.BytesIO(source_bytes) if source_bytes is not None else open_path_nofollow(path)
        with stream, Image.open(stream) as source:
            _validate_decoded_pixels(source.size, path)
            base = ImageOps.exif_transpose(source).convert("RGBA")
            if base.size != (panel_width, panel_height):
                base = ImageOps.fit(base, (panel_width, panel_height), method=Image.Resampling.LANCZOS)
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        detail = type(error).__name__
        errno_value = getattr(error, "errno", None)
        if errno_value is not None:
            detail += f" errno={errno_value}"
        raise ValueError(f"panel is not a readable image ({detail}): {path}") from error

    ordered = sorted(
        (dict(item) for item in text_items),
        key=lambda item: (item.get("priority", 0), str(item.get("id", ""))),
    )
    for item in ordered:
        if item.get("kind") == "dialogue" and not _known_character(character_bible, item.get("speaker")):
            raise ValueError(f"unknown dialogue character: {item.get('speaker')}")
        if item.get("kind") == "dialogue":
            if "tail_target" in item:
                raise ValueError(
                    "balloon-tail-migration-required: replace tail_target with "
                    "explicit voice_source and speaker_anchor"
                )
            if item.get("voice_source") not in {"human", "device"}:
                raise ValueError(
                    f"text item {item.get('id', 'unknown')} voice_source must be human or device"
                )
            speaker_anchor = item.get("speaker_anchor")
            if (
                not isinstance(speaker_anchor, list)
                or len(speaker_anchor) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not 0 <= value <= 1
                    for value in speaker_anchor
                )
            ):
                raise ValueError(
                    f"text item {item.get('id', 'unknown')} speaker_anchor must be "
                    "finite normalized coordinates"
                )
        content = normalize_content(item.get("content", ""))
        limit = {"dialogue": 32, "caption": 45, "sfx": 3}.get(item.get("kind"))
        if limit is None:
            raise ValueError(f"text item {item.get('id', 'unknown')} has unknown kind")
        if not content or normalized_word_count(content) > limit:
            raise ValueError(f"text item {item.get('id', 'unknown')} exceeds its content limit")
        anchor = item.get("anchor", "top-left")
        if anchor not in ANCHORS:
            raise ValueError(f"text item {item.get('id', 'unknown')} has unknown anchor")
        item["content"] = content

    renderable = [item for item in ordered if item.get("kind") != "sfx"]
    rendered_text_count = len(renderable)
    sfx_count = len(ordered) - rendered_text_count
    word_count = sum(normalized_word_count(item["content"]) for item in ordered)
    summary = {
        "font_used": str(Path(_load_font(12).path)),
        "lettered_path": str(path),
        "rendered_text_count": rendered_text_count,
        "sfx_count": sfx_count,
        "text_count": len(ordered),
        "word_count": word_count,
        "placements": [],
    }
    if rendered_text_count == 0:
        return summary

    canvas = base.copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    occupied: list[dict[str, int]] = []
    for reading_order, item in enumerate(renderable, 1):
        requested = item.get("anchor", "top-left")
        start = ANCHORS.index(requested)
        rect = None
        font = None
        selected_anchor = None
        for offset in range(len(ANCHORS)):
            candidate_anchor = ANCHORS[(start + offset) % len(ANCHORS)]
            candidate = _anchor_rect(candidate_anchor, panel_width, panel_height)
            candidate_item = dict(item)
            candidate_item["anchor"] = candidate_anchor
            candidate_font = _item_font(draw, candidate_item, candidate)
            fitted = _fitted_item_rect(draw, candidate_item, candidate, candidate_font)
            if not any(_overlap(fitted, prior) for prior in occupied):
                rect = fitted
                font = candidate_font
                selected_anchor = candidate_anchor
                break
        if rect is None:
            raise ValueError(f"text item {item.get('id', 'unknown')} has no non-overlapping placement")
        assert font is not None
        assert selected_anchor is not None
        render_text_item(draw, item, rect, font, character_bible)
        display = _display_content(item.get("kind"), item.get("content", ""))
        font_runs = [
            {
                "font_id": Path(run_font.path).name,
                "style": (
                    "bold"
                    if Path(run_font.path) == FONT_PATH_BOLD
                    else "regular"
                ),
                "text": run_text,
            }
            for run_text, run_font in _styled_font_runs(display, font)
        ]
        tail_geometry = None
        tail = item.get("speaker_anchor")
        voice_source = item.get("voice_source")
        if (
            item.get("kind") == "dialogue"
            and isinstance(tail, list)
            and len(tail) == 2
            and all(isinstance(value, (int, float)) for value in tail)
        ):
            if not isinstance(voice_source, str):
                raise ValueError("dialogue voice source must be human or device")
            tail_geometry = _organic_tail_geometry(
                rect,
                tail,
                panel_width,
                panel_height,
                voice_source,
            )
        summary["placements"].append({
            "anchor": selected_anchor,
            "box": {key: int(rect[key]) for key in ("x", "y", "width", "height")},
            "font_runs": font_runs,
            "id": item.get("id"),
            "kind": item.get("kind"),
            "reading_order": reading_order,
            "tail": tail_geometry,
        })
        occupied.append(rect)

    encoded = io.BytesIO()
    canvas.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=9)
    atomic_write_bytes(path, encoded.getvalue())
    return summary


def _letter_project_with_summaries(
    project_dir: Path,
) -> tuple[list[Path], list[dict]]:
    """Letter every accepted project panel and collect outputs plus summaries."""
    project_dir = Path(project_dir)
    storyboard = read_json(
        contained_project_path(project_dir, "plan/storyboard.json", must_exist=True)
    )
    pages = storyboard.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("storyboard pages must be a non-empty array")
    panels: list[dict] = []
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise ValueError(f"storyboard page {page_index + 1} must be an object")
        page_panels = page.get("panels")
        if not isinstance(page_panels, list) or not page_panels:
            raise ValueError(f"storyboard page {page_index + 1} panels must be a non-empty array")
        for panel_index, panel in enumerate(page_panels):
            if not isinstance(panel, dict):
                raise ValueError(
                    f"storyboard page {page_index + 1} panel {panel_index + 1} must be an object"
                )
            panel_id = panel.get("id")
            if not isinstance(panel_id, str) or re.fullmatch(r"p[0-9]{2}-[0-9]{2}", panel_id) is None:
                raise ValueError(
                    f"storyboard page {page_index + 1} panel {panel_index + 1} has an invalid ID"
                )
            if not isinstance(panel.get("text"), list):
                raise ValueError(f"storyboard panel {panel_id} text must be an array")
            panels.append(panel)
    bible = read_json(
        contained_project_path(project_dir, "plan/character-bible.json", must_exist=True)
    ).get("characters")
    if not isinstance(bible, list) or any(not isinstance(character, dict) for character in bible):
        raise ValueError("character bible characters must be an array of objects")

    font_policy = {
        "regular": FONT_PATH,
        "bold": FONT_PATH_BOLD,
        "fallback": FONT_PATH_FALLBACK,
    }
    preflights: dict[str, dict[str, object]] = {}
    for panel in panels:
        preflights[panel["id"]] = preflight_text_items(
            panel.get("text", []), font_policy
        )

    outputs: list[Path] = []
    summaries: list[dict] = []
    staged: list[tuple[str, bytes, bytes, bytes, dict]] = []
    storyboard_path = contained_project_path(
        project_dir, "plan/storyboard.json", must_exist=True
    )
    storyboard_sha256 = sha256_file(storyboard_path)
    with tempfile.TemporaryDirectory(prefix="comic-sol-lettering-") as temporary:
        temporary_root = Path(temporary)
        for panel in panels:
            panel_id = panel["id"]
            canonical_source = f"panels/{panel_id}/clean.png"
            legacy_source = f"panels/clean/{panel_id}.png"
            source_relative = (
                canonical_source
                if contained_project_path(project_dir, canonical_source).is_file()
                else legacy_source
            )
            try:
                source = contained_project_path(project_dir, source_relative, must_exist=True)
                source_bytes = read_contained_bytes(project_dir, source_relative)
                with Image.open(io.BytesIO(source_bytes)) as image:
                    _validate_decoded_pixels(image.size, source)
                    image.load()
                    width, height = image.size
            except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
                raise ValueError(f"panel {panel_id} is not a readable image") from error
            staged_path = temporary_root / f"{panel_id}.png"
            summary = letter_panel(
                str(staged_path), width, height, panel.get("text", []), bible,
                source_bytes=source_bytes,
            )
            destination_relative = f"panels/{panel_id}/lettered.png"
            destination = contained_project_path(project_dir, destination_relative)
            summary["lettered_path"] = str(destination)
            lettered_payload = staged_path.read_bytes()
            preflight = preflights[panel_id]
            geometry: dict[str, object] = {
                "bindings": {
                    "clean_path": source_relative,
                    "clean_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "font_policy_sha256": preflight["font_policy_sha256"],
                    "storyboard_path": "plan/storyboard.json",
                    "storyboard_sha256": storyboard_sha256,
                    "typography_sha256": hashlib.sha256(
                        canonical_artifact_bytes(preflight)
                    ).hexdigest(),
                },
                "items": summary["placements"],
                "kind": "lettering-geometry",
                "lettered": {
                    "path": destination_relative,
                    "sha256": hashlib.sha256(lettered_payload).hexdigest(),
                },
                "panel_id": panel_id,
                "schema_version": "1.0",
            }
            geometry["geometry_sha256"] = lettering_geometry_hash(geometry)
            staged.append((
                panel_id,
                lettered_payload,
                canonical_artifact_bytes(preflight),
                canonical_artifact_bytes(geometry),
                summary,
            ))

        with ProjectTransaction(project_dir, "lettering") as transaction:
            for panel_id, image_payload, preflight_payload, geometry_payload, _ in staged:
                transaction.stage_bytes(
                    f"panels/{panel_id}/lettered.png", image_payload
                )
                transaction.stage_bytes(
                    f"panels/{panel_id}/typography.json", preflight_payload
                )
                transaction.stage_bytes(
                    f"panels/{panel_id}/lettering.json", geometry_payload
                )
            transaction.commit()

        for panel_id, _, _, _, summary in staged:
            outputs.append(contained_project_path(
                project_dir, f"panels/{panel_id}/lettered.png", must_exist=True
            ))
            summaries.append(summary)
    return outputs, summaries


def letter_project(project_dir: Path) -> list[Path]:
    """Letter every accepted project panel and return its output paths."""
    outputs, _ = _letter_project_with_summaries(project_dir)
    return outputs


class _LetteringArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"invalid invocation: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _LetteringArgumentParser(prog="letter_panels.py")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    global FONT_PATH
    previous_font = FONT_PATH
    try:
        arguments = _build_parser().parse_args(argv)
        try:
            _load_font_path(str(arguments.font), 12)
        except OSError as error:
            raise ValueError(f"font is not a readable TrueType/OpenType file: {arguments.font}") from error
        FONT_PATH = arguments.font
        _, summaries = _letter_project_with_summaries(arguments.project_dir)
        print(json.dumps(summaries, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        FONT_PATH = previous_font


if __name__ == "__main__":
    raise SystemExit(main())
