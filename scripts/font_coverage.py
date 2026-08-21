#!/usr/bin/env python3
"""Unicode block inventory and script shaping policy for Comic Sol lettering.

Comic Sol letters text with Pillow's advance-only text drawing: every character
is placed at the pen position its predecessor's nominal advance produced. That is
faithful for scripts whose visible form does not depend on neighbours, and it is
silently wrong for scripts that need contextual joining, cluster reordering, or
bidirectional runs. Pillow can be built with Raqm, but that is an optional native
dependency, so a host-dependent shaping capability would make identical text
render differently on different machines. This module therefore states the
supported set as data rather than deriving it from the host build, which keeps
lettering deterministic and keeps the reason a script is refused reviewable.

The module depends only on ``font_cmap``, so a coverage inventory can be taken
without Pillow and without ``fontTools``.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from .font_cmap import cmap_glyph_id, unicode_cmap_subtables


INVENTORY_SCHEMA_VERSION = "1.0"

# Advance-only placement is faithful for these scripts.
SHAPING_LINEAR = "linear"
# These scripts need a shaping engine Comic Sol deliberately does not depend on.
SHAPING_COMPLEX = "complex"

# The lettering plane. Comic Sol letters from the Basic Multilingual Plane only:
# no bundled or recommended face covers the supplementary planes, and the
# pictographic blocks that dominate them are colour-font territory rather than
# comic lettering.
LETTERING_PLANE_LAST = 0xFFFF

_ASTRAL_REASON = (
    "codepoint is outside the basic multilingual plane that lettering supports"
)


@dataclass(frozen=True)
class UnicodeBlock:
    """One named Unicode block and its lettering shaping classification."""

    script: str
    block: str
    first: int
    last: int
    shaping: str
    reason: str = ""

    @property
    def total(self) -> int:
        """Return how many codepoints the block spans."""
        return self.last - self.first + 1

    @property
    def label(self) -> str:
        """Return the block's inclusive range in ``U+XXXX-U+XXXX`` form."""
        return f"U+{self.first:04X}-U+{self.last:04X}"


def _linear(script: str, block: str, first: int, last: int) -> UnicodeBlock:
    """Declare a block that advance-only placement renders faithfully."""
    return UnicodeBlock(script, block, first, last, SHAPING_LINEAR)


def _complex(script: str, block: str, first: int, last: int, reason: str) -> UnicodeBlock:
    """Declare a block that requires shaping Comic Sol does not perform."""
    return UnicodeBlock(script, block, first, last, SHAPING_COMPLEX, reason)


_JOINING = "script requires contextual joining and bidirectional reordering"
_BIDI = "script requires bidirectional reordering"
_CLUSTER = "script requires syllable cluster reordering and conjunct formation"
_STACKING = "script requires mark stacking that nominal advances cannot express"
_VERTICAL = "script requires vertical layout and contextual joining"
_JAMO = "conjoining jamo require syllable composition; use precomposed syllables"

