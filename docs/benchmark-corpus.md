# Comic benchmark corpus

The golden project in `tests/golden/mini-comic` proves one deterministic end-to-end run.
It cannot represent the range of layouts and story structures Comic Sol has to handle, so
this corpus adds nine compact, plan-complete comic projects. Each project stresses one
realistic production scenario and states the capability it covers.

Definitions live in `tests/benchmark_corpus.py`; `tests/test_benchmark_corpus.py` validates
every project under the current schema.

## Scenarios

| Scenario | Pages / panels | Capability under test | Stress tags |
| --- | --- | --- | --- |
| `dialogue-heavy` | 1 / 2 | Dense multi-speaker dialogue: three text items per panel, alternating speakers, and explicit human speaker anchors. | `text:dialogue-density`, `text:speaker-anchor`, `characters:pair` |
| `action-sequence` | 1 / 3 | Rapid action beats: three stacked panels, authored sound effects, and motion-led composition with minimal dialogue. | `motion:action-beats`, `text:authored-sfx`, `layout:three-horizontal` |
| `two-character` | 1 / 2 | Sustained two-character staging: alternating speakers, shared scene anchors, and stable pair continuity across panels. | `characters:pair`, `continuity:shared-anchor`, `staging:two-shot` |
| `multi-character` | 1 / 1 | Ensemble framing: four distinct characters in one full-page panel with unique fingerprints and unambiguous speakers. | `characters:ensemble`, `composition:crowded-frame`, `continuity:multi-owner` |
| `silent-manga` | 1 / 4 | Wordless storytelling: a four-panel grid with zero text items and no authored sound effects, carried entirely by staging. | `text:none`, `pacing:silent`, `layout:four-grid` |
| `night-low-light` | 1 / 3 | Night and low-light rendering: single-source dark key lighting, narrow night palettes, and text legibility at low contrast. | `lighting:low-light`, `palette:night`, `contrast:legibility` |
| `long-dialogue` | 1 / 1 | Long-form dialogue close to the 32-word ceiling, paired with a caption inside a full-page panel that has to reserve a tall text column. | `text:long-dialogue`, `text:word-budget`, `layout:full-page` |
| `complex-background` | 1 / 3 | Dense environmental detail: crowded, signage-heavy backgrounds that still reserve text-safe space in every panel. | `background:dense-detail`, `composition:text-safe-space`, `layout:two-top-hero-bottom` |
| `four-page-story` | 4 / 7 | A complete four-page arc: contiguous page numbering, four different layouts, and a beginning-to-resolution structure across seven panels. | `story:four-page-arc`, `layout:mixed`, `pages:contiguous-numbering` |

Across the corpus the nine projects cover every registered layout: `full-page`,
`two-horizontal`, `three-horizontal`, `hero-top-two-bottom`, `two-top-hero-bottom`, and
`four-grid`.

## Size and evidence boundary

Nothing in the corpus is committed as a project tree. Scenarios are authored as data and
materialized on demand, so the repository carries only text, the engine writes the
canonical JSON, and panel rectangles come from the immutable layout registry instead of
being duplicated by hand. A materialized project is a few tens of kilobytes, which keeps
the corpus practical for CI and for local evaluation.

Each project carries a metadata record — `capability`, `stresses`, and the expected page,
panel, character, and text counts — written next to the project as
`{scenario}.benchmark.json`. The sidecar is deliberately outside the project boundary so a
benchmark project contains only artifacts the engine itself recognizes.

Benchmark projects stop at `STORYBOARDED`. They are structural evidence: they prove plan,
character, storyboard, layout, continuity, and text-budget mechanics under the current
schema. They make no claim about live visual quality, which still requires a provider
capability and bounded visual review.

## Relationship to the benchmark harness

This corpus is the scenario inventory, not a metric harness. It answers "which realistic
comic shapes must the pipeline handle" and proves each one is a schema-valid project. It
deliberately stops short of running the full lifecycle so nine scenarios stay cheap enough
to validate on every pull request.

The benchmark harness tracked in issue #118 consumes project fixtures through a separate
case contract. Each scenario here is already shaped to be registered as such a case once
that harness lands: it supplies the source, the three plan artifacts, contiguous panel
identities, and a declared page and panel count. Registering a scenario as a metric case is
a follow-up decision per scenario, because a full lifecycle run costs far more than
structural validation.

## Commands

Validate the corpus:

```bash
python -m unittest tests.test_benchmark_corpus -v
```

Materialize every benchmark project for local evaluation:

```bash
python -m tests.benchmark_corpus /tmp/comic-sol-benchmark
python scripts/validate_project.py /tmp/comic-sol-benchmark/<project-dir> --stage storyboard
```

Materialize a single scenario:

```bash
python -m tests.benchmark_corpus /tmp/comic-sol-benchmark --scenario four-page-story
```
