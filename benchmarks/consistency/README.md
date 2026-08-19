# Character consistency baselines

Each `baseline-v<version>.json` file is the character consistency benchmark report for
one engine revision, written by:

```bash
python -m tests.consistency_benchmark baseline benchmarks/consistency/baseline-v<version>.json
```

The engine revision under test has to carry its own baseline: `tests/test_consistency_benchmark.py`
requires `baseline-v<current version>.json` to exist and to describe the current benchmark
definition, so a version bump without a regenerated baseline fails rather than silently
inheriting an older one. Baselines for earlier revisions stay as history and are only
required to be named for the engine they measured and to state their own evidence boundary.

A report carries two planes. `structural` holds measured, definition-derived numbers and
the storyboard-stage validation result; `visual` holds identity scores and stays
`"scored": false` until a reviewer renders the twelve panels with a real image capability.
Subjective scores belong in a scorecard file, not here, and never in CI.

See [`docs/character-consistency-benchmark.md`](../../docs/character-consistency-benchmark.md)
for the definition, the matrix, the scoring scale, and the rerun contract.