# Blocks are declared in ascending, non-overlapping codepoint order. Codepoints
# in the lettering plane that no block claims default to `SHAPING_LINEAR`: the
# unclaimed remainder is punctuation, symbol, and technical blocks that place
# linearly, so silence here is the correct permissive answer. Every script whose
# rendering depends on neighbours is claimed explicitly below.
UNICODE_BLOCKS: tuple[UnicodeBlock, ...] = (
    _linear("latin", "Basic Latin", 0x0000, 0x007F),
    _linear("latin", "Latin-1 Supplement", 0x0080, 0x00FF),
    _linear("latin", "Latin Extended-A", 0x0100, 0x017F),
    _linear("latin", "Latin Extended-B", 0x0180, 0x024F),
    _linear("latin", "IPA Extensions", 0x0250, 0x02AF),
    _linear("common", "Spacing Modifier Letters", 0x02B0, 0x02FF),
    _linear("inherited", "Combining Diacritical Marks", 0x0300, 0x036F),
    _linear("greek", "Greek and Coptic", 0x0370, 0x03FF),
    _linear("cyrillic", "Cyrillic", 0x0400, 0x04FF),
    _linear("cyrillic", "Cyrillic Supplement", 0x0500, 0x052F),
    _linear("armenian", "Armenian", 0x0530, 0x058F),
    _complex("hebrew", "Hebrew", 0x0590, 0x05FF, _BIDI),
    _complex("arabic", "Arabic", 0x0600, 0x06FF, _JOINING),
    _complex("syriac", "Syriac", 0x0700, 0x074F, _JOINING),
    _complex("arabic", "Arabic Supplement", 0x0750, 0x077F, _JOINING),
    _complex("thaana", "Thaana", 0x0780, 0x07BF, _BIDI),
    _complex("nko", "NKo", 0x07C0, 0x07FF, _JOINING),
    _complex("samaritan", "Samaritan", 0x0800, 0x083F, _BIDI),
    _complex("mandaic", "Mandaic", 0x0840, 0x085F, _JOINING),
    _complex("arabic", "Arabic Extended-A", 0x08A0, 0x08FF, _JOINING),
    _complex("devanagari", "Devanagari", 0x0900, 0x097F, _CLUSTER),
    _complex("bengali", "Bengali", 0x0980, 0x09FF, _CLUSTER),
    _complex("gurmukhi", "Gurmukhi", 0x0A00, 0x0A7F, _CLUSTER),
    _complex("gujarati", "Gujarati", 0x0A80, 0x0AFF, _CLUSTER),
    _complex("oriya", "Oriya", 0x0B00, 0x0B7F, _CLUSTER),
    _complex("tamil", "Tamil", 0x0B80, 0x0BFF, _CLUSTER),
    _complex("telugu", "Telugu", 0x0C00, 0x0C7F, _CLUSTER),
    _complex("kannada", "Kannada", 0x0C80, 0x0CFF, _CLUSTER),
    _complex("malayalam", "Malayalam", 0x0D00, 0x0D7F, _CLUSTER),
    _complex("sinhala", "Sinhala", 0x0D80, 0x0DFF, _CLUSTER),
    _complex("thai", "Thai", 0x0E00, 0x0E7F, _STACKING),
    _complex("lao", "Lao", 0x0E80, 0x0EFF, _STACKING),
    _complex("tibetan", "Tibetan", 0x0F00, 0x0FFF, _STACKING),
    _complex("myanmar", "Myanmar", 0x1000, 0x109F, _CLUSTER),
    _linear("georgian", "Georgian", 0x10A0, 0x10FF),
    _complex("hangul", "Hangul Jamo", 0x1100, 0x11FF, _JAMO),
    _linear("ethiopic", "Ethiopic", 0x1200, 0x137F),
    _linear("cherokee", "Cherokee", 0x13A0, 0x13FF),
    _complex("khmer", "Khmer", 0x1780, 0x17FF, _CLUSTER),
    _complex("mongolian", "Mongolian", 0x1800, 0x18AF, _VERTICAL),
    _linear("inherited", "Combining Diacritical Marks Extended", 0x1AB0, 0x1AFF),
    _linear("cyrillic", "Cyrillic Extended-C", 0x1C80, 0x1C8F),
    # Mtavruli capitals live here, and dialogue is displayed uppercased, so this
    # block is on the path of any Georgian dialogue line.
    _linear("georgian", "Georgian Extended", 0x1C90, 0x1CBF),
    _linear("common", "Phonetic Extensions", 0x1D00, 0x1D7F),
    _linear("common", "Phonetic Extensions Supplement", 0x1D80, 0x1DBF),
    _linear("inherited", "Combining Diacritical Marks Supplement", 0x1DC0, 0x1DFF),
    _linear("latin", "Latin Extended Additional", 0x1E00, 0x1EFF),
    _linear("greek", "Greek Extended", 0x1F00, 0x1FFF),
    _linear("common", "General Punctuation", 0x2000, 0x206F),
    _linear("common", "Superscripts and Subscripts", 0x2070, 0x209F),
    _linear("common", "Currency Symbols", 0x20A0, 0x20CF),
    _linear("inherited", "Combining Marks for Symbols", 0x20D0, 0x20FF),
    _linear("common", "Letterlike Symbols", 0x2100, 0x214F),
    _linear("common", "Number Forms", 0x2150, 0x218F),
    _linear("common", "Arrows", 0x2190, 0x21FF),
    _linear("common", "Mathematical Operators", 0x2200, 0x22FF),
    _linear("common", "Miscellaneous Technical", 0x2300, 0x23FF),
    _linear("common", "Geometric Shapes", 0x25A0, 0x25FF),
    _linear("common", "Miscellaneous Symbols", 0x2600, 0x26FF),
    _linear("common", "Dingbats", 0x2700, 0x27BF),
    _linear("glagolitic", "Glagolitic", 0x2C00, 0x2C5F),
    _linear("latin", "Latin Extended-C", 0x2C60, 0x2C7F),
    _linear("coptic", "Coptic", 0x2C80, 0x2CFF),
    _linear("georgian", "Georgian Supplement", 0x2D00, 0x2D2F),
    _linear("cyrillic", "Cyrillic Extended-A", 0x2DE0, 0x2DFF),
    _linear("common", "Supplemental Punctuation", 0x2E00, 0x2E7F),
    _linear("han", "CJK Symbols and Punctuation", 0x3000, 0x303F),
    _linear("kana", "Hiragana", 0x3040, 0x309F),
    _linear("kana", "Katakana", 0x30A0, 0x30FF),
    _linear("bopomofo", "Bopomofo", 0x3100, 0x312F),
    _linear("kana", "Katakana Phonetic Extensions", 0x31F0, 0x31FF),
    _linear("han", "CJK Unified Ideographs Extension A", 0x3400, 0x4DBF),
    _linear("han", "CJK Unified Ideographs", 0x4E00, 0x9FFF),
    _linear("yi", "Yi Syllables", 0xA000, 0xA48F),
    _linear("cyrillic", "Cyrillic Extended-B", 0xA640, 0xA69F),
    _linear("common", "Modifier Tone Letters", 0xA700, 0xA71F),
    _linear("latin", "Latin Extended-D", 0xA720, 0xA7FF),
    _linear("latin", "Latin Extended-E", 0xAB30, 0xAB6F),
    _linear("cherokee", "Cherokee Supplement", 0xAB70, 0xABBF),
    _linear("hangul", "Hangul Syllables", 0xAC00, 0xD7A3),
    _linear("latin", "Alphabetic Presentation Forms", 0xFB00, 0xFB1C),
    _complex("hebrew", "Hebrew Presentation Forms", 0xFB1D, 0xFB4F, _BIDI),
    _complex("arabic", "Arabic Presentation Forms-A", 0xFB50, 0xFDFF, _JOINING),
    _linear("inherited", "Variation Selectors", 0xFE00, 0xFE0F),
    _linear("inherited", "Combining Half Marks", 0xFE20, 0xFE2F),
    # U+FEFF is a byte-order mark rather than an Arabic form, so the joining
    # block stops one codepoint short of it and the mark keeps placing linearly.
    _complex("arabic", "Arabic Presentation Forms-B", 0xFE70, 0xFEFE, _JOINING),
    _linear("common", "Halfwidth and Fullwidth Forms", 0xFF00, 0xFFEF),
    _linear("common", "Specials", 0xFFF0, 0xFFFF),
)

