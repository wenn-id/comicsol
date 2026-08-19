# Comic Sol benchmark framework

Quality work needs a scoreboard. Without one, "the panels look better now" is an
opinion and a regression between two releases is invisible. `scripts/benchmark.py`
runs a fixed set of benchmark cases through the real lifecycle engine and reports
six comparable metrics per case, in a machine-readable format that two engine
revisions can be diffed against.

## What the harness proves, and what it does not

Deterministic runs synthesize their own panel rasters, so they measure **pipeline,
geometry, and provenance mechanics only**. They never claim visual quality:

- Panel checks that need a real render (`character-identity`, `anatomy`, `action`,
  `composition`, `continuity`) are recorded as unresolved warnings, so a
  deterministic run honestly terminates in `COMPLETE_WITH_WARNINGS`.
- Every result carries `evidence.proves_visual_quality: false` and an explicit
  `limitations` list, matching the labeling contract in `scripts/quality_sample.py`.
- Panel `text-free` and `technical` checks *are* proven: the harness draws no glyphs
  and the engine decodes and dimension-verifies each raster before promoting it.

Visual-quality scoring for arbitrary art styles is deliberately out of scope.

## Benchmark project contract

A benchmark case is a JSON contract in `benchmarks/cases/<case-id>.json`. It names
an in-repo project fixture and the mechanics the run must exercise. Every field is
required and validated by `validate_case()` before a run starts.

| field | meaning |
| --- | --- |
| `schema_version` | contract version, currently `"1.0"` |
| `kind` | always `"benchmark-case"` |
| `case_id` | stable slug; must equal the file stem |
| `title` | project title passed to `init_project` |
| `evidence_mode` | `"deterministic"` or `"live-visual"` |
| `seed` | integer seed for raster synthesis; makes runs reproducible |
| `fixture` | repository-relative directory holding `source/`, `plan/`, and `prompts/panels/` |
| `panels` | ordered canonical panel IDs; must match the fixture storyboard |
| `page_count` | expected composed page count |
| `dialogue_count` | expected dialogue items; the run fails closed if the fixture disagrees |
| `repair_panels` | panels that receive a scripted `visual_retry`, exercising repair accounting |
| `resume_stage` | stage the resume drill invalidates (`lettering`, `composition`, or `export`) |

A fixture must supply `source/input.txt`, `source/request.json`, the three
`plan/*.json` artifacts, and one `prompts/panels/<panel-id>.txt` per panel. Fixture
paths may not escape the fixture root.

## Metrics

| metric | numerator / denominator | direction |
| --- | --- | --- |
| `pipeline_success` | project reached a terminal status **and** passed `require_valid_project(final)` | higher is better |
| `resume_success` | the resume drill preserved every upstream stage, invalidated exactly the downstream stages, and refinalized to a valid terminal state | higher is better |
| `repair_rate` | extra generation calls (`logs/generation-counters.json`) per panel | **lower is better** |
| `panel_acceptance` | panels whose QA decision is `accept` or `accept-warning` | higher is better |
| `dialogue_correctness` | passing `clipped-text`, `text-overlap`, and `reading-order` page checks plus per-dialogue bounded tail regions | higher is better |
| `export_success` | pages verified in `exports/pdf-verification.json`, bound to the exported PDF digest | higher is better |

Dialogue tail regions are not reviewer opinions: `tail_direction_result()` verifies
each tail attaches to its balloon, points at the authored `speaker_anchor`, keeps a
positive `source_gap`, and stays inside the panel bounds.

## Running a benchmark

```sh
# list registered cases and metric IDs
python scripts/benchmark.py --list

# run every registered case
python scripts/benchmark.py --all \
  --output-root build/benchmark/projects \
  --results build/benchmark/results
```

Each run writes `build/benchmark/results/result-<case-id>.json`. Records exclude
timestamps and filesystem paths, so **repeated deterministic runs of one case are
byte-identical** and can be compared directly. Every record carries the revision it
is accountable to: `engine_version`, `git_revision`, the harness version, and the
project's `stage_versions`. A run that raises still publishes a `failed` record with
the exception, so CI never loses evidence.

The exit code is `0` only when every case passes.

## Optional live-provider runs

The engine keeps image generation in the agent capability plane, so the harness
never calls a provider itself. A live run consumes rasters you already retained and
requires explicit provenance:

```sh
python scripts/benchmark.py --case benchmarks/cases/mini-comic.json \
  --output-root build/benchmark/projects \
  --results build/benchmark/results \
  --attempt-root path/to/retained/rasters \
  --provider example-provider --model example-model \
  --reviewer-method "human bounded visual review" \
  --limitation "single reviewer, single locale"
```

Rasters are read as `<panel-id>-<revision>.png`, falling back to `<panel-id>.png`.
Set the case's `evidence_mode` to `live-visual`; the harness then upgrades the
subjective panel and page checks to reviewer assertions and marks the result
`proves_visual_quality: true`. Live results are not byte-reproducible.

## Diffing two engine revisions

Run the same cases from two checkouts, then diff the result directories:

```sh
git worktree add ../comicsol-baseline <baseline-rev>
(cd ../comicsol-baseline && python scripts/benchmark.py --all \
  --output-root /tmp/baseline/projects --results /tmp/baseline/results)

python scripts/benchmark.py --all \
  --output-root /tmp/candidate/projects --results /tmp/candidate/results

python scripts/benchmark.py \
  --baseline /tmp/baseline/results \
  --candidate /tmp/candidate/results \
  --diff-output build/benchmark/diff.json
```

The diff writes `diff.json` plus a reviewable `diff.md` and exits non-zero unless
the decision is `NO REGRESSION`. It fails closed on:

- any metric that moved the wrong way beyond `--tolerance` (default `0.0`),
- a candidate case whose own status is not `passed`,
- a case present in the baseline but missing from the candidate,
- a case whose `case_sha256` changed, meaning the contract itself moved and the two
  runs are not comparable,
- unreadable, foreign, or duplicated result records.

`.github/workflows/benchmark.yml` runs this end to end: it benchmarks the merge base
and the current revision, uploads both result sets, and gates on the diff.

## Adding a case

1. Add a fixture directory (or reuse an existing one) with the required files.
2. Add `benchmarks/cases/<case-id>.json` following the contract above.
3. Run `python scripts/benchmark.py --case benchmarks/cases/<case-id>.json ...` and
   confirm the case passes and its metrics are stable across two runs.
4. Run `python -m unittest tests.test_benchmark -v`; the registered-case test
   validates every contract in `benchmarks/cases/`.

Changing an existing case is a contract change: bump nothing, but expect the diff to
report `benchmark case contract changed between runs` until both sides are rerun.
