# Character consistency baselines

Each `baseline-v<version>.json` file is the character consistency benchmark report for
one engine revision, written by:

```bash
python -m tests.consistency_benchmark baseline benchmarks/consistency/baseline-v<version>.json
```

A report carries two planes. `structural` holds measured, definition-derived numbers and
the storyboard-stage validation result; `visual` holds identity scores and stays
`"scored": false` until a reviewer renders the twelve panels with a real image capability.
Subjective scores belong in a scorecard file, not here, and never in CI.

See [`docs/character-consistency-benchmark.md`](../../docs/character-consistency-benchmark.md)
for the definition, the matrix, the scoring scale, and the rerun contract.
