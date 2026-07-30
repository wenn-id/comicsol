"""Shared helpers for tests that need real filesystem links."""

from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

REQUIRE_SYMLINK_TESTS = os.environ.get("COMIC_SOL_REQUIRE_SYMLINK_TESTS") == "1"


def _scenario(name: str, *dimensions: str) -> dict[str, object]:
    return {
        "name": name,
        "evidence_mode": "deterministic",
        "dimensions": list(dimensions),
    }


QUALITY_SCENARIOS = {
    "continuity-pair": _scenario(
        "continuity-pair",
        "characters:recurring-pair",
        "continuity:wardrobe",
        "continuity:prop",
        "continuity:palette",
    ),
    "layout-registry": _scenario(
        "layout-registry",
        "layout:single",
        "layout:two-horizontal",
        "layout:two-vertical",
        "layout:three-top",
        "layout:three-bottom",
        "layout:four-grid",
    ),
    "dense-text": _scenario(
        "dense-text", "text:dense-dialogue", "text:caption", "text:sfx"
    ),
    "orientations": _scenario(
        "orientations", "orientation:portrait", "orientation:landscape"
    ),
    "image-formats": _scenario(
        "image-formats", "format:png", "format:jpeg", "format:webp", "format:exif"
    ),
    "typography": _scenario(
        "typography",
        "typography:regular",
        "typography:bold",
        "typography:combining",
        "typography:non-latin-fallback",
    ),
    "retry-paths": _scenario(
        "retry-paths", "retry:transient-repeat", "retry:visual-retry"
    ),
    "terminal-outcomes": _scenario(
        "terminal-outcomes", "outcome:accepted-warning", "outcome:hard-failure"
    ),
    "interrupted-resume": _scenario(
        "interrupted-resume", "resume:interrupted"
    ),
}


def _fixture_image(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, size[0] - 9, size[1] - 9), outline="black", width=3)
    draw.line((8, 8, size[0] - 9, size[1] - 9), fill="white", width=2)
    return image


def build_quality_fixture(root: Path, scenario: str) -> Path:
    """Build a compact, provider-free synthetic fixture for one matrix scenario."""
    definition = QUALITY_SCENARIOS.get(scenario)
    if definition is None:
        raise ValueError(f"unknown quality scenario: {scenario}")

    project = Path(root) / scenario
    sources = project / "sources"
    sources.mkdir(parents=True)
    metadata = {
        "evidence_mode": "deterministic",
        "local_only": True,
        "scenario": scenario,
        "dimensions": definition["dimensions"],
        "seed": 20260730,
    }
    (project / "quality-fixture.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    portrait = _fixture_image((96, 144), (28, 88, 132))
    landscape = _fixture_image((144, 96), (132, 72, 28))
    try:
        portrait.save(sources / "portrait.png", format="PNG", optimize=False)
        landscape.save(
            sources / "landscape.jpg", format="JPEG", quality=90,
            subsampling=0, optimize=False, progressive=False,
        )
        portrait.save(sources / "panel.webp", format="WEBP", lossless=True, method=0)
        exif = Image.Exif()
        exif[274] = 6
        landscape.save(
            sources / "oriented.jpg", format="JPEG", quality=90,
            subsampling=0, optimize=False, progressive=False, exif=exif,
        )
    finally:
        portrait.close()
        landscape.close()
    return project


def make_symlink(test_case, link: Path, target: Path, *, directory: bool = False) -> None:
    """Create a link at ``link`` pointing to ``target``, or skip the test.

    Creating a symlink on Windows needs Developer Mode or elevation, so these
    path-containment tests would otherwise vanish silently on a normal Windows
    machine. Directory links fall back to a junction, which needs no privilege.
    Set COMIC_SOL_REQUIRE_SYMLINK_TESTS=1 (CI does) to turn a skip into a
    failure so the lost coverage cannot go unnoticed.
    """
    try:
        link.symlink_to(target, target_is_directory=directory)
        return
    except OSError as error:
        first_error = error

    if directory and os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return
        first_error = OSError(
            f"symlink failed ({first_error}); junction failed "
            f"({completed.stderr.strip() or completed.stdout.strip()})"
        )

    message = f"symlink unavailable: {first_error}"
    if REQUIRE_SYMLINK_TESTS:
        test_case.fail(
            f"{message}. COMIC_SOL_REQUIRE_SYMLINK_TESTS=1 forbids skipping this "
            f"path-containment test; enable Developer Mode or run elevated."
        )
    test_case.skipTest(message)