_BLOCK_STARTS: tuple[int, ...] = tuple(block.first for block in UNICODE_BLOCKS)

# A script is letterable when at least one of its blocks places linearly, so
# Hangul qualifies through its precomposed syllables even though its conjoining
# jamo do not. Configuring an extension font for anything outside this set would
# buy glyphs that still could not be positioned correctly, so policy refuses it.
LINEAR_SCRIPTS: frozenset[str] = frozenset(
    block.script for block in UNICODE_BLOCKS if block.shaping == SHAPING_LINEAR
)


@dataclass(frozen=True)
class ScriptFont:
    """A vetted, redistributable face that covers one target script."""

    script: str
    family: str
    file_name: str
    upstream: str
    license_id: str

    def as_record(self) -> dict[str, str]:
        """Return the recommendation as a canonical, sorted-key record."""
        return {
            "family": self.family,
            "file_name": self.file_name,
            "license": self.license_id,
            "script": self.script,
            "upstream": self.upstream,
        }


# Selected for redistributability first: every entry is SIL Open Font License
# 1.1, the same licence the bundled faces already carry, so an extension font
# travels under a licence Comic Sol already satisfies. None of these are bundled.
# Each is several megabytes, and shipping them in every install to serve one
# project's language would trade a large, permanent package cost for an
# occasional benefit. They are opt-in per project instead, and preflight names
# the one a refused script needs.
SCRIPT_FONTS: tuple[ScriptFont, ...] = (
    ScriptFont(
        "han",
        "Noto Sans SC",
        "NotoSansSC-Regular.ttf",
        "https://github.com/notofonts/noto-cjk",
        "OFL-1.1",
    ),
    ScriptFont(
        "kana",
        "Noto Sans JP",
        "NotoSansJP-Regular.ttf",
        "https://github.com/notofonts/noto-cjk",
        "OFL-1.1",
    ),
    ScriptFont(
        "hangul",
        "Noto Sans KR",
        "NotoSansKR-Regular.ttf",
        "https://github.com/notofonts/noto-cjk",
        "OFL-1.1",
    ),
    ScriptFont(
        "armenian",
        "Noto Sans Armenian",
        "NotoSansArmenian-Regular.ttf",
        "https://github.com/notofonts/armenian",
        "OFL-1.1",
    ),
    ScriptFont(
        "georgian",
        "Noto Sans Georgian",
        "NotoSansGeorgian-Regular.ttf",
        "https://github.com/notofonts/georgian",
        "OFL-1.1",
    ),
    ScriptFont(
        "ethiopic",
        "Noto Sans Ethiopic",
        "NotoSansEthiopic-Regular.ttf",
        "https://github.com/notofonts/ethiopic",
        "OFL-1.1",
    ),
)

