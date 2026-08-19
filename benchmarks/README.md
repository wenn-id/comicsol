# Benchmark cases

Each JSON file in `cases/` is one benchmark project contract consumed by
`scripts/benchmark.py`. A contract names an in-repo project fixture and the
mechanics a run must exercise; it never contains prose, thresholds, or expected
metric values, so results stay comparable when the engine changes.

See [`docs/benchmark.md`](../docs/benchmark.md) for the field-by-field contract, the
six reported metrics, how to run a benchmark, how to diff two engine revisions, and
how `scripts/benchmark_summary.py` folds a directory of result records into one
compact, version-tagged summary suitable for release notes or a CI artifact.

`consistency/` holds character consistency baselines instead of case contracts. That
benchmark measures repeated character identity across views and lighting, keeps its
subjective scores out of CI, and is documented in
[`docs/character-consistency-benchmark.md`](../docs/character-consistency-benchmark.md).
A summary can fold a baseline or a scored scorecard in as reported, never gated,
character-consistency metrics.
