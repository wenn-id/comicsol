#!/usr/bin/env python3
"""Schema-2.1 page QA records derived from composition and lettering evidence."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image

from .comic_sol import atomic_write_json, read_json
from .input_limits import MAX_JSON_BYTES, loads_bounded_json
from .core_primitives import (
    BALLOON_COVERAGE_WARNING_RATIO,
    TAIL_ATTACHMENT_TOLERANCE,
    balloon_outline_deviation,
    balloon_separation_minimum,
    balloon_subject_clearance,
    canonical_artifact_bytes,
    is_geometry_point,
    is_normalized_point,
    rectangle_overlap_area,
    rectangle_separation,
    subject_keepout_radius,
    tail_geometry_result,
)
from .layouts import LAYOUT_VERSION, match_layout, validate_custom_layout
from .project_io import (
    PROJECT_OPERATION_LOCK_TIMEOUT,
    ProjectLock,
    ProjectTransaction,
    contained_project_path,
    open_path_nofollow,
    read_bytes_nofollow,
)
from .quality_records import PAGE_CHECK_IDS, validate_quality_checks
from .schema import UnsupportedSchemaVersionError

# The page-QA record carries its own artifact-level version, independent from the
# `project.json` version owned by scripts/schema.py. It moved to 2.1 when the
# check tuple grew to ten entries and `bindings` gained `normalization_sha256s`,
# so a record written by the previous engine is reported as needing migration
# rather than as malformed.
CURRENT_PAGE_QA_SCHEMA_VERSION = "2.1"
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


def _read_and_digest(path: Path, *, max_bytes: int | None = None) -> tuple[bytes, str]:
    """Read one artifact and digest exactly the bytes that were read.

    Every digest this module derives comes from here, so the cost of one page
    validation is countable: a test can assert how many times each artifact was
    read instead of inspecting the call sites.
    """
    if max_bytes is None:
        payload = read_bytes_nofollow(Path(path))
    else:
        payload = read_bytes_nofollow(Path(path), max_bytes=max_bytes)
    return payload, hashlib.sha256(payload).hexdigest()


class _ArtifactSnapshots:
    """Read and digest each distinct artifact at most once per operation.

    Page validation reaches for the same artifact twice on purpose. The
    recorded-path pass digests whatever a record's own `bindings` point at, so it
    can tell a missing bound artifact from one whose bytes changed. The
    re-derived pass rebuilds every binding from the current storyboard, so it can
    also catch a binding that is well-formed and current but simply wrong. Both
    verdicts are needed; reading the bytes behind them twice is not.

    Memoizing on the fully resolved path is what keeps the two passes
    independent. Each still chooses its own path and its own comparison, so a
    record binding `pages/page-002.png` while the page is `page-001.png` digests
    both files and reports both verdicts. Only a second read of one identical
    path is elided.

    The payload is held alongside the digest because a caller that parses an
    artifact must parse the bytes that were digested. Handing back a digest from
    an earlier read and letting the caller reopen the file is exactly the split
    generation `_json_snapshot()` exists to prevent. One instance therefore holds
    one page's artifacts — including its raster, which `_page_context()` already
    reads whole — and is discarded when the operation returns.
    """

    __slots__ = ("_snapshots",)

    def __init__(self) -> None:
        self._snapshots: dict[Path, tuple[bytes, str]] = {}

    def snapshot(self, path: Path, *, max_bytes: int | None = None) -> tuple[bytes, str]:
        """Return one artifact's bytes with the digest of exactly those bytes."""
        key = Path(path)
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            snapshot = _read_and_digest(key, max_bytes=max_bytes)
            self._snapshots[key] = snapshot
        return snapshot

    def digest(self, path: Path) -> str:
        """Return one artifact's digest, reading it only if it is not held yet."""
        return self.snapshot(path)[1]


