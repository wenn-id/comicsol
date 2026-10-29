"""Small, dependency-free primitives shared across Comic Sol modules."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import TypeAlias, TypeGuard, cast


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
    "balloon-subject-obstruction",
    "bubble-tail-geometry",
    "balloon-crowding",
)

# Balloon placement policy shared by the renderer and deterministic page QA.
#
# A balloon keeps at least `subject_keepout_radius()` away from a protected
# subject anchor. This is the same gap the renderer reserves between a tail tip
# and its voice source, which makes the audit exact for one pair: a dialogue
# balloon against the anchor it speaks from can never fail a check the renderer
# already satisfied.
#
# It is not a guarantee for the whole panel. Placement resolves a box from its
# anchor keyword alone and consults no other line's `speaker_anchor`, so the
# renderer can legally emit a caption, or a second balloon, sitting on someone
# else's face. Catching that is the point of the check, and resolving it is an
# authoring change — a shorter line, a different anchor — exactly like the
# existing `clipped-text` and `text-overlap` failures.
SUBJECT_KEEPOUT_MINIMUM = 8.0
SUBJECT_KEEPOUT_MAXIMUM = 24.0
SUBJECT_KEEPOUT_RATIO = 0.025
# Crowding degrades reading comfort rather than correctness, so it is reported as
# an actionable warning instead of a hard geometry failure.
BALLOON_COVERAGE_WARNING_RATIO = 0.30
BALLOON_SEPARATION_MINIMUM = 8.0
BALLOON_SEPARATION_RATIO = 0.02
# Cosine alignment a tail must hold toward its authored speaker anchor.
TAIL_ALIGNMENT_MINIMUM = 0.999
# `source_gap` is the one number carrying the renderer's clearance promise, so it
# is recomputed from the retained tip. Geometry points are published rounded to
# four decimals, so a pixel of slack is far more than rounding needs.
TAIL_SOURCE_GAP_TOLERANCE = 1.0
# The renderer resolves every attachment exactly onto the balloon outline, so the
# only slack needed here is coordinate rounding, which costs well under a
# thousandth of a pixel. Half a pixel is generous and still rejects an
# attachment that has drifted into the balloon body.
TAIL_ATTACHMENT_TOLERANCE = 0.5

# Speaker attribution policy for panels that letter more than one balloon.
#
# A `speaker_anchor` names the visible mouth or audio device a balloon speaks
# from, so it is the only machine-readable evidence tying a balloon to a
# character. Two rules make that evidence decide attribution instead of merely
# describing it, and both are measured in normalized panel space so the renderer
# and the storyboard validator apply exactly the same thresholds.
#
# Anchors closer than this read as one voice source, so two different speakers
# claiming them cannot be told apart by anything drawn in the panel.
DISTINCT_SPEAKER_ANCHOR_SEPARATION = 0.04
# One character occupies one place in a panel. Anchors farther apart than this
# claim one speaker is talking from two positions, so the balloons cannot both
# be attributed to them.
SAME_SPEAKER_ANCHOR_SPREAD = 0.25
ATTRIBUTION_CONFLICT_REASONS = ("shared-anchor", "split-anchor")

Rectangle: TypeAlias = Mapping[str, int] | Sequence[int]
Point: TypeAlias = Sequence[float]


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
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


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


def rectangle_overlap_area(first: Rectangle, second: Rectangle) -> int:
    """Return the shared area of two rectangles, zero when they do not overlap."""
    ax, ay, aw, ah = _rectangle_values(first)
    bx, by, bw, bh = _rectangle_values(second)
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    if width <= 0 or height <= 0:
        return 0
    return width * height


def _cubic_bezier_points(
    start: Point, first_control: Point, second_control: Point, end: Point
) -> tuple[tuple[float, float], ...]:
    points = (start, first_control, second_control, end)
    if not all(is_geometry_point(point) for point in points):
        raise ValueError("cubic curve points must contain finite coordinates")
    start_x, start_y = float(start[0]), float(start[1])
    first_x, first_y = float(first_control[0]), float(first_control[1])
    second_x, second_y = float(second_control[0]), float(second_control[1])
    end_x, end_y = float(end[0]), float(end[1])
    result = []
    for index in range(25):
        t = index / 24
        inverse = 1 - t
        result.append(
            (
                inverse**3 * start_x
                + 3 * inverse**2 * t * first_x
                + 3 * inverse * t**2 * second_x
                + t**3 * end_x,
                inverse**3 * start_y
                + 3 * inverse**2 * t * first_y
                + 3 * inverse * t**2 * second_y
                + t**3 * end_y,
            )
        )
    return tuple(result)


def _tail_polygon(tail: Mapping[str, object]) -> tuple[tuple[float, float], ...] | None:
    base = tail.get("base")
    control = tail.get("control")
    tip = tail.get("tip")
    if (
        not isinstance(base, (list, tuple))
        or len(base) != 2
        or any(not is_geometry_point(point) for point in base)
        or not isinstance(control, (list, tuple))
        or len(control) != 2
        or not is_geometry_point(tip)
    ):
        return None
    control_points = []
    for side in control:
        if (
            not isinstance(side, (list, tuple))
            or len(side) != 2
            or any(not is_geometry_point(point) for point in side)
        ):
            return None
        control_points.append((cast(Point, side[0]), cast(Point, side[1])))
    first_base, second_base = cast(Point, base[0]), cast(Point, base[1])
    tip_point = cast(Point, tip)
    first_side = _cubic_bezier_points(
        first_base, control_points[0][0], control_points[0][1], tip_point
    )
    second_side = _cubic_bezier_points(
        second_base, control_points[1][0], control_points[1][1], tip_point
    )
    return first_side + tuple(reversed(second_side))


def _polygon_contains_origin(polygon: Sequence[Point]) -> bool:
    inside = False
    previous_x, previous_y = polygon[-1]
    for current_x, current_y in polygon:
        if (previous_y > 0) != (current_y > 0):
            crossing_x = previous_x + (current_x - previous_x) * (
                -previous_y / (current_y - previous_y)
            )
            if 0 < crossing_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def _segment_intersects_unit_circle(first: Point, second: Point) -> bool:
    delta_x = second[0] - first[0]
    delta_y = second[1] - first[1]
    length_squared = delta_x**2 + delta_y**2
    fraction = (
        0.0
        if length_squared == 0
        else max(
            0.0,
            min(1.0, -(first[0] * delta_x + first[1] * delta_y) / length_squared),
        )
    )
    nearest_x = first[0] + fraction * delta_x
    nearest_y = first[1] + fraction * delta_y
    return nearest_x**2 + nearest_y**2 <= 1.0


def _polygon_intersects_ellipse(polygon: Sequence[Point], rectangle: Rectangle) -> bool:
    if len(polygon) < 3 or any(not is_geometry_point(point) for point in polygon):
        return False
    try:
        x, y, width, height = _rectangle_values(rectangle)
    except ValueError:
        return False
    if width <= 0 or height <= 0:
        return False
    center_x, center_y = x + width / 2, y + height / 2
    radius_x, radius_y = max(0.5, width / 2), max(0.5, height / 2)
    normalized = tuple(
        ((float(point[0]) - center_x) / radius_x, (float(point[1]) - center_y) / radius_y)
        for point in polygon
    )
    if (
        max(point[0] for point in normalized) < -1.0
        or min(point[0] for point in normalized) > 1.0
        or max(point[1] for point in normalized) < -1.0
        or min(point[1] for point in normalized) > 1.0
    ):
        return False
    if any(point[0] ** 2 + point[1] ** 2 <= 1.0 for point in normalized):
        return True

    if _polygon_contains_origin(normalized):
        return True

    return any(
        _segment_intersects_unit_circle(first, second)
        for first, second in zip(normalized, (*normalized[1:], normalized[0]), strict=True)
    )


def _tail_intersects_ellipse(tail: object, rectangle: Rectangle) -> bool:
    if not isinstance(tail, Mapping):
        return False
    polygon = _tail_polygon(tail)
    return polygon is not None and _polygon_intersects_ellipse(polygon, rectangle)


def rectangle_separation(first: Rectangle, second: Rectangle) -> float:
    """Return the shortest gap between two rectangles, zero when they touch."""
    ax, ay, aw, ah = _rectangle_values(first)
    bx, by, bw, bh = _rectangle_values(second)
    horizontal = max(bx - (ax + aw), ax - (bx + bw), 0)
    vertical = max(by - (ay + ah), ay - (by + bh), 0)
    return math.hypot(horizontal, vertical)


def is_geometry_point(value: object) -> TypeGuard[Sequence[int | float]]:
    """Report whether a value is a finite two-dimensional geometry point."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def is_normalized_point(value: object) -> bool:
    """Report whether a point's coordinates are normalized into `[0, 1]`.

    A `speaker_anchor` addresses a location inside its own panel, so a coordinate
    outside this range names a voice source that is not in the artwork at all.
    """
    return is_geometry_point(value) and all(0.0 <= float(item) <= 1.0 for item in value)


