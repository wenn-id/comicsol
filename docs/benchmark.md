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
| `dialogue_correctness` | passing `clipped-text`, `text-overlap`, `reading-order`, `balloon-subject-obstruction`, and `bubble-tail-geometry` page checks plus per-dialogue bounded tail regions | higher is better |
| `export_success` | pages verified in `exports/pdf-verification.json`, bound to the exported PDF digest | higher is better |

The counted set is every deterministic, error-severity page check that verifies
dialogue geometry, so the metric measures the dialogue correctness the engine
enforces rather than a narrower subset of it. Only pages carrying at least one
`dialogue` text item contribute, and a check absent from a page record is skipped
instead of counted as a failure.

`balloon-crowding` is deliberately **excluded**. It never fails: it reports `pass` or
a warning-severity `warning`, because crowded lettering is a reading-comfort hint, not
a defect, and a crowded page is still accepted as `accept-warning`. Counting it would
score a comfortable page 1/1 and a merely tight one 0/1, conflating reading comfort
with correctness and moving the metric on pages the engine accepts.

Dialogue tail regions are not reviewer opinions: `tail_direction_result()` verifies
each tail attaches to its balloon, points at the authored `speaker_anchor`, keeps a
positive `source_gap`, and stays inside the panel bounds. `bubble-tail-geometry` is
the same verdict recomputed inside the pipeline, so a tail regression moves both the
per-dialogue regions and the page check.

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

The harness version identifies the measuring instrument, not the record layout. It is
bumped whenever a metric's numerator or denominator is redefined, because such a record
still validates against an unchanged schema while no longer meaning what it meant
before. Both the summary and the diff refuse to mix harness versions, so a stale
archived baseline fails closed instead of reporting a definition change as a metric
change. Re-run the benchmark to compare across such a bump; the workflow already
benchmarks the baseline revision with the current harness, so CI needs no action.

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
  --review-assertions path/to/review-assertions.json \
  --limitation "single reviewer, single locale"
```

Rasters are read as `<panel-id>-<revision>.png`, falling back to `<panel-id>.png`.
Set the case's `evidence_mode` to `live-visual`. The assertions file must contain
one passing, non-empty evidence assertion for every panel/check, for example:

```json
{
  "p01-01": {
    "character-identity": {"result": "pass", "evidence": "reviewed identity"},
    "anatomy": {"result": "pass", "evidence": "reviewed anatomy"},
    "action": {"result": "pass", "evidence": "reviewed action"},
    "composition": {"result": "pass", "evidence": "reviewed composition"},
    "continuity": {"result": "pass", "evidence": "reviewed continuity"},
    "text-free": {"result": "pass", "evidence": "reviewed text-free art"},
    "technical": {"result": "pass", "evidence": "reviewed technical output"}
  }
}
```

The harness then upgrades the subjective panel and page checks to reviewer
assertions and marks the result `proves_visual_quality: true`. Live results are not
byte-reproducible.

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
- a case present only in the candidate (new benchmark coverage must be explicitly
  established against a baseline),
- a case whose `case_sha256` changed, meaning the contract itself moved and the two
  runs are not comparable,
- unreadable, foreign, malformed, or duplicated result records.

`.github/workflows/benchmark.yml` runs this end to end: it benchmarks the merge base
and the current revision, uploads both result sets, and gates on the diff.

## Summary report

A directory of result records is complete evidence and an unreadable report. Nobody
qualifying a release reads six metrics across every case and concludes anything from
them, so `scripts/benchmark_summary.py` folds one revision's records into one
compact, version-tagged summary:

```sh
python scripts/benchmark_summary.py \
  --results build/benchmark/results \
  --output build/benchmark/summary.json
```

The summary is a separate tool on purpose. Result records are the interface, so a
summary can be produced from results that already exist — an archived CI artifact, or
a baseline benchmarked from another worktree — without rerunning the engine and
without touching the byte-reproducible run path.

It writes `summary.json` and a reviewable `summary.md` beside it (or wherever
`--markdown` points), and:

- pools every metric's numerator and denominator across cases, so `panel_acceptance`
  is accepted panels over **all** panels, `repair_rate` is extra generation calls over
  all panels, `dialogue_correctness` is passing dialogue geometry page checks and
  tail regions over all of them, `export_success` is verified PDF pages over all
  expected pages, and `pipeline_success` and `resume_success` are successful cases
  over all cases. An average of per-case ratios would weight a one-panel case like a
  twelve-panel case; pooling does not,
- carries one `version_tag` of the form `v<engine version>+<git revision>`, taken from
  the records themselves, and fails closed when the records span more than one engine
  revision or a foreign harness version, because those records do not summarize one
  revision,
- keeps the honesty labeling of the records it summarizes: it repeats their
  `limitations` and reports `proves_visual_quality: true` only when every summarized
  record does,
- is byte-identical for two byte-identical deterministic runs.

`status` is `passed` only when every case passed, no summary exception was raised, and
every gating metric is at `1.0`. `repair_rate` is not a gating metric: a case that
declares a scripted repair is supposed to record one, so a non-zero repair rate is
evidence rather than a failure.

`repair_rate` counts extra generation calls, not repair strategies. A selective repair and
a full regeneration each consume one call from the same budget, so the metric measures how
often a panel needed a second attempt, and `panel_acceptance` measures whether that attempt
worked. Which strategy a repair used is recorded per panel in `logs/repair-plan.json`
rather than aggregated here, because the deterministic corpus accepts every panel on its
scripted attempt and would report the same trivial strategy mix for every revision.

### Character consistency metrics

`--consistency-baseline` folds the character consistency baseline
(`benchmarks/consistency/baseline-v<version>.json`) into the summary, and
`--consistency-scorecard` folds a scored scorecard:

```sh
version="$(python -c 'from comic_sol_product import __version__; print(__version__)')"
python scripts/benchmark_summary.py \
  --results build/benchmark/results \
  --consistency-baseline "benchmarks/consistency/baseline-v${version}.json" \
  --consistency-scorecard /tmp/consistency-scorecard.json \
  --output build/benchmark/summary.json
