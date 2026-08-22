"""Character consistency benchmark for Comic Sol.

Repeated character identity is the known generative weakness of the pipeline, and
a weakness cannot be improved while it is unmeasured. This module defines one
plan-complete benchmark project whose only variables are the camera view, the
expression, the pose, the light, and the background. The characters themselves are
pinned: every immutable visual trait is owned by the character bible and restated
verbatim in every panel prompt, so a difference between two panels is a defect of
the render rather than a difference of instruction.

The benchmark has two evidence planes and deliberately keeps them apart.

* The structural plane is deterministic. Matrix coverage, trait immutability,
  invariant pinning, prompt restatement, schema validity, and rerun determinism
  are asserted by ``tests/test_consistency_benchmark.py`` on every pull request.
* The visual plane is subjective. Identity drift across views and lighting can only
  be judged by a human or a model looking at rendered panels, so those scores live
  in a scorecard file and never enter deterministic CI.

Materialize the benchmark, emit a scorecard, write a baseline report, or summarize a
filled scorecard with:

    python -m tests.consistency_benchmark materialize OUTPUT_ROOT
    python -m tests.consistency_benchmark scorecard OUTPUT_PATH
    python -m tests.consistency_benchmark baseline OUTPUT_PATH
    python -m tests.consistency_benchmark summarize SCORECARD_PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_sol_product import __version__ as ENGINE_VERSION
from scripts.comic_sol import (
    atomic_write_json,
    init_project,
    read_json,
    sha256_file,
    transition,
)
from scripts.validate_project import ProjectValidationError, require_valid_project

# The corpus module already owns the canonical artifact builders for these shapes.
# Reusing them keeps one schema statement in the test suite instead of two.
from tests.benchmark_corpus import (
    BASE_NEGATIVE,
    INPUT_MODE,
    LANGUAGE,
    SCHEMA_VERSION,
    _bible,
    _character,
    _fingerprint,
    _page,
    _scene,
    _story,
    build_storyboard,
)

SCENARIO = "character-consistency"
TITLE = "Character Consistency Benchmark"
SCORECARD_KIND = "character-consistency-scorecard"
BASELINE_KIND = "character-consistency-baseline"
SOURCE = (
    "Two salvage divers lose a brass compass off a harbour at noon and trace it back "
    "through an engine shed to a night market, so the same two faces have to survive "
    "every light of one working day.\n"
)
STYLE_ANCHOR = (
    "original high-contrast manga/anime ink linework with restrained colour accents"
)
CAPABILITY = (
    "Repeated character identity: two canonical characters carried across five camera "
    "views, nine expressions, four lighting conditions, and four backgrounds while every "
    "immutable visual trait stays pinned."
)
STRESSES = (
    "identity:repeat-character",
    "views:five-camera-views",
    "expression:nine-variations",
    "lighting:four-conditions",
    "background:four-locations",
    "scoring:manual-or-model-assisted",
)

# The five views the benchmark has to exercise for every canonical character.
VIEWS = ("front", "profile", "three-quarter", "full-body", "close-up")
SHOTS = {
    "close-up": "close-up head-and-shoulders shot at eye level",
    "front": "front-facing medium shot at eye level",
    "full-body": "full-body wide shot at eye level",
    "profile": "left-profile medium shot at eye level",
    "three-quarter": "three-quarter medium shot slightly above eye level",
}

# The recorded consistency dimensions. Each one is owned by a character-bible field,
# so a scored dimension always has one canonical text to be scored against.
CONSISTENCY_DIMENSIONS = (
    "face",
    "hair",
    "age",
    "clothing",
    "accessories",
    "proportions",
    "signature-traits",
)
DIMENSION_SOURCES = {
    "accessories": ("visual_fingerprint", "signature_props"),
    "age": ("age_band",),
    "clothing": ("visual_fingerprint", "wardrobe"),
    "face": ("visual_fingerprint", "face"),
    "hair": ("visual_fingerprint", "hair"),
    "proportions": ("visual_fingerprint", "silhouette"),
    "signature-traits": ("visual_fingerprint", "invariants"),
}
QA_TRAITS_BY_DIMENSION = (
    ("face", "face"),
    ("hair", "hair"),
    ("age", "age-appearance"),
    ("clothing", "clothing"),
    ("accessories", "accessories"),
    ("proportions", "proportions"),
    ("signature-traits", "immutable-traits"),
)

# Scoring is a reviewer judgement, so the scale is published with the benchmark and
# never compared against a threshold by CI.
SCORE_SCALE = {
    "labels": {
        "0": "a different character",
        "1": "major drift in this dimension",
        "2": "recognizable but wrong in this dimension",
        "3": "minor drift a reader would forgive",
        "4": "indistinguishable from the canonical trait",
    },
    "max": 4,
    "min": 0,
}
VISUAL_REASON = (
    "identity drift is a reviewer judgement, so scored dimensions stay out of "
    "deterministic CI"
)
VISUAL_LIMITATIONS = (
    "no image provider runs in CI, so this baseline renders no panel and scores no dimension",
    "a deterministic run proves definition mechanics only; it never claims visual quality",
    "one reviewer with one provider produces one opinion, not a release-wide claim",
)
SCORING_COMMAND = (
    "python -m tests.consistency_benchmark materialize OUTPUT_ROOT, render every "
    "prompts/panels/*.txt with one provider, score the emitted scorecard, then run "
    "python -m tests.consistency_benchmark summarize SCORECARD_PATH"
)

# Identity evidence must not be occluded, so no panel authors text at all.
PANEL_NEGATIVE = (
    "dialogue",
    "sound effects",
    "onomatopoeia",
    "reference sheet labels",
    "model sheet grid lines",
)
PANELS_PER_PAGE = 4
PAGE_LAYOUT = "four-grid"


# --- canonical characters ---------------------------------------------------

RANI_INVARIANTS = (
    "crescent scar under the right eye",
    "blunt chin-length bob",
    "olive field jacket",
    "brass compass on a bootlace",
)
BAYU_INVARIANTS = (
    "chipped front tooth",
    "tight black curls cropped at the temples",
    "faded blue coveralls tied at the waist",
    "yellow ear-defenders around the neck",
)

CHARACTER_BIBLE = _bible([
    _character(
        "rani",
        "Rani",
        "salvage diver",
        "adult, late twenties",
        "she/her",
        ("stubborn", "methodical"),
        "recover the compass her mother dived with",
        "short flat statements",
        _fingerprint(
            "compact square-shouldered build with a slight forward lean",
            "round face with a crescent scar under the right eye",
            "black hair in a blunt chin-length bob with a straight fringe",
            "olive field jacket with a buttoned left chest pocket",
            ("olive", "brick red", "bone white"),
            ("brass compass on a bootlace",),
            RANI_INVARIANTS,
        ),
    ),
    _character(
        "bayu",
        "Bayu",
        "deck hand",
        "teen, seventeen",
        "he/him",
        ("eager", "literal"),
        "prove he can hold a dive line",
        "quick short questions",
        _fingerprint(
            "tall narrow build with long forearms",
            "long face with a broad flat nose and a chipped front tooth",
            "tight black curls cropped at the temples",
            "faded blue mechanic coveralls tied at the waist",
            ("faded blue", "steel grey", "ochre"),
            ("yellow ear-defenders around the neck",),
            BAYU_INVARIANTS,
        ),
    ),
])
CHARACTERS = {character["id"]: character for character in CHARACTER_BIBLE["characters"]}


# --- backgrounds and lighting conditions -----------------------------------

REFERENCE_STUDIO = "seamless bone-white office wall under even neutral daylight"
HARBOR_NOON = "salt-bleached harbour planks under hard vertical noon sun"
ENGINE_SHED = "oil-dark engine shed lit by one hanging work lamp"
RAIN_NIGHT_MARKET = "wet night-market brick under cold blue rim light and red stall glow"

SCENES = {
    "reference-studio": _scene(
        "reference-studio",
        "photograph Rani for the shift board",
        "salvage office photo wall",
        "early morning",
        ("rani",),
        REFERENCE_STUDIO,
    ),
    "harbor-noon": _scene(
        "harbor-noon",
        "lose the compass overboard",
        "open harbour planking above the salvage berth",
        "noon",
        ("rani", "bayu"),
        HARBOR_NOON,
    ),
    "engine-shed": _scene(
        "engine-shed",
        "trace the compass inside",
        "engine shed behind the berth",
        "late afternoon",
        ("rani", "bayu"),
        ENGINE_SHED,
    ),
    "rain-night-market": _scene(
        "rain-night-market",
        "recover the compass",
        "covered night-market aisle in the rain",
        "night",
        ("rani", "bayu"),
        RAIN_NIGHT_MARKET,
    ),
}

STORY = _story(
    "Same Compass, Four Lights",
    "Two salvage divers lose a brass compass at noon and buy it back under night-market rain.",
    "A crew is recognizable by what it refuses to lose.",
    ("steady", "observant"),
    "A working salvage harbour that photographs its crew at the start of every shift.",
    "Rani is photographed against the salvage office wall before the shift.",
    "The brass compass goes over the harbour planks at noon.",
    "The compass is traced through the engine shed to a night-market table.",
    "Rani buys the compass back and reties it to her bootlace.",
    [
        SCENES["reference-studio"],
        SCENES["harbor-noon"],
        SCENES["engine-shed"],
        SCENES["rain-night-market"],
    ],
)


# --- the consistency matrix ------------------------------------------------
#
# One row per panel. The first page is the control: one background, one light, one
# expression, four views. Every later page changes the light, the background, the
# expression, and the pose while the identity stays fixed.

MATRIX = (
    {
        "beat": "Rani is photographed for the shift board.",
        "characters": ("rani",),
        "composition": "single figure centered with the whole face unobstructed",
        "expression": "neutral",
        "expression_detail": "neutral, mouth closed and eyes level",
        "lighting": "even neutral daylight from the front with no cast shadow",
        "lighting_condition": "even-neutral-daylight",
        "pose": "Rani stands square to camera with both hands at her sides",
        "scene": "reference-studio",
        "view": "front",
    },
    {
        "beat": "The same face is photographed from the side.",
        "characters": ("rani",),
        "composition": "single figure on the right third with the profile edge clear",
        "expression": "neutral",
        "expression_detail": "neutral, jaw relaxed and lips together",
        "lighting": "even neutral daylight from the front with no cast shadow",
        "lighting_condition": "even-neutral-daylight",
        "pose": "Rani turns to her left and holds the pose for the lens",
        "scene": "reference-studio",
        "view": "profile",
    },
    {
        "beat": "The photographer takes the angled frame.",
        "characters": ("rani",),
        "composition": "single figure centered with the near shoulder below the far shoulder",
        "expression": "neutral",
        "expression_detail": "neutral, eyes returned to the lens",
        "lighting": "even neutral daylight from the front with no cast shadow",
        "lighting_condition": "even-neutral-daylight",
        "pose": "Rani angles her shoulders away and looks back over them",
        "scene": "reference-studio",
        "view": "three-quarter",
    },
    {
        "beat": "The full-height frame closes the shift board set.",
        "characters": ("rani",),
        "composition": "single figure from head to boots inside the frame",
        "expression": "neutral",
        "expression_detail": "neutral, chin level",
        "lighting": "even neutral daylight from the front with no cast shadow",
        "lighting_condition": "even-neutral-daylight",
        "pose": "Rani stands at full height with the compass hanging free",
        "scene": "reference-studio",
        "view": "full-body",
    },
    {
        "beat": "The shift starts easy on the planks.",
        "characters": ("rani",),
        "composition": "head and shoulders filling the frame with the scar side to the lens",
        "expression": "delighted",
        "expression_detail": "delighted, eyes narrowed against the glare and teeth showing",
        "lighting": "hard vertical noon sun with short black shadows",
        "lighting_condition": "hard-noon-sun",
        "pose": "Rani laughs back at the winch crew over her shoulder",
        "scene": "harbor-noon",
        "view": "close-up",
    },
    {
        "beat": "The compass goes over the planks.",
        "characters": ("rani", "bayu"),
        "composition": "two figures on opposite thirds with both faces unobstructed",
        "expression": "alarmed",
        "expression_detail": "alarmed, brows high and mouth open",
        "lighting": "hard noon sun from overhead with glare off the water",
        "lighting_condition": "hard-noon-sun",
        "pose": "Rani grabs for the bootlace as Bayu lunges past her",
        "scene": "harbor-noon",
        "view": "three-quarter",
    },
    {
        "beat": "The crew drags the harbour floor.",
        "characters": ("rani", "bayu"),
        "composition": "two full-height figures with a clear gap between them",
        "expression": "braced",
        "expression_detail": "braced determination with the jaw set",
        "lighting": "hard noon sun with reflected light from the wet planks",
        "lighting_condition": "hard-noon-sun",
        "pose": "Rani and Bayu haul the drag line hand over hand",
        "scene": "harbor-noon",
        "view": "full-body",
    },
    {
        "beat": "Bayu admits the drag came up empty.",
        "characters": ("bayu",),
        "composition": "single figure centered with the drag hook low in frame",
        "expression": "wry",
        "expression_detail": "wry amusement with one corner of the mouth raised",
        "lighting": "hard noon sun straight down with a hard shadow under the chin",
        "lighting_condition": "hard-noon-sun",
        "pose": "Bayu faces the lens and lifts the empty drag hook",
        "scene": "harbor-noon",
        "view": "front",
    },
    {
        "beat": "The search moves inside at dusk.",
        "characters": ("rani",),
        "composition": "head and shoulders filling the frame with the lamp above the eyeline",
        "expression": "exhausted",
        "expression_detail": "exhausted, eyelids low and mouth slack",
        "lighting": "one hanging work lamp above and behind with deep falloff",
        "lighting_condition": "single-lamp-low-light",
        "pose": "Rani leans her head against the shed post",
        "scene": "engine-shed",
        "view": "close-up",
    },
    {
        "beat": "Bayu finds the compass lace in a bilge grate.",
        "characters": ("bayu",),
        "composition": "single figure on the left third in profile against the lamp",
        "expression": "focused",
        "expression_detail": "narrow focus with the eyes tracking one hand",
        "lighting": "single hanging lamp from the front left with a black surround",
        "lighting_condition": "single-lamp-low-light",
        "pose": "Bayu turns a bilge grate over under the lamp",
        "scene": "engine-shed",
        "view": "profile",
    },
    {
        "beat": "The compass turns up on a night-market table.",
        "characters": ("rani", "bayu"),
        "composition": "tight two-shot close-up with both faces inside the frame",
        "expression": "furious",
        "expression_detail": "cold anger with the mouth flat and the eyes fixed",
        "lighting": "cold blue rim light from the left with red stall glow from below",
        "lighting_condition": "cold-rim-and-red-glow",
        "pose": "Rani stares down the stall keeper while Bayu holds the lamp up",
        "scene": "rain-night-market",
        "view": "close-up",
    },
    {
        "beat": "Rani buys the compass back.",
        "characters": ("rani",),
        "composition": "single figure centered with the compass raised at chest height",
        "expression": "relieved",
        "expression_detail": "quiet relief with the eyes closed for a moment",
        "lighting": "cold blue rim light from behind with warm red fill from the stall",
        "lighting_condition": "cold-rim-and-red-glow",
        "pose": "Rani reties the compass to her bootlace facing the lens",
        "scene": "rain-night-market",
        "view": "front",
    },
)


def _panel(row):
    """Return one storyboard panel for a matrix row, pinning every invariant."""
    scene = SCENES[row["scene"]]
    continuity = [
        f"{character_id}:{fact}"
        for character_id in row["characters"]
        for fact in CHARACTERS[character_id]["visual_fingerprint"]["invariants"]
    ]
    continuity.append(f"{row['scene']}:{scene['continuity_anchor']}")
    return {
        "action": row["pose"],
        "beat": row["beat"],
        "characters": list(row["characters"]),
        "composition": row["composition"],
        "continuity": continuity,
        "expression": row["expression_detail"],
        "lighting": row["lighting"],
        "negative": list(BASE_NEGATIVE) + list(PANEL_NEGATIVE),
        "scene_id": row["scene"],
        "shot": SHOTS[row["view"]],
        "text": [],
    }


PAGES = [
    _page(
        PAGE_LAYOUT,
        [_panel(row) for row in MATRIX[start:start + PANELS_PER_PAGE]],
    )
    for start in range(0, len(MATRIX), PANELS_PER_PAGE)
]
STORYBOARD = build_storyboard(PAGES)
PANEL_IDS = tuple(
    panel["id"] for page in STORYBOARD["pages"] for panel in page["panels"]
)
MATRIX_BY_PANEL = dict(zip(PANEL_IDS, MATRIX))


def resolved_panels():
    """Return every resolved storyboard panel in reading order."""
    return [panel for page in STORYBOARD["pages"] for panel in page["panels"]]


def immutable_traits(character_id):
    """Return the canonical text of every consistency dimension for one character.

    Each dimension reads its own character-bible field, so the benchmark never keeps
    a second copy of an immutable trait that could drift away from the bible.
    """
    character = CHARACTERS[character_id]
    traits = {}
    for dimension in CONSISTENCY_DIMENSIONS:
        value = character
        for key in DIMENSION_SOURCES[dimension]:
            value = value[key]
        traits[dimension] = "; ".join(value) if isinstance(value, (list, tuple)) else value
    return traits


def panel_prompt(panel_id):
    """Return the generation prompt for one benchmark panel.

    The prompt varies the camera, expression, pose, light, and background, and
    restates every immutable trait verbatim for every character in the panel.
    """
    row = MATRIX_BY_PANEL.get(panel_id)
    if row is None:
        raise ValueError(f"unknown benchmark panel: {panel_id}")
    scene = SCENES[row["scene"]]
    lines = [
        f"Panel {panel_id} - {TITLE}, {row['view']} view.",
        f"Style: {STYLE_ANCHOR}.",
        f"Camera: {SHOTS[row['view']]}.",
        f"Composition: {row['composition']}.",
        f"Pose: {row['pose']}.",
        f"Expression: {row['expression_detail']}.",
        f"Lighting: {row['lighting']}.",
        f"Background: {scene['continuity_anchor']}.",
    ]
    for character_id in row["characters"]:
        traits = immutable_traits(character_id)
        lines.append(
            f"{CHARACTERS[character_id]['name']} - immutable identity, reproduce exactly:"
        )
        lines.extend(f"- {dimension}: {traits[dimension]}" for dimension in CONSISTENCY_DIMENSIONS)
    prohibited = list(BASE_NEGATIVE) + list(PANEL_NEGATIVE)
    lines.append("Do not render: " + ", ".join(prohibited) + ".")
    return "\n".join(lines) + "\n"


# --- deterministic measurements --------------------------------------------


def view_counts():
    """Return how many panels exercise each camera view."""
    counts = {view: 0 for view in VIEWS}
    for row in MATRIX:
        counts[row["view"]] += 1
    return counts


def views_per_character():
    """Return the camera views each canonical character is exercised in."""
    views = {character_id: set() for character_id in CHARACTERS}
    for row in MATRIX:
        for character_id in row["characters"]:
            views[character_id].add(row["view"])
    return {character_id: sorted(items) for character_id, items in views.items()}


def conditions_per_view(key="lighting_condition"):
    """Return the distinct values of one matrix field per camera view."""
    conditions = {view: set() for view in VIEWS}
    for row in MATRIX:
        conditions[row["view"]].add(row[key])
    return {view: sorted(items) for view, items in conditions.items()}


def invariant_pins():
    """Return required and recorded (panel, character, invariant) pin counts.

    A pin is a storyboard continuity entry, which is exactly what panel QA rechecks,
    so this counts identity constraints the engine itself will verify.
    """
    required = 0
    recorded = 0
    for panel in resolved_panels():
        entries = set(panel["continuity"])
        for character_id in panel["characters"]:
            for fact in CHARACTERS[character_id]["visual_fingerprint"]["invariants"]:
                required += 1
                if f"{character_id}:{fact}" in entries:
                    recorded += 1
    return {"expected": required, "recorded": recorded}


def trait_restatements():
    """Return required and recorded (panel, character, dimension) restatements."""
    required = 0
    recorded = 0
    for panel_id in PANEL_IDS:
        prompt = panel_prompt(panel_id)
        for character_id in MATRIX_BY_PANEL[panel_id]["characters"]:
            for trait in immutable_traits(character_id).values():
                required += 1
                if trait in prompt:
                    recorded += 1
    return {"expected": required, "recorded": recorded}


def definition():
    """Return the complete benchmark input definition.

    The definition is the contract a score is comparable against: characters,
    dimensions, matrix, story, storyboard, and every panel prompt.
    """
    return {
        "characters": CHARACTER_BIBLE,
        "dimensions": {
            "order": list(CONSISTENCY_DIMENSIONS),
            "sources": {
                dimension: list(path) for dimension, path in DIMENSION_SOURCES.items()
            },
        },
        "matrix": [dict(row) for row in MATRIX],
        "prompts": {panel_id: panel_prompt(panel_id) for panel_id in PANEL_IDS},
        "scenario": SCENARIO,
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "story": STORY,
        "storyboard": STORYBOARD,
        "title": TITLE,
    }


def definition_digest():
    """Return the stable digest of the benchmark input definition."""
    payload = json.dumps(definition(), ensure_ascii=False, indent=2, sort_keys=True)
    return hashlib.sha256((payload + "\n").encode("utf-8")).hexdigest()


def structural_baseline():
    """Return the deterministic, definition-derived plane of the benchmark."""
    return {
        "backgrounds": sorted(SCENES),
        "character_count": len(CHARACTERS),
        "characters": sorted(CHARACTERS),
        "dimensions": list(CONSISTENCY_DIMENSIONS),
        "expressions": sorted({row["expression"] for row in MATRIX}),
        "immutable_traits": {
            character_id: immutable_traits(character_id) for character_id in sorted(CHARACTERS)
        },
        "invariant_pins": invariant_pins(),
        "layouts": [page["layout"] for page in STORYBOARD["pages"]],
        "lighting_conditions": sorted({row["lighting_condition"] for row in MATRIX}),
        "page_count": len(STORYBOARD["pages"]),
        "panel_count": len(PANEL_IDS),
        "panels": list(PANEL_IDS),
        "text_item_count": sum(len(panel["text"]) for panel in resolved_panels()),
        "trait_restatements": trait_restatements(),
        "views": view_counts(),
        "views_per_character": views_per_character(),
    }


def visual_plane():
    """Return the unscored visual plane and the boundary it declares."""
    return {
        "how_to_score": SCORING_COMMAND,
        "limitations": list(VISUAL_LIMITATIONS),
        "reason": VISUAL_REASON,
        "scale": SCORE_SCALE,
        "scored": False,
        "scored_dimensions": 0,
        "total_dimensions": trait_restatements()["expected"],
    }


def consistency_metadata():
    """Return the metadata sidecar describing what this benchmark measures."""
    return {
        "capability": CAPABILITY,
        "definition_sha256": definition_digest(),
        "dimensions": list(CONSISTENCY_DIMENSIONS),
        "evidence_mode": "structural",
        "expected": {
            "backgrounds": sorted(SCENES),
            "character_count": len(CHARACTERS),
            "expressions": sorted({row["expression"] for row in MATRIX}),
            "layouts": [page["layout"] for page in STORYBOARD["pages"]],
            "lighting_conditions": sorted({row["lighting_condition"] for row in MATRIX}),
            "page_count": len(STORYBOARD["pages"]),
            "panel_count": len(PANEL_IDS),
            "panels": list(PANEL_IDS),
            "text_item_count": sum(len(panel["text"]) for panel in resolved_panels()),
            "views": view_counts(),
        },
        "local_only": True,
        "scenario": SCENARIO,
        "schema_version": SCHEMA_VERSION,
        "scoring": {
            "ci_asserted": False,
            "plane": "manual-or-model-assisted",
            "scale": SCORE_SCALE,
        },
        "stresses": list(STRESSES),
        "title": TITLE,
    }


# --- scorecards ------------------------------------------------------------


class ScorecardError(Exception):
    """Raised when a scorecard does not match the current benchmark definition."""


def scorecard_template():
    """Return an unscored scorecard for every panel, character, and dimension."""
    return {
        "benchmark": SCENARIO,
        "definition_sha256": definition_digest(),
        "dimensions": list(CONSISTENCY_DIMENSIONS),
        "kind": SCORECARD_KIND,
        "panels": {
            panel_id: {
                "background": row["scene"],
                "characters": {
                    character_id: {dimension: None for dimension in CONSISTENCY_DIMENSIONS}
                    for character_id in row["characters"]
                },
                "expression": row["expression"],
                "lighting_condition": row["lighting_condition"],
                "view": row["view"],
            }
            for panel_id, row in MATRIX_BY_PANEL.items()
        },
        "review": {
            "engine_version": None,
            "evidence_mode": None,
            "limitations": [],
            "method": None,
            "model": None,
            "provider": None,
            "reviewed_at": None,
            "reviewer": None,
        },
        "scale": SCORE_SCALE,
        "schema_version": SCHEMA_VERSION,
    }


def _scorecard_scores(scorecard):
    """Yield every recorded (panel, character, dimension, score) tuple."""
    panels = scorecard.get("panels")
    if not isinstance(panels, dict):
        return
    for panel_id in sorted(panels):
        panel = panels[panel_id]
        characters = panel.get("characters") if isinstance(panel, dict) else None
        if not isinstance(characters, dict):
            continue
        for character_id in sorted(characters):
            scores = characters[character_id]
            if not isinstance(scores, dict):
                continue
            for dimension in sorted(scores):
                yield panel_id, character_id, dimension, scores[dimension]


def load_scorecard(path):
    """Return the scorecard at ``path``, or raise ``ScorecardError``.

    Unreadable, non-UTF-8, and malformed input are scorecard problems like any
    other, so callers get one error type and one failure path instead of a
    traceback for the most ordinary mistake there is.
    """
    path = Path(path)
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScorecardError(f"{path}: cannot be read: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise ScorecardError(f"{path}: is not valid UTF-8") from error
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise ScorecardError(f"{path}: is not valid JSON: {error}") from error


def validate_scorecard(scorecard):
    """Return the scorecard, or raise ``ScorecardError`` describing every problem."""
    if not isinstance(scorecard, dict):
        raise ScorecardError("scorecard must be a JSON object")
    problems = []
    if scorecard.get("kind") != SCORECARD_KIND:
        problems.append(f"kind must be {SCORECARD_KIND!r}")
    if scorecard.get("benchmark") != SCENARIO:
        problems.append(f"benchmark must be {SCENARIO!r}")
    if scorecard.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if scorecard.get("definition_sha256") != definition_digest():
        problems.append(
            "definition_sha256 does not match the current benchmark definition, so the "
            "score is not comparable to it"
        )
    panels = scorecard.get("panels")
    if not isinstance(panels, dict):
        problems.append("panels must be a JSON object")
    else:
        if set(panels) != set(PANEL_IDS):
            problems.append("panels must contain exactly every benchmark panel")
        for panel_id in sorted(set(panels) & set(PANEL_IDS)):
            panel = panels[panel_id]
            characters = panel.get("characters") if isinstance(panel, dict) else None
            if not isinstance(characters, dict):
                problems.append(f"{panel_id}: characters must be a JSON object")
                continue
            expected_characters = set(MATRIX_BY_PANEL[panel_id]["characters"])
            if set(characters) != expected_characters:
                problems.append(
                    f"{panel_id}: characters must be exactly {sorted(expected_characters)}"
                )
            for character_id in sorted(set(characters) & expected_characters):
                scores = characters[character_id]
                if not isinstance(scores, dict):
                    problems.append(f"{panel_id}/{character_id}: scores must be a JSON object")
                    continue
                if set(scores) != set(CONSISTENCY_DIMENSIONS):
                    problems.append(
                        f"{panel_id}/{character_id}: every consistency dimension is required"
                    )
    for panel_id, character_id, dimension, score in _scorecard_scores(scorecard):
        if dimension not in CONSISTENCY_DIMENSIONS or score is None:
            continue
        location = f"{panel_id}/{character_id}/{dimension}"
        if isinstance(score, bool) or not isinstance(score, int):
            problems.append(f"{location}: score must be an integer or null")
        elif not SCORE_SCALE["min"] <= score <= SCORE_SCALE["max"]:
            problems.append(
                f"{location}: score must be between {SCORE_SCALE['min']} and "
                f"{SCORE_SCALE['max']}"
            )
    scored = any(
        score is not None for _, _, _, score in _scorecard_scores(scorecard)
    )
    review = scorecard.get("review")
    if scored and not isinstance(review, dict):
        problems.append("a scored scorecard must record review provenance")
    elif scored and not (
        str(review.get("reviewer") or "").strip() and str(review.get("method") or "").strip()
    ):
        problems.append("a scored scorecard must name review.method and review.reviewer")
    if problems:
        raise ScorecardError("; ".join(problems))
    return scorecard


def _aggregate(entries):
    """Return one deterministic aggregate over scored entries only."""
    scores = [entry["score"] for entry in entries if entry["score"] is not None]
    return {
        "max": max(scores) if scores else None,
        "mean": round(sum(scores) / len(scores), 3) if scores else None,
        "min": min(scores) if scores else None,
        "scored": len(scores),
        "total": len(entries),
    }


def _group(entries, key):
    """Return aggregates grouped by one entry field, ordered by group name."""
    groups = {}
    for entry in entries:
        groups.setdefault(entry[key], []).append(entry)
    return {name: _aggregate(items) for name, items in sorted(groups.items())}


def summarize_scorecard(scorecard):
    """Return per-dimension, per-view, and per-character aggregates of a scorecard.

    Aggregation is arithmetic over whatever a reviewer recorded. It never compares a
    score against a threshold, and an unscored dimension is reported as unscored
    rather than counted as a zero.
    """
    validate_scorecard(scorecard)
    entries = [
        {
            "character": character_id,
            "dimension": dimension,
            "panel": panel_id,
            "score": score,
            "view": MATRIX_BY_PANEL[panel_id]["view"],
        }
        for panel_id, character_id, dimension, score in _scorecard_scores(scorecard)
    ]
    scored = [entry for entry in entries if entry["score"] is not None]
    return {
        "by_character": _group(entries, "character"),
        "by_dimension": _group(entries, "dimension"),
        "by_view": _group(entries, "view"),
        "complete": len(scored) == len(entries),
        "definition_sha256": scorecard["definition_sha256"],
        "overall": _aggregate(entries),
        "review": scorecard.get("review", {}),
        "scale": SCORE_SCALE,
    }


def panel_qa_assessments(scorecard, panel_id):
    """Convert one scored CS-013 panel into provider-neutral QA assessments.

    This repair-oriented projection does not alter the benchmark's advisory summary:
    score 4 passes, score 3 is a warning, and scores 0-2 require regeneration.
    """
    validate_scorecard(scorecard)
    panels = scorecard["panels"]
    if panel_id not in panels:
        raise ScorecardError(f"scorecard has no panel {panel_id!r}")
    scores_by_character = panels[panel_id]["characters"]
    ordered_ids = [
        character["id"]
        for character in CHARACTER_BIBLE["characters"]
        if character["id"] in scores_by_character
    ]
    assessments = []
    for character_id in ordered_ids:
        scores = scores_by_character[character_id]
        for dimension, trait in QA_TRAITS_BY_DIMENSION:
            score = scores[dimension]
            if score is None:
                raise ScorecardError(
                    f"{panel_id}/{character_id}/{dimension}: score is required for QA"
                )
            if score == SCORE_SCALE["max"]:
                result, severity = "pass", "error"
            elif score == SCORE_SCALE["max"] - 1:
                result, severity = "warning", "warning"
            else:
                result, severity = "fail", "error"
            assessments.append({
                "character_id": character_id,
                "evidence": (
                    f"CS-013 score {score}/{SCORE_SCALE['max']}: "
                    f"{SCORE_SCALE['labels'][str(score)]}"
                ),
                "result": result,
                "severity": severity,
                "trait": trait,
            })
    return assessments


# --- materialization and reporting -----------------------------------------


def build_consistency_project(root):
    """Materialize the benchmark project and its sidecars, returning its directory.

    The project advances to ``STORYBOARDED`` so it validates without a provider call
    or a committed raster, and carries one prompt per panel so a reviewer with an
    image capability can render the matrix without rewriting the definition.
    """
    root = Path(root)
    project = init_project(
        root,
        TITLE,
        SOURCE.encode("utf-8"),
        {"language": LANGUAGE, "mode": INPUT_MODE, "title": TITLE},
    )
    atomic_write_json(project / "plan/story-plan.json", STORY)
    atomic_write_json(project / "plan/character-bible.json", CHARACTER_BIBLE)
    atomic_write_json(project / "plan/storyboard.json", STORYBOARD)

    prompts = project / "prompts/panels"
    prompts.mkdir(parents=True, exist_ok=True)
    for panel_id in PANEL_IDS:
        (prompts / f"{panel_id}.txt").write_text(panel_prompt(panel_id), encoding="utf-8")

    manifest = read_json(project / "project.json")
    descriptors = {
        "character_bible": "plan/character-bible.json",
        "story_plan": "plan/story-plan.json",
        "storyboard": "plan/storyboard.json",
    }
    manifest.update({
        "project_id": SCENARIO,
        "title": TITLE,
        "panels": list(PANEL_IDS),
        "artifacts": {
            name: {"path": relative, "sha256": sha256_file(project / relative)}
            for name, relative in descriptors.items()
        },
        "settings": {
            **manifest["settings"],
            "page_count": len(STORYBOARD["pages"]),
            "panel_count": len(PANEL_IDS),
        },
        "input": {
            **manifest["input"],
            "source_sha256": sha256_file(project / "source/input.txt"),
        },
    })
    atomic_write_json(project / "project.json", manifest)
    transition(project, "PLANNED")
    transition(project, "SCRIPTED")
    transition(project, "STORYBOARDED")

    # Both sidecars stay outside the project boundary so the project itself contains
    # only artifacts the engine recognizes.
    atomic_write_json(root / f"{SCENARIO}.benchmark.json", consistency_metadata())
    atomic_write_json(root / f"{SCENARIO}.scorecard.json", scorecard_template())
    return project


def storyboard_validation(root):
    """Materialize the benchmark under ``root`` and return its validation result."""
    project = build_consistency_project(root)
    try:
        require_valid_project(project, "storyboard")
    except ProjectValidationError as error:
        return {
            "issues": [
                f"{issue.path}:{issue.field}: {issue.message}" for issue in error.issues
            ],
            "result": "fail",
            "stage": "storyboard",
        }
    return {"result": "pass", "stage": "storyboard"}


def build_baseline_report(engine_version=ENGINE_VERSION):
    """Return the baseline report for one engine revision."""
    with tempfile.TemporaryDirectory() as raw:
        validation = storyboard_validation(Path(raw))
    return {
        "benchmark": SCENARIO,
        "definition_sha256": definition_digest(),
        "engine_version": engine_version,
        "evidence_mode": "structural",
        "generated_by": "python -m tests.consistency_benchmark baseline",
        "kind": BASELINE_KIND,
        "project_validation": validation,
        "schema_version": SCHEMA_VERSION,
        "structural": structural_baseline(),
        "visual": visual_plane(),
    }


def main(argv=None):
    """Materialize, score, or report the character consistency benchmark."""
    parser = argparse.ArgumentParser(prog="tests.consistency_benchmark")
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser(
        "materialize", help="write the benchmark project, metadata, and scorecard"
    )
    materialize.add_argument("output_root", type=Path)
    scorecard = commands.add_parser("scorecard", help="write an unscored scorecard")
    scorecard.add_argument("output_path", type=Path)
    baseline = commands.add_parser("baseline", help="write the baseline report")
    baseline.add_argument("output_path", type=Path)
    summarize = commands.add_parser("summarize", help="summarize a scored scorecard")
    summarize.add_argument("scorecard_path", type=Path)
    qa_results = commands.add_parser(
        "qa-results", help="emit actionable per-trait QA results for one panel"
    )
    qa_results.add_argument("scorecard_path", type=Path)
    qa_results.add_argument("panel_id")
    arguments = parser.parse_args(argv)

    if arguments.command == "materialize":
        arguments.output_root.mkdir(parents=True, exist_ok=True)
        project = build_consistency_project(arguments.output_root)
        print(f"{SCENARIO}\t{project}")
        return 0
    if arguments.command == "scorecard":
        arguments.output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(arguments.output_path, scorecard_template())
        print(arguments.output_path)
        return 0
    if arguments.command == "baseline":
        arguments.output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(arguments.output_path, build_baseline_report())
        print(arguments.output_path)
        return 0

    # Reading and parsing belong inside the handled path: a missing file is a far
    # more likely failure than an invalid score, and both deserve one diagnostic.
    try:
        scorecard_document = load_scorecard(arguments.scorecard_path)
        result = (
            panel_qa_assessments(scorecard_document, arguments.panel_id)
            if arguments.command == "qa-results"
            else summarize_scorecard(scorecard_document)
        )
    except ScorecardError as error:
        print(f"invalid scorecard: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual materialization
    raise SystemExit(main())