def dialogue_attribution_conflicts(
    attributions: Sequence[tuple[str, str, object]],
) -> list[tuple[str, str, str]]:
    """Return speaker-anchor pairings that make one panel's attribution ambiguous.

    `attributions` holds one `(text_id, speaker, speaker_anchor)` triple per
    spoken balloon in a single panel, where `speaker_anchor` is a normalized
    point. Each returned triple is `(reason, first_text_id, second_text_id)` with
    the two IDs ordered so the report does not depend on authoring order.

    A balloon whose anchor is unusable is not compared: an absent or out-of-range
    anchor is already a hard defect reported against that item, and pairing it
    here would only duplicate the failure under a less specific reason.
    """
    usable = [
        (text_id, speaker, (float(anchor[0]), float(anchor[1])))  # type: ignore[index]
        for text_id, speaker, anchor in attributions
        if is_normalized_point(anchor)
    ]
    conflicts: list[tuple[str, str, str]] = []
    for index, (first_id, first_speaker, first_anchor) in enumerate(usable):
        for second_id, second_speaker, second_anchor in usable[index + 1 :]:
            distance = math.dist(first_anchor, second_anchor)
            if first_speaker != second_speaker:
                if distance < DISTINCT_SPEAKER_ANCHOR_SEPARATION:
                    reason = "shared-anchor"
                else:
                    continue
            elif distance > SAME_SPEAKER_ANCHOR_SPREAD:
                reason = "split-anchor"
            else:
                continue
            pair = (first_id, second_id) if first_id <= second_id else (second_id, first_id)
            conflicts.append((reason, *pair))
    return sorted(set(conflicts))