```

Both planes are read from their own published artifacts, never by importing the test
tree, and **neither one gates**:

| metric | numerator / denominator |
| --- | --- |
| `consistency_invariant_pinning` | recorded over expected storyboard invariant pins |
| `consistency_trait_restatement` | recorded over expected per-prompt trait restatements |
| `consistency_visual_coverage` | dimensions a reviewer scored over all scoreable dimensions |
| `consistency_visual_score` | summed scores over the published scale maximum, **over scored entries only** |

The first two restate the structural plane that `tests/test_consistency_benchmark.py`
already asserts. The last two report the visual plane, which is a reviewer judgement:
an unscored dimension is reported as unscored and never averaged in as a zero, a
scored scorecard without `review.reviewer` and `review.method` is refused rather than
summarized, and a scorecard describing a different number of scoreable dimensions than
the baseline is refused because the two are not one definition. See
[`docs/character-consistency-benchmark.md`](character-consistency-benchmark.md).

## Comparing two summaries

The result diff answers "did any case regress". The summary delta answers the release
question — "did this revision get better or worse" — in one table:

```sh
python scripts/benchmark_summary.py \
  --baseline /tmp/baseline/summary.json \
  --candidate /tmp/candidate/summary.json \
  --output build/benchmark/summary-delta.json
```

Every pooled metric is reported as `baseline`, `candidate`, `delta`, and a verdict of
`improved`, `unchanged`, or `regressed` under `--tolerance` (default `0.0`). The delta
writes `summary-delta.json` plus `summary-delta.md`, exits non-zero unless the decision
is `NO REGRESSION`, and fails closed on an unreadable or foreign summary, a candidate
case that is not `passed`, and case sets that differ between the two runs, because
pooled aggregates over different case sets are not comparable.

Character consistency verdicts are reported under `advisory` and never change the
decision. A consistency metric published by only one of the two summaries is listed as
unavailable rather than treated as a change.

### Wiring the summary into CI

`.github/workflows/benchmark.yml` already benchmarks the merge base and the current
revision. Summarize both and gate on the delta so a pull request shows a metric table
instead of a directory of JSON:

```yaml
      - name: Summarize the candidate revision
        shell: bash
        env:
          VERSION: ${{ steps.engine.outputs.version }}
        run: |
          set -euo pipefail
          python scripts/benchmark_summary.py \
            --results benchmark/candidate \
            --consistency-baseline "benchmarks/consistency/baseline-v${VERSION}.json" \
            --output benchmark/candidate-summary.json

      - name: Summarize the baseline revision
        shell: bash
        run: |
          set -euo pipefail
          python scripts/benchmark_summary.py \
            --results benchmark/baseline \
            --output benchmark/baseline-summary.json

      - name: Compare the two summaries
        shell: bash
        run: |
          set -euo pipefail
          python scripts/benchmark_summary.py \
            --baseline benchmark/baseline-summary.json \
            --candidate benchmark/candidate-summary.json \
            --output benchmark/summary-delta.json
          grep -Fq "NO REGRESSION" benchmark/summary-delta.md
```

The baseline is summarized by the current tool from the records it produced, not from
its own worktree: result records are the interface, so a baseline predating the summary
tool still summarizes. Only the candidate folds in a consistency baseline, because the
baseline revision may carry a different engine version and therefore a different
consistency baseline file, and a one-sided consistency metric is advisory anyway.
`cat benchmark/candidate-summary.md benchmark/summary-delta.md >> "$GITHUB_STEP_SUMMARY"`
publishes both tables on the run page; `benchmark/` is already uploaded as the
`benchmark-results` artifact.

## Adding a case

1. Add a fixture directory (or reuse an existing one) with the required files.
2. Add `benchmarks/cases/<case-id>.json` following the contract above.
3. Run `python scripts/benchmark.py --case benchmarks/cases/<case-id>.json ...` and
   confirm the case passes and its metrics are stable across two runs.
4. Run `python -m unittest tests.test_benchmark -v`; the registered-case test
   validates every contract in `benchmarks/cases/`.

Changing an existing case is a contract change: bump nothing, but expect the diff to
report `benchmark case contract changed between runs` until both sides are rerun.
