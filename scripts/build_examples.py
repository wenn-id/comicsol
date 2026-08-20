#!/usr/bin/env python3
"""Deterministic builder for the official Comic Sol example projects.

An example under ``samples/<example-id>/`` commits only editable inputs: the
source text, the request, the three plan artifacts, and the panel prompts. This
builder replays the deterministic half of the pipeline over those inputs and
produces a complete project directory, up to and including the exported PDF.

It never contacts an image provider. Panel artwork is synthesized locally from
the example seed, so a build is reproducible offline and no large binary has to
be committed to the repository. That also fixes the evidence boundary: a built
example proves project structure, lettering, composition, export, and validation
mechanics. It does not demonstrate live illustration quality. See
``samples/README.md`` for the evidence tiers and ``samples/sunlight-courier``
for the tracked live-generated example.

Because the artwork is a placeholder, most QA checks have nothing to inspect.
Those are recorded as warning-level "not reviewed" rather than passed, so a built
example terminates as ``COMPLETE_WITH_WARNINGS`` and its report states the gap. A
recorded pass is always earned: ``technical`` is measured off the raster,
``text-free`` follows from a generator that loads no font, and the page-QA layer
independently re-derives tail geometry before accepting ``bubble-tail-direction``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .comic_sol import (
    atomic_write_json,
    finalize_project,
    init_project,
    read_json,
    sha256_file,
    transition,
)
from .compose_pages import compose_project
from .core_primitives import PANEL_CHECK_IDS
from .layouts import match_layout
from .letter_panels import letter_project
from .normalize_panels import NormalizationSpec, normalize_panels
from .page_quality import build_page_quality_record, write_page_quality_record
from .project_io import ProjectTransaction
from .quality_sample import build_evidence_record
from .validate_project import ProjectValidationError, require_valid_project


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
DEFAULT_OUTPUT_ROOT = ROOT / "build/examples"
REVIEW_METHOD = "deterministic-example-build-no-visual-review"
REVIEWER = "comic-sol-example-builder (automated; no visual review)"
REVIEWED_AT = "2026-08-19T00:00:00Z"

PLAN_ARTIFACTS = {
    "story_plan": "plan/story-plan.json",
    "character_bible": "plan/character-bible.json",
    "storyboard": "plan/storyboard.json",
}

# A placeholder raster contains no cast, no faces, and no staging, so most
# panel checks have nothing to review. Only these two are decidable from the
# artifact itself, and the builder proves both before recording a pass.
VERIFIED_PANEL_CHECK_IDS = ("text-free", "technical")

# Everything else is a judgement about artwork this build does not have. These
# are recorded as warning-level "not reviewed" so the decision becomes
# accept-warning, the project lands in COMPLETE_WITH_WARNINGS, and the QA report
# states the gap instead of implying a review that never happened.
UNREVIEWED_PANEL_EVIDENCE = {
    "character-identity": (
        "Not reviewed: character identity needs generated artwork to compare "
        "against the character-bible fingerprint, and this panel is a synthetic "
        "placeholder with no cast drawn in it."
    ),
    "anatomy": (
        "Not reviewed: anatomy needs a drawn figure, and this panel contains only "
        "seeded geometry with no limbs, joints, or proportions to assess."
    ),
    "action": (
        "Not reviewed: whether the artwork performs the storyboard beat cannot be "
        "judged from a placeholder that depicts no action."
    ),
    "composition": (
        "Not reviewed: subject placement and text-safe space need a drawn subject; "
        "this panel has none, so only the rectangle itself is known to be correct."
    ),
    "continuity": (
        "Not reviewed: the declared continuity facts describe visible traits, and "
        "no such traits are rendered in a placeholder raster."
    ),
}

# Tail direction is geometric, and page-QA construction independently re-derives
# the expected regions from the storyboard and the placed lettering geometry,
# rejecting anything stale or incomplete. That makes it the one subjective-slot
# page check a placeholder build can honestly pass.
VERIFIED_PAGE_EVIDENCE = {
    "bubble-tail-direction": (
        "Verified geometrically: every authored dialogue has a tail region whose "
        "tip is the actual computed geometry for its panel, cross-checked against "
        "the authored speaker anchor and rejected if stale or incomplete. Reading "
        "quality over drawn artwork is not claimed."
    ),
}

UNREVIEWED_PAGE_EVIDENCE = {
    "face-action-obstruction": (
        "Not reviewed: this page composes placeholder panels, so there is no "
        "speaking face or beat action that a balloon could be found to cover."
    ),
    "accidental-text-watermark": (
        "Not reviewed: the builder draws no glyphs and contacts no provider, so a "
        "provider watermark is out of scope rather than inspected and cleared."
    ),
}


class ExampleError(ValueError):
    """Raised when an example contract or its committed inputs are unusable."""


def _seeded_color(seed: int, key: str, *, floor: int = 24, ceiling: int = 210) -> tuple[int, int, int]:
    """Return one deterministic RGB triple for a seed and key."""
    digest = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).digest()
    span = ceiling - floor
    return tuple(floor + digest[index] % span for index in range(3))  # type: ignore[return-value]


def _synthetic_raster(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    """Draw one deterministic, text-free placeholder raster.

    The figure is intentionally geometric, and no font is loaded here. That is
    what lets the panel record honestly pass the text-free check: generated panel
    artwork must contain no glyphs, and this generator cannot produce any.
    """
    width, height = size
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    inset = max(6, min(width, height) // 24)
    accent = tuple(min(255, channel + 45) for channel in color)
    shadow = tuple(max(0, channel - 45) for channel in color)
    draw.rectangle(
        (inset, inset, width - inset - 1, height - inset - 1),
        outline=shadow,
        width=max(2, inset // 3),
    )
    draw.line((inset, height - inset - 1, width - inset - 1, inset), fill=accent, width=max(2, inset // 4))
    draw.ellipse(
        (
            width // 2 - width // 6,
            height // 2 - height // 6,
            width // 2 + width // 6,
            height // 2 + height // 6,
        ),
        outline=accent,
        width=max(2, inset // 4),
    )
    return image


def _verified_panel_evidence(
    project: Path, panel_id: str, size: tuple[int, int]
) -> dict[str, str]:
    """Prove the two decidable panel checks and return their earned evidence.

    Nothing here is asserted on trust. The raster is measured, and a mismatch
    raises instead of recording a pass, so a `pass` in the published record
    always corresponds to a property this function actually confirmed.
    """
    clean = project / f"panels/{panel_id}/clean.png"
    with Image.open(clean) as image:
        mode, actual = image.mode, image.size
    if mode != "RGB":
        raise ExampleError(f"{panel_id}: clean raster must be opaque RGB, found {mode}")
    if actual != size:
        raise ExampleError(
            f"{panel_id}: clean raster is {actual[0]}x{actual[1]}, "
            f"expected the storyboard rectangle {size[0]}x{size[1]}"
        )
    return {
        "text-free": (
            "Verified by construction: the example builder renders panels with "
            "rectangle, line, and ellipse primitives only and loads no font, so "
            "the raster cannot contain dialogue, captions, balloons, signatures, "
            "logos, or watermarks. Authored lettering is applied downstream."
        ),
        "technical": (
            f"Verified by measurement: the clean raster opened as opaque {mode} at "
            f"{actual[0]}x{actual[1]}, matching the storyboard rectangle exactly, "
            "and is reproduced deterministically from the example seed."
        ),
    }


def _read_contract(example_dir: Path) -> dict[str, object]:
    """Load and check one example contract."""
    contract_path = example_dir / "example.json"
    if not contract_path.is_file():
        raise ExampleError(f"missing example contract: {contract_path.relative_to(ROOT).as_posix()}")
    contract = read_json(contract_path)
    if not isinstance(contract, dict):
        raise ExampleError("example.json must contain an object")
    for field in ("example_id", "title", "evidence_mode", "seed", "page_count", "panel_count"):
        if field not in contract:
            raise ExampleError(f"example.json is missing {field}")
    if contract["evidence_mode"] != "deterministic":
        raise ExampleError("build_examples.py only builds deterministic examples")
    if contract["example_id"] != example_dir.name:
        raise ExampleError("example_id must match its directory name")
    return contract


def _storyboard_panels(storyboard: dict[str, object]) -> tuple[tuple[int, str, dict[str, object]], ...]:
    """Return (page_number, layout, panel) triples in reading order."""
    pages = storyboard.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ExampleError("storyboard must declare at least one page")
    ordered: list[tuple[int, str, dict[str, object]]] = []
    for page in sorted(pages, key=lambda item: item["number"]):
        for panel in sorted(page["panels"], key=lambda item: item["order"]):
            ordered.append((page["number"], page["layout"], panel))
    return tuple(ordered)


def _panel_size(panel: dict[str, object]) -> tuple[int, int]:
    """Return the exact storyboard rectangle size for a panel."""
    rect = panel["rect"]
    return int(rect["width"]), int(rect["height"])


def _verify_declared_layouts(panels: Sequence[tuple[int, str, dict[str, object]]]) -> None:
    """Fail when a declared page layout does not match its rectangles."""
    by_page: dict[int, tuple[str, list[tuple[int, int, int, int]]]] = {}
    for page_number, layout, panel in panels:
        rect = panel["rect"]
        entry = by_page.setdefault(page_number, (layout, []))
        entry[1].append((int(rect["x"]), int(rect["y"]), int(rect["width"]), int(rect["height"])))
    for page_number, (layout, rectangles) in sorted(by_page.items()):
        matched = match_layout(rectangles)
        if matched != layout:
            raise ExampleError(
                f"page {page_number} declares layout {layout!r} but its rectangles match {matched!r}"
            )


def _stage_inputs(example_dir: Path, project: Path, seed: int) -> None:
    """Copy committed plan/prompt inputs and synthesize character references."""
    for relative in PLAN_ARTIFACTS.values():
        source = example_dir / relative
        if not source.is_file():
            raise ExampleError(f"missing committed input: {relative}")
        shutil.copyfile(source, project / relative)

    prompts = sorted((example_dir / "prompts/panels").glob("*.txt"))
    if not prompts:
        raise ExampleError("missing committed panel prompts")
    for prompt in prompts:
        shutil.copyfile(prompt, project / "prompts/panels" / prompt.name)

    bible = read_json(project / "plan/character-bible.json")
    references = project / "references/characters"
    references.mkdir(parents=True, exist_ok=True)
    for character in bible["characters"]:
        image = _synthetic_raster((512, 512), _seeded_color(seed, f"reference:{character['id']}"))
        try:
            image.save(references / f"{character['id']}.png", format="PNG", optimize=False)
        finally:
            image.close()


def _prepare_manifest(project: Path, contract: dict[str, object], panel_ids: Sequence[str]) -> None:
    """Record committed plan provenance and advance to REFERENCES_READY."""
    manifest = read_json(project / "project.json")
    manifest.update({
        "title": str(contract["title"]),
        "panels": list(panel_ids),
        "artifacts": {
            name: {"path": relative, "sha256": sha256_file(project / relative)}
            for name, relative in sorted(PLAN_ARTIFACTS.items())
        },
        "capability": {
            "detected_at": REVIEWED_AT,
            "name": "deterministic-example-build",
            "status": "available",
            "supports_dimensions": True,
            "supports_reference_images": True,
        },
        "settings": {
            **manifest["settings"],
            "page_count": int(contract["page_count"]),
            "panel_count": len(panel_ids),
        },
        "input": {
            **manifest["input"],
            "source_sha256": sha256_file(project / "source/input.txt"),
        },
    })
    atomic_write_json(project / "project.json", manifest)
    for status in ("PLANNED", "SCRIPTED", "STORYBOARDED", "REFERENCES_READY"):
        transition(project, status)


def _record_manifest_warnings(project: Path, warnings: Iterable[str]) -> None:
    """Mirror unresolved panel warnings into the manifest.

    Final validation requires every unresolved panel warning to appear in the
    manifest and forbids a plain ``COMPLETE`` while any remain, so this is what
    steers the build to the honest ``COMPLETE_WITH_WARNINGS`` terminal state.
    """
    manifest = read_json(project / "project.json")
    existing = manifest.get("warnings")
    merged = list(existing) if isinstance(existing, list) else []
    for warning in warnings:
        if warning not in merged:
            merged.append(warning)
    manifest["warnings"] = merged
    atomic_write_json(project / "project.json", manifest)


def _build_panels(
    project: Path,
    panels: Sequence[tuple[int, str, dict[str, object]]],
    seed: int,
) -> None:
    """Synthesize, normalize, and accept every panel raster."""
    raw_root = project / "panels/raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    specs: list[NormalizationSpec] = []
    for _page_number, _layout, panel in panels:
        panel_id = str(panel["id"])
        size = _panel_size(panel)
        image = _synthetic_raster(size, _seeded_color(seed, f"panel:{panel_id}"))
        try:
            image.save(raw_root / f"{panel_id}.png", format="PNG", optimize=False)
        finally:
            image.close()
        specs.append(NormalizationSpec(panel_id, f"panels/raw/{panel_id}.png", size, "exact"))

    normalize_panels(project, specs)

    template = read_json(ROOT / "templates/panel-record.json")
    for _page_number, _layout, panel in panels:
        panel_id = str(panel["id"])
        record = json.loads(json.dumps(template))
        record.update({
            "subject_id": panel_id,
            "decision": "accept-warning",
            "unresolved_warnings": [
                UNREVIEWED_PANEL_EVIDENCE[check_id]
                for check_id in PANEL_CHECK_IDS
                if check_id in UNREVIEWED_PANEL_EVIDENCE
            ],
            "review": {
                "method": REVIEW_METHOD,
                "reviewer": REVIEWER,
                "reviewed_at": REVIEWED_AT,
            },
        })
        width, height = _panel_size(panel)
        record["bindings"].update({
            "clean_height": height,
            "clean_path": f"panels/{panel_id}/clean.png",
            "clean_sha256": sha256_file(project / f"panels/{panel_id}/clean.png"),
            "clean_width": width,
            "normalization_path": f"panels/{panel_id}/normalization.json",
            "normalization_sha256": sha256_file(project / f"panels/{panel_id}/normalization.json"),
            "raw_height": height,
            "raw_path": f"panels/raw/{panel_id}.png",
            "raw_sha256": sha256_file(project / f"panels/raw/{panel_id}.png"),
            "raw_width": width,
        })
        verified = _verified_panel_evidence(project, panel_id, (width, height))
        by_id = {check["id"]: check for check in record["checks"]}
        for check_id in PANEL_CHECK_IDS:
            reviewed = check_id in VERIFIED_PANEL_CHECK_IDS
            by_id[check_id].update({
                "result": "pass" if reviewed else "warning",
                "severity": "error" if reviewed else "warning",
                "evidence": (
                    verified[check_id] if reviewed
                    else UNREVIEWED_PANEL_EVIDENCE[check_id]
                ),
                "method": REVIEW_METHOD,
                "reviewer": REVIEWER,
                "regions": [],
            })
        record["checks"] = [by_id[check_id] for check_id in PANEL_CHECK_IDS]
        atomic_write_json(project / f"qa/panels/{panel_id}.json", record)

    _record_manifest_warnings(project, UNREVIEWED_PANEL_EVIDENCE.values())
    transition(project, "PANELS_READY")
    transition(project, "QA_READY")


def _tail_regions(project: Path, page_number: int) -> list[dict[str, object]]:
    """Build exact dialogue tail regions from committed text and placed geometry."""
    storyboard = read_json(project / "plan/storyboard.json")
    page = next(page for page in storyboard["pages"] if page["number"] == page_number)
    regions: list[dict[str, object]] = []
    for panel in page["panels"]:
        dialogue = [item for item in panel["text"] if item.get("kind") == "dialogue"]
        if not dialogue:
            continue
        geometry = read_json(project / f"panels/{panel['id']}/lettering.json")
        placed = {item["id"]: item for item in geometry["items"]}
        for item in dialogue:
            regions.append({
                "panel_id": panel["id"],
                "text_id": item["id"],
                "speaker": item["speaker"],
                "voice_source": item["voice_source"],
                "speaker_anchor": item["speaker_anchor"],
                "tip": placed[item["id"]]["tail"]["tip"],
                "result": "pass",
            })
    return regions


def _write_page_records(project: Path, page_numbers: Iterable[int]) -> None:
    """Write one page QA record per composed page.

    The four deterministic page checks are computed by the page-QA layer itself.
    Of the three reviewer-supplied slots, only tail direction is machine-earned;
    the two artwork-content checks are recorded as unreviewed warnings.
    """
    for page_number in page_numbers:
        checks = [
            {
                "id": check_id,
                "result": "pass" if check_id in VERIFIED_PAGE_EVIDENCE else "warning",
                "severity": (
                    "error" if check_id in VERIFIED_PAGE_EVIDENCE else "warning"
                ),
                "evidence": (
                    VERIFIED_PAGE_EVIDENCE.get(check_id)
                    or UNREVIEWED_PAGE_EVIDENCE[check_id]
                ),
                "method": REVIEW_METHOD,
                "reviewer": REVIEWER,
                "regions": (
                    _tail_regions(project, page_number)
                    if check_id == "bubble-tail-direction"
                    else [{"scope": "page"}]
                ),
            }
            for check_id in (
                "face-action-obstruction",
                "bubble-tail-direction",
                "accidental-text-watermark",
            )
        ]
        write_page_quality_record(
            project,
            page_number,
            build_page_quality_record(
                project,
                page_number,
                checks,
                reviewer=REVIEWER,
                reviewed_at=REVIEWED_AT,
            ),
        )


def _record_deterministic_evidence(project: Path) -> None:
    """Label the build as deterministic, mechanics-only evidence.

    The rendered QA report reads this record. Writing it keeps a built example
    from reading as a claim about live illustration quality.
    """
    payload = (
        json.dumps(
            build_evidence_record("deterministic"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    with ProjectTransaction(project, "quality-evidence") as transaction:
        transaction.stage_bytes("qa/evidence.json", payload)


def build_example(example_dir: Path, output_root: Path) -> Path:
    """Build one committed example into a complete validated project."""
    example_dir = Path(example_dir)
    contract = _read_contract(example_dir)
    seed = int(contract["seed"])

    storyboard = read_json(example_dir / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    _verify_declared_layouts(panels)
    panel_ids = [str(panel["id"]) for _page, _layout, panel in panels]
    if len(panel_ids) != int(contract["panel_count"]):
        raise ExampleError(
            f"contract declares {contract['panel_count']} panels but the storyboard has {len(panel_ids)}"
        )
    page_numbers = sorted({page for page, _layout, _panel in panels})
    if len(page_numbers) != int(contract["page_count"]):
        raise ExampleError(
            f"contract declares {contract['page_count']} pages but the storyboard has {len(page_numbers)}"
        )

    output_root = Path(output_root)
    destination = output_root / str(contract["example_id"])
    if destination.exists():
        shutil.rmtree(destination)
    output_root.mkdir(parents=True, exist_ok=True)

    project = init_project(
        output_root,
        str(contract["title"]),
        (example_dir / "source/input.txt").read_bytes(),
        read_json(example_dir / "source/request.json"),
    )
    if project != destination:
        raise ExampleError(
            f"example_id {contract['example_id']!r} must equal the slug of its title "
            f"(got project directory {project.name!r})"
        )

    _stage_inputs(example_dir, project, seed)
    _prepare_manifest(project, contract, panel_ids)
    _build_panels(project, panels, seed)

    letter_project(project)
    transition(project, "LETTERED")
    compose_project(project)
    transition(project, "COMPOSED")
    _write_page_records(project, page_numbers)
    _record_deterministic_evidence(project)
    finalize_project(project)

    require_valid_project(project, "final")
    return project


def discover_examples() -> tuple[Path, ...]:
    """Return every committed deterministic example directory."""
    return tuple(sorted(
        path.parent
        for path in SAMPLES.glob("*/example.json")
    ))


def _summarize(project: Path) -> str:
    """Return one compact human-readable build summary line."""
    manifest = read_json(project / "project.json")
    pdf = manifest["artifacts"]["pdf"]["path"]
    size = (project / pdf).stat().st_size
    return (
        f"{manifest['project_id']}: {manifest['status']} "
        f"pages={manifest['settings']['page_count']} "
        f"panels={manifest['settings']['panel_count']} "
        f"pdf={pdf} ({size / 1024:.0f} KiB)"
    )


def main(argv: list[str] | None = None) -> int:
    """Build the requested example projects and report each result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--example",
        action="append",
        default=[],
        help="example ID to build; repeatable. Defaults to every committed example.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"directory that receives built projects (default: {DEFAULT_OUTPUT_ROOT.relative_to(ROOT).as_posix()})",
    )
    arguments = parser.parse_args(argv)

    available = {path.name: path for path in discover_examples()}
    if not available:
        print("ERROR no committed examples were found", file=sys.stderr)
        return 1
    if arguments.example:
        unknown = sorted(set(arguments.example) - set(available))
        if unknown:
            print(f"ERROR unknown example: {', '.join(unknown)}", file=sys.stderr)
            return 1
        selected = [available[name] for name in arguments.example]
    else:
        selected = [available[name] for name in sorted(available)]

    for example_dir in selected:
        try:
            project = build_example(example_dir, arguments.output_root)
        except ExampleError as error:
            print(f"ERROR {example_dir.name}: {error}", file=sys.stderr)
            return 1
        except ProjectValidationError as error:
            print(f"ERROR {example_dir.name}: validation failed", file=sys.stderr)
            for issue in error.issues:
                print(f"  {issue.path}: {issue.field}: {issue.message}", file=sys.stderr)
            return 1
        print(_summarize(project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