SCRIPT_FONTS_BY_SCRIPT: dict[str, ScriptFont] = {
    entry.script: entry for entry in SCRIPT_FONTS
}

# Scripts the bundled faces are expected to letter without an extension font.
# `tests/test_font_coverage.py` asserts the bundled policy still covers these,
# so a font swap that silently drops a script fails the suite.
BUNDLED_TARGET_SCRIPTS: tuple[str, ...] = (
    "latin",
    "greek",
    "cyrillic",
)

# Scripts that place linearly and are therefore admitted by policy, but whose
# glyphs no bundled face carries. Authors reach them by configuring the matching
# `SCRIPT_FONTS` extension; until they do, preflight reports `missing-glyph`.
EXTENSION_TARGET_SCRIPTS: tuple[str, ...] = (
    "armenian",
    "ethiopic",
    "georgian",
    "han",
    "hangul",
    "kana",
)


def block_for_codepoint(codepoint: int) -> UnicodeBlock | None:
    """Return the declared Unicode block owning a codepoint, if any."""
    index = bisect_right(_BLOCK_STARTS, codepoint) - 1
    if index < 0:
        return None
    block = UNICODE_BLOCKS[index]
    return block if codepoint <= block.last else None


def script_for_codepoint(codepoint: int) -> str:
    """Return the lettering script a codepoint belongs to."""
    block = block_for_codepoint(codepoint)
    return block.script if block is not None else "unassigned"


def shaping_policy(codepoint: int) -> tuple[str, str]:
    """Classify a codepoint as linearly placeable or shaping-dependent.

    Returns the shaping class and, when the codepoint is refused, the reason a
    reviewer needs. Codepoints inside the lettering plane that no block claims
    are linear: the unclaimed remainder is symbol and punctuation territory.
    """
    if codepoint > LETTERING_PLANE_LAST:
        return SHAPING_COMPLEX, _ASTRAL_REASON
    block = block_for_codepoint(codepoint)
    if block is None:
        return SHAPING_LINEAR, ""
    return block.shaping, block.reason


def recommended_font(script: str) -> ScriptFont | None:
    """Return the vetted extension face for a script, when one is selected."""
    return SCRIPT_FONTS_BY_SCRIPT.get(script)


