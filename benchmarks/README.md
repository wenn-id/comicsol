# Benchmark cases

Each JSON file in `cases/` is one benchmark project contract consumed by
`scripts/benchmark.py`. A contract names an in-repo project fixture and the
mechanics a run must exercise; it never contains prose, thresholds, or expected
metric values, so results stay comparable when the engine changes.

See [`docs/benchmark.md`](../docs/benchmark.md) for the field-by-field contract, the
six reported metrics, how to run a benchmark, and how to diff two engine revisions.

`consistency/` holds character consistency baselines instead of case contracts. That
benchmark measures repeated character identity across views and lighting, keeps its
subjective scores out of CI, and is documented in
[`docs/character-consistency-benchmark.md`](../docs/character-consistency-benchmark.md).