@dataclass(frozen=True)
class PageContext:
    """One pass over the artifacts that define a page's QA context.

    Every digest here is captured in the same read as the value it describes.
    With `ProjectLock` held, the collection therefore describes one serialized
    project generation. Without the lock, each value/digest pair is internally
    consistent but the cross-artifact view remains advisory because a writer may
    publish between reads. `_page_bindings()` never re-reads these artifacts.

    The reads flow through an `_ArtifactSnapshots` cache, so an artifact another
    pass of the same operation already read is not read again and the whole
    operation observes one set of bytes per file.
    """

    storyboard_path: Path
    storyboard_sha256: str
    panels: tuple[dict[str, object], ...]
    rectangles: tuple[tuple[int, int, int, int], ...]
    declared_layout: str
    matched_layout: str
    page_relative: str
    page_path: Path
    page_sha256: str
    page_width: int
    page_height: int
    cache_relative: str
    cache_path: Path
    cache_sha256: str
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


def _json_snapshot(
    path: Path, artifacts: _ArtifactSnapshots
) -> tuple[dict[str, object], str]:
    """Read one JSON artifact and digest the exact bytes that were parsed.

    A separate read for the value and the digest opens the file twice, so the
    digest can describe a generation the parsed value never came from. Parsing the
    single buffer that was digested is what makes the resulting binding provable,
    and `artifacts` supplies that buffer whether this is the first pass of the
    operation to ask for the file or a later one.

    The parse is repeated for each call rather than cached with the bytes, so no
    two callers share a mutable value.
    """
    payload, digest = artifacts.snapshot(Path(path), max_bytes=MAX_JSON_BYTES)
    value = loads_bounded_json(payload, source=Path(path).name)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value, digest


def _clean_raster(
    project_dir: Path, panel_id: str, artifacts: _ArtifactSnapshots
) -> tuple[tuple[int, int], str]:
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
    normalization, digest = _json_snapshot(path, artifacts)
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
    return (size[0], size[1]), digest


