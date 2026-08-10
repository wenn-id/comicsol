#!/usr/bin/env python3
"""Compose deterministic Comic Sol page PNGs from lettered panel images."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from comic_sol import (
    PAGE_HEIGHT,
    PAGE_WIDTH,
    atomic_write_bytes,
    canonical_artifact_bytes,
    read_json,
    sha256_file,
)
from layouts import LAYOUT_VERSION, get_layout, match_layout, validate_custom_layout
from project_io import ProjectTransaction, contained_project_path, open_path_nofollow

COMPOSITION_CACHE_PATH = "cache/composition.json"


def _storyboard_page(storyboard: dict, page_number: int) -> dict:
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ValueError("storyboard pages must be an array")
    matches = [
        page for page in pages
        if isinstance(page, dict) and page.get("number") == page_number
    ]
    if len(matches) != 1:
        raise ValueError(f"storyboard page {page_number} was not found exactly once")
    return matches[0]


def _artifact_path(project_dir: Path, panel_id: str, source_artifacts: dict) -> str | Path:
    configured = source_artifacts.get(panel_id)
    if isinstance(configured, dict):
        configured = configured.get("path")
    candidates: list[str | Path] = []
    if isinstance(configured, (str, Path)):
        candidates.append(configured)
    candidates.extend((
        f"panels/{panel_id}/lettered.png",
        f"pages/{panel_id}.png",
        f"panels/lettered/{panel_id}.png",
    ))
    for relative in candidates:
        candidate = contained_project_path(project_dir, relative)
        if candidate.is_file():
            contained_project_path(project_dir, relative, must_exist=True)
            return relative
    raise FileNotFoundError(f"missing required lettered panel image: {panel_id}")


def _page_sources(
    project_dir: Path, page: dict, source_artifacts: dict
) -> list[tuple[dict, str | Path]]:
    panels = page.get("panels")
    if not isinstance(panels, list):
        raise ValueError(f"page {page.get('number')} panels must be an array")
    sources: list[tuple[dict, str | Path]] = []
    missing: list[str] = []
    for panel in panels:
        if not isinstance(panel, dict) or not isinstance(panel.get("id"), str):
            raise ValueError(f"page {page.get('number')} contains an invalid panel")
        panel_id = panel["id"]
        try:
            source = _artifact_path(project_dir, panel_id, source_artifacts)
        except FileNotFoundError:
            missing.append(panel_id)
        else:
            sources.append((panel, source))
    if missing:
        raise FileNotFoundError(
            "missing required lettered panel image(s): " + ", ".join(missing)
        )
    return sources


def _background_color(manifest_settings: dict) -> tuple[int, int, int]:
    configured = manifest_settings.get(
        "page_background", manifest_settings.get("background", "white")
    )
    if configured == "black":
        return (0, 0, 0)
    if configured == "white" or configured is None:
        return (255, 255, 255)
    raise ValueError("manifest page background must be black or white")


def _rect(panel: dict) -> tuple[int, int, int, int]:
    rect = panel.get("rect")
    if not isinstance(rect, dict):
        raise ValueError(f"panel {panel.get('id')} has no rectangle")
    values = tuple(rect.get(key) for key in ("x", "y", "width", "height"))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError(f"panel {panel.get('id')} rectangle must contain integers")
    x, y, width, height = values
    if (
        x < 0 or y < 0 or width <= 0 or height <= 0
        or x + width > PAGE_WIDTH or y + height > PAGE_HEIGHT
    ):
        raise ValueError(f"panel {panel.get('id')} rectangle exceeds the page")
    return x, y, width, height


def _page_layout(page: dict) -> tuple[str, tuple[tuple[int, int, int, int], ...]]:
    """Validate page geometry and return its canonical layout identity."""
    panels = page.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValueError(f"page {page.get('number')} panels must be a non-empty array")
    rectangles = tuple(_rect(panel) for panel in panels if isinstance(panel, dict))
    if len(rectangles) != len(panels):
        raise ValueError(f"page {page.get('number')} contains an invalid panel")
    validate_custom_layout(rectangles, tuple(range(1, len(rectangles) + 1)))
    matched = match_layout(rectangles)
    declared = page.get("layout")
    if not isinstance(declared, str) or not declared:
        return matched, rectangles
    if declared == "custom":
        if matched != "custom":
            raise ValueError("declared layout custom does not match storyboard rectangles")
        return declared, rectangles
    definition = get_layout(declared)
    if definition.rectangles != rectangles:
        raise ValueError(
            f"declared layout {declared} does not match storyboard rectangles"
        )
    return declared, rectangles


def _compose_to_bytes(
    project_dir: Path,
    page: dict,
    sources: list[tuple[dict, str | Path]],
    manifest_settings: dict,
) -> bytes:
    canvas = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), _background_color(manifest_settings))
    draw = ImageDraw.Draw(canvas)
    for panel, source_relative in sources:
        x, y, width, height = _rect(panel)
        try:
            source_path = contained_project_path(
                project_dir, source_relative, must_exist=True
            )
            with open_path_nofollow(source_path) as stream, Image.open(stream) as source:
                source.load()
                fitted = ImageOps.fit(
                    source.convert("RGB"),
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        except (OSError, SyntaxError) as error:
            raise ValueError(
                f"panel {panel.get('id')} is not a readable image"
            ) from error
        canvas.paste(fitted, (x, y))
        draw.rectangle(
            (x, y, x + width - 1, y + height - 1),
            outline=(0, 0, 0),
            width=6,
        )
    encoded = io.BytesIO()
    canvas.save(encoded, format="PNG", optimize=False, compress_level=9)
    return encoded.getvalue()


def compose_page(
    project_dir: Path,
    page_number: int,
    storyboard: dict,
    manifest_settings: dict,
    source_artifacts: dict,
) -> Path:
    """Compose one numbered page and atomically publish its deterministic PNG."""
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page number must be a positive integer")
    if not isinstance(storyboard, dict) or not isinstance(manifest_settings, dict):
        raise TypeError("storyboard and manifest_settings must be objects")
    if not isinstance(source_artifacts, dict):
        raise TypeError("source_artifacts must be an object")
    project_dir = Path(project_dir)
    page = _storyboard_page(storyboard, page_number)
    _page_layout(page)
    sources = _page_sources(project_dir, page, source_artifacts)
    payload = _compose_to_bytes(project_dir, page, sources, manifest_settings)
    output_path = project_dir / f"pages/page-{page_number:03d}.png"
    atomic_write_bytes(output_path, payload)
    return output_path


def compose_all_pages(project_dir: Path) -> list[Path]:
    """Compose every storyboard page in numeric order after a complete preflight."""
    project_dir = Path(project_dir)
    storyboard = read_json(contained_project_path(project_dir, "plan/storyboard.json", must_exist=True))
    manifest = read_json(contained_project_path(project_dir, "project.json", must_exist=True))
    settings = manifest.get("settings")
    artifacts = manifest.get("artifacts", {})
    if not isinstance(settings, dict) or not isinstance(artifacts, dict):
        raise ValueError("manifest settings and artifacts must be objects")
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ValueError("storyboard pages must be an array")
    page_numbers = sorted(
        page.get("number") for page in pages
        if isinstance(page, dict)
        and isinstance(page.get("number"), int)
        and not isinstance(page.get("number"), bool)
    )
    if len(page_numbers) != len(pages) or page_numbers != list(range(1, len(pages) + 1)):
        raise ValueError("storyboard pages must be numbered contiguously from 1")

    prepared_pages = []
    for number in page_numbers:
        page = _storyboard_page(storyboard, number)
        layout_name, rectangles = _page_layout(page)
        sources = _page_sources(project_dir, page, artifacts)
        prepared_pages.append((number, page, sources, layout_name, rectangles))
    payloads = [
        (f"pages/page-{number:03d}.png", _compose_to_bytes(project_dir, page, sources, settings))
        for number, page, sources, _, _ in prepared_pages
    ]
    payload_by_number = {
        number: (relative, payload)
        for (number, _, _, _, _), (relative, payload)
        in zip(prepared_pages, payloads)
    }
    cache_pages = []
    for number, page, sources, layout_name, rectangles in prepared_pages:
        relative, payload = payload_by_number[number]
        panel_ids = [panel["id"] for panel, _ in sources]
        ordered_hashes = []
        for panel, source_relative in sources:
            source_path = contained_project_path(
                project_dir, source_relative, must_exist=True
            )
            ordered_hashes.append(f"{panel['id']}:{sha256_file(source_path)}")
        cache_pages.append({
            "layout": {
                "name": layout_name,
                "rectangles": [list(rectangle) for rectangle in rectangles],
                "version": LAYOUT_VERSION,
            },
            "ordered_lettered_sha256s": ordered_hashes,
            "output": {
                "dimensions": [PAGE_WIDTH, PAGE_HEIGHT],
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "page_id": f"page-{number:03d}",
            "panel_ids": panel_ids,
            "settings_sha256": hashlib.sha256(
                canonical_artifact_bytes(settings)
            ).hexdigest(),
            "storyboard_page_sha256": hashlib.sha256(
                canonical_artifact_bytes(page)
            ).hexdigest(),
        })
    cache_payload = canonical_artifact_bytes({
        "kind": "composition-cache",
        "pages": cache_pages,
        "schema_version": "2.0",
    })
    output_paths = []
    with ProjectTransaction(project_dir, "composition") as transaction:
        for relative, payload in payloads:
            transaction.stage_bytes(relative, payload)
            output_paths.append(project_dir / relative)
        transaction.stage_bytes(COMPOSITION_CACHE_PATH, cache_payload)
        # Re-read under the lock so a concurrent writer's manifest is not lost.
        locked_manifest = read_json(project_dir / "project.json")
        descriptors = locked_manifest.get("artifacts")
        if not isinstance(descriptors, dict):
            descriptors = {}
        descriptors["composition_cache"] = {
            "path": COMPOSITION_CACHE_PATH,
            "sha256": hashlib.sha256(cache_payload).hexdigest(),
        }
        locked_manifest["artifacts"] = descriptors
        transaction.stage_bytes(
            "project.json", canonical_artifact_bytes(locked_manifest)
        )
        transaction.commit()
    return output_paths


def compose_project(project_dir: Path) -> list[Path]:
    """Compose all pages for a generated project."""
    return compose_all_pages(Path(project_dir))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compose_pages.py")
    parser.add_argument("project_dir", type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--page", type=int)
    selection.add_argument("--all", action="store_true", dest="all_pages")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.page is None:
            paths = compose_all_pages(arguments.project_dir)
        else:
            storyboard = read_json(contained_project_path(arguments.project_dir, "plan/storyboard.json", must_exist=True))
            manifest = read_json(contained_project_path(arguments.project_dir, "project.json", must_exist=True))
            settings = manifest.get("settings")
            artifacts = manifest.get("artifacts", {})
            if not isinstance(settings, dict) or not isinstance(artifacts, dict):
                raise ValueError("manifest settings and artifacts must be objects")
            paths = [compose_page(
                arguments.project_dir, arguments.page, storyboard, settings, artifacts
            )]
        print("\n".join(path.as_posix() for path in paths))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