def _subtable_codepoints(table: bytes) -> set[int]:
    """Return the codepoints one Unicode cmap subtable maps to a real glyph."""
    if len(table) < 4:
        return set()
    format_number = struct.unpack_from(">H", table, 0)[0]
    spans: list[tuple[int, int]] = []
    if format_number == 0:
        spans.append((0x00, 0xFF))
    elif format_number == 4:
        if len(table) < 16:
            return set()
        segment_count = struct.unpack_from(">H", table, 6)[0] // 2
        ends = 14
        starts = ends + segment_count * 2 + 2
        if starts + segment_count * 2 > len(table):
            return set()
        for index in range(segment_count):
            end = struct.unpack_from(">H", table, ends + index * 2)[0]
            start = struct.unpack_from(">H", table, starts + index * 2)[0]
            if start <= end:
                spans.append((start, end))
    elif format_number == 6:
        if len(table) < 10:
            return set()
        first, count = struct.unpack_from(">HH", table, 6)
        spans.append((first, first + max(0, count - 1)))
    elif format_number == 10:
        if len(table) < 20:
            return set()
        first, count = struct.unpack_from(">II", table, 12)
        spans.append((first, first + max(0, count - 1)))
    elif format_number in {12, 13}:
        if len(table) < 16:
            return set()
        group_count = struct.unpack_from(">I", table, 12)[0]
        for index in range(group_count):
            position = 16 + index * 12
            if position + 12 > len(table):
                break
            start, end, _ = struct.unpack_from(">III", table, position)
            if start <= end:
                spans.append((start, end))
    else:
        return set()

    covered: set[int] = set()
    for start, end in spans:
        # Clamp to the lettering plane: the segment terminator in format 4 spans
        # to U+FFFF and format 12 groups can span the whole codespace, so an
        # unclamped walk would cost far more than the inventory is worth.
        for codepoint in range(max(0, start), min(end, LETTERING_PLANE_LAST) + 1):
            if cmap_glyph_id(table, codepoint) != 0:
                covered.add(codepoint)
    return covered


@lru_cache(maxsize=None)
def font_codepoints(path: str) -> frozenset[int]:
    """Return every lettering-plane codepoint a font maps to a real glyph."""
    covered: set[int] = set()
    for table in unicode_cmap_subtables(path):
        covered |= _subtable_codepoints(table)
    return frozenset(covered)


def condense_ranges(codepoints: Sequence[int] | frozenset[int]) -> tuple[tuple[int, int], ...]:
    """Condense codepoints into ascending, inclusive, non-adjacent ranges."""
    ranges: list[list[int]] = []
    for codepoint in sorted(codepoints):
        if ranges and codepoint == ranges[-1][1] + 1:
            ranges[-1][1] = codepoint
        else:
            ranges.append([codepoint, codepoint])
    return tuple((first, last) for first, last in ranges)


def format_ranges(ranges: Sequence[tuple[int, int]]) -> tuple[str, ...]:
    """Render codepoint ranges as reviewable ``U+XXXX`` labels."""
    return tuple(
        f"U+{first:04X}" if first == last else f"U+{first:04X}-U+{last:04X}"
        for first, last in ranges
    )


