#!/usr/bin/env python3
"""Schema-2.0 page QA records derived from composition and lettering evidence."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image

from .comic_sol import atomic_write_json, read_json, sha256_file
from .core_primitives import (
    BALLOON_COVERAGE_WARNING_RATIO,
    balloon_separation_minimum,
    balloon_subject_clearance,
    is_geometry_point,
    rectangle_overlap_area,
    rectangle_separation,
    subject_keepout_radius,
    tail_geometry_result,
)
from .layouts import LAYOUT_VERSION, match_layout, validate_custom_layout
from .project_io import contained_project_path, open_path_nofollow
from .quality_records import PAGE_CHECK_IDS, validate_quality_checks

DETERMINISTIC_PAGE_CHECK_IDS = frozenset({
    "clipped-text",
    "text-overlap",
    "reading-order",
    "layout-border-integrity",
    "balloon-subject-obstruction",
    "bubble-tail-geometry",
    "balloon-crowding",
})
SUBJECTIVE_PAGE_CHECK_IDS = tuple(
    check_id for check_id in PAGE_CHECK_IDS
    if check_id not in DETERMINISTIC_PAGE_CHECK_IDS
)
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PAGE_RECORD_FIELDS = {
    "bindings", "checks", "decision", "kind", "review", "schema_version",
    "subject_id", "unresolved_warnings",
}
PAGE_REVIEW_FIELDS = {"method", "reviewer", "reviewed_at"}
PAGE_BINDING_FIELDS = {
    "composition_cache_path", "composition_cache_sha256", "layout_name",
    "layout_version", "lettering_sha256s", "normalization_sha256s", "page_height",
    "page_path", "page_sha256", "page_width", "storyboard_path",
    "storyboard_sha256",
}


@dataclass(frozen=True)
class PageQualityIssue:
    path: str
    field: str
    message: str


@dataclass(frozen=True)
class PageContext:
    storyboard_path: Path
    panels: tuple[dict[str, object], ...]
    rectangles: tuple[tuple[int, int, int, int], ...]
    declared_layout: str
    matched_layout: str
    page_relative: str
    page_path: Path
    page_width: int
    page_height: int
    cache_relative: str
    cache_path: Path
    lettering: tuple[tuple[str, str, dict[str, object]], ...]
    # Lettering boxes and tails are expressed in each panel's own clean-raster
    # pixel space, which is not the storyboard page rectangle the panel is later
    # fitted into. Balloon geometry must be audited in this space.
    clean_sizes: tuple[tuple[int, int], ...]
    normalization: tuple[tuple[str, str], ...]


def _page_id(page_number: int) -> str:
    """Return the canonical identifier for a page number."""
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page number must be a positive integer")
    return f"page-{page_number:03d}"


def _storyboard_page(storyboard: Mapping[str, object], page_number: int) -> dict[str, object]:
    """Return the storyboard record for a page number."""
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ValueError("storyboard pages must be an array")
    matches = [page for page in pages if isinstance(page, dict) and page.get("number") == page_number]
    if len(matches) != 1:
        raise ValueError(f"storyboard page {page_number} was not found exactly once")
    return matches[0]


def _rect_tuple(panel: Mapping[str, object]) -> tuple[int, int, int, int]:
    """Return a rectangle as integer coordinate bounds."""
    rect = panel.get("rect")
    if not isinstance(rect, dict):
        raise ValueError("storyboard panel rectangle is missing")
    values = tuple(rect.get(key) for key in ("x", "y", "width", "height"))
    if len(values) != 4 or any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("storyboard panel rectangle must contain integers")
    return values  # type: ignore[return-value]


def _clean_raster(project_dir: Path, panel_id: str) -> tuple[tuple[int, int], str]:
    """Return the pixel space one panel's lettering geometry is expressed in.

    The digest travels with the size because every balloon verdict is measured in
    this space, so the record must bind the artifact that defines it.
    """
    relative = f"panels/{panel_id}/normalization.json"
    try:
        path = contained_project_path(project_dir, relative, must_exist=True)
    except (OSError, ValueError) as error:
        raise ValueError(
            f"normalization record is missing for panel {panel_id}"
        ) from error
    normalization = read_json(path)
    clean = normalization.get("clean") if isinstance(normalization, dict) else None
    size = clean.get("size") if isinstance(clean, dict) else None
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in size
        )
    ):
        raise ValueError(f"normalization record is invalid for panel {panel_id}")
    return (size[0], size[1]), sha256_file(path)


def _page_context(project_dir: Path, page_number: int) -> PageContext:
    """Build deterministic quality-check context for one page."""
    storyboard_path = contained_project_path(project_dir, "plan/storyboard.json", must_exist=True)
    storyboard = read_json(storyboard_path)
    page = _storyboard_page(storyboard, page_number)
    panels = page.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValueError("storyboard page panels must be a non-empty array")
    if not all(isinstance(panel, dict) for panel in panels):
        raise ValueError("storyboard page contains an invalid panel")
    typed_panels = tuple(panels)
    rectangles = tuple(_rect_tuple(panel) for panel in typed_panels)
    validate_custom_layout(rectangles, tuple(range(1, len(rectangles) + 1)))
    matched_layout = match_layout(rectangles)
    declared_layout = page.get("layout")
    if not isinstance(declared_layout, str) or not declared_layout:
        declared_layout = matched_layout

    page_id = _page_id(page_number)
    page_relative = f"pages/{page_id}.png"
    page_path = contained_project_path(project_dir, page_relative, must_exist=True)
    with open_path_nofollow(page_path) as stream, Image.open(stream) as image:
        image.load()
        page_width, page_height = image.size

    cache_relative = "cache/composition.json"
    cache_path = contained_project_path(project_dir, cache_relative, must_exist=True)
    lettering: list[tuple[str, str, dict[str, object]]] = []
    clean_sizes: list[tuple[int, int]] = []
    normalization: list[tuple[str, str]] = []
    for panel in typed_panels:
        panel_id = panel.get("id")
        if not isinstance(panel_id, str):
            raise ValueError("storyboard panel ID is invalid")
        relative = f"panels/{panel_id}/lettering.json"
        try:
            path = contained_project_path(project_dir, relative, must_exist=True)
        except (OSError, ValueError) as error:
            raise ValueError(
                f"lettering geometry is missing for panel {panel_id}"
            ) from error
        geometry = read_json(path)
        if not isinstance(geometry, dict):
            raise ValueError(f"lettering geometry is invalid for panel {panel_id}")
        lettering.append((panel_id, sha256_file(path), geometry))
        clean_size, normalization_digest = _clean_raster(project_dir, panel_id)
        clean_sizes.append(clean_size)
        normalization.append((panel_id, normalization_digest))

    return PageContext(
        storyboard_path=storyboard_path,
        panels=typed_panels,
        rectangles=rectangles,
        declared_layout=declared_layout,
        matched_layout=matched_layout,
        page_relative=page_relative,
        page_path=page_path,
        page_width=page_width,
        page_height=page_height,
        cache_relative=cache_relative,
        cache_path=cache_path,
        lettering=tuple(lettering),
        clean_sizes=tuple(clean_sizes),
        normalization=tuple(normalization),
    )


def _protected_anchors(
    panel: Mapping[str, object], width: int, height: int
) -> list[tuple[str, int, int]]:
    """Return authored voice-source points for a panel in clean-raster pixels.

    A `speaker_anchor` marks the visible mouth or face a balloon speaks from, so
    it is the only machine-readable protected subject the storyboard carries. A
    panel that authors no dialogue anchor has nothing to protect.
    """
    text_items = panel.get("text")
    if not isinstance(text_items, list):
        return []
    anchors: list[tuple[str, int, int]] = []
    for item in text_items:
        if not isinstance(item, dict) or item.get("kind") != "dialogue":
            continue
        anchor = item.get("speaker_anchor")
        # Match the renderer's contract exactly: finite and normalized into the
        # panel. An out-of-range anchor points outside the artwork and is a
        # storyboard defect, not a protected subject to measure against.
        if (
            not isinstance(anchor, list)
            or len(anchor) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 1
                for value in anchor
            )
        ):
            continue
        text_id = item.get("id")
        anchors.append((
            text_id if isinstance(text_id, str) else "unknown",
            round(float(anchor[0]) * width),
            round(float(anchor[1]) * height),
        ))
    return anchors


def _subject_obstruction_regions(
    panel_id: str,
    anchors: Sequence[tuple[str, int, int]],
    boxes: Sequence[tuple[str, dict[str, object], object]],
    width: int,
    height: int,
) -> list[dict[str, object]]:
    """Return balloons that crowd a protected speaker anchor in one panel."""
    if not anchors:
        return []
    required = subject_keepout_radius(width, height)
    regions: list[dict[str, object]] = []
    for item_id, box, kind in boxes:
        for text_id, anchor_x, anchor_y in anchors:
            clearance = balloon_subject_clearance(
                box, (anchor_x, anchor_y), ellipse=kind == "dialogue"
            )
            if clearance < required:
                regions.append({
                    "clearance": round(clearance, 4),
                    "item_id": item_id,
                    "panel_id": panel_id,
                    "required_clearance": round(required, 4),
                    "subject_text_id": text_id,
                })
    return regions


def _attachment_near_box(
    attachment: object, box: object
) -> bool:
    """Return whether a tail attachment point sits on or inside the balloon box.

    The renderer places the attachment on the inscribed ellipse, which is always
    inside the bounding box. Geometry is rounded to four decimals, so a 1-pixel
    tolerance accommodates rounding without accepting a fully detached tail.
    """
    if not is_geometry_point(attachment) or not isinstance(box, Mapping):
        return False
    x = box.get("x")
    y = box.get("y")
    w = box.get("width")
    h = box.get("height")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y, w, h)):
        return False
    ax, ay = float(attachment[0]), float(attachment[1])  # type: ignore[index]
    tolerance = 1.0
    return (
        ax >= x - tolerance and ax <= x + w + tolerance
        and ay >= y - tolerance and ay <= y + h + tolerance
    )


def _tail_geometry_regions(
    panel_id: str,
    panel: Mapping[str, object],
    placements: Mapping[str, Mapping[str, object]],
    width: int,
    height: int,
) -> list[dict[str, object]]:
    """Return dialogue tails that disagree with their authored speaker."""
    text_items = panel.get("text")
    if not isinstance(text_items, list):
        return []
    regions: list[dict[str, object]] = []
    for item in text_items:
        if not isinstance(item, dict) or item.get("kind") != "dialogue":
            continue
        text_id = item.get("id")
        placement = placements.get(text_id) if isinstance(text_id, str) else None
        tail = placement.get("tail") if isinstance(placement, Mapping) else None
        box = placement.get("box") if isinstance(placement, Mapping) else None
        anchor = item.get("speaker_anchor")
        if not isinstance(tail, Mapping):
            reason = "missing-tail"
        elif tail.get("speaker_anchor") != anchor:
            reason = "speaker-anchor-mismatch"
        elif tail.get("voice_source") != item.get("voice_source"):
            reason = "voice-source-mismatch"
        elif not _attachment_near_box(tail.get("attachment"), box):
            reason = "detached-tail"
        elif tail_geometry_result(tail, anchor, width, height) != "pass":
            reason = "tail-does-not-point-at-speaker"
        else:
            continue
        regions.append({
            "panel_id": panel_id,
            "reason": reason,
            "text_id": text_id if isinstance(text_id, str) else "unknown",
        })
    return regions


def _crowding_regions(
    panel_id: str,
    boxes: Sequence[tuple[str, dict[str, object], object]],
    width: int,
    height: int,
) -> list[dict[str, object]]:
    """Return one crowded-layout observation when a panel reads as congested."""
    area = width * height
    if area <= 0 or not boxes:
        return []
    # Count only the part of each box that actually lands on the panel, so a box
    # that `clipped-text` already rejected cannot inflate the reported share. The
    # sum is exact whenever the boxes do not overlap, which `text-overlap`
    # enforces as an error, and is capped so a published ratio stays meaningful
    # on a page that is failing both checks at once.
    panel = {"x": 0, "y": 0, "width": width, "height": height}
    covered = sum(rectangle_overlap_area(box, panel) for _, box, _ in boxes)
    coverage = min(1.0, covered / area)
    required_separation = balloon_separation_minimum(width, height)
    tight: list[dict[str, object]] = []
    for index, (item_id, box, _) in enumerate(boxes):
        for other_id, other_box, _ in boxes[index + 1:]:
            separation = rectangle_separation(box, other_box)
            if separation < required_separation:
                tight.append({
                    "items": [item_id, other_id],
                    "separation": round(separation, 4),
                })
    if coverage <= BALLOON_COVERAGE_WARNING_RATIO and not tight:
        return []
    return [{
        "balloons": len(boxes),
        "coverage_limit": BALLOON_COVERAGE_WARNING_RATIO,
        "coverage_ratio": round(coverage, 4),
        "panel_id": panel_id,
        "required_separation": round(required_separation, 4),
        "tight_pairs": tight,
    }]


def _crowding_check(regions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Return the crowded-layout check, which warns instead of blocking a page."""
    if not regions:
        return {
            "id": "balloon-crowding",
            "result": "pass",
            "severity": "info",
            "evidence": (
                "Every panel keeps balloon coverage and balloon separation inside the "
                "comfortable reading budget."
            ),
            "method": "deterministic-geometry-v1",
            "reviewer": "comic-sol",
            "regions": [],
        }
    panels = ", ".join(str(region.get("panel_id")) for region in regions)
    crowded = any(
        isinstance(region.get("coverage_ratio"), float)
        and region["coverage_ratio"] > BALLOON_COVERAGE_WARNING_RATIO
        for region in regions
    )
    tight = any(region.get("tight_pairs") for region in regions)
    # Name the signal that actually fired: a warning that describes the wrong
    # problem costs the reader the time the warning was supposed to save.
    if crowded and tight:
        observed = (
            f"balloon coverage exceeds {BALLOON_COVERAGE_WARNING_RATIO:.0%} of the panel "
            "and balloons sit closer than the readable separation"
        )
    elif crowded:
        observed = (
            f"balloon coverage exceeds {BALLOON_COVERAGE_WARNING_RATIO:.0%} of the panel"
        )
    else:
        observed = "two balloons sit closer than the readable separation"
    return {
        "id": "balloon-crowding",
        "result": "warning",
        "severity": "warning",
        "evidence": (
            f"Crowded lettering in {len(regions)} panel(s) ({panels}): {observed}. "
            "Shorten the dialogue, move a line to another panel, or re-anchor the "
            "balloons."
        ),
        "method": "deterministic-geometry-v1",
        "reviewer": "comic-sol",
        "regions": list(regions),
    }