def subject_keepout_radius(width: int, height: int) -> float:
    """Return the clearance a balloon keeps from a protected subject anchor."""
    shortest = min(float(width), float(height))
    return max(
        SUBJECT_KEEPOUT_MINIMUM,
        min(SUBJECT_KEEPOUT_MAXIMUM, shortest * SUBJECT_KEEPOUT_RATIO),
    )


def balloon_separation_minimum(width: int, height: int) -> float:
    """Return the gap below which two balloons read as one crowded cluster."""
    shortest = min(float(width), float(height))
    return max(BALLOON_SEPARATION_MINIMUM, shortest * BALLOON_SEPARATION_RATIO)


def balloon_subject_clearance(box: Rectangle, point: Point, *, ellipse: bool) -> float:
    """Return the distance from a protected point to one balloon outline.

    Captions are drawn as their box and are measured exactly. Dialogue balloons
    are drawn as the ellipse inscribed in their box and are measured *radially*,
    along the ray from the balloon centre, which is precisely how
    `letter_panels._organic_tail_geometry` resolves a tail attachment.

    Radial distance is an upper bound on the true distance to the ellipse, and
    the two diverge as a balloon gets more eccentric, so this measure is
    deliberately permissive for wide balloons. That is a trade, not an
    oversight: because the renderer reserves `subject_keepout_radius()` using
    this same radial quantity, measuring the same way is what lets a balloon
    this engine drew keep clearance from the anchor it speaks from by
    construction. A tighter measure would reject renders the renderer considers
    valid. A point inside the ellipse always reports zero.
    """
    x, y, width, height = _rectangle_values(box)
    if not is_geometry_point(point):
        raise ValueError("point must contain two finite coordinates")
    point_x, point_y = (float(value) for value in point)
    if not ellipse:
        horizontal = max(x - point_x, 0.0, point_x - (x + width))
        vertical = max(y - point_y, 0.0, point_y - (y + height))
        return math.hypot(horizontal, vertical)
    center_x = x + width / 2
    center_y = y + height / 2
    radius_x = max(0.5, width / 2)
    radius_y = max(0.5, height / 2)
    delta_x = point_x - center_x
    delta_y = point_y - center_y
    normalized_distance = math.sqrt((delta_x / radius_x) ** 2 + (delta_y / radius_y) ** 2)
    if normalized_distance <= 1.0:
        return 0.0
    return math.hypot(delta_x, delta_y) * (1.0 - 1.0 / normalized_distance)


