# Character consistency benchmark

Repeated character identity is the known generative weakness of this pipeline. Panel
QA already records a `character-identity` check per panel, but a check per panel
cannot answer the release question: *is the same character still the same character
across camera angles, expressions, lighting, and backgrounds?* Without a baseline,
"consistency is better now" is an opinion.

This benchmark is that baseline. It defines one project in which **only** the camera,
the expression, the pose, the light, and the background change. The characters do not:
every immutable trait is owned by the character bible and restated verbatim in every
panel prompt, so a difference between two rendered panels is a defect of the render
rather than a difference of instruction.

Definitions live in `tests/consistency_benchmark.py`; `tests/test_consistency_benchmark.py`
validates the definition, the project, and the scorecard mechanics on every pull request.

## Two evidence planes

| Plane | Question | Who answers | In CI |
| --- | --- | --- | --- |
| Structural | does the benchmark still pin identity and validate under the current schema? | deterministic tests | yes |
| Visual | did the render keep the character? | a human or a model looking at panels | never |

The structural plane is asserted on every pull request: matrix coverage, trait
immutability, invariant pinning, prompt restatement, storyboard-stage schema validity,
rerun determinism, and the arithmetic of the scorecard summary. The visual plane is a
judgement, so it lives in a scorecard file that CI reads for shape and never for score.
Stress tags: `identity:repeat-character`, `views:five-camera-views`,
`expression:nine-variations`, `lighting:four-conditions`, `background:four-locations`,
`scoring:manual-or-model-assisted`.

## Canonical characters

Two characters carry the whole benchmark, and both appear in all five views. Traits are
never restated by the benchmark itself: each consistency dimension reads one
character-bible field, so the definition cannot drift away from the artifact the engine
validates.

| Dimension | Character-bible source |
| --- | --- |
| `face` | `visual_fingerprint.face` |
| `hair` | `visual_fingerprint.hair` |
| `age` | `age_band` |
| `clothing` | `visual_fingerprint.wardrobe` |
| `accessories` | `visual_fingerprint.signature_props` |
| `proportions` | `visual_fingerprint.silhouette` |
| `signature-traits` | `visual_fingerprint.invariants` |

`rani` is a salvage diver with a crescent scar, a blunt chin-length bob, an olive field
jacket, and a brass compass on a bootlace. `bayu` is a deck hand with a chipped front
tooth, tight cropped curls, faded blue coveralls, and yellow ear-defenders. Every panel
pins every invariant of every character in the frame as a storyboard `continuity` entry,
which is exactly the field panel QA rechecks: 60 pins across 12 panels, with no panel
leaving an invariant unpinned.

## The matrix

Four backgrounds double as four lighting conditions: `reference-studio`
(`even-neutral-daylight`), `harbor-noon` (`hard-noon-sun`), `engine-shed`
(`single-lamp-low-light`), and `rain-night-market` (`cold-rim-and-red-glow`).

Page 1 is the control. It holds one background, one light, and one expression while the
camera walks around the character, so a drift on page 1 is a pure view failure. Pages 2
and 3 then change light, background, expression, and pose together.

| Panel | View | Expression | Background | Lighting condition | Characters |
| --- | --- | --- | --- | --- | --- |
| `p01-01` | `front` | neutral | `reference-studio` | `even-neutral-daylight` | rani |
| `p01-02` | `profile` | neutral | `reference-studio` | `even-neutral-daylight` | rani |
| `p01-03` | `three-quarter` | neutral | `reference-studio` | `even-neutral-daylight` | rani |
| `p01-04` | `full-body` | neutral | `reference-studio` | `even-neutral-daylight` | rani |
| `p02-01` | `close-up` | delighted | `harbor-noon` | `hard-noon-sun` | rani |
| `p02-02` | `three-quarter` | alarmed | `harbor-noon` | `hard-noon-sun` | rani, bayu |
| `p02-03` | `full-body` | braced | `harbor-noon` | `hard-noon-sun` | rani, bayu |
| `p02-04` | `front` | wry | `harbor-noon` | `hard-noon-sun` | bayu |
| `p03-01` | `close-up` | exhausted | `engine-shed` | `single-lamp-low-light` | rani |
| `p03-02` | `profile` | focused | `engine-shed` | `single-lamp-low-light` | bayu |
| `p03-03` | `close-up` | furious | `rain-night-market` | `cold-rim-and-red-glow` | rani, bayu |
| `p03-04` | `front` | relieved | `rain-night-market` | `cold-rim-and-red-glow` | rani |