def _deterministic_checks(context: PageContext) -> list[dict[str, object]]:
    """Return deterministic quality checks for a page."""
    clipped_regions: list[dict[str, object]] = []
    overlap_regions: list[dict[str, object]] = []
    order_regions: list[dict[str, object]] = []
    obstruction_regions: list[dict[str, object]] = []
    tail_regions: list[dict[str, object]] = []
    crowding_regions: list[dict[str, object]] = []

    for panel_index, ((panel_id, _, geometry), panel) in enumerate(
        zip(context.lettering, context.panels, strict=True), 1
    ):
        items = geometry.get("items")
        if not isinstance(items, list):
            items = []
        boxes: list[tuple[str, dict[str, object], object]] = []
        orders: list[int] = []
        placements: dict[str, Mapping[str, object]] = {}
        panel_width, panel_height = context.clean_sizes[panel_index - 1]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id") if isinstance(item.get("id"), str) else "unknown"
            if isinstance(item.get("id"), str):
                placements[item["id"]] = item
            box = item.get("box")
            order = item.get("reading_order")
            if isinstance(order, int) and not isinstance(order, bool):
                orders.append(order)
            if not isinstance(box, dict):
                clipped_regions.append({"panel_id": panel_id, "item_id": item_id, "reason": "missing-box"})
                continue
            values = tuple(box.get(key) for key in ("x", "y", "width", "height"))
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                clipped_regions.append({"panel_id": panel_id, "item_id": item_id, "reason": "invalid-box"})
                continue
            x, y, width, height = values
            clipped = (
                x < 0 or y < 0 or width <= 0 or height <= 0
                or x + width > panel_width or y + height > panel_height
            )
            if clipped:
                clipped_regions.append({"panel_id": panel_id, "item_id": item_id, "box": box})
                # A box outside the clean raster is already a regenerate-level
                # defect. Do not let it trigger secondary overlap, obstruction, or
                # crowding failures that would obscure the primary problem.
                continue
            for prior_id, prior_box, _ in boxes:
                shared = rectangle_overlap_area(box, prior_box)
                if shared <= 0:
                    continue
                smallest = min(
                    width * height,
                    int(prior_box["width"]) * int(prior_box["height"]),
                )
                overlap_regions.append({
                    "items": [prior_id, item_id],
                    "overlap_area": shared,
                    "overlap_ratio": round(shared / smallest, 4) if smallest > 0 else 1.0,
                    "panel_id": panel_id,
                })
            boxes.append((item_id, box, item.get("kind")))
        if orders != list(range(1, len(items) + 1)):
            order_regions.append({"panel_id": panel_id, "observed": orders})
        anchors = _protected_anchors(panel, panel_width, panel_height)
        obstruction_regions.extend(
            _subject_obstruction_regions(
                panel_id, anchors, boxes, panel_width, panel_height
            )
        )
        tail_regions.extend(
            _tail_geometry_regions(
                panel_id, panel, placements, panel_width, panel_height
            )
        )
        crowding_regions.extend(
            _crowding_regions(panel_id, boxes, panel_width, panel_height)
        )

    layout_regions = [{
        "layout": context.declared_layout,
        "matched_layout": context.matched_layout,
        "rectangles": [list(rectangle) for rectangle in context.rectangles],
    }]
    definitions = (
        ("clipped-text", clipped_regions, "All lettering boxes remain inside their source panel bounds."),
        ("text-overlap", overlap_regions, "No lettering boxes overlap within any source panel."),
        ("reading-order", order_regions, "Every panel uses a contiguous one-based lettering reading order."),
        ("layout-border-integrity", [] if context.declared_layout == context.matched_layout else layout_regions,
         "Storyboard rectangles are contained, non-overlapping, and match the declared layout."),
        ("balloon-subject-obstruction", obstruction_regions,
         "Every balloon clears the protected speaker anchors authored in its panel."),
        ("bubble-tail-geometry", tail_regions,
         "Every dialogue tail attaches to its balloon and points at its authored speaker anchor inside the panel."),
    )
    checks = []
    for check_id, failures, evidence in definitions:
        checks.append({
            "id": check_id,
            "result": "fail" if failures else "pass",
            "severity": "error",
            "evidence": evidence if not failures else f"Deterministic geometry found {len(failures)} failure region(s) for {check_id}.",
            "method": "deterministic-geometry-v1",
            "reviewer": "comic-sol",
            "regions": failures,
        })
    checks.append(_crowding_check(crowding_regions))
    return checks


