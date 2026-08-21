#!/usr/bin/env python3
"""Versioned, reproducible benchmark harness for Comic Sol projects.

A benchmark case is a machine-readable contract (``benchmarks/cases/*.json``)
that names an in-repo project fixture and the mechanics a run must exercise.
Running a case drives the real lifecycle engine end to end and reports six
comparable metrics: pipeline success, resume success, repair rate, panel
acceptance, dialogue correctness, and export success.

Deterministic runs synthesize their own panel rasters, so they prove pipeline
and geometry mechanics only. They never claim visual quality: subjective panel
and page checks are recorded as unresolved warnings and the result carries
``proves_visual_quality: false``, matching ``scripts/quality_sample.py``. Live
provider runs are supported by supplying already retained rasters together with
explicit provenance, which upgrades those checks to a reviewer assertion.

Result records deliberately exclude timestamps and filesystem paths so repeated
deterministic runs of one case are byte-identical and two engine revisions can
be diffed mechanically.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from PIL import Image, ImageDraw

from .comic_sol import (
    PAGE_HEIGHT,
    PAGE_WIDTH,
    TERMINAL_STATUSES,
    atomic_write_json,
    finalize_project,
    init_project,
    invalidate_from,
    promote_attempt,
    read_json,
    record_stage,
    resume_project,
    retain_generation_attempt,
    sha256_file,
    transition,
)
from .core_primitives import (
    PANEL_CHECK_IDS,
    PANEL_ID_PATTERN,
    canonical_artifact_bytes,
)
from .normalize_panels import normalize_panel
from .page_quality import (
    SUBJECTIVE_PAGE_CHECK_IDS,
    build_page_quality_record,
    write_page_quality_record,
)
from .pdf_quality import (
    MAX_GRID_REGION_ERROR,
    MAX_HIGH_ERROR_PIXEL_RATIO,
    MAX_MEAN_ABSOLUTE_CHANNEL_ERROR,
)
from .project_io import PROJECT_OPERATION_LOCK_TIMEOUT, ProjectLock
from .quality_sample import build_evidence_record
from .raster_limits import MAX_DECODED_PIXELS
from .stage_registry import RESUME_STAGES
from .validate_project import ProjectValidationError, require_valid_project

# tail_geometry_result was added alongside the balloon placement QA checks.
# When the CI benchmark workflow copies this harness into a baseline checkout
# that predates that addition, the import fails. Keep the harness runnable
# against older engine revisions by falling back to a local implementation.
try:
    from .core_primitives import tail_geometry_result
except ImportError:
    def _is_point(value: object) -> bool:
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

    def tail_geometry_result(  # type: ignore[misc]
        tail: Mapping[str, Any], speaker_anchor: object, width: int, height: int
    ) -> str:
        """Fallback for baselines that predate the shared primitive."""
        attachment = tail.get("attachment")
        tip = tail.get("tip")
        gap = tail.get("source_gap")
        if (
            not isinstance(speaker_anchor, list)
            or not _is_point(speaker_anchor)
            or any(not 0.0 <= float(value) <= 1.0 for value in speaker_anchor)
            or not _is_point(attachment)
            or not _is_point(tip)
            or not isinstance(gap, (int, float))
            or isinstance(gap, bool)
            or gap <= 0
        ):
            return "fail"
        target = (
            round(float(speaker_anchor[0]) * width),
            round(float(speaker_anchor[1]) * height),
        )
        tail_x, tail_y = tip[0] - attachment[0], tip[1] - attachment[1]
        target_x = target[0] - attachment[0]
        target_y = target[1] - attachment[1]
        tail_length = math.hypot(tail_x, tail_y)
        target_length = math.hypot(target_x, target_y)
        if tail_length <= 0 or target_length <= 0 or tail_length >= target_length:
            return "fail"
        alignment = (tail_x * target_x + tail_y * target_y) / (tail_length * target_length)
        if alignment < 0.999:
            return "fail"
        if not (0 <= tip[0] <= width and 0 <= tip[1] <= height):
            return "fail"
        observed_gap = math.hypot(target[0] - tip[0], target[1] - tip[1])
        if abs(observed_gap - float(gap)) > 1.0:
            return "fail"
        return "pass"

ROOT = Path(__file__).resolve().parents[1]
CASES_ROOT = ROOT / "benchmarks/cases"

# Bumped to "2" when `dialogue_correctness` grew to count `balloon-subject-obstruction`
# and `bubble-tail-geometry`. The record schema is unchanged, but the metric no longer
# means what it meant under "1", so a harness-1 record and a harness-2 record are not
# comparable even though both validate. Summaries and diffs refuse to mix them.
HARNESS_VERSION = "2"
CASE_SCHEMA_VERSION = "1.0"
RESULT_SCHEMA_VERSION = "1.0"
DIFF_SCHEMA_VERSION = "1.0"
CASE_KIND = "benchmark-case"
RESULT_KIND = "benchmark-result"
DIFF_KIND = "benchmark-diff"

EVIDENCE_MODES = ("deterministic", "live-visual")
# Rewinding planning, storyboard, or generation lands a project in a state only
# an agent can leave, so a headless benchmark can only drill the command stages.
RESUMABLE_STAGES = ("lettering", "composition", "export")
CASE_REQUIRED_FIELDS = frozenset({
    "case_id", "dialogue_count", "evidence_mode", "fixture", "kind", "page_count",
    "panels", "repair_panels", "resume_stage", "schema_version", "seed", "title",
})
FIXTURE_REQUIRED_FILES = ("source/input.txt", "source/request.json")
FIXTURE_REQUIRED_PLAN = (
    "plan/story-plan.json",
    "plan/character-bible.json",
    "plan/storyboard.json",
)

METRIC_DIRECTIONS = {
    "pipeline_success": "higher-is-better",
    "resume_success": "higher-is-better",
    "repair_rate": "lower-is-better",
    "panel_acceptance": "higher-is-better",
    "dialogue_correctness": "higher-is-better",
    "export_success": "higher-is-better",
}
METRIC_IDS = tuple(sorted(METRIC_DIRECTIONS))

# Every deterministic, error-severity page check that verifies dialogue geometry, so
# the metric measures the dialogue correctness the engine actually enforces. Each one
# is always emitted and is strictly pass/fail, so each contributes one unit.
#
# `balloon-crowding` is deliberately excluded: `_crowding_check()` never returns
# `fail`, only `pass` or a warning-severity `warning`, because crowded lettering is a
# reading-comfort hint rather than a defect. Counting it would score a comfortable
# page 1/1 and a merely tight one 0/1, conflating comfort with correctness and making
# the metric move on pages the engine still accepts.
DIALOGUE_PAGE_CHECK_IDS = (
    "clipped-text",
    "text-overlap",
    "reading-order",
    "balloon-subject-obstruction",
    "bubble-tail-geometry",
)
TAIL_CHECK_ID = "bubble-tail-direction"

REVIEWER = "comic-sol-benchmark"
REVIEW_TIMESTAMP = "1970-01-01T00:00:00Z"
DETERMINISTIC_METHOD = "benchmark-deterministic-v1"
DETERMINISTIC_PANEL_WARNING = (
    "benchmark-deterministic-mode-does-not-prove-visual-quality"
)
# Mechanically provable for a harness-synthesized raster: no glyphs are drawn and
# the raster is decoded and dimension-checked before it is retained.
MECHANICAL_PANEL_CHECK_IDS = frozenset({"text-free", "technical"})
PANEL_CHECK_EVIDENCE = {
    "character-identity": (
        "Deterministic benchmark mode synthesized this raster, so character identity "
        "against the retained reference is not established by a visual review."
    ),
    "anatomy": (
        "Deterministic benchmark mode draws geometric plates, so limb and proportion "
        "coherence is not established by a visual review."
    ),
    "action": (
        "Deterministic benchmark mode depicts no figures, so the storyboard action beat "
        "is not established by a visual review."
    ),
    "composition": (
        "Deterministic benchmark mode fixes the crop from the storyboard rectangle, so "
        "focal readability is not established by a visual review."
    ),
    "continuity": (
        "Deterministic benchmark mode reuses one seeded palette, so wardrobe, palette, "
        "and prop continuity is not established by a visual review."
    ),
    "text-free": (
        "The harness synthesized this raster without rendering any glyph, so the panel "
        "artwork is text-free by construction."
    ),
    "technical": (
        "The retained raster decoded as RGB at the recorded dimensions and passed the "
        "engine raster verification before promotion."
    ),
}
LIVE_PANEL_EVIDENCE = {
    check_id: (
        f"A reviewer inspected the retained provider raster and accepted the "
        f"{check_id} requirement for this panel."
    )
    for check_id in PANEL_CHECK_IDS
}
PAGE_CHECK_EVIDENCE = {
    "face-action-obstruction": {
        "deterministic": (
            "Deterministic benchmark mode depicts no faces, so lettering obstruction of "
            "a face or delivery action is not established by a visual review."
        ),
        "live-visual": (
            "A reviewer inspected the composed page and found no lettering obstructing a "
            "face or its delivery action."
        ),
    },
    "accidental-text-watermark": {
        "deterministic": (
            "Every panel raster was synthesized without glyphs and only authored "
            "lettering was composited, so the page carries no accidental text."
        ),
        "live-visual": (
            "A reviewer inspected the composed page and found no accidental text, logo, "
            "or watermark outside authored lettering."
        ),
    },
}
TAIL_CHECK_EVIDENCE = {
    "pass": (
        "Every dialogue tail attaches to its balloon and points at the authored speaker "
        "anchor without leaving the panel bounds."
    ),
    "fail": (
        "At least one dialogue tail does not point at its authored speaker anchor or "
        "leaves the panel bounds."
    ),
}
LIMITATIONS = {
    "deterministic": (
        "Deterministic runs synthesize panel rasters and prove pipeline, geometry, and "
        "provenance mechanics only; they do not score visual or artistic quality.",
        "Panel and page checks that require a real render are recorded as unresolved "
        "warnings, so deterministic runs terminate in COMPLETE_WITH_WARNINGS.",
    ),
    "live-visual": (
        "Live runs consume caller-supplied retained rasters and caller-asserted review "
        "provenance; the harness does not itself invoke an image provider.",
        "Live runs are not byte-reproducible because provider output varies between "
        "runs.",
    ),
}


# --------------------------------------------------------------------------- #
# Benchmark project contract
# --------------------------------------------------------------------------- #


def validate_case(case: object, *, fixture_root: Path = ROOT) -> tuple[str, ...]:
    """Return stable issue categories for one benchmark case contract."""
    issues: set[str] = set()
    if not isinstance(case, Mapping):
        return ("case-structure",)
    unknown = set(case) - CASE_REQUIRED_FIELDS
    missing = CASE_REQUIRED_FIELDS - set(case)
    if unknown:
        issues.add("case-unknown-field")
    if missing:
        issues.add("case-missing-field")
    if case.get("schema_version") != CASE_SCHEMA_VERSION:
        issues.add("case-schema-version")
    if case.get("kind") != CASE_KIND:
        issues.add("case-kind")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id or case_id != _slug(case_id):
        issues.add("case-id")
    title = case.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.add("case-title")
    if case.get("evidence_mode") not in EVIDENCE_MODES:
        issues.add("case-evidence-mode")
    seed = case.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        issues.add("case-seed")
    if case.get("resume_stage") not in RESUMABLE_STAGES:
        issues.add("case-resume-stage")

    panels = case.get("panels")
    if (
        not isinstance(panels, list)
        or not panels
        or any(
            not isinstance(panel_id, str) or PANEL_ID_PATTERN.fullmatch(panel_id) is None
            for panel_id in panels
        )
        or len(set(panels)) != len(panels)
    ):
        issues.add("case-panels")
        panels = []
    repair_panels = case.get("repair_panels")
    if (
        not isinstance(repair_panels, list)
        or len(set(repair_panels)) != len(repair_panels)
        or not set(repair_panels) <= set(panels)
    ):
        issues.add("case-repair-panels")
    for field in ("page_count", "dialogue_count"):
        value = case.get(field)
        minimum = 1 if field == "page_count" else 0
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            issues.add(f"case-{field.replace('_', '-')}")

    fixture = case.get("fixture")
    if not isinstance(fixture, str) or not fixture:
        issues.add("case-fixture")
    else:
        try:
            directory = _fixture_directory(fixture, fixture_root)
        except (OSError, ValueError):
            issues.add("case-fixture-missing")
        else:
            required = list(FIXTURE_REQUIRED_FILES) + list(FIXTURE_REQUIRED_PLAN)
            required += [f"prompts/panels/{panel_id}.txt" for panel_id in panels]
            if any(not (directory / relative).is_file() for relative in required):
                issues.add("case-fixture-incomplete")
            else:
                try:
                    storyboard = json.loads(
                        (directory / "plan/storyboard.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    issues.add("case-fixture-incomplete")
                else:
                    pages = storyboard.get("pages") if isinstance(storyboard, dict) else None
                    if (
                        isinstance(case.get("page_count"), int)
                        and not isinstance(case.get("page_count"), bool)
                        and isinstance(pages, list)
                        and len(pages) != case["page_count"]
                    ):
                        issues.add("case-page-count")
    return tuple(sorted(issues))


def load_case(path: Path, *, fixture_root: Path = ROOT) -> dict[str, Any]:
    """Read and validate one benchmark case contract."""
    path = Path(path)
    case = json.loads(path.read_text(encoding="utf-8"))
    issues = validate_case(case, fixture_root=fixture_root)
    if isinstance(case, Mapping) and case.get("case_id") != path.stem:
        issues = tuple(sorted((*issues, "case-filename")))
    if issues:
        raise ValueError(f"invalid benchmark case {path.name}: {', '.join(issues)}")
    return cast(dict[str, Any], case)


def discover_cases(root: Path = CASES_ROOT) -> tuple[Path, ...]:
    """Return every registered benchmark case contract in deterministic order."""
    return tuple(sorted(Path(root).glob("*.json")))


def case_digest(case: Mapping[str, Any]) -> str:
    """Return the canonical digest that ties one result to its exact contract."""
    return hashlib.sha256(canonical_artifact_bytes(dict(case))).hexdigest()


def _slug(value: str) -> str:
    """Return the canonical identifier form used for case IDs and file names."""
    return "".join(
        character if character.isalnum() or character == "-" else "-"
        for character in value.lower()
    ).strip("-")


def _fixture_directory(fixture: str, fixture_root: Path) -> Path:
    """Resolve one fixture path and refuse to leave the fixture root."""
    root = Path(fixture_root).resolve(strict=True)
    directory = (root / fixture).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"benchmark fixture is not a directory: {fixture}")
    if root != directory and root not in directory.parents:
        raise ValueError(f"benchmark fixture escapes the fixture root: {fixture}")
    return directory


# --------------------------------------------------------------------------- #
# Reproducibility metadata
# --------------------------------------------------------------------------- #


def engine_revision(project_dir: Path | None = None) -> dict[str, Any]:
    """Return the engine identity a benchmark result is accountable to."""
    try:
        from comic_sol_product.version import VERSION as engine_version
    except ImportError:  # pragma: no cover - source checkout always resolves
        engine_version = "unknown"
    revision: dict[str, Any] = {
        "engine_version": engine_version,
        "git_revision": _git_revision(),
        "harness_version": HARNESS_VERSION,
        "stage_versions": {},
    }
    if project_dir is not None:
        versions = read_json(Path(project_dir) / "project.json").get("stage_versions")
        if isinstance(versions, dict):
            revision["stage_versions"] = {
                str(stage): versions[stage] for stage in sorted(versions)
            }
    return revision


def _git_revision() -> str:
    """Return the checkout revision, or ``unknown`` outside a git worktree."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "unknown"
    output = completed.stdout.strip()
    return output if completed.returncode == 0 and output else "unknown"