Every view is exercised under at least two lighting conditions and two backgrounds, so a
view never gets to pass under a single flattering light. No panel authors any text at
all: dialogue, captions, and SFX are prohibited so nothing occludes the face being
scored, and lettering never becomes a variable of an identity score.

## Scoring

Scoring is manual or model-assisted, and it is deliberately outside CI.

1. `python -m tests.consistency_benchmark materialize OUTPUT_ROOT` writes the
   plan-complete project, `prompts/panels/<panel-id>.txt` for all twelve panels, a
   metadata sidecar, and an unscored scorecard.
2. Render every panel prompt with one image capability, keeping provider, model, and
   references constant across the twelve panels.
3. Score every dimension of every character in every panel from 0 ("a different
   character") to 4 ("indistinguishable from the canonical trait"), and fill
   `review.reviewer` and `review.method`. A scorecard with scores and no reviewer is
   rejected: an unattributable score is not evidence.
4. `python -m tests.consistency_benchmark summarize SCORECARD_PATH` reports means per
   dimension, per view, and per character, over scored entries only. An unscored
   dimension is reported as unscored, never averaged in as a zero.
5. `python -m tests.consistency_benchmark qa-results SCORECARD_PATH PANEL_ID` emits the
   seven provider-neutral assessment records consumed by character QA. Score 4 is a pass,
   score 3 is a warning, and scores 0–2 are hard failures with actionable repair guidance
   added when the records are built into the panel's `character-identity` check. This
   repair projection does not turn the benchmark summary into a release gate.

A scorecard records the digest of the definition it was scored against. Change the
matrix, the characters, or a prompt and an old scorecard is refused rather than silently
compared, because two scores are only comparable when the input definition is identical.
That is also the rerun contract: a consistency change is measured by rendering and
scoring the same definition again, never by editing the definition.

## Baseline

`benchmarks/consistency/baseline-v2.0.0rc4.json` is the baseline for the current
release. It records the structural plane as measured numbers — 12 panels, 3 pages, 2
characters, 4 backgrounds, 4 lighting conditions, 9 expressions, 60 invariant pins, 105
trait restatements, `storyboard` validation `pass` — and records the visual plane as
explicitly **unscored**, with its limitations named: no image provider runs in CI, so no
panel of this baseline was rendered.

That honesty is the point. A committed number that nobody measured would make the next
comparison meaningless. For the same reason the engine revision under test has to carry
its own baseline: the suite requires `baseline-v<current version>.json` to exist and to
describe the current definition, so a version bump without a regenerated baseline fails
instead of inheriting an older one. Baselines for earlier revisions stay as history.
Regenerate a baseline for a release with:

```bash
python -m tests.consistency_benchmark baseline benchmarks/consistency/baseline-v<version>.json
```

## Relationship to the other benchmarks

- [`docs/benchmark-corpus.md`](benchmark-corpus.md) answers "which comic shapes must the
  pipeline handle at all", with one project per production scenario.
- [`docs/benchmark.md`](benchmark.md) answers "did this engine revision regress", with six
  deterministic pipeline metrics per case.
- This benchmark answers "is the same character still the same character", which no
  deterministic run can answer, so it keeps its subjective plane in a scorecard.

Implementing a consistency improvement is out of scope here. This is the instrument the
improvement will be measured with.

## Commands

```bash
# validate the benchmark definition, project, scorecard, and baseline
python -m unittest tests.test_consistency_benchmark -v

# materialize the project, prompts, metadata, and an unscored scorecard
python -m tests.consistency_benchmark materialize /tmp/comic-sol-consistency

# emit a standalone scorecard, or summarize a scored one
python -m tests.consistency_benchmark scorecard /tmp/consistency-scorecard.json
python -m tests.consistency_benchmark summarize /tmp/consistency-scorecard.json
python -m tests.consistency_benchmark qa-results /tmp/consistency-scorecard.json p01-01
```