def _reviewer_checks(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return reviewer-supplied quality checks for a page."""
    checks = [dict(value) for value in values]
    categories = validate_quality_checks(checks, SUBJECTIVE_PAGE_CHECK_IDS)
    if categories:
        raise ValueError(", ".join(categories))
    return checks


def _validate_tail_evidence(context: PageContext, checks: Sequence[Mapping[str, object]]) -> None:
    """Require one current bounded-review region for every authored dialogue."""
    tail_check = next(
        (check for check in checks if check.get("id") == "bubble-tail-direction"),
        None,
    )
    if tail_check is None:
        raise ValueError("bubble-tail-evidence-mismatch: check is missing")
    regions = tail_check.get("regions")
    if not isinstance(regions, list):
        raise ValueError("bubble-tail-evidence-mismatch: regions must be an array")

    geometry_by_panel = {
        panel_id: geometry
        for panel_id, _, geometry in context.lettering
    }
    expected: dict[tuple[str, str], dict[str, object]] = {}
    for panel in context.panels:
        if not isinstance(panel.get("id"), str):
            raise ValueError("bubble-tail-evidence-mismatch: panel is invalid")
        panel_id = panel["id"]
        geometry = geometry_by_panel.get(panel_id)
        items = geometry.get("items") if isinstance(geometry, dict) else None
        if not isinstance(items, list):
            raise ValueError("bubble-tail-evidence-mismatch: lettering geometry is missing")
        placed = {
            item.get("id"): item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        text_items = panel.get("text")
        if not isinstance(text_items, list):
            raise ValueError("bubble-tail-evidence-mismatch: panel text is invalid")
        for item in text_items:
            if not isinstance(item, dict) or item.get("kind") != "dialogue":
                continue
            text_id = item.get("id")
            geometry_item = placed.get(text_id)
            tail = geometry_item.get("tail") if isinstance(geometry_item, dict) else None
            if not isinstance(text_id, str) or not isinstance(tail, dict):
                raise ValueError("bubble-tail-evidence-mismatch: dialogue tail is missing")
            expected[(panel_id, text_id)] = {
                "panel_id": panel_id,
                "text_id": text_id,
                "speaker": item.get("speaker"),
                "voice_source": item.get("voice_source"),
                "speaker_anchor": item.get("speaker_anchor"),
                "tip": tail.get("tip"),
            }

    observed: dict[tuple[str, str], Mapping[str, object]] = {}
    required_fields = {
        "panel_id", "text_id", "speaker", "voice_source",
        "speaker_anchor", "tip", "result",
    }
    for region in regions:
        if not isinstance(region, dict) or set(region) != required_fields:
            raise ValueError("bubble-tail-evidence-mismatch: region fields are invalid")
        panel_id = region.get("panel_id")
        text_id = region.get("text_id")
        if not isinstance(panel_id, str) or not isinstance(text_id, str):
            raise ValueError("bubble-tail-evidence-mismatch: region identity is invalid")
        key = (panel_id, text_id)
        if key in observed or region.get("result") not in {"pass", "fail"}:
            raise ValueError("bubble-tail-evidence-mismatch: region identity is invalid")
        observed[key] = region

    if set(observed) != set(expected):
        raise ValueError("bubble-tail-evidence-mismatch: dialogue coverage is incomplete")
    for key, expected_region in expected.items():
        region = observed[key]
        if any(region.get(field) != value for field, value in expected_region.items()):
            raise ValueError("bubble-tail-evidence-mismatch: region is stale")

    results = [region.get("result") for region in observed.values()]
    check_result = tail_check.get("result")
    is_warning = check_result == "warning" or tail_check.get("severity") == "warning"
    if (check_result == "pass" and any(result != "pass" for result in results)) or (
        check_result == "fail" and results and all(result == "pass" for result in results)
    ) or (is_warning and not any(result == "fail" for result in results)
    ):
        raise ValueError("bubble-tail-evidence-mismatch: check result is inconsistent")


def _valid_timestamp(value: object) -> bool:
    """Report whether a value is a valid UTC timestamp."""
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def build_page_quality_record(
    project_dir: Path,
    page_number: int,
    visual_checks: Sequence[Mapping[str, object]],
    *,
    reviewer: str,
    reviewed_at: str,
) -> dict[str, object]:
    """Build a current schema-2.0 page QA record without inventing visual evidence."""
    project_dir = Path(project_dir)
    context = _page_context(project_dir, page_number)
    deterministic = _deterministic_checks(context)
    subjective = _reviewer_checks(visual_checks)
    _validate_tail_evidence(context, subjective)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer must be non-empty")
    if not _valid_timestamp(reviewed_at):
        raise ValueError("reviewed_at must be an ISO 8601 UTC timestamp")
    checks_by_id = {check["id"]: check for check in deterministic + subjective}
    checks = [checks_by_id[check_id] for check_id in PAGE_CHECK_IDS]
    failures = [
        check for check in checks
        if check.get("result") == "fail" and check.get("severity") == "error"
    ]
    warnings = [
        check for check in checks
        if check.get("result") == "warning" or check.get("severity") == "warning"
    ]
    record: dict[str, object] = {
        "bindings": {
            "composition_cache_path": context.cache_relative,
            "composition_cache_sha256": sha256_file(context.cache_path),
            "layout_name": context.declared_layout,
            "layout_version": LAYOUT_VERSION,
            "lettering_sha256s": [f"{panel_id}:{digest}" for panel_id, digest, _ in context.lettering],
            "normalization_sha256s": [
                f"{panel_id}:{digest}" for panel_id, digest in context.normalization
            ],
            "page_height": context.page_height,
            "page_path": context.page_relative,
            "page_sha256": sha256_file(context.page_path),
            "page_width": context.page_width,
            "storyboard_path": "plan/storyboard.json",
            "storyboard_sha256": sha256_file(context.storyboard_path),
        },
        "checks": checks,
        "decision": "regenerate" if failures else "accept-warning" if warnings else "accept",
        "kind": "page-qa",
        "review": {
            "method": "deterministic-plus-bounded-visual-review",
            "reviewed_at": reviewed_at,
            "reviewer": reviewer,
        },
        "schema_version": "2.0",
        "subject_id": _page_id(page_number),
        "unresolved_warnings": [check["evidence"] for check in warnings],
    }
    categories = validate_quality_checks(checks, PAGE_CHECK_IDS)
    if categories:
        raise ValueError(", ".join(categories))
    return record


def write_page_quality_record(
    project_dir: Path, page_number: int, record: Mapping[str, object]
) -> Path:
    """Write an authoritative page-QA record."""
    destination = contained_project_path(
        Path(project_dir), f"qa/pages/{_page_id(page_number)}.json"
    )
    atomic_write_json(destination, dict(record))
    return destination


def validate_page_quality(project_dir: Path, page_number: int) -> tuple[PageQualityIssue, ...]:
    """Fail closed when page QA or any of its provenance bindings is stale."""
    project_dir = Path(project_dir)
    relative = f"qa/pages/{_page_id(page_number)}.json"
    issues: list[PageQualityIssue] = []

    def stale(field: str, detail: str) -> None:
        """Report whether a page-QA record is stale for current artifacts."""
        issues.append(PageQualityIssue(relative, field, f"page-quality-stale: {detail}"))

    try:
        path = contained_project_path(project_dir, relative, must_exist=True)
        record = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        stale("record.path", "page quality record is missing or unreadable")
        return tuple(issues)
    if not isinstance(record, dict):
        stale("record", "page quality record must be an object")
        return tuple(issues)
    unknown_fields = set(record) - PAGE_RECORD_FIELDS
    missing_fields = PAGE_RECORD_FIELDS - set(record)
    for field in sorted(unknown_fields):
        stale(field, "unknown page quality record field")
    for field in sorted(missing_fields):
        stale(field, "required page quality record field is missing")
    if record.get("schema_version") != "2.0" or record.get("kind") != "page-qa":
        stale("schema_version", "page quality record is not schema 2.0")
        return tuple(issues)
    if record.get("subject_id") != _page_id(page_number):
        stale("subject_id", "page subject does not match its path")
    categories = validate_quality_checks(record.get("checks"), PAGE_CHECK_IDS)
    for category in categories:
        stale("checks", category)
    checks = record.get("checks")
    if isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("id") in DETERMINISTIC_PAGE_CHECK_IDS
        and check.get("result") == "pass"
        and check.get("regions") != []
        for check in checks
    ):
        stale("checks", "deterministic passing checks must not include failure regions")
    # A deterministic check cannot report a defect it does not locate. Without
    # this, a warning-capable deterministic check could select accept-warning and
    # publish an unresolved warning with no geometry behind it.
    if isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("id") in DETERMINISTIC_PAGE_CHECK_IDS
        and check.get("result") in {"warning", "fail"}
        and check.get("regions") == []
        for check in checks
    ):
        stale("checks", "deterministic failing checks must include failure regions")

    review = record.get("review")
    if not isinstance(review, dict):
        stale("review", "page quality review is missing")
    else:
        if set(review) != PAGE_REVIEW_FIELDS:
            stale("review", "page quality review fields are invalid")
        if review.get("method") != "deterministic-plus-bounded-visual-review":
            stale("review.method", "page quality review method is invalid")
        if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
            stale("review.reviewer", "page quality reviewer is missing")
        if not _valid_timestamp(review.get("reviewed_at")):
            stale("review.reviewed_at", "page quality review timestamp is invalid")

    if isinstance(checks, list):
        failures = [
            check for check in checks if isinstance(check, dict)
            and check.get("result") == "fail" and check.get("severity") == "error"
        ]
        warnings = [
            check for check in checks if isinstance(check, dict)
            and (check.get("result") == "warning" or check.get("severity") == "warning")
        ]
        expected_decision = "regenerate" if failures else "accept-warning" if warnings else "accept"
        if record.get("decision") != expected_decision:
            stale("decision", "page quality decision does not match checks")
        expected_warnings = [check.get("evidence") for check in warnings]
        if record.get("unresolved_warnings") != expected_warnings:
            stale("unresolved_warnings", "page quality warnings do not match checks")

    bindings = record.get("bindings")
    if not isinstance(bindings, dict):
        stale("bindings", "page quality bindings are missing")
        return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))
    unexpected_bindings = set(bindings) - PAGE_BINDING_FIELDS
    missing_bindings = PAGE_BINDING_FIELDS - set(bindings)
    if unexpected_bindings:
        stale("bindings", "page quality bindings contain unknown fields")
    for field in sorted(missing_bindings):
        stale(f"bindings.{field}", "required page quality binding is missing")
    for field in ("composition_cache_path", "page_path", "storyboard_path"):
        if not isinstance(bindings.get(field), str):
            stale(f"bindings.{field}", "bound artifact path must be a string")
    for field in (
        "composition_cache_sha256", "page_sha256", "storyboard_sha256",
    ):
        if not isinstance(bindings.get(field), str) or SHA256_PATTERN.fullmatch(bindings[field]) is None:
            stale(f"bindings.{field}", "bound artifact hash must be a lowercase SHA-256")
    for field in ("page_width", "page_height"):
        value = bindings.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            stale(f"bindings.{field}", "bound page dimension must be a positive integer")
    if not isinstance(bindings.get("layout_name"), str) or not bindings["layout_name"]:
        stale("bindings.layout_name", "bound layout name must be non-empty")
    if not isinstance(bindings.get("layout_version"), str) or not bindings["layout_version"]:
        stale("bindings.layout_version", "bound layout version must be non-empty")

    # Verify byte bindings before parsing their semantic contents. Corrupt JSON
    # must still identify the exact stale artifact rather than collapse into one
    # generic provenance error.
    direct_artifacts = (
        ("page_sha256", bindings.get("page_path")),
        ("composition_cache_sha256", bindings.get("composition_cache_path")),
        ("storyboard_sha256", bindings.get("storyboard_path")),
    )
    for digest_field, relative_path in direct_artifacts:
        try:
            artifact = (
                contained_project_path(project_dir, relative_path, must_exist=True)
                if isinstance(relative_path, str)
                else None
            )
        except (OSError, ValueError):
            artifact = None
        if artifact is None or not artifact.is_file():
            stale(f"bindings.{digest_field}", "bound artifact is missing")
        elif bindings.get(digest_field) != sha256_file(artifact):
            stale(f"bindings.{digest_field}", "bound artifact hash does not match")

    def verify_per_panel_bindings(field: str, artifact: str, label: str) -> None:
        """Re-derive one ordered `panel-id:sha256` binding list from disk."""
        recorded = bindings.get(field)
        if not isinstance(recorded, list):
            stale(f"bindings.{field}", f"ordered {label} bindings are missing")
            return
        current: list[str] = []
        readable = True
        for binding in recorded:
            if not isinstance(binding, str) or ":" not in binding:
                readable = False
                break
            panel_id, _ = binding.split(":", 1)
            try:
                path = contained_project_path(
                    project_dir, f"panels/{panel_id}/{artifact}", must_exist=True
                )
            except (OSError, ValueError):
                readable = False
                break
            current.append(f"{panel_id}:{sha256_file(path)}")
        if not readable or current != recorded:
            stale(
                f"bindings.{field}",
                f"ordered {label} artifact hashes do not match",
            )

    verify_per_panel_bindings("lettering_sha256s", "lettering.json", "lettering")
    verify_per_panel_bindings(
        "normalization_sha256s", "normalization.json", "normalization"
    )
    try:
        context = _page_context(project_dir, page_number)
    except (OSError, ValueError, json.JSONDecodeError):
        if not issues:
            stale("bindings", "current page provenance is missing or unreadable")
        return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))

    if isinstance(checks, list):
        try:
            _validate_tail_evidence(context, checks)
        except ValueError as error:
            stale("checks", str(error))

    expected = {
        "composition_cache_path": context.cache_relative,
        "page_sha256": sha256_file(context.page_path),
        "page_width": context.page_width,
        "page_height": context.page_height,
        "page_path": context.page_relative,
        "composition_cache_sha256": sha256_file(context.cache_path),
        "storyboard_sha256": sha256_file(context.storyboard_path),
        "storyboard_path": "plan/storyboard.json",
        "layout_name": context.declared_layout,
        "layout_version": LAYOUT_VERSION,
        "lettering_sha256s": [
            f"{panel_id}:{digest}" for panel_id, digest, _ in context.lettering
        ],
        "normalization_sha256s": [
            f"{panel_id}:{digest}" for panel_id, digest in context.normalization
        ],
    }
    for field, current in expected.items():
        if bindings.get(field) != current:
            stale(f"bindings.{field}", "bound value does not match current artifacts")
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))
