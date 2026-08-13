#!/usr/bin/env python3
"""Schema-2.0 page QA records derived from composition and lettering evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image

from comic_sol import atomic_write_json, read_json, sha256_file
from layouts import LAYOUT_VERSION, match_layout, validate_custom_layout
from project_io import contained_project_path, open_path_nofollow
from quality_records import PAGE_CHECK_IDS, validate_quality_checks

DETERMINISTIC_PAGE_CHECK_IDS = frozenset({
    "clipped-text",
    "text-overlap",
    "reading-order",
    "layout-border-integrity",
})
SUBJECTIVE_PAGE_CHECK_IDS = frozenset(PAGE_CHECK_IDS) - DETERMINISTIC_PAGE_CHECK_IDS


@dataclass(frozen=True)
class PageQualityIssue:
    path: str
    field: str
    message: str


def _page_id(page_number: int) -> str:
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page number must be a positive integer")
    return f"page-{page_number:03d}"


def _storyboard_page(storyboard: Mapping[str, object], page_number: int) -> dict[str, object]:
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ValueError("storyboard pages must be an array")
    matches = [page for page in pages if isinstance(page, dict) and page.get("number") == page_number]
    if len(matches) != 1:
        raise ValueError(f"storyboard page {page_number} was not found exactly once")
    return matches[0]


def _rect_tuple(panel: Mapping[str, object]) -> tuple[int, int, int, int]:
    rect = panel.get("rect")
    if not isinstance(rect, dict):
        raise ValueError("storyboard panel rectangle is missing")
    values = tuple(rect.get(key) for key in ("x", "y", "width", "height"))
    if len(values) != 4 or any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("storyboard panel rectangle must contain integers")
    return values  # type: ignore[return-value]


def _overlap(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    ax, ay, aw, ah = (first.get(key) for key in ("x", "y", "width", "height"))
    bx, by, bw, bh = (second.get(key) for key in ("x", "y", "width", "height"))
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (ax, ay, aw, ah, bx, by, bw, bh)):
        return True
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)  # type: ignore[operator]


def _page_context(project_dir: Path, page_number: int) -> dict[str, object]:
    storyboard_path = contained_project_path(project_dir, "plan/storyboard.json", must_exist=True)
    storyboard = read_json(storyboard_path)
    page = _storyboard_page(storyboard, page_number)
    panels = page.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValueError("storyboard page panels must be a non-empty array")
    rectangles = tuple(_rect_tuple(panel) for panel in panels if isinstance(panel, dict))
    if len(rectangles) != len(panels):
        raise ValueError("storyboard page contains an invalid panel")
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
    for panel in panels:
        panel_id = panel.get("id") if isinstance(panel, dict) else None
        if not isinstance(panel_id, str):
            raise ValueError("storyboard panel ID is invalid")
        relative = f"panels/{panel_id}/lettering.json"
        path = contained_project_path(project_dir, relative, must_exist=True)
        geometry = read_json(path)
        lettering.append((panel_id, sha256_file(path), geometry))

    return {
        "storyboard": storyboard,
        "storyboard_path": storyboard_path,
        "page": page,
        "panels": panels,
        "rectangles": rectangles,
        "declared_layout": declared_layout,
        "matched_layout": matched_layout,
        "page_relative": page_relative,
        "page_path": page_path,
        "page_width": page_width,
        "page_height": page_height,
        "cache_relative": cache_relative,
        "cache_path": cache_path,
        "lettering": lettering,
    }


def _deterministic_checks(context: Mapping[str, object]) -> list[dict[str, object]]:
    panels = context["panels"]
    lettering = context["lettering"]
    assert isinstance(panels, list) and isinstance(lettering, list)
    clipped_regions: list[dict[str, object]] = []
    overlap_regions: list[dict[str, object]] = []
    order_regions: list[dict[str, object]] = []

    for panel_index, ((panel_id, _, geometry), panel) in enumerate(zip(lettering, panels), 1):
        assert isinstance(panel_id, str) and isinstance(geometry, dict) and isinstance(panel, dict)
        items = geometry.get("items")
        if not isinstance(items, list):
            items = []
        boxes: list[tuple[str, dict[str, object]]] = []
        orders: list[int] = []
        clean = context["rectangles"][panel_index - 1]  # type: ignore[index]
        _, _, panel_width, panel_height = clean
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id") if isinstance(item.get("id"), str) else "unknown"
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
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > panel_width or y + height > panel_height:
                clipped_regions.append({"panel_id": panel_id, "item_id": item_id, "box": box})
            for prior_id, prior_box in boxes:
                if _overlap(box, prior_box):
                    overlap_regions.append({"panel_id": panel_id, "items": [prior_id, item_id]})
            boxes.append((item_id, box))
        if orders != list(range(1, len(items) + 1)):
            order_regions.append({"panel_id": panel_id, "observed": orders})

    layout_regions = [{
        "layout": context["declared_layout"],
        "matched_layout": context["matched_layout"],
        "rectangles": [list(rectangle) for rectangle in context["rectangles"]],
    }]
    definitions = (
        ("clipped-text", clipped_regions, "All lettering boxes remain inside their source panel bounds."),
        ("text-overlap", overlap_regions, "No lettering boxes overlap within any source panel."),
        ("reading-order", order_regions, "Every panel uses a contiguous one-based lettering reading order."),
        ("layout-border-integrity", [] if context["declared_layout"] == context["matched_layout"] else layout_regions,
         "Storyboard rectangles are contained, non-overlapping, and match the declared layout."),
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
            "regions": failures or layout_regions,
        })
    return checks


def _reviewer_checks(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    checks = [dict(value) for value in values]
    ids = [value.get("id") for value in checks]
    if len(checks) != len(SUBJECTIVE_PAGE_CHECK_IDS) or set(ids) != SUBJECTIVE_PAGE_CHECK_IDS:
        raise ValueError("subjective page checks require exact bounded reviewer evidence")
    categories = validate_quality_checks(
        checks + [
            {
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": f"Deterministic evidence for {check_id} is independently derived.",
                "method": "deterministic-geometry-v1",
                "reviewer": "comic-sol",
                "regions": [{"scope": "page"}],
            }
            for check_id in DETERMINISTIC_PAGE_CHECK_IDS
        ],
        PAGE_CHECK_IDS,
    )
    if categories:
        raise ValueError(", ".join(categories))
    return checks


def _validate_tail_evidence(
    context: Mapping[str, object], checks: Sequence[Mapping[str, object]]
) -> None:
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

    panels = context.get("panels")
    lettering = context.get("lettering")
    if not isinstance(panels, list) or not isinstance(lettering, list):
        raise ValueError("bubble-tail-evidence-mismatch: page context is invalid")
    geometry_by_panel = {
        panel_id: geometry
        for panel_id, _, geometry in lettering
        if isinstance(panel_id, str) and isinstance(geometry, dict)
    }
    expected: dict[tuple[str, str], dict[str, object]] = {}
    for panel in panels:
        if not isinstance(panel, dict) or not isinstance(panel.get("id"), str):
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
    if (check_result == "pass" and any(result != "pass" for result in results)) or (
        check_result == "fail" and results and all(result == "pass" for result in results)
    ):
        raise ValueError("bubble-tail-evidence-mismatch: check result is inconsistent")


def build_page_quality_record(
    project_dir: Path,
    page_number: int,
    visual_checks: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build a current schema-2.0 page QA record without inventing visual evidence."""
    project_dir = Path(project_dir)
    context = _page_context(project_dir, page_number)
    deterministic = _deterministic_checks(context)
    subjective = _reviewer_checks(visual_checks)
    _validate_tail_evidence(context, subjective)
    checks_by_id = {check["id"]: check for check in deterministic + subjective}
    checks = [checks_by_id[check_id] for check_id in PAGE_CHECK_IDS]
    failures = [check for check in checks if check.get("result") == "fail"]
    lettering = context["lettering"]
    assert isinstance(lettering, list)
    record: dict[str, object] = {
        "bindings": {
            "composition_cache_path": context["cache_relative"],
            "composition_cache_sha256": sha256_file(context["cache_path"]),  # type: ignore[arg-type]
            "layout_name": context["declared_layout"],
            "layout_version": LAYOUT_VERSION,
            "lettering_sha256s": [f"{panel_id}:{digest}" for panel_id, digest, _ in lettering],
            "page_height": context["page_height"],
            "page_path": context["page_relative"],
            "page_sha256": sha256_file(context["page_path"]),  # type: ignore[arg-type]
            "page_width": context["page_width"],
            "storyboard_path": "plan/storyboard.json",
            "storyboard_sha256": sha256_file(context["storyboard_path"]),  # type: ignore[arg-type]
        },
        "checks": checks,
        "decision": "regenerate" if failures else "accept",
        "kind": "page-qa",
        "review": {
            "method": "deterministic-plus-bounded-visual-review",
            "reviewed_at": "fixture-deterministic",
            "reviewer": "fixture-reviewer",
        },
        "schema_version": "2.0",
        "subject_id": _page_id(page_number),
        "unresolved_warnings": [],
    }
    categories = validate_quality_checks(checks, PAGE_CHECK_IDS)
    if categories:
        raise ValueError(", ".join(categories))
    return record


