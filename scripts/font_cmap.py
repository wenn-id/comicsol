"""Minimal, dependency-free Unicode cmap reader for bundled TrueType faces."""

from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def unicode_cmap_subtables(path: str) -> tuple[bytes, ...]:
    """Return Unicode character-map subtables from a font."""
    data = Path(path).read_bytes()
    sfnt_offset = 0
    if data[:4] == b"ttcf":
        if len(data) < 16 or struct.unpack_from(">I", data, 8)[0] < 1:
            raise OSError(f"invalid TrueType collection: {Path(path).name}")
        sfnt_offset = struct.unpack_from(">I", data, 12)[0]
    if sfnt_offset + 12 > len(data):
        raise OSError(f"invalid font header: {Path(path).name}")
    table_count = struct.unpack_from(">H", data, sfnt_offset + 4)[0]
    cmap: bytes | None = None
    for index in range(table_count):
        position = sfnt_offset + 12 + index * 16
        if position + 16 > len(data):
            raise OSError(f"invalid font table directory: {Path(path).name}")
        tag, _, offset, length = struct.unpack_from(">4sIII", data, position)
        if tag == b"cmap":
            if offset + length > len(data):
                raise OSError(f"invalid cmap table: {Path(path).name}")
            cmap = data[offset : offset + length]
            break
    if cmap is None or len(cmap) < 4:
        return ()
    count = struct.unpack_from(">H", cmap, 2)[0]
    subtables: list[bytes] = []
    seen: set[int] = set()
    for index in range(count):
        position = 4 + index * 8
        if position + 8 > len(cmap):
            raise OSError(f"invalid cmap encoding records: {Path(path).name}")
        platform, encoding, offset = struct.unpack_from(">HHI", cmap, position)
        if not (platform == 0 or (platform == 3 and encoding in {1, 10})):
            continue
        if offset in seen or offset + 4 > len(cmap):
            continue
        seen.add(offset)
        format_number = struct.unpack_from(">H", cmap, offset)[0]
        if format_number in {8, 10, 12, 13}:
            if offset + 8 > len(cmap):
                continue
            length = struct.unpack_from(">I", cmap, offset + 4)[0]
        else:
            length = struct.unpack_from(">H", cmap, offset + 2)[0]
        if length >= 4 and offset + length <= len(cmap):
            subtables.append(cmap[offset : offset + length])
    return tuple(subtables)


def cmap_glyph_id(table: bytes, codepoint: int) -> int:
    """Return the glyph identifier for a Unicode code point."""
    format_number = struct.unpack_from(">H", table, 0)[0]
    if format_number == 0:
        return table[6 + codepoint] if codepoint <= 0xFF and 6 + codepoint < len(table) else 0
    if format_number == 4:
        if codepoint > 0xFFFF or len(table) < 16:
            return 0
        segment_count = struct.unpack_from(">H", table, 6)[0] // 2
        ends = 14
        starts = ends + segment_count * 2 + 2
        deltas = starts + segment_count * 2
        ranges = deltas + segment_count * 2
        if ranges + segment_count * 2 > len(table):
            return 0
        for index in range(segment_count):
            end = struct.unpack_from(">H", table, ends + index * 2)[0]
            if codepoint > end:
                continue
            start = struct.unpack_from(">H", table, starts + index * 2)[0]
            if codepoint < start:
                return 0
            delta = struct.unpack_from(">h", table, deltas + index * 2)[0]
            range_position = ranges + index * 2
            range_offset = struct.unpack_from(">H", table, range_position)[0]
            if range_offset == 0:
                return (codepoint + delta) & 0xFFFF
            glyph_position = range_position + range_offset + (codepoint - start) * 2
            if glyph_position + 2 > len(table):
                return 0
            glyph = struct.unpack_from(">H", table, glyph_position)[0]
            return (glyph + delta) & 0xFFFF if glyph else 0
        return 0
    if format_number == 6:
        if len(table) < 10:
            return 0
        first, count = struct.unpack_from(">HH", table, 6)
        index = codepoint - first
        position = 10 + index * 2
        return (
            struct.unpack_from(">H", table, position)[0]
            if 0 <= index < count and position + 2 <= len(table)
            else 0
        )
    if format_number == 10:
        if len(table) < 20:
            return 0
        first, count = struct.unpack_from(">II", table, 12)
        index = codepoint - first
        position = 20 + index * 2
        return (
            struct.unpack_from(">H", table, position)[0]
            if 0 <= index < count and position + 2 <= len(table)
            else 0
        )
    if format_number in {12, 13}:
        if len(table) < 16:
            return 0
        count = struct.unpack_from(">I", table, 12)[0]
        for index in range(count):
            position = 16 + index * 12
            if position + 12 > len(table):
                return 0
            start, end, first = struct.unpack_from(">III", table, position)
            if codepoint < start:
                return 0
            if codepoint <= end:
                return first if format_number == 13 else first + codepoint - start
        return 0
    return 0


def font_supports(path: Path, character: str) -> bool:
    """Report whether a font supports every requested character."""
    if len(character) != 1:
        raise ValueError("glyph coverage requires exactly one character")
    return any(
        cmap_glyph_id(table, ord(character)) != 0 for table in unicode_cmap_subtables(str(path))
    )


if __name__ == "__main__":
    assert font_supports(
        Path(__file__).resolve().parents[1] / "assets/fonts/ComicNeue-Regular.ttf", "A"
    )
    print("font-cmap-ok")


__all__ = ["cmap_glyph_id", "font_supports", "unicode_cmap_subtables"]