def _page_context(
    project_dir: Path, page_number: int, artifacts: _ArtifactSnapshots
) -> PageContext:
    """Collect one page's artifacts and build its deterministic check context.

    This is the single collection point used to derive page-QA bindings. Callers
    that need a cross-artifact view serialized against a concurrent compose or
    lettering run must hold `ProjectLock`. Without it, each parsed value and its
    digest still come from one read, but the collection may span writer generations.

    `artifacts` is required rather than defaulted so every caller reads through
    the same mechanism. Construction and migration pass a cache used by nothing
    else, which changes nothing for them; validation passes the one its
    recorded-path passes already populated, which is what removes the second read
    of every bound artifact.
    """
    storyboard_path = contained_project_path(project_dir, "plan/storyboard.json", must_exist=True)
    storyboard, storyboard_sha256 = _json_snapshot(storyboard_path, artifacts)
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
    # Decode the dimensions from the same bytes the digest covers. Hashing the
    # page separately from the decode lets a record bind one raster while the
    # pixel space its balloon verdicts were measured against came from another.
    page_payload, page_sha256 = artifacts.snapshot(page_path)
    with Image.open(io.BytesIO(page_payload)) as image:
        image.load()
        page_width, page_height = image.size

    cache_relative = "cache/composition.json"
    cache_path = contained_project_path(project_dir, cache_relative, must_exist=True)
    cache_sha256 = artifacts.digest(cache_path)
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
        try:
            geometry, geometry_digest = _json_snapshot(path, artifacts)
        except ValueError as error:
            raise ValueError(
                f"lettering geometry is invalid for panel {panel_id}"
            ) from error
        lettering.append((panel_id, geometry_digest, geometry))
        clean_size, normalization_digest = _clean_raster(
            project_dir, panel_id, artifacts
        )
        clean_sizes.append(clean_size)
        normalization.append((panel_id, normalization_digest))

    return PageContext(
        storyboard_path=storyboard_path,
        storyboard_sha256=storyboard_sha256,
        panels=typed_panels,
        rectangles=rectangles,
        declared_layout=declared_layout,
        matched_layout=matched_layout,
        page_relative=page_relative,
        page_path=page_path,
        page_sha256=page_sha256,
        page_width=page_width,
        page_height=page_height,
        cache_relative=cache_relative,
        cache_path=cache_path,
        cache_sha256=cache_sha256,
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


def _attached_to_balloon(attachment: object, box: object) -> bool:
    """Return whether a tail attachment sits on the outline of its balloon.

    Membership in the bounding box is not enough: the balloon is the inscribed
    ellipse, so an attachment resting at the centre is inside the box and still
    detached from anything drawn.
    """
    if not is_geometry_point(attachment) or not isinstance(box, Mapping):
        return False
    # This box comes straight from the retained placement rather than from the
    # bounds-checked list, so it is arbitrary JSON. A malformed rectangle raises
    # ValueError, and an absurdly large integer overflows the float conversion;
    # either way an unverifiable attachment is a failed check, never an exception
    # escaping page-QA construction.
    try:
        deviation = balloon_outline_deviation(box, attachment)  # type: ignore[arg-type]
    except (ValueError, TypeError, ArithmeticError):
        return False
    return deviation <= TAIL_ATTACHMENT_TOLERANCE


def _attribution_is_complete(attribution: object) -> bool:
    """Report whether a placement names the speaker it was lettered for.

    Attribution is the only field tying a drawn balloon back to a character, so a
    record that is absent or partial cannot be compared against the storyboard at
    all and is reported rather than assumed to agree.
    """
    if not isinstance(attribution, Mapping):
        return False
    return (
        all(
            isinstance(attribution.get(field), str) and attribution.get(field)
            for field in ("authored_speaker", "speaker")
        )
        and attribution.get("resolution") in {"declared", "inferred"}
        and is_geometry_point(attribution.get("speaker_anchor"))
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
        kind = placement.get("kind") if isinstance(placement, Mapping) else None
        attribution = placement.get("attribution") if isinstance(placement, Mapping) else None
        anchor = item.get("speaker_anchor")
        if not isinstance(tail, Mapping):
            reason = "missing-tail"
        elif not _attribution_is_complete(attribution):
            reason = "missing-attribution"
        elif any(
            attribution[field] != item.get("speaker")  # type: ignore[index]
            for field in ("authored_speaker", "speaker")
        ):
            # A balloon retained against a different character than the storyboard
            # gives it is the exact defect a swapped pair of speakers produces, and
            # neither balloon's geometry looks wrong on its own.
            #
            # Both retained fields must agree, not just one: the canonical
            # `speaker` is the identity every consumer reads, so accepting a
            # record whose authored echo still matched would let that field be
            # wrong silently. Requiring both is exact here because a validated
            # storyboard authors a character-bible ID, which is what the renderer
            # resolves and stores in both fields. An `inferred` record — which
            # only a storyboard that failed validation by authoring a display name
            # could produce — therefore fails closed rather than being trusted.
            reason = "speaker-mismatch"
        elif tail.get("speaker_anchor") != anchor:
            reason = "speaker-anchor-mismatch"
        elif tail.get("voice_source") != item.get("voice_source"):
            reason = "voice-source-mismatch"
        elif kind != "dialogue":
            # Only dialogue is drawn as a balloon with a tail, so there is no
            # outline to attach to and the shape cannot be verified.
            reason = "placement-kind-mismatch"
        elif not is_normalized_point(anchor):
            # Reported explicitly rather than as a direction failure: a tail can
            # be perfectly self-consistent while aiming at a voice source that is
            # not inside the panel at all.
            reason = "speaker-anchor-out-of-range"
        elif not _attached_to_balloon(tail.get("attachment"), box):
            reason = "detached-tail"
        elif tail_geometry_result(tail, anchor, width, height) != "pass":
            reason = "tail-does-not-point-at-speaker"
        elif attribution["speaker_anchor"] != tail.get("speaker_anchor"):  # type: ignore[index]
            # Checked last and against the tail rather than the storyboard: a
            # storyboard edit is already reported as an anchor mismatch, so what
            # is left here is attribution naming a different voice source than the
            # tail that was actually drawn.
            reason = "attribution-anchor-mismatch"
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
        # `clipped-text`, `text-overlap`, and `reading-order` judge every drawn box,
        # lettered SFX included: an effect can be cut off by the panel edge or
        # printed over a balloon exactly like any other lettering.
        #
        # The two balloon checks below are different. Both encode a rule about
        # *speech*: a balloon must not cover the mouth it speaks from, and a
        # cluster of balloons must not crowd the reading path. A sound effect is
        # deliberately placed over the action — that is what makes it read as a
        # sound — and its evidence would tell the reviewer to shorten dialogue
        # that is not the cause. Judging SFX by those rules would fail correct
        # pages, so they see balloons only.
        balloons = [entry for entry in boxes if entry[2] != "sfx"]
        obstruction_regions.extend(
            _subject_obstruction_regions(
                panel_id, anchors, balloons, panel_width, panel_height
            )
        )
        tail_regions.extend(
            _tail_geometry_regions(
                panel_id, panel, placements, panel_width, panel_height
            )
        )
        crowding_regions.extend(
            _crowding_regions(panel_id, balloons, panel_width, panel_height)
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


def _page_bindings(context: PageContext) -> dict[str, object]:
    """Project one page snapshot onto its twelve provenance bindings.

    Construction, migration, and validation all read their bound values from
    here, so a published record can never bind something the validator would not
    re-derive for the same project.

    This function performs no I/O. Every digest it returns was captured by
    `_page_context()` in the same pass that produced the values the deterministic
    checks were measured against, so a record cannot bind one generation of an
    artifact while reporting verdicts about another.
    """
    return {
        "composition_cache_path": context.cache_relative,
        "composition_cache_sha256": context.cache_sha256,
        "layout_name": context.declared_layout,
        "layout_version": LAYOUT_VERSION,
        "lettering_sha256s": [
            f"{panel_id}:{digest}" for panel_id, digest, _ in context.lettering
        ],
        "normalization_sha256s": [
            f"{panel_id}:{digest}" for panel_id, digest in context.normalization
        ],
        "page_height": context.page_height,
        "page_path": context.page_relative,
        "page_sha256": context.page_sha256,
        "page_width": context.page_width,
        "storyboard_path": "plan/storyboard.json",
        "storyboard_sha256": context.storyboard_sha256,
    }


def _compose_page_record(
    checks: Sequence[Mapping[str, object]],
    *,
    bindings: Mapping[str, object],
    review: Mapping[str, object],
    subject_id: str,
) -> dict[str, object]:
    """Assemble a current page-QA record, deriving its decision from its checks."""
    failures = [
        check for check in checks
        if check.get("result") == "fail" and check.get("severity") == "error"
    ]
    warnings = [
        check for check in checks
        if check.get("result") == "warning" or check.get("severity") == "warning"
    ]
    return {
        "bindings": dict(bindings),
        "checks": [dict(check) for check in checks],
        "decision": "regenerate" if failures else "accept-warning" if warnings else "accept",
        "kind": "page-qa",
        "review": dict(review),
        "schema_version": CURRENT_PAGE_QA_SCHEMA_VERSION,
        "subject_id": subject_id,
        "unresolved_warnings": [check["evidence"] for check in warnings],
    }


def build_page_quality_record(
    project_dir: Path,
    page_number: int,
    visual_checks: Sequence[Mapping[str, object]],
    *,
    reviewer: str,
    reviewed_at: str,
) -> dict[str, object]:
    """Build a current page QA record without inventing visual evidence.

    The derivation runs under `ProjectLock`, so no compose or lettering run can
    replace an artifact while the page is being measured. Use
    `publish_page_quality_record()` when the record is also being written: this
    function releases the lock before returning, so a separate
    `write_page_quality_record()` call publishes under a second acquisition and
    leaves a window in which the record can go stale before it lands.
    """
    project_dir = Path(project_dir)
    with ProjectLock(project_dir, timeout=PROJECT_OPERATION_LOCK_TIMEOUT):
        return _build_page_quality_record_locked(
            project_dir,
            page_number,
            visual_checks,
            reviewer=reviewer,
            reviewed_at=reviewed_at,
        )


def _build_page_quality_record_locked(
    project_dir: Path,
    page_number: int,
    visual_checks: Sequence[Mapping[str, object]],
    *,
    reviewer: str,
    reviewed_at: str,
) -> dict[str, object]:
    """Derive a page QA record from one snapshot, with the project lock held."""
    context = _page_context(project_dir, page_number, _ArtifactSnapshots())
    deterministic = _deterministic_checks(context)
    subjective = _reviewer_checks(visual_checks)
    _validate_tail_evidence(context, subjective)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("reviewer must be non-empty")
    if not _valid_timestamp(reviewed_at):
        raise ValueError("reviewed_at must be an ISO 8601 UTC timestamp")
    checks_by_id = {check["id"]: check for check in deterministic + subjective}
    checks = [checks_by_id[check_id] for check_id in PAGE_CHECK_IDS]
    record = _compose_page_record(
        checks,
        bindings=_page_bindings(context),
        review={
            "method": "deterministic-plus-bounded-visual-review",
            "reviewed_at": reviewed_at,
            "reviewer": reviewer,
        },
        subject_id=_page_id(page_number),
    )
    categories = validate_quality_checks(checks, PAGE_CHECK_IDS)
    if categories:
        raise ValueError(", ".join(categories))
    return record


def write_page_quality_record(
    project_dir: Path, page_number: int, record: Mapping[str, object]
) -> Path:
    """Write an authoritative page-QA record.

    Publication runs under `ProjectLock` so it is serialized against every other
    project operation. `durable_atomic_write()` alone makes the replacement atomic
    but takes no lock, so an unlocked publish could land a record between two
    steps of a concurrent compose run.
    """
    project_dir = Path(project_dir)
    with ProjectLock(project_dir, timeout=PROJECT_OPERATION_LOCK_TIMEOUT):
        return _write_page_quality_record_locked(project_dir, page_number, record)


def _write_page_quality_record_locked(
    project_dir: Path, page_number: int, record: Mapping[str, object]
) -> Path:
    """Publish a page-QA record with the project lock held."""
    destination = contained_project_path(
        project_dir, f"qa/pages/{_page_id(page_number)}.json"
    )
    atomic_write_json(destination, dict(record))
    return destination


def publish_page_quality_record(
    project_dir: Path,
    page_number: int,
    visual_checks: Sequence[Mapping[str, object]],
    *,
    reviewer: str,
    reviewed_at: str,
) -> Path:
    """Derive and publish one page-QA record under a single lock acquisition.

    This is the entry point for producing a record. Deriving and publishing in one
    critical section is what makes the published provenance authoritative: calling
    `build_page_quality_record()` and `write_page_quality_record()` separately
    releases the lock in between, so a compose or lettering run can replace an
    artifact after the record is measured but before it is published, landing a
    record that was already stale the moment it appeared.

    Returns the path the record was written to.
    """
    project_dir = Path(project_dir)
    with ProjectLock(project_dir, timeout=PROJECT_OPERATION_LOCK_TIMEOUT):
        record = _build_page_quality_record_locked(
            project_dir,
            page_number,
            visual_checks,
            reviewer=reviewer,
            reviewed_at=reviewed_at,
        )
        return _write_page_quality_record_locked(project_dir, page_number, record)


class PageQualityMigrationError(ValueError):
    """Raised when a page-QA record cannot be migrated from current evidence."""


@dataclass(frozen=True)
class PageQualityRebase:
    """Current, engine-derived facts a page-QA migration may rebuild on.

    A migration hook never derives evidence itself. It receives the deterministic
    checks and provenance bindings the current engine computes for the page on
    disk, plus whether the record's bound page raster is still that page, and
    decides only what the old record may carry across.
    """

    deterministic_checks: tuple[Mapping[str, object], ...]
    bindings: Mapping[str, object]
    page_unchanged: bool
    subject_id: str


PageQualityMigration = Callable[
    [Mapping[str, object], PageQualityRebase], dict[str, object]
]
# The ordered check tuple a schema-2.0 record was written against, before CS-023
# added the three deterministic balloon checks. A migration verifies its input
# against the shape of its own source version, so a record that was already
# malformed for that version is refused rather than normalized into a current one.
PAGE_QA_2_0_CHECK_IDS = (
    "clipped-text",
    "text-overlap",
    "face-action-obstruction",
    "bubble-tail-direction",
    "reading-order",
    "accidental-text-watermark",
    "layout-border-integrity",
)


def _carried_reviewer_checks(record: Mapping[str, object]) -> list[dict[str, object]]:
    """Return a record's reviewer-supplied checks in canonical order."""
    checks = record.get("checks")
    if not isinstance(checks, list):
        raise PageQualityMigrationError("page QA checks must be an array")
    present = [
        check for check in checks
        if isinstance(check, dict) and check.get("id") in SUBJECTIVE_PAGE_CHECK_IDS
    ]
    carried = {check["id"]: dict(check) for check in present}
    # Building the mapping would otherwise keep the last of a repeated ID and
    # silently normalize a non-canonical tuple into a valid record.
    if len(carried) != len(present):
        raise PageQualityMigrationError("page QA record repeats a reviewer check")
    missing = [
        check_id for check_id in SUBJECTIVE_PAGE_CHECK_IDS if check_id not in carried
    ]
    if missing:
        raise PageQualityMigrationError(
            f"page QA record is missing reviewer checks: {', '.join(missing)}"
        )
    try:
        # Reuse the reviewer-check validator so a carried check must clear exactly
        # the bar a freshly supplied one does.
        return _reviewer_checks([carried[check_id] for check_id in SUBJECTIVE_PAGE_CHECK_IDS])
    except ValueError as error:
        raise PageQualityMigrationError(str(error)) from error


def _migrate_page_qa_2_0_to_2_1(
    record: Mapping[str, object], rebase: PageQualityRebase
) -> dict[str, object]:
    """Carry a schema-2.0 page-QA record onto the current ten-check set.

    Every deterministic check and every binding is re-derived from current
    artifacts, never copied and never invented. The three reviewer-supplied
    checks are carried across only while the record's bound `page_sha256` still
    matches the page on disk, because that digest is the evidence the reviewer
    inspected these pixels. A page that changed makes the record genuinely stale
    and demands a fresh review instead.
    """
    if not rebase.page_unchanged:
        raise PageQualityMigrationError(
            "page-quality-stale: bindings.page_sha256 bound artifact hash does not "
            "match, so the recorded review is not evidence for the page on disk"
        )
    review = record.get("review")
    if not isinstance(review, dict):
        raise PageQualityMigrationError("page QA review is missing")
    # Refuse a record that is not a well-formed record of the version it claims.
    # Migrating one would launder a malformed artifact into a valid current one.
    categories = validate_quality_checks(record.get("checks"), PAGE_QA_2_0_CHECK_IDS)
    if categories:
        raise PageQualityMigrationError(
            f"page QA record is not a valid schema-2.0 record: {', '.join(categories)}"
        )
    carried = _carried_reviewer_checks(record)
    checks_by_id = {
        check["id"]: check for check in [*rebase.deterministic_checks, *carried]
    }
    return _compose_page_record(
        [checks_by_id[check_id] for check_id in PAGE_CHECK_IDS],
        bindings=rebase.bindings,
        review=review,
        subject_id=rebase.subject_id,
    )


# Page-QA migrations are explicit and keyed by `(source_version, target_version)`,
# mirroring PROJECT_MIGRATIONS in scripts/schema.py. A record whose version has no
# registered hook fails closed rather than being widened into support.
PAGE_QA_MIGRATIONS: dict[tuple[str, str], PageQualityMigration] = {
    ("2.0", CURRENT_PAGE_QA_SCHEMA_VERSION): _migrate_page_qa_2_0_to_2_1,
}
PAGE_QA_MIGRATION_SOURCES = frozenset(
    source
    for source, target in PAGE_QA_MIGRATIONS
    if target == CURRENT_PAGE_QA_SCHEMA_VERSION
)


def _require_publishable(context: PageContext, record: Mapping[str, object]) -> None:
    """Refuse a migrated record the current validator would report as stale."""
    if set(record) != PAGE_RECORD_FIELDS:
        raise PageQualityMigrationError("migrated page QA record fields are invalid")
    bindings = record.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != PAGE_BINDING_FIELDS:
        raise PageQualityMigrationError("migrated page QA bindings are invalid")
    review = record.get("review")
    if (
        not isinstance(review, dict)
        or set(review) != PAGE_REVIEW_FIELDS
        or review.get("method") != "deterministic-plus-bounded-visual-review"
        or not isinstance(review.get("reviewer"), str)
        or not review["reviewer"].strip()
        or not _valid_timestamp(review.get("reviewed_at"))
    ):
        raise PageQualityMigrationError("migrated page QA review is invalid")
    categories = validate_quality_checks(record.get("checks"), PAGE_CHECK_IDS)
    if categories:
        raise PageQualityMigrationError(", ".join(categories))
    try:
        _validate_tail_evidence(context, record["checks"])
    except ValueError as error:
        raise PageQualityMigrationError(str(error)) from error


def _read_migratable_page_record(project_dir: Path, relative: str, subject_id: str) -> dict:
    """Read a page-QA record and reject one no migration could ever republish."""
    record = read_json(contained_project_path(project_dir, relative, must_exist=True))
    if not isinstance(record, dict):
        raise PageQualityMigrationError("page QA record must contain a JSON object")
    if record.get("kind") != "page-qa":
        raise PageQualityMigrationError("page QA record kind is not page-qa")
    if record.get("subject_id") != subject_id:
        raise PageQualityMigrationError("page QA subject does not match its path")
    return record


def migrate_page_quality_record(project_dir: Path, page_number: int) -> dict[str, object]:
    """Run a registered page-QA migration transactionally, or fail without mutation.

    A record already at the current version is returned without opening a
    transaction, so reading one never leaves a lock file or journal directory
    behind. Otherwise the record is re-read, rebased on current artifacts, and
    republished inside one `ProjectTransaction`, so a refused or interrupted
    migration leaves the project byte-for-byte unchanged.
    """
    project_dir = Path(project_dir)
    subject_id = _page_id(page_number)
    relative = f"qa/pages/{subject_id}.json"
    current = _read_migratable_page_record(project_dir, relative, subject_id)
    if current.get("schema_version") == CURRENT_PAGE_QA_SCHEMA_VERSION:
        return current
    with ProjectTransaction(project_dir, "page-qa-migration") as transaction:
        # The check above is advisory and unlocked. The record on disk now, under
        # the lock, is the only one that may be rebased and republished.
        record = _read_migratable_page_record(project_dir, relative, subject_id)
        source_version = record.get("schema_version")
        if source_version == CURRENT_PAGE_QA_SCHEMA_VERSION:
            return record
        migration = PAGE_QA_MIGRATIONS.get(
            (str(source_version), CURRENT_PAGE_QA_SCHEMA_VERSION)
        )
        if migration is None:
            raise UnsupportedSchemaVersionError(source_version, artifact="page QA")
        context = _page_context(project_dir, page_number, _ArtifactSnapshots())
        bindings = _page_bindings(context)
        recorded_bindings = record.get("bindings")
        recorded_page_digest = (
            recorded_bindings.get("page_sha256")
            if isinstance(recorded_bindings, dict)
            else None
        )
        migrated = migration(
            record,
            PageQualityRebase(
                deterministic_checks=tuple(_deterministic_checks(context)),
                bindings=bindings,
                page_unchanged=recorded_page_digest == bindings["page_sha256"],
                subject_id=subject_id,
            ),
        )
        if not isinstance(migrated, dict):
            raise PageQualityMigrationError(
                "page QA migration must return a JSON object"
            )
        migrated["schema_version"] = CURRENT_PAGE_QA_SCHEMA_VERSION
        _require_publishable(context, migrated)
        transaction.stage_bytes(relative, canonical_artifact_bytes(migrated))
        return migrated


def validate_page_quality(project_dir: Path, page_number: int) -> tuple[PageQualityIssue, ...]:
    """Fail closed when page QA or any of its provenance bindings is stale.

    This validator deliberately does not acquire `ProjectLock`. It is a read-only
    diagnostic, and taking the exclusive write lock would make it block on — and
    block — the very compose or lettering run it exists to report on. The contract
    is therefore advisory: run concurrently with a writer, it may report a binding
    as stale that the writer was mid-way through making current again, and the
    remedy is to re-run it once the project is quiet. That is a deliberate choice,
    not an oversight.

    The finalization gate does not rely on that advisory contract:
    `finalize_project()` reaches this function from inside `ProjectLock`. A caller
    that already holds the lock likewise gives the reads below one serialized
    view, and the lock's per-thread reentrancy means the page-QA helpers can be
    used safely in that critical section. The standalone `validate_project.py`
    command remains deliberately advisory and should be re-run after concurrent
    project work finishes.

    `_page_context()` pairs each semantic value with a digest from the same read,
    and `_page_bindings()` performs no further I/O. Without an outer lock, the
    complete set may still span writer generations; the advisory re-run contract
    above applies to that cross-artifact view as well.

    Each distinct artifact is read and digested once per call. The two passes that
    need those bytes stay separate and keep reporting separately: the
    recorded-path passes below distinguish a bound artifact that is missing from
    one whose bytes changed, and the final comparison against `_page_bindings()`
    reports a binding that is well-formed and current but wrong. Sharing the reads
    also narrows the advisory window above, because the passes can no longer
    observe two generations of one file within a single call.
    """
    project_dir = Path(project_dir)
    relative = f"qa/pages/{_page_id(page_number)}.json"
    issues: list[PageQualityIssue] = []
    # The recorded-path passes below and the re-derived pass at the end both need
    # the bytes of every bound artifact, and the page raster is expensive. Sharing
    # one cache reads and digests each file once for the whole validation without
    # merging the two verdicts: each pass still resolves its own paths and makes
    # its own comparison.
    artifacts = _ArtifactSnapshots()

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
    if (
        record.get("schema_version") != CURRENT_PAGE_QA_SCHEMA_VERSION
        or record.get("kind") != "page-qa"
    ):
        stale(
            "schema_version",
            f"page quality record is not schema {CURRENT_PAGE_QA_SCHEMA_VERSION}",
        )
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
        elif bindings.get(digest_field) != artifacts.digest(artifact):
            stale(f"bindings.{digest_field}", "bound artifact hash does not match")

    def verify_per_panel_bindings(field: str, artifact: str, label: str) -> None:
        """Re-derive one ordered `panel-id:sha256` binding list from disk.

        This walks the panel IDs the record names, while `_page_context()` walks
        the panels the storyboard declares. The two traversals differ on purpose,
        so a record naming the wrong panel set is caught by the difference. They
        share the digest cache, not the traversal.
        """
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
            current.append(f"{panel_id}:{artifacts.digest(path)}")
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
        context = _page_context(project_dir, page_number, artifacts)
    except (OSError, ValueError, json.JSONDecodeError):
        if not issues:
            stale("bindings", "current page provenance is missing or unreadable")
        return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))

    if isinstance(checks, list):
        try:
            _validate_tail_evidence(context, checks)
        except ValueError as error:
            stale("checks", str(error))

    for field, current in _page_bindings(context).items():
        if bindings.get(field) != current:
            stale(f"bindings.{field}", "bound value does not match current artifacts")
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))
