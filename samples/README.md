# Official Comic Sol examples

These are the reference projects for "what does Comic Sol actually produce, and how
is the result organized?" Each one is a real project directory, not a screenshot,
so every editable artifact — story plan, character bible, storyboard, prompts, QA
records — can be opened and diffed.

Start with the catalog, then read the evidence tiers before drawing conclusions
about image quality from any example.

## Catalog

| Example | Pages | Panels | Layouts | Cast | Tier |
|---|---:|---:|---|---:|---|
| [`first-light-signal`](first-light-signal) | 1 | 3 | `hero-top-two-bottom` | 1 | Deterministic |
| [`sunlight-courier`](sunlight-courier) | 2 | 4 | `two-horizontal` ×2 | 1 | Live-generated |
| [`the-quiet-ledger`](the-quiet-ledger) | 4 | 11 | `full-page`, `three-horizontal`, `four-grid`, `two-top-hero-bottom` | 2 | Deterministic |

`first-light-signal` is the smallest complete project. `the-quiet-ledger` is the
largest supported scope: four pages, eleven panels, two recurring characters, four
distinct layouts, and one authored sound effect.

## Evidence tiers

The two tiers exist because a comic project contains two very different kinds of
claim, and mixing them would let placeholder geometry masquerade as illustration.

### Live-generated

`sunlight-courier` contains real generated artwork from an image capability, and
its final PDF and page PNGs are tracked in Git. This is the tier to look at for
visual output. It is also the expensive tier: its rasters dominate the repository's
sample weight, which is why there is exactly one of them.

Its canonical clean panel art is tracked once at `panels/<panel-id>/clean.png`.
Raw and legacy-clean compatibility copies are generated locally and ignored by
Git; see that project's README and `scripts/materialize_sample.py`.

### Deterministic

`first-light-signal` and `the-quiet-ledger` commit **only editable inputs**:

```text
example.json              build contract: pages, panels, layouts, seed
source/input.txt          the story source
source/request.json       the recorded request
plan/story-plan.json      story plan
plan/character-bible.json character bible with visual fingerprints
plan/storyboard.json      pages, layouts, panel rectangles, and authored text
prompts/panels/*.txt      the editable per-panel image prompts
```

Everything else — panel rasters, lettered panels, composed pages, QA records, the
composition cache, the PDF — is produced on demand by
[`scripts/build_examples.py`](../scripts/build_examples.py), which synthesizes
panel artwork locally from the example seed and never contacts an image provider.

**A deterministic build proves mechanics, not illustration quality.** It exercises
schema validity, lettering geometry, balloon tail direction, page composition,
export, hashing, and full-project validation. Its panel artwork is flat geometric
placeholder — deliberately so, because generated panel art must be text-free and a
placeholder cannot smuggle in glyphs. Each built project records this boundary in
`qa/evidence.json` and the rendered `qa/report.md`:

```text
- Mode: deterministic
- Scope: mechanics-only
- Claim boundary: deterministic evidence proves mechanics only and does not prove live visual quality.
```

#### Deterministic builds finish as `COMPLETE_WITH_WARNINGS`, on purpose

A placeholder raster has no cast, no faces, and no staging, so most QA checks have
nothing to look at. Rather than record them as passing, the builder records them as
**unreviewed warnings**:

| Record | Passes | Recorded as unreviewed warning |
|---|---|---|
| Panel QA | `text-free`, `technical` | `character-identity`, `anatomy`, `action`, `composition`, `continuity` |
| Page QA | the four deterministic checks, plus `bubble-tail-direction` | `face-action-obstruction`, `accidental-text-watermark` |

The passes are earned rather than asserted. `technical` is recorded only after the
clean raster is opened and measured as opaque RGB at the exact storyboard
rectangle; `text-free` follows from the generator loading no font and drawing only
geometry; and `bubble-tail-direction` is machine-checked, because page-QA
construction re-derives the expected tail regions from the storyboard and the
placed lettering geometry and rejects anything stale or incomplete.

Each unreviewed check carries evidence beginning `Not reviewed:` that says exactly
what could not be assessed and why. Those reasons propagate into the panel
decision (`accept-warning`), the manifest warnings, and the report's unresolved
warnings section. The project still exports a verified PDF and still passes
`--stage final`; it simply terminates as `COMPLETE_WITH_WARNINGS` instead of
claiming a visual review nobody performed.

This is also a useful thing to have published: it is the reference example of what
an honestly accepted-with-warnings project looks like.

## Build the deterministic examples

From the repository root, with Python 3.11+ and `Pillow==12.3.0` installed:

```bash
"$PYTHON" scripts/build_examples.py
```

Windows PowerShell:

```powershell
& $PYTHON -3 scripts\build_examples.py
```

Each example is built into `build/examples/<example-id>/` (ignored by Git) and is
validated at the `final` stage before the command reports success:

```text
first-light-signal: COMPLETE_WITH_WARNINGS pages=1 panels=3 pdf=exports/first-light-signal.pdf (256 KiB)
the-quiet-ledger: COMPLETE_WITH_WARNINGS pages=4 panels=11 pdf=exports/the-quiet-ledger.pdf (1029 KiB)
```

`COMPLETE_WITH_WARNINGS` is the expected outcome, not a defect — see the
deterministic tier above for why.

Build one example, or send the output somewhere else:

```bash
"$PYTHON" scripts/build_examples.py --example the-quiet-ledger
"$PYTHON" scripts/build_examples.py --output-root /tmp/comic-sol-examples
```

A built project is a normal Comic Sol project. Re-validate it directly:

```bash
"$PYTHON" scripts/validate_project.py build/examples/the-quiet-ledger --stage final
```

## Sample weight policy

Generated rasters are large and compress poorly, so this directory keeps exactly
one tracked live-generated project and derives everything else:

- Tracked binaries are limited to the single live-generated example.
- Deterministic examples track no rasters at all; a full four-page build is about
  2.5 MB and is produced locally in seconds.
- No example requires a provider call, a credential, or a downloaded asset to
  build.
- Duplicate panel copies are materialized locally rather than committed.

## Editing an example

Deterministic examples are meant to be edited. The loop is short because the
builder re-validates every time:

1. Change something committed — a caption, a panel rectangle, a fingerprint.
2. Run `scripts/build_examples.py --example <example-id>`.
3. Read the failure, or inspect the new `pages/page-001.png` and `qa/report.md`.

The builder fails closed on contract drift. If a page's rectangles stop matching
its declared `layout`, or the storyboard's page and panel counts stop matching
`example.json`, the build reports that mismatch instead of producing a project
that quietly disagrees with its own contract.

For the artifact rules these examples follow, see
[`references/schemas.md`](../references/schemas.md).