def write_page_quality_record(
    project_dir: Path, page_number: int, record: Mapping[str, object]
) -> Path:
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
        issues.append(PageQualityIssue(relative, field, f"page-quality-stale: {detail}"))

    try:
        path = contained_project_path(project_dir, relative, must_exist=True)
        record = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        stale("record.path", "page quality record is missing or unreadable")
        return tuple(issues)
    if record.get("schema_version") != "2.0" or record.get("kind") != "page-qa":
        stale("schema_version", "page quality record is not schema 2.0")
        return tuple(issues)
    if record.get("subject_id") != _page_id(page_number):
        stale("subject_id", "page subject does not match its path")
    categories = validate_quality_checks(record.get("checks"), PAGE_CHECK_IDS)
    for category in categories:
        stale("checks", category)

    bindings = record.get("bindings")
    if not isinstance(bindings, dict):
        stale("bindings", "page quality bindings are missing")
        return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))

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

    lettering_bindings = bindings.get("lettering_sha256s")
    if not isinstance(lettering_bindings, list):
        stale("bindings.lettering_sha256s", "ordered lettering bindings are missing")
    else:
        current_lettering: list[str] = []
        lettering_readable = True
        for binding in lettering_bindings:
            if not isinstance(binding, str) or ":" not in binding:
                lettering_readable = False
                break
            panel_id, _ = binding.split(":", 1)
            try:
                geometry_path = contained_project_path(
                    project_dir, f"panels/{panel_id}/lettering.json", must_exist=True
                )
            except (OSError, ValueError):
                lettering_readable = False
                break
            current_lettering.append(f"{panel_id}:{sha256_file(geometry_path)}")
        if not lettering_readable or current_lettering != lettering_bindings:
            stale(
                "bindings.lettering_sha256s",
                "ordered lettering artifact hashes do not match",
            )
    try:
        context = _page_context(project_dir, page_number)
    except (OSError, ValueError, json.JSONDecodeError):
        if not issues:
            stale("bindings", "current page provenance is missing or unreadable")
        return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))

    expected = {
        "page_sha256": sha256_file(context["page_path"]),  # type: ignore[arg-type]
        "page_width": context["page_width"],
        "page_height": context["page_height"],
        "composition_cache_sha256": sha256_file(context["cache_path"]),  # type: ignore[arg-type]
        "storyboard_sha256": sha256_file(context["storyboard_path"]),  # type: ignore[arg-type]
        "layout_name": context["declared_layout"],
        "layout_version": LAYOUT_VERSION,
        "lettering_sha256s": [
            f"{panel_id}:{digest}" for panel_id, digest, _ in context["lettering"]  # type: ignore[union-attr]
        ],
    }
    for field, current in expected.items():
        if bindings.get(field) != current:
            stale(f"bindings.{field}", "bound value does not match current artifacts")
    return tuple(sorted(issues, key=lambda issue: (issue.path, issue.field, issue.message)))
