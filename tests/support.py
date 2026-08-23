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
    "dense-text": _scenario("dense-text", "text:dense-dialogue", "text:caption", "text:sfx"),
    "orientations": _scenario("orientations", "orientation:portrait", "orientation:landscape"),
    "image-formats": _scenario(
        "image-formats", "format:png", "format:jpeg", "format:webp", "format:exif"
    ),
    "typography": _scenario(
        "typography",
        "typography:regular",
        "typography:bold",
        "typography:combining",
        "typography:non-latin-fallback",
        "typography:latin-extended",
        "typography:greek-extended",
        "typography:cyrillic-extended",
        "typography:script-extension",
    ),
    "retry-paths": _scenario("retry-paths", "retry:transient-repeat", "retry:visual-retry"),
    "terminal-outcomes": _scenario(
        "terminal-outcomes", "outcome:accepted-warning", "outcome:hard-failure"
    ),
    "interrupted-resume": _scenario("interrupted-resume", "resume:interrupted"),
}


MULTI_SPEAKER_PANEL_ID = "p01-02"
MULTI_SPEAKER_TEXT_IDS = ("p01-02-t01", "p01-02-t02")


def write_multi_speaker_panel(project: Path) -> tuple[str, ...]:
    """Turn one lettering fixture panel into a two-character dialogue exchange.

    The fixture projects all speak with a single voice, so nothing in them can
    show whether attribution survives a panel where two characters talk. The
    anchors and anchor keywords chosen here keep both balloons clear of each
    other and of both protected voice sources, so a correct multi-character panel
    is expected to pass every deterministic page check unaided.
    """
    project = Path(project)
    bible_path = project / "plan/character-bible.json"
    bible = json.loads(bible_path.read_text("utf-8"))
    if not any(character["id"] == "ren" for character in bible["characters"]):
        gatekeeper = dict(bible["characters"][0])
        gatekeeper.update(
            {
                "id": "ren",
                "name": "Ren",
                "role": "gatekeeper",
                "reference_path": "references/characters/ren.png",
            }
        )
        bible["characters"].append(gatekeeper)
        _write_json(bible_path, bible)

    storyboard_path = project / "plan/storyboard.json"
    storyboard = json.loads(storyboard_path.read_text("utf-8"))
    panel = next(
        candidate
        for page in storyboard["pages"]
        for candidate in page["panels"]
        if candidate["id"] == MULTI_SPEAKER_PANEL_ID
    )
    panel["characters"] = ["mira", "ren"]
    first = dict(panel["text"][0])
    first.update(
        {
            "id": MULTI_SPEAKER_TEXT_IDS[0],
            "anchor": "top-left",
            "content": "No bridge.",
            "priority": 1,
            "speaker": "mira",
            "speaker_anchor": [0.78, 0.34],
        }
    )
    second = dict(first)
    second.update(
        {
            "id": MULTI_SPEAKER_TEXT_IDS[1],
            "anchor": "bottom-right",
            "content": "Then I hold the gate.",
            "priority": 2,
            "speaker": "ren",
            "speaker_anchor": [0.22, 0.62],
        }
    )
    panel["text"] = [first, second]
    _write_json(storyboard_path, storyboard)
    return MULTI_SPEAKER_TEXT_IDS


def _write_json(path: Path, value: object) -> None:
    """Write one project artifact the way the engine and fixtures store JSON."""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "utf-8",
    )


def bounded_tail_regions(project: Path, page_number: int) -> list[dict[str, object]]:
    """Build exact test-only tail regions from current storyboard and geometry."""
    storyboard = json.loads((Path(project) / "plan/storyboard.json").read_text("utf-8"))
    page = next(page for page in storyboard["pages"] if page.get("number") == page_number)
    regions: list[dict[str, object]] = []
    for panel in page["panels"]:
        geometry = json.loads(
            (Path(project) / f"panels/{panel['id']}/lettering.json").read_text("utf-8")
        )
        placed = {item["id"]: item for item in geometry["items"]}
        for item in panel["text"]:
            if item.get("kind") != "dialogue":
                continue
            tail = placed[item["id"]]["tail"]
            regions.append(
                {
                    "panel_id": panel["id"],
                    "text_id": item["id"],
                    "speaker": item["speaker"],
                    "voice_source": item["voice_source"],
                    "speaker_anchor": item["speaker_anchor"],
                    "tip": tail["tip"],
                    "result": "pass",
                }
            )
    return regions


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
            sources / "landscape.jpg",
            format="JPEG",
            quality=90,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        portrait.save(sources / "panel.webp", format="WEBP", lossless=True, method=0)
        exif = Image.Exif()
        exif[274] = 6
        landscape.save(
            sources / "oriented.jpg",
            format="JPEG",
            quality=90,
            subsampling=0,
            optimize=False,
            progressive=False,
            exif=exif,
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
        if link.is_symlink():
            return
        link.unlink(missing_ok=True)
        first_error = OSError("created path is not recognized as a symlink")
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
