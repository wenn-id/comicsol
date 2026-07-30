# Comic Quality deterministic matrix

The matrix is generated programmatically by `tests.support.build_quality_fixture`.
No provider call, downloaded asset, credential, or committed large binary is required.
Every scenario writes a `quality-fixture.json` record with a fixed seed, the covered
dimensions, `local_only: true`, and `evidence_mode: deterministic`.

## Scenarios

| Scenario | Coverage |
| --- | --- |
| `continuity-pair` | Two recurring characters; wardrobe, prop, and palette continuity |
| `layout-registry` | Single, two-horizontal, two-vertical, three-top, three-bottom, and four-grid layouts |
| `dense-text` | Dense dialogue, caption, and authored SFX mechanics |
| `orientations` | Portrait and landscape normalization inputs |
| `image-formats` | PNG, JPEG, WebP, and EXIF-oriented JPEG |
| `typography` | Regular, bold, combining marks, and non-Latin fallback policy |
| `retry-paths` | Transient repeat and visual retry accounting |
| `terminal-outcomes` | Accepted warning and hard failure reporting |
| `interrupted-resume` | Deterministic interruption and downstream-only resume |

## Evidence boundary

Deterministic fixtures prove mechanics, provenance binding, rollback, retry accounting,
resume behavior, and artifact integrity. They do **not** prove live visual quality.

A live visual record is allowed only for an already retained local attempt. It must name
the provider and model, attempt SHA-256, references, reviewer method, and known
limitations. `scripts/quality_sample.py` never invokes a provider.

```bash
python scripts/quality_sample.py PROJECT_DIR --mode deterministic
python scripts/quality_sample.py PROJECT_DIR --mode live-visual \
  --retained-attempt panels/raw/p01-01/attempt-001.png \
  --provider PROVIDER --model MODEL \
  --reviewer-method bounded-visual-review \
  --reference references/characters/aria.png \
  --limitation "Describe the known limitation"
```

Run the matrix with:

```bash
python -m unittest tests.test_quality_matrix -v
```