def coverage_inventory(font_policy: Mapping[str, object]) -> dict[str, object]:
    """Inventory which declared Unicode blocks a font policy actually covers.

    The record is derived entirely from the fonts' cmap tables and this module's
    block table, so it is reproducible from the policy alone and safe to publish
    in documentation or attach to a review.
    """
    roles: dict[str, Path] = {}
    for role in sorted(font_policy):
        value = font_policy[role]
        if not isinstance(value, (str, Path)):
            raise TypeError(f"font policy {role} must be a path")
        roles[role] = Path(value)
    coverage = {role: font_codepoints(str(path)) for role, path in roles.items()}

    fonts: dict[str, object] = {}
    for role, path in roles.items():
        covered = coverage[role]
        fonts[role] = {
            "codepoints": len(covered),
            "font_id": path.name,
            "ranges": list(format_ranges(condense_ranges(covered))),
        }

    union: set[int] = set()
    for covered in coverage.values():
        union |= covered

    scripts: dict[str, dict[str, object]] = {}
    for block in UNICODE_BLOCKS:
        span = range(block.first, block.last + 1)
        covered_here = sum(1 for codepoint in span if codepoint in union)
        entry = scripts.setdefault(
            block.script,
            {
                "blocks": [],
                "covered": 0,
                "script": block.script,
                "shaping": SHAPING_COMPLEX,
                "total": 0,
            },
        )
        # A script is shaping-refused as a whole only when every one of its
        # blocks is, because Hangul mixes conjoining jamo that need composition
        # with precomposed syllables that place linearly.
        if block.shaping == SHAPING_LINEAR:
            entry["shaping"] = SHAPING_LINEAR
        blocks = entry["blocks"]
        assert isinstance(blocks, list)
        blocks.append({
            "block": block.block,
            "covered": covered_here,
            "range": block.label,
            "shaping": block.shaping,
            "total": block.total,
        })
        entry["covered"] = int(entry["covered"]) + covered_here
        entry["total"] = int(entry["total"]) + block.total

    for entry in scripts.values():
        recommendation = recommended_font(str(entry["script"]))
        entry["status"] = _script_status(entry)
        entry["recommended_font"] = (
            recommendation.as_record() if recommendation is not None else None
        )

    claimed = sum(block.total for block in UNICODE_BLOCKS)
    return {
        "fonts": fonts,
        "kind": "font-coverage-inventory",
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "scripts": [scripts[script] for script in sorted(scripts)],
        "summary": {
            "bundled_target_scripts": list(BUNDLED_TARGET_SCRIPTS),
            "claimed_codepoints": claimed,
            "covered_codepoints": len(union),
            "extension_target_scripts": list(EXTENSION_TARGET_SCRIPTS),
            "lettering_plane_last": f"U+{LETTERING_PLANE_LAST:04X}",
        },
    }


def _script_status(entry: Mapping[str, object]) -> str:
    """Summarize one script's lettering availability under a font policy."""
    if entry["shaping"] == SHAPING_COMPLEX:
        return "shaping-unsupported"
    covered = int(entry["covered"])
    if covered == 0:
        return "uncovered"
    return "covered" if covered == int(entry["total"]) else "partial"


def _default_policy() -> dict[str, Path]:
    """Return the bundled font policy used when no fonts are supplied."""
    fonts = Path(__file__).resolve().parents[1] / "assets/fonts"
    return {
        "bold": fonts / "ComicNeue-Bold.ttf",
        "fallback": fonts / "NotoSans-Regular.ttf",
        "regular": fonts / "ComicNeue-Regular.ttf",
    }


def main(argv: list[str] | None = None) -> int:
    """Print the coverage inventory for a font policy as canonical JSON."""
    parser = argparse.ArgumentParser(prog="font_coverage.py")
    parser.add_argument(
        "--font",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="Font policy entry to inventory; repeatable. Defaults to the bundled policy.",
    )
    try:
        arguments = parser.parse_args(argv)
        policy: dict[str, Path] = {}
        for entry in arguments.font:
            role, separator, value = str(entry).partition("=")
            if not separator or not role.strip() or not value.strip():
                raise ValueError(f"font policy entry must be ROLE=PATH: {entry}")
            policy[role.strip()] = Path(value.strip())
        inventory = coverage_inventory(policy or _default_policy())
        print(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUNDLED_TARGET_SCRIPTS",
    "EXTENSION_TARGET_SCRIPTS",
    "INVENTORY_SCHEMA_VERSION",
    "LINEAR_SCRIPTS",
    "SCRIPT_FONTS",
    "SCRIPT_FONTS_BY_SCRIPT",
    "SHAPING_COMPLEX",
    "SHAPING_LINEAR",
    "UNICODE_BLOCKS",
    "ScriptFont",
    "UnicodeBlock",
    "block_for_codepoint",
    "condense_ranges",
    "coverage_inventory",
    "font_codepoints",
    "format_ranges",
    "recommended_font",
    "script_for_codepoint",
    "shaping_policy",
]