# --------------------------------------------------------------------------- #
# Deterministic raster synthesis
# --------------------------------------------------------------------------- #


def synthesize_panel_raster(seed: int, panel_id: str, revision: int, size: tuple[int, int]) -> bytes:
    """Return a seeded, glyph-free RGB PNG for one panel attempt."""
    width, height = size
    if width < 512 or height < 512:
        raise ValueError("benchmark panel raster must be at least 512px on both axes")
    if width * height > MAX_DECODED_PIXELS:
        raise ValueError("benchmark panel raster exceeds the decoded pixel limit")
    digest = hashlib.sha256(f"{seed}:{panel_id}:{revision}".encode("utf-8")).digest()
    background = tuple(40 + digest[index] // 2 for index in range(3))
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(18, 18, 18), width=6)
    step = max(24, min(width, height) // 12)
    for index in range(1, 12):
        offset = index * step
        level = 70 + digest[(index + 3) % len(digest)] // 2
        draw.line((offset, 0, 0, offset), fill=(level, level, level), width=3)
        draw.line((width - offset, height, width, height - offset), fill=(level, level, level), width=3)
    inset = min(width, height) // 6
    draw.ellipse(
        (inset, inset, width - inset - 1, height - inset - 1),
        outline=(238, 238, 238),
        width=5,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _reference_raster(seed: int, character_id: str) -> bytes:
    """Return a seeded, glyph-free RGB PNG for one character reference."""
    digest = hashlib.sha256(character_id.encode("utf-8")).digest()
    revision = int.from_bytes(digest[:4], "big") % 7
    return synthesize_panel_raster(seed, "p99-99", revision, (512, 512))


def panel_raster_size(rect: Mapping[str, object]) -> tuple[int, int]:
    """Return a raster size that preserves the storyboard aspect ratio."""
    width, height = rect.get("width"), rect.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        raise ValueError("storyboard panel rectangle must contain positive integers")
    divisor = 2
    while width % divisor == 0 and height % divisor == 0 and min(width, height) // divisor >= 512:
        divisor *= 2
    divisor = max(1, divisor // 2)
    raster_size = (width // divisor, height // divisor)
    if min(raster_size) < 512:
        raise ValueError("benchmark panel raster must be at least 512px on both axes")
    if raster_size[0] * raster_size[1] > MAX_DECODED_PIXELS:
        raise ValueError("benchmark panel raster exceeds the decoded pixel limit")
    return raster_size


# --------------------------------------------------------------------------- #
# Project materialization
# --------------------------------------------------------------------------- #


def _storyboard_panels(storyboard: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return every storyboard panel keyed by its canonical panel ID."""
    panels: dict[str, dict[str, Any]] = {}
    for page in storyboard.get("pages", []):
        if not isinstance(page, dict):
            continue
        for panel in page.get("panels", []):
            if isinstance(panel, dict) and isinstance(panel.get("id"), str):
                panels[panel["id"]] = panel
    return panels


def materialize_case_project(
    case: Mapping[str, Any], output_root: Path, *, fixture_root: Path = ROOT
) -> Path:
    """Create a lifecycle project from a case fixture and advance it to REFERENCES_READY."""
    fixture = _fixture_directory(str(case["fixture"]), Path(fixture_root))
    request = json.loads((fixture / "source/request.json").read_text(encoding="utf-8"))
    project = init_project(
        Path(output_root),
        str(case["title"]),
        (fixture / "source/input.txt").read_bytes(),
        request,
    )
    for relative in FIXTURE_REQUIRED_PLAN:
        (project / relative).write_bytes((fixture / relative).read_bytes())
    for panel_id in case["panels"]:
        relative = f"prompts/panels/{panel_id}.txt"
        (project / relative).write_bytes((fixture / relative).read_bytes())

    bible = read_json(project / "plan/character-bible.json")
    for character in bible.get("characters", []):
        if not isinstance(character, dict) or not isinstance(character.get("id"), str):
            continue
        reference = project / f"references/characters/{character['id']}.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(_reference_raster(int(case["seed"]), character["id"]))

    storyboard = read_json(project / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    if list(panels) != list(case["panels"]):
        raise ValueError("benchmark case panels do not match the fixture storyboard")
    manifest = read_json(project / "project.json")
    settings = manifest["settings"]
    if not isinstance(settings, dict):
        raise ValueError("manifest settings must be an object")
    manifest["panels"] = list(case["panels"])
    manifest["settings"] = {
        **settings,
        "page_count": int(case["page_count"]),
        "panel_count": len(case["panels"]),
    }
    manifest["artifacts"] = {
        name: {
            "path": relative,
            "sha256": sha256_file(project / relative),
        }
        for name, relative in (
            ("story_plan", "plan/story-plan.json"),
            ("character_bible", "plan/character-bible.json"),
            ("storyboard", "plan/storyboard.json"),
        )
    }
    atomic_write_json(project / "project.json", manifest)

    transition(project, "PLANNED")
    transition(project, "SCRIPTED")
    record_stage(project, "planning")
    transition(project, "STORYBOARDED")
    record_stage(project, "storyboard")
    transition(project, "REFERENCES_READY")
    return project


def _panel_attempts(
    project: Path,
    case: Mapping[str, Any],
    panels: Mapping[str, Mapping[str, Any]],
    *,
    attempt_root: Path | None,
) -> dict[str, Path]:
    """Retain and promote one raster per panel, repairing the declared panels."""
    repairs = set(case["repair_panels"])
    promoted_attempts: dict[str, Path] = {}
    for panel_id in case["panels"]:
        size = panel_raster_size(panels[panel_id]["rect"])
        planned: list[tuple[str, int]] = [("initial", 0)]
        if panel_id in repairs:
            planned.append(("visual_retry", 1))
        for kind, revision in planned:
            payload = _attempt_payload(case, panel_id, revision, size, attempt_root=attempt_root)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
            retain_generation_attempt(
                project, panel_id, kind, payload, "image/png", width, height
            )
            sequence = 1
            attempt = project / f"panels/attempts/{panel_id}/{kind}-{sequence}.png"
            promote_attempt(project, panel_id, attempt.relative_to(project))
            promoted_attempts[panel_id] = attempt.relative_to(project)
        normalize_panel(
            project, panel_id, f"panels/raw/{panel_id}.png", size, "exact"
        )
    return promoted_attempts


def _attempt_payload(
    case: Mapping[str, Any],
    panel_id: str,
    revision: int,
    size: tuple[int, int],
    *,
    attempt_root: Path | None,
) -> bytes:
    """Return the raster bytes for one attempt in the case's evidence mode."""
    if case["evidence_mode"] == "deterministic":
        return synthesize_panel_raster(int(case["seed"]), panel_id, revision, size)
    if attempt_root is None:
        raise ValueError("live-visual benchmark runs require --attempt-root")
    candidates = [f"{panel_id}-{revision}.png"]
    if revision == 0:
        candidates.append(f"{panel_id}.png")
    for name in candidates:
        candidate = Path(attempt_root) / name
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(
        f"live-visual benchmark run is missing a retained raster for {panel_id}"
    )


def _panel_checks(
    mode: str,
    *,
    reviewer_method: str | None,
    review_assertions: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the seven panel checks a benchmark run can honestly record."""
    method = DETERMINISTIC_METHOD if mode == "deterministic" else str(reviewer_method)
    checks: list[dict[str, Any]] = []
    for check_id in PANEL_CHECK_IDS:
        assertion = (review_assertions or {}).get(check_id)
        if mode == "live-visual" and (
            not isinstance(assertion, Mapping)
            or assertion.get("result") != "pass"
            or not isinstance(assertion.get("evidence"), str)
            or not assertion["evidence"].strip()
        ):
            raise ValueError(
                f"live-visual evidence requires a passing assertion for {check_id}"
            )
        proven = mode == "live-visual" or check_id in MECHANICAL_PANEL_CHECK_IDS
        evidence = (
            str(cast(Mapping[str, str], assertion)["evidence"])
            if mode == "live-visual"
            else PANEL_CHECK_EVIDENCE[check_id]
        )
        checks.append({
            "evidence": evidence,
            "id": check_id,
            "method": method if proven or mode == "live-visual" else DETERMINISTIC_METHOD,
            "regions": [],
            "result": "pass" if proven else "warning",
            "reviewer": REVIEWER,
            "severity": "error" if proven else "warning",
        })
    return checks


def _write_panel_records(
    project: Path,
    case: Mapping[str, Any],
    *,
    reviewer_method: str | None,
    review_assertions: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> None:
    """Write one schema-2.0 panel QA record per panel bound to current artifacts."""
    mode = str(case["evidence_mode"])
    for panel_id in case["panels"]:
        raw = project / f"panels/raw/{panel_id}.png"
        clean = project / f"panels/{panel_id}/clean.png"
        normalization = project / f"panels/{panel_id}/normalization.json"
        with Image.open(raw) as image:
            raw_size = image.size
        with Image.open(clean) as image:
            clean_size = image.size
        checks = _panel_checks(
            mode,
            reviewer_method=reviewer_method,
            review_assertions=(review_assertions or {}).get(panel_id),
        )
        warnings = [check for check in checks if check["result"] == "warning"]
        record = {
            "bindings": {
                "clean_height": clean_size[1],
                "clean_path": f"panels/{panel_id}/clean.png",
                "clean_sha256": sha256_file(clean),
                "clean_width": clean_size[0],
                "normalization_path": f"panels/{panel_id}/normalization.json",
                "normalization_sha256": sha256_file(normalization),
                "raw_height": raw_size[1],
                "raw_path": f"panels/raw/{panel_id}.png",
                "raw_sha256": sha256_file(raw),
                "raw_width": raw_size[0],
            },
            "checks": checks,
            "decision": "accept-warning" if warnings else "accept",
            "kind": "panel-qa",
            "review": {
                "method": DETERMINISTIC_METHOD if mode == "deterministic" else str(reviewer_method),
                "reviewed_at": REVIEW_TIMESTAMP,
                "reviewer": REVIEWER,
            },
            "schema_version": "2.0",
            "subject_id": panel_id,
            "unresolved_warnings": [DETERMINISTIC_PANEL_WARNING] if warnings else [],
        }
        atomic_write_json(project / f"qa/panels/{panel_id}.json", record)
    if mode == "deterministic":
        _record_manifest_warning(project, DETERMINISTIC_PANEL_WARNING)


def _record_manifest_warning(project: Path, warning: str) -> None:
    """Record an unresolved panel warning on the manifest so final validation agrees."""
    manifest_path = project / "project.json"
    manifest = read_json(manifest_path)
    warnings = manifest.get("warnings")
    if not isinstance(warnings, list):
        raise ValueError("manifest warnings must be an array")
    if warning not in warnings:
        warnings.append(warning)
        atomic_write_json(manifest_path, manifest)


# --------------------------------------------------------------------------- #
# Dialogue geometry evidence
# --------------------------------------------------------------------------- #


def dialogue_tail_regions(project: Path, page_number: int) -> list[dict[str, Any]]:
    """Return artifact-bound tail regions with a deterministic direction verdict."""
    project = Path(project)
    storyboard = read_json(project / "plan/storyboard.json")
    pages = [
        page
        for page in storyboard.get("pages", [])
        if isinstance(page, dict) and page.get("number") == page_number
    ]
    if len(pages) != 1:
        raise ValueError(f"storyboard page {page_number} was not found exactly once")
    regions: list[dict[str, Any]] = []
    for panel in pages[0].get("panels", []):
        if not isinstance(panel, dict) or not isinstance(panel.get("id"), str):
            raise ValueError("storyboard panel is invalid")
        panel_id = panel["id"]
        geometry = read_json(project / f"panels/{panel_id}/lettering.json")
        placed = {
            item.get("id"): item
            for item in geometry.get("items", [])
            if isinstance(item, dict)
        }
        normalization = read_json(project / f"panels/{panel_id}/normalization.json")
        clean = normalization.get("clean")
        size = clean.get("size") if isinstance(clean, dict) else None
        if not isinstance(size, list) or len(size) != 2:
            raise ValueError(f"normalization record is invalid for panel {panel_id}")
        for item in panel.get("text", []):
            if not isinstance(item, dict) or item.get("kind") != "dialogue":
                continue
            placement = placed.get(item.get("id"))
            tail = placement.get("tail") if isinstance(placement, dict) else None
            if not isinstance(tail, dict):
                raise ValueError(f"dialogue tail geometry is missing for panel {panel_id}")
            regions.append({
                "panel_id": panel_id,
                "result": tail_direction_result(
                    tail, item.get("speaker_anchor"), int(size[0]), int(size[1])
                ),
                "speaker": item.get("speaker"),
                "speaker_anchor": item.get("speaker_anchor"),
                "text_id": item.get("id"),
                "tip": tail.get("tip"),
                "voice_source": item.get("voice_source"),
            })
    return regions


def tail_direction_result(
    tail: Mapping[str, Any], speaker_anchor: object, width: int, height: int
) -> str:
    """Verify one tail attaches to its balloon and points at the authored speaker.

    The verdict itself is the engine-side primitive the deterministic page check
    uses, so the harness reports exactly what the pipeline enforces.
    """
    return tail_geometry_result(tail, speaker_anchor, width, height)


def _write_page_records(
    project: Path, case: Mapping[str, Any], *, reviewer_method: str | None
) -> int:
    """Write one schema-2.0 page QA record per page and return the dialogue count."""
    mode = str(case["evidence_mode"])
    method = DETERMINISTIC_METHOD if mode == "deterministic" else str(reviewer_method)
    dialogue_count = 0
    for page_number in range(1, int(case["page_count"]) + 1):
        regions = dialogue_tail_regions(project, page_number)
        dialogue_count += len(regions)
        tail_passed = all(region["result"] == "pass" for region in regions)
        by_id: dict[str, dict[str, Any]] = {
            TAIL_CHECK_ID: {
                "evidence": TAIL_CHECK_EVIDENCE["pass" if tail_passed else "fail"],
                "id": TAIL_CHECK_ID,
                "method": "benchmark-tail-geometry-v1",
                "regions": regions,
                "result": "pass" if tail_passed else "fail",
                "reviewer": REVIEWER,
                "severity": "error",
            }
        }
        for check_id, evidence in PAGE_CHECK_EVIDENCE.items():
            proven = mode == "live-visual" or check_id == "accidental-text-watermark"
            by_id[check_id] = {
                "evidence": evidence[mode],
                "id": check_id,
                "method": method,
                "regions": [{"scope": "page"}],
                "result": "pass" if proven else "warning",
                "reviewer": REVIEWER,
                "severity": "error" if proven else "warning",
            }
        # The current harness is copied into the baseline checkout by CI, so use
        # page-quality APIs shared by both engine revisions. The outer lock spans
        # derivation and publication; current helpers simply re-enter it.
        checks = [by_id[check_id] for check_id in SUBJECTIVE_PAGE_CHECK_IDS]
        with ProjectLock(project, timeout=PROJECT_OPERATION_LOCK_TIMEOUT):
            record = build_page_quality_record(
                project,
                page_number,
                checks,
                reviewer=REVIEWER,
                reviewed_at=REVIEW_TIMESTAMP,
            )
            write_page_quality_record(project, page_number, record)
    return dialogue_count


# --------------------------------------------------------------------------- #
# Metric collection
# --------------------------------------------------------------------------- #


def _metric(metric_id: str, numerator: float, denominator: int) -> dict[str, Any]:
    """Return one comparable metric record."""
    if metric_id not in METRIC_DIRECTIONS:
        raise ValueError(f"unknown benchmark metric: {metric_id}")
    denominator = int(denominator)
    if denominator > 0:
        value = round(numerator / denominator, 6)
    else:
        value = 1.0 if metric_id == "dialogue_correctness" else 0.0
    return {
        "denominator": denominator,
        "direction": METRIC_DIRECTIONS[metric_id],
        "numerator": numerator,
        "unit": "ratio",
        "value": value,
    }


def _final_validation_issues(project: Path) -> list[str]:
    """Return stable descriptions of any final validation failure."""
    try:
        require_valid_project(project, "final")
    except ProjectValidationError as error:
        return [f"{issue.path}: {issue.field}: {issue.message}" for issue in error.issues]
    return []


def _panel_acceptance(project: Path, case: Mapping[str, Any]) -> int:
    """Count panels whose QA record records a reusable acceptance decision."""
    accepted = 0
    for panel_id in case["panels"]:
        record = read_json(project / f"qa/panels/{panel_id}.json")
        decisions = (
            {"accept", "accept-warning"}
            if record.get("schema_version") == "2.0"
            else {"accept", "accept_with_warnings"}
        )
        if record.get("decision") in decisions:
            accepted += 1
    return accepted


def _dialogue_counts(project: Path, case: Mapping[str, Any]) -> tuple[int, int]:
    """Count passing and total dialogue-bearing page checks and tail regions."""
    passed = 0
    total = 0
    storyboard = read_json(project / "plan/storyboard.json")
    pages = {
        page.get("number"): page
        for page in storyboard.get("pages", [])
        if isinstance(page, dict) and isinstance(page.get("number"), int)
    }
    for page_number in range(1, int(case["page_count"]) + 1):
        page = pages.get(page_number)
        dialogue_items = [
            item
            for storyboard_panel in (
                page.get("panels", []) if isinstance(page, Mapping) else []
            )
            if isinstance(storyboard_panel, dict)
            for item in storyboard_panel.get("text", [])
            if isinstance(item, dict) and item.get("kind") == "dialogue"
        ]
        if not dialogue_items:
            continue
        record = read_json(project / f"qa/pages/page-{page_number:03d}.json")
        checks = {
            check["id"]: check
            for check in record.get("checks", [])
            if isinstance(check, dict) and isinstance(check.get("id"), str)
        }
        for check_id in DIALOGUE_PAGE_CHECK_IDS:
            check = checks.get(check_id)
            if check is None:
                continue
            total += 1
            passed += 1 if check.get("result") == "pass" else 0
        tail = checks.get(TAIL_CHECK_ID)
        regions = tail.get("regions") if isinstance(tail, dict) else None
        for region in regions if isinstance(regions, list) else []:
            total += 1
            passed += 1 if isinstance(region, dict) and region.get("result") == "pass" else 0
    return passed, total


def _repair_counts(project: Path, case: Mapping[str, Any]) -> tuple[int, int]:
    """Return extra generation calls and total retained attempts."""
    counters_path = project / "logs/generation-counters.json"
    if not counters_path.is_file():
        return 0, 0
    counters = read_json(counters_path)
    panels = counters.get("panels")
    attempts = 0
    if isinstance(panels, dict):
        for panel in panels.values():
            if isinstance(panel, dict):
                attempts += sum(
                    value
                    for value in panel.values()
                    if isinstance(value, int) and not isinstance(value, bool)
                )
    extras = counters.get("global_extra_calls", 0)
    if not isinstance(extras, int) or isinstance(extras, bool):
        extras = 0
    return extras, attempts


def _export_verified(project: Path, case: Mapping[str, Any]) -> tuple[int, int]:
    """Return verified and expected exported pages from the PDF verification record."""
    expected = int(case["page_count"])
    manifest = read_json(project / "project.json")
    project_id = manifest.get("project_id")
    pdf = project / f"exports/{project_id}.pdf"
    verification_path = project / "exports/pdf-verification.json"
    if not pdf.is_file() or not verification_path.is_file():
        return 0, expected
    verification = read_json(verification_path)
    if (
        verification.get("kind") != "pdf-verification"
        or verification.get("pdf_sha256") != sha256_file(pdf)
        or verification.get("page_count") != expected
    ):
        return 0, expected
    pages = verification.get("pages")
    if not isinstance(pages, list) or len(pages) != expected:
        return 0, expected
    source_pages = verification.get("source_pages")
    if not isinstance(source_pages, list) or len(source_pages) != expected:
        return 0, expected
    for number, (page, source) in enumerate(zip(pages, source_pages), 1):
        if not isinstance(page, dict) or not isinstance(source, dict):
            return 0, expected
        if (
            page.get("page_number") != number
            or page.get("dimensions") != [PAGE_WIDTH, PAGE_HEIGHT]
            or page.get("mode") != "RGB"
            or page.get("compared_pixels") != PAGE_WIDTH * PAGE_HEIGHT
        ):
            return 0, expected
        for field in (
            "mean_absolute_channel_error",
            "high_error_pixel_ratio",
            "maximum_grid_region_error",
        ):
            value = page.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value < 0
            ):
                return 0, expected
        if (
            page["mean_absolute_channel_error"] > MAX_MEAN_ABSOLUTE_CHANNEL_ERROR
            or page["high_error_pixel_ratio"] > MAX_HIGH_ERROR_PIXEL_RATIO
            or page["maximum_grid_region_error"] > MAX_GRID_REGION_ERROR
        ):
            return 0, expected
        page_path = project / f"pages/page-{number:03d}.png"
        qa_path = project / f"qa/pages/page-{number:03d}.json"
        if (
            source.get("path") != f"pages/page-{number:03d}.png"
            or source.get("page_qa_path") != f"qa/pages/page-{number:03d}.json"
            or not page_path.is_file()
            or not qa_path.is_file()
            or source.get("sha256") != sha256_file(page_path)
            or source.get("page_qa_sha256") != sha256_file(qa_path)
        ):
            return 0, expected
    return expected, expected


def _resume_drill(project: Path, case: Mapping[str, Any]) -> dict[str, Any]:
    """Invalidate one stage, prove earlier stages are preserved, and refinalize."""
    stage = str(case["resume_stage"])
    index = RESUME_STAGES.index(stage)
    expected_preserved = list(RESUME_STAGES[:index])
    expected_invalidated = list(RESUME_STAGES[index:])
    observation: dict[str, Any] = {
        "stage": stage,
        "preserved": [],
        "invalidated": [],
        "refinalized_status": None,
        "succeeded": False,
    }
    before = resume_project(project)
    observation["preserved_before_invalidation"] = list(before.get("preserved", []))
    invalidate_from(project, stage)
    after = resume_project(project)
    observation["preserved"] = list(after.get("preserved", []))
    observation["invalidated"] = list(after.get("invalidated", []))
    if (
        observation["preserved_before_invalidation"] != list(RESUME_STAGES)
        or observation["preserved"] != expected_preserved
        or observation["invalidated"] != expected_invalidated
    ):
        return observation
    result = finalize_project(project)
    observation["refinalized_status"] = result.get("status")
    observation["succeeded"] = (
        result.get("status") in TERMINAL_STATUSES and not _final_validation_issues(project)
    )
    return observation


# --------------------------------------------------------------------------- #
# Case execution
# --------------------------------------------------------------------------- #


def run_case(
    case: Mapping[str, Any],
    *,
    output_root: Path,
    fixture_root: Path = ROOT,
    attempt_root: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    reviewer_method: str | None = None,
    review_assertions: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one benchmark case end to end and return its comparable result record."""
    issues = validate_case(case, fixture_root=fixture_root)
    if issues:
        raise ValueError(f"invalid benchmark case: {', '.join(issues)}")
    mode = str(case["evidence_mode"])
    if mode == "live-visual" and not (
        attempt_root and provider and model and reviewer_method and review_assertions
    ):
        raise ValueError(
            "live-visual benchmark runs require an attempt root, provider, model, "
            "reviewer method, and per-check review assertions"
        )

    project = materialize_case_project(case, Path(output_root), fixture_root=fixture_root)
    storyboard = read_json(project / "plan/storyboard.json")
    panels = _storyboard_panels(storyboard)
    promoted_attempts = _panel_attempts(project, case, panels, attempt_root=attempt_root)
    _write_panel_records(
        project,
        case,
        reviewer_method=reviewer_method,
        review_assertions=review_assertions,
    )
    transition(project, "PANELS_READY")
    transition(project, "QA_READY")
    record_stage(project, "generation")

    from .compose_pages import compose_project
    from .letter_panels import letter_project

    letter_project(project)
    transition(project, "LETTERED")
    record_stage(project, "lettering")
    compose_project(project)
    transition(project, "COMPOSED")
    record_stage(project, "composition")
    dialogue_count = _write_page_records(project, case, reviewer_method=reviewer_method)
    if dialogue_count != int(case["dialogue_count"]):
        raise ValueError(
            f"benchmark case declares {case['dialogue_count']} dialogue item(s) but the "
            f"fixture produced {dialogue_count}"
        )

    finalized = finalize_project(project)
    validation_issues = _final_validation_issues(project)
    pipeline_ok = finalized.get("status") in TERMINAL_STATUSES and not validation_issues
    resume = _resume_drill(project, case) if pipeline_ok else {"succeeded": False}

    panel_count = len(case["panels"])
    accepted = _panel_acceptance(project, case)
    dialogue_passed, dialogue_total = _dialogue_counts(project, case)
    extras, attempts = _repair_counts(project, case)
    verified_pages, expected_pages = _export_verified(project, case)

    metrics = {
        "pipeline_success": _metric("pipeline_success", 1 if pipeline_ok else 0, 1),
        "resume_success": _metric("resume_success", 1 if resume.get("succeeded") else 0, 1),
        "repair_rate": _metric("repair_rate", extras, panel_count),
        "panel_acceptance": _metric("panel_acceptance", accepted, panel_count),
        "dialogue_correctness": _metric(
            "dialogue_correctness", dialogue_passed, dialogue_total
        ),
        "export_success": _metric("export_success", verified_pages, expected_pages),
    }
    observations = {
        "accepted_panels": accepted,
        "dialogue_checks_passed": dialogue_passed,
        "dialogue_checks_total": dialogue_total,
        "extra_generation_calls": extras,
        "final_validation_issues": validation_issues,
        "page_count": expected_pages,
        "panel_count": panel_count,
        "retained_attempts": attempts,
        "resume": {key: resume[key] for key in sorted(resume)},
        "terminal_status": finalized.get("status"),
        "verified_pdf_pages": verified_pages,
    }
    evidence_panels = {
        panel_id: build_evidence_record(
            mode,
            retained_attempt=(
                promoted_attempts[panel_id].as_posix() if mode == "live-visual" else None
            ),
            attempt_sha256=(
                sha256_file(project / promoted_attempts[panel_id])
                if mode == "live-visual"
                else None
            ),
            provider=provider,
            model=model,
            reviewer_method=reviewer_method,
            limitations=limitations,
        )
        for panel_id in case["panels"]
    }
    record: dict[str, Any] = {
        "case_id": case["case_id"],
        "case_sha256": case_digest(case),
        "evidence": {
            "mode": mode,
            "panels": evidence_panels,
            "proves_visual_quality": mode == "live-visual",
        },
        "harness_version": HARNESS_VERSION,
        "kind": RESULT_KIND,
        "limitations": list(LIMITATIONS[mode]) + list(limitations),
        "metrics": metrics,
        "observations": observations,
        "revision": engine_revision(project),
        "schema_version": RESULT_SCHEMA_VERSION,
        "seed": case["seed"],
        "status": "failed",
    }
    record["status"] = (
        "passed"
        if all(
            metrics[metric_id]["value"] >= 1.0
            for metric_id in (
                "pipeline_success",
                "resume_success",
                "panel_acceptance",
                "dialogue_correctness",
                "export_success",
            )
        )
        else "failed"
    )
    return record


def write_result(record: Mapping[str, Any], results_root: Path) -> Path:
    """Write one benchmark result in the comparable canonical format."""
    output = Path(results_root) / f"result-{record['case_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(record), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def _failed_result_record(
    case_id: str, error: Exception, *, case: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build a schema-valid failed record so failed cases remain diffable evidence."""
    return {
        "case_id": case_id,
        "case_sha256": case_digest(case) if case is not None else "0" * 64,
        "exceptions": [f"{type(error).__name__}: {error}"],
        "harness_version": HARNESS_VERSION,
        "kind": RESULT_KIND,
        "metrics": {metric_id: _metric(metric_id, 0, 1) for metric_id in METRIC_IDS},
        "revision": engine_revision(),
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": "failed",
    }


# --------------------------------------------------------------------------- #
# Revision diffing
# --------------------------------------------------------------------------- #


def load_results(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load benchmark results from one file or a directory of result records."""
    path = Path(path)
    paths = [path] if path.is_file() else sorted(path.glob("result-*.json"))
    records: dict[str, dict[str, Any]] = {}
    exceptions: list[str] = []
    if not paths:
        exceptions.append(f"{path.name}: no benchmark results were found")
    for item in paths:
        try:
            record = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            exceptions.append(f"{item.name}: {type(error).__name__}: {error}")
            continue
        validation_error = _validate_result_record(record)
        if validation_error is not None:
            exceptions.append(f"{item.name}: invalid benchmark result: {validation_error}")
            continue
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            exceptions.append(f"{item.name}: result has no case ID")
            continue
        if case_id in records:
            exceptions.append(f"duplicate benchmark result for case: {case_id}")
            continue
        records[case_id] = record
    return records, exceptions


def _validate_result_record(record: object) -> str | None:
    """Return a stable error for a malformed result before it enters a diff."""
    if not isinstance(record, Mapping):
        return "record must be an object"
    if record.get("kind") != RESULT_KIND:
        return "wrong result kind"
    if record.get("schema_version") != RESULT_SCHEMA_VERSION:
        return "unsupported result schema"
    case_id = record.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        return "result has no case ID"
    digest = record.get("case_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return "result has no valid case digest"
    if record.get("status") not in {"passed", "failed"}:
        return "invalid result status"
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != set(METRIC_IDS):
        return "result metrics do not match the registered metric IDs"
    for metric_id in METRIC_IDS:
        metric = metrics[metric_id]
        if not isinstance(metric, Mapping):
            return f"metric is not an object: {metric_id}"
        if metric.get("direction") != METRIC_DIRECTIONS[metric_id]:
            return f"metric direction is invalid: {metric_id}"
        if metric.get("unit") != "ratio":
            return f"metric unit is invalid: {metric_id}"
        denominator = metric.get("denominator")
        if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator < 0:
            return f"metric denominator is invalid: {metric_id}"
        for field in ("numerator", "value"):
            value = metric.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value < 0
            ):
                return f"metric {field} is invalid: {metric_id}"
        numerator = cast(float | int, metric["numerator"])
        value = cast(float | int, metric["value"])
        expected_value = (
            round(float(numerator) / denominator, 6) if denominator else (
                1.0 if metric_id == "dialogue_correctness" else 0.0
            )
        )
        if value != expected_value:
            return f"metric value is inconsistent: {metric_id}"
        if metric_id != "repair_rate" and value > 1:
            return f"metric value exceeds ratio range: {metric_id}"
    return None


def diff_results(
    baseline: Path,
    candidate: Path,
    output: Path,
    *,
    tolerance: float = 0.0,
    markdown: Path | None = None,
) -> dict[str, Any]:
    """Diff two engine revisions' benchmark results with a fail-closed verdict."""
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("benchmark tolerance must be finite and non-negative")
    baseline_records, exceptions = load_results(baseline)
    candidate_records, candidate_exceptions = load_results(candidate)
    exceptions = [f"baseline: {item}" for item in exceptions]
    exceptions += [f"candidate: {item}" for item in candidate_exceptions]

    # A record only validates against the schema, which cannot tell that a metric was
    # redefined under an older harness. Without this gate a stale archived baseline
    # would diff cleanly against a current run and report the definition change as an
    # improvement or as no change at all, which is the one thing a regression gate
    # exists to prevent.
    for label, records in (("baseline", baseline_records), ("candidate", candidate_records)):
        for case_id in sorted(records):
            harness = records[case_id].get("harness_version")
            if harness != HARNESS_VERSION:
                exceptions.append(
                    f"{label}: {case_id}: result was produced by benchmark harness "
                    f"{harness!r}, not {HARNESS_VERSION!r}, so it is not comparable"
                )

    missing_cases = sorted(set(baseline_records) - set(candidate_records))
    new_cases = sorted(set(candidate_records) - set(baseline_records))
    regressions: list[str] = []
    improvements: list[str] = []
    cases: dict[str, Any] = {}
    for case_id in sorted(set(baseline_records) & set(candidate_records)):
        before, after = baseline_records[case_id], candidate_records[case_id]
        if before.get("case_sha256") != after.get("case_sha256"):
            exceptions.append(f"{case_id}: benchmark case contract changed between runs")
        comparisons: dict[str, Any] = {}
        case_regressed = False
        for metric_id in METRIC_IDS:
            comparison = _compare_metric(
                before.get("metrics"), after.get("metrics"), metric_id, tolerance
            )
            if comparison is None:
                exceptions.append(f"{case_id}: metric is missing from a run: {metric_id}")
                continue
            comparisons[metric_id] = comparison
            if comparison["verdict"] == "regressed":
                case_regressed = True
                regressions.append(f"{case_id}/{metric_id}")
            elif comparison["verdict"] == "improved":
                improvements.append(f"{case_id}/{metric_id}")
        candidate_status = after.get("status")
        cases[case_id] = {
            "baseline_status": before.get("status"),
            "candidate_status": candidate_status,
            "metrics": comparisons,
            "status": (
                "failed" if case_regressed or candidate_status != "passed" else "passed"
            ),
        }
        if candidate_status != "passed":
            regressions.append(f"{case_id}/status")

    if new_cases:
        regressions.extend(f"{case_id}/new-case" for case_id in new_cases)
    clean = not regressions and not missing_cases and not new_cases and not exceptions
    result: dict[str, Any] = {
        "baseline_revisions": _revisions(baseline_records),
        "candidate_revisions": _revisions(candidate_records),
        "cases": cases,
        "decision": "NO REGRESSION" if clean else "REGRESSION",
        "exceptions": exceptions,
        "harness_version": HARNESS_VERSION,
        "improvements": sorted(set(improvements)),
        "kind": DIFF_KIND,
        "missing_cases": missing_cases,
        "new_cases": new_cases,
        "regressions": sorted(set(regressions)),
        "schema_version": DIFF_SCHEMA_VERSION,
        "status": "passed" if clean else "failed",
        "tolerance": tolerance,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = Path(markdown) if markdown is not None else output.with_suffix(".md")
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(_diff_markdown(result), encoding="utf-8", newline="\n")
    return result


def _compare_metric(
    baseline: object, candidate: object, metric_id: str, tolerance: float
) -> dict[str, Any] | None:
    """Compare one metric across revisions and classify the change."""
    if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
        return None
    before, after = baseline.get(metric_id), candidate.get(metric_id)
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    before_value = cast(float | int, before.get("value"))
    after_value = cast(float | int, after.get("value"))
    for value in (before_value, after_value):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            return None
    delta = round(float(after_value) - float(before_value), 6)
    direction = METRIC_DIRECTIONS[metric_id]
    if direction == "higher-is-better":
        verdict = "regressed" if delta < -tolerance else "improved" if delta > tolerance else "unchanged"
    else:
        verdict = "regressed" if delta > tolerance else "improved" if delta < -tolerance else "unchanged"
    return {
        "baseline": float(before_value),
        "candidate": float(after_value),
        "delta": delta,
        "direction": direction,
        "verdict": verdict,
    }


def _revisions(records: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the distinct engine revisions represented by a set of results."""
    seen: dict[str, dict[str, Any]] = {}
    for record in records.values():
        revision = record.get("revision")
        if not isinstance(revision, Mapping):
            continue
        key = json.dumps(dict(revision), sort_keys=True)
        seen.setdefault(key, dict(revision))
    return [seen[key] for key in sorted(seen)]


def _diff_markdown(result: Mapping[str, Any]) -> str:
    """Render a reviewable Markdown summary of one benchmark revision diff."""
    lines = [
        f"# Benchmark {result['decision']}",
        "",
        f"Status: `{result['status']}` (tolerance `{result['tolerance']}`)",
        "",
    ]
    for case_id, case in result["cases"].items():
        lines.append(f"## `{case_id}` — **{case['status']}**")
        lines.append("")
        lines.append("| metric | baseline | candidate | delta | verdict |")
        lines.append("| --- | --- | --- | --- | --- |")
        for metric_id, comparison in case["metrics"].items():
            lines.append(
                f"| `{metric_id}` | {comparison['baseline']} | {comparison['candidate']} "
                f"| {comparison['delta']:+} | {comparison['verdict']} |"
            )
        lines.append("")
    for label, key in (
        ("Regressions", "regressions"),
        ("Improvements", "improvements"),
        ("Missing cases", "missing_cases"),
        ("New cases", "new_cases"),
    ):
        if result[key]:
            lines.extend([f"{label}:", *[f"- `{item}`" for item in result[key]], ""])
    if result["exceptions"]:
        lines.extend(["Diff errors:", *[f"- {item}" for item in result["exceptions"]], ""])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    """Build the benchmark command-line parser."""
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description="Run versioned Comic Sol benchmarks or diff two engine revisions",
    )
    parser.add_argument("--list", action="store_true", help="list registered cases")
    parser.add_argument("--case", type=Path, action="append", default=[])
    parser.add_argument("--all", action="store_true", dest="all_cases")
    parser.add_argument("--cases-root", type=Path, default=CASES_ROOT)
    parser.add_argument("--fixture-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--attempt-root", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--reviewer-method")
    parser.add_argument("--review-assertions", type=Path)
    parser.add_argument("--limitation", action="append", default=[])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--diff-output", type=Path)
    parser.add_argument("--diff-markdown", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run benchmark cases or diff two revisions, always leaving machine-readable evidence."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    if arguments.list:
        print(
            json.dumps(
                {
                    "cases": [path.name for path in discover_cases(arguments.cases_root)],
                    "metrics": list(METRIC_IDS),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    diff_requested = any(
        value is not None
        for value in (arguments.baseline, arguments.candidate, arguments.diff_output)
    )
    if diff_requested:
        if arguments.baseline is None or arguments.candidate is None or arguments.diff_output is None:
            parser.error("--baseline, --candidate, and --diff-output must be supplied together")
        try:
            result = diff_results(
                arguments.baseline,
                arguments.candidate,
                arguments.diff_output,
                tolerance=arguments.tolerance,
                markdown=arguments.diff_markdown,
            )
        except (OSError, ValueError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "passed" else 1

    case_paths = list(arguments.case)
    if arguments.all_cases:
        case_paths.extend(discover_cases(arguments.cases_root))
    if not case_paths:
        parser.error("supply --case, --all, --list, or a --baseline/--candidate diff")
    if arguments.output_root is None or arguments.results is None:
        parser.error("benchmark runs require --output-root and --results")

    review_assertions: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None
    if arguments.review_assertions is not None:
        loaded_assertions = json.loads(
            arguments.review_assertions.read_text(encoding="utf-8")
        )
        if not isinstance(loaded_assertions, Mapping):
            parser.error("--review-assertions must contain a JSON object")
        review_assertions = cast(
            Mapping[str, Mapping[str, Mapping[str, str]]], loaded_assertions
        )

    status = 0
    for path in dict.fromkeys(case_paths):
        case_id = Path(path).stem
        loaded_case: Mapping[str, Any] | None = None
        try:
            case = load_case(path, fixture_root=arguments.fixture_root)
            loaded_case = case
            case_id = str(case["case_id"])
            record = run_case(
                case,
                output_root=arguments.output_root,
                fixture_root=arguments.fixture_root,
                attempt_root=arguments.attempt_root,
                provider=arguments.provider,
                model=arguments.model,
                reviewer_method=arguments.reviewer_method,
                review_assertions=review_assertions,
                limitations=arguments.limitation,
            )
        except Exception as error:  # noqa: BLE001 - evidence must survive any failure
            record = _failed_result_record(case_id, error, case=loaded_case)
            print(record["exceptions"][0], file=sys.stderr)
        write_result(record, arguments.results)
        if record["status"] != "passed":
            status = 1
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