def balloon_outline_deviation(box: Rectangle, point: Point) -> float:
    """Return the radial distance from a point to an inscribed ellipse outline.

    Zero means the point lies on the outline. `letter_panels` resolves every tail
    attachment onto this outline exactly, so an attachment that has drifted off
    it — to the balloon centre, for instance — is not attached to the balloon it
    claims to speak from, even when it still sits inside the bounding box.
    """
    x, y, width, height = _rectangle_values(box)
    if not is_geometry_point(point):
        raise ValueError("point must contain two finite coordinates")
    point_x, point_y = (float(value) for value in point)
    center_x = x + width / 2
    center_y = y + height / 2
    radius_x = max(0.5, width / 2)
    radius_y = max(0.5, height / 2)
    delta_x = point_x - center_x
    delta_y = point_y - center_y
    normalized_distance = math.sqrt((delta_x / radius_x) ** 2 + (delta_y / radius_y) ** 2)
    if normalized_distance == 0.0:
        # The point is the centre, whose nearest outline is one semi-axis away.
        return min(radius_x, radius_y)
    distance = math.hypot(delta_x, delta_y)
    return abs(distance - distance / normalized_distance)


def tail_geometry_result(
    tail: Mapping[str, object], speaker_anchor: object, width: int, height: int
) -> str:
    """Verify one tail attaches to its balloon and points at the authored speaker."""
    attachment = tail.get("attachment")
    tip = tail.get("tip")
    gap = tail.get("source_gap")
    # Every coordinate is validated before any arithmetic: a corrupt tail is a
    # failed check, never an exception escaping the QA record builder. The anchor
    # must also be normalized into the panel, matching the renderer's contract —
    # a self-consistent tail aimed at an off-panel target is still wrong.
    if (
        not isinstance(speaker_anchor, list)
        or not is_geometry_point(speaker_anchor)
        or not is_normalized_point(speaker_anchor)
        or not is_geometry_point(attachment)
        or not is_geometry_point(tip)
        or not isinstance(gap, (int, float))
        or isinstance(gap, bool)
        or not math.isfinite(float(gap))
        or gap <= 0
    ):
        return "fail"
    target = (
        round(float(speaker_anchor[0]) * width),
        round(float(speaker_anchor[1]) * height),
    )
    tail_x = tip[0] - attachment[0]  # type: ignore[index]
    tail_y = tip[1] - attachment[1]  # type: ignore[index]
    target_x = target[0] - attachment[0]  # type: ignore[index]
    target_y = target[1] - attachment[1]  # type: ignore[index]
    tail_length = math.hypot(tail_x, tail_y)
    target_length = math.hypot(target_x, target_y)
    if tail_length <= 0 or target_length <= 0 or tail_length >= target_length:
        return "fail"
    alignment = (tail_x * target_x + tail_y * target_y) / (tail_length * target_length)
    if alignment < TAIL_ALIGNMENT_MINIMUM:
        return "fail"
    if not (0 <= tip[0] <= width and 0 <= tip[1] <= height):  # type: ignore[index]
        return "fail"
    # A tail may not claim a clearance it does not hold: the recorded gap must be
    # the distance actually left between the tip and the authored voice source.
    observed_gap = math.hypot(target[0] - tip[0], target[1] - tip[1])  # type: ignore[index]
    if abs(observed_gap - float(gap)) > TAIL_SOURCE_GAP_TOLERANCE:
        return "fail"
    return "pass"
