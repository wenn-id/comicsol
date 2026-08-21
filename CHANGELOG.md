# Changelog

## Unreleased

### Added

- Expanded typography coverage and made the supported script set inventoried rather than
  implied. New `scripts/font_coverage.py` declares every Unicode block Comic Sol letters
  along with whether advance-only placement renders it faithfully, reads the bundled cmap
  tables without Pillow or `fontTools`, and prints a coverage inventory — the bundled
  three-face policy covers 2842 codepoints, which was never stated anywhere before. This
  puts Vietnamese (Latin Extended Additional), polytonic Greek, historic and minority
  Cyrillic, Latin Extended-C/D/E, and the IPA and phonetic blocks under regression test;
  all were already covered by the bundled fallback and none were claimed or verified.
  `doctor` gained a `typography` check that fails when the bundled faces stop covering a
  documented script, and `references/schemas.md` now specifies `typography.json`.
- Font policies may now carry one optional face per script, configured with
  `letter_panels.py --font-script SCRIPT=PATH` and recorded as a `script:{script}` role.
  This admits the linear scripts no bundled face covers — CJK ideographs, kana, precomposed
  Hangul syllables, Armenian, Georgian, and Ethiopic — without adding megabytes of font
  binaries to every install; `docs/typography.md` records the vetted SIL OFL 1.1 face for
  each. A policy that configures no extension hashes exactly as it did before, so no
  existing project is marked stale by the mechanism. An extension is refused for a script
  that could not be placed correctly even with a covering face, rather than accepted and
  mis-drawn.
- Typography preflight records the checks it performed — `typography-shaping-policy` and
  `typography-glyph-coverage` — and rolls up which face served which script, so an
  unintended font substitution is visible in the record instead of only on the page.
  `validate_lettering_provenance` recomputes rather than reads back: each glyph's
  `character` must agree with its `codepoint`, its `script` must be the script that
  codepoint belongs to, and that codepoint must still be letterable, so a stale or edited
  record cannot label a bidirectional or reordering script as linear and carry the claim
  past the export gate. Check records are verified field by field and rejected when
  duplicated, so a record reduced to passing IDs cannot pose as evidence the checks ran.
- Added `tests/fixtures/typography-scripts/`: 17 fixtures that drive the supported set
  from data, so declaring a script means adding a file rather than editing a test body.
  Lettering integration coverage now renders the newly declared scripts and asserts every
  character of every drawn font run resolves to a real glyph, catching any divergence
  between what preflight authorized and what the renderer reached for.
- Made speaker attribution explicit and verifiable for multi-character panels. Every
  spoken balloon now resolves to a stable character-bible ID, and that identity is retained
  in lettering geometry as a per-placement `attribution` record alongside the voice source
  it is bound to. The record's `resolution` reports how identity was established: a
  storyboard `speaker` must be a bible ID, so every validated project records `declared`,
  while `inferred` covers callers invoking `letter_panel()` directly with a display name —
  which the renderer previously accepted silently and now resolves to exactly one character
  or refuses, because a name shared by two characters resolves to no one. Authoring dialogue
  against display names remains unsupported at the storyboard level.
- Panels whose balloons cannot be told apart now fail with `dialogue-attribution-ambiguous`
  at both storyboard validation and lettering, using one shared policy in
  `scripts/core_primitives.py`: different speakers claiming anchors closer than `0.04`
  normalized report `shared-anchor`, and one speaker claiming anchors farther apart than
  `0.25` reports `split-anchor`. A spoken balloon without a text ID, or two sharing one, is
  refused as `dialogue-attribution-required` because attribution would not be addressable.
- `bubble-tail-geometry` gained three identity reasons — `missing-attribution`,
  `speaker-mismatch`, and `attribution-anchor-mismatch` — so a swapped pair of speakers is
  detected from the record even when both tails attach and point correctly.
- Added multi-speaker regression coverage: a two-character dialogue panel that passes every
  deterministic page check without an override, a swap of its two retained attributions that
  is detected on both balloons, and three named balloon-layout fixtures for the new reasons.
- Added deterministic speech balloon placement QA to the composed-page record: three new
  page checks in `scripts/page_quality.py` audit balloon geometry the engine previously
  left to the eye. `balloon-subject-obstruction` fails when a balloon comes closer to an
  authored `speaker_anchor` than the clearance the renderer reserves for a tail, measuring
  dialogue against the ellipse actually drawn and captions against their box, and passing
  when a panel authors no anchor to protect. `bubble-tail-geometry` promotes the tail
  verdict that previously existed only in the benchmark harness into the pipeline, so a
  tail must attach to its balloon, stop short of its voice source, point at the authored
  anchor, stay inside the panel, and still agree with the storyboard's `speaker_anchor` and
  `voice_source`. `balloon-crowding` reports crowded layouts as an actionable
  warning — naming the panels, their balloon coverage, and any pair closer than the
  readable separation — selecting `accept-warning` instead of blocking export.
- `text-overlap` regions now report `overlap_area` and `overlap_ratio` against the smaller
  box, so a hairline touch is distinguishable from a buried balloon without weakening the
  existing rule that any overlap fails.
- Added named good and bad balloon layout fixtures (`tests/fixtures/balloon-layouts/`) that
  describe a placement defect as data applied to the lettered one-page fixture, covering the
  good baseline, out-of-bounds placement, dialogue and caption subject obstruction, stale
  tail direction and voice source, and both crowding signals.
- Added a committed pre-change page-QA record fixture (`tests/fixtures/page-qa-2.0/`) and a
  migration test module, so the registered `("2.0", "2.1")` page-QA migration is exercised
  against a record in the superseded shape rather than one assembled inside a test body.

- Added the Comic Sol benchmark framework (`scripts/benchmark.py`) with a validated
  benchmark project contract in `benchmarks/cases/`, comparable pipeline success,
  resume success, repair rate, panel acceptance, dialogue correctness, and export
  success metrics, versioned machine-readable result records, and a fail-closed
  two-revision diff wired into `.github/workflows/benchmark.yml`.
- Deterministic benchmark runs are byte-reproducible and label themselves
  `proves_visual_quality: false`; optional live-provider runs consume already
  retained rasters with explicit caller-supplied provenance.
- Added a real-world comic benchmark corpus (`tests/benchmark_corpus.py`,
  `docs/benchmark-corpus.md`) with nine plan-complete projects covering dialogue-heavy,
  action, two-character, multi-character, silent-manga, night/low-light, long-dialogue,
  complex-background, and four-page story scenarios.
- Benchmark corpus projects are materialized on demand rather than committed, take panel
  rectangles from the immutable layout registry, validate at the `storyboard` stage, and
  record the capability each project stresses alongside expected page, panel, character,
  and text counts.
- Added a character consistency benchmark (`tests/consistency_benchmark.py`,
  `docs/character-consistency-benchmark.md`) that carries two canonical characters through
  twelve panels covering front, profile, three-quarter, full-body, and close-up views
  across nine expressions, four lighting conditions, and four backgrounds, with a
  single-condition control page and one renderable prompt per panel.
- Character consistency scores stay out of deterministic CI: the benchmark emits an
  unscored scorecard for manual or model-assisted review, refuses a scorecard produced
  against a different definition digest or without review provenance, averages only the
  dimensions a reviewer actually scored, and records the release baseline in
  `benchmarks/consistency/baseline-v2.0.0rc4.json` with its visual plane explicitly
  unscored.
- Added a compact, version-tagged benchmark summary report
  (`scripts/benchmark_summary.py`) that pools each metric's numerator and denominator
  across cases, so pipeline success, resume success, panel acceptance, repair rate,
  dialogue correctness, and export success are reported over all cases, all panels, all
  dialogue checks, and all exported pages, and that publishes both machine-readable JSON
  and a reviewable Markdown table suitable for release notes or a CI artifact.
- Benchmark summaries carry one `v<engine version>+<git revision>` tag taken from the
  records themselves, fail closed on records spanning more than one engine revision or a
  foreign harness version, repeat the limitations of the records they summarize, claim
  `proves_visual_quality` only when every summarized record does, and are byte-identical
  for two byte-identical deterministic runs.
- Comparing two benchmark summaries reports every pooled metric as a baseline,
  candidate, delta, and verdict under an explicit tolerance, and fails closed on an
  unreadable or foreign summary, a candidate case that is not passed, and case sets that
  differ between the two runs, because pooled aggregates over different case sets are not
  comparable.
- Benchmark summaries fold in character consistency metrics from the published baseline
  or a scored scorecard as reported, never gated, evidence: invariant pinning and trait
  restatement restate the structural plane, visual coverage and the normalized visual
  score report only what a reviewer actually scored, an unscored dimension is never
  averaged in as a zero, and an unattributable score or a scorecard from another
  definition is refused rather than summarized.
- Added a shot-aware character reference strategy (`scripts/reference_strategy.py`) that
  classifies each storyboard panel's authored `shot` into close-up, profile,
  three-quarter, full-body, or unclassified by its earliest framing cue, then selects the
  identity pack's reference views for that framing instead of attaching one canonical
  reference to every panel regardless of camera setup.
- Reference selection anchors identity before detail: the canonical view leads every shot
  class because it is the only view cross-checked against the character bible, the view
  matching the panel's shot follows, the remaining identity views come next, and
  scene-specific views rank last.
- A caller-supplied reference budget is spent breadth-first across a panel's characters,
  so every character receives its canonical anchor before any character receives a second
  view; one path is attached at most once per panel, and a limit below the cast size is
  recorded rather than hidden.
- Every panel's selections and omissions are published with their reasons at
  `logs/reference-selection.json` through the project transaction and are byte-identical
  on resume, so a panel that drifts can be reviewed against the references it actually
  received. The engine still names no provider, model, endpoint, or credential, and takes
  the reference limit from the caller rather than inferring one.
- Added provider-neutral character-consistency QA with explicit face, hair, age-
  appearance, clothing, accessories, proportions, and immutable-trait results for every
  on-panel character. Rich checks retain canonical expectations, selected-reference and
  identity hashes, specific evidence, warning versus hard-failure severity, and
  actionable subject-level repair guidance.
- Accepted character reviews are reusable only while their panel artifacts, character
  bible, identity pack, and reference plan remain current. The CS-013 scorecard can emit
  the same seven per-trait assessment records with `qa-results`, while its aggregate
  visual score remains advisory and provider-independent.
- Added targeted panel repair planning (`scripts/repair_strategy.py`) that classifies every
  non-passing check into the narrowest scope its evidence supports — one reviewed subject
  from a character trait region, one bounded anchor area from an `anatomy` or `text-free`
  defect region, or the whole panel — and selects `selective-repair` so a localized defect
  no longer re-rolls an accepted panel.
- Repair falls back to a full regeneration whenever the narrower option is unsafe, and
  records exactly one reason: `stale-bindings` when a bound artifact no longer matches the
  review, `editing-unsupported` when the detected capability cannot edit in place,
  `panel-wide-check` for camera framing, the scripted beat, continuity, or whole-raster
  faults, and `unlocalized-evidence` when nothing located the defect. Every defect is
  recorded whether or not it can be repaired in place.
- Added the `logs/repair-plan.json` provenance record, published atomically with its own
  schema version, carrying each panel's strategy, ordered repair targets, the accepted
  raster the repair must preserve, and the checks and subjects that reviewed clean.
  Validation re-derives every entry from the current QA record, so a plan cannot outlive
  the review it describes.
- `comic_sol.py promote-attempt` now refuses to overwrite an accepted panel raster while its
  QA record still accepts the panel, and records the planned repair strategy on the
  promotion event. A repair therefore starts from a review that asked for one, and the
  previous accepted bytes are still archived before the replacement is published.

### Fixed

- Page-QA verdicts and provenance can no longer come from different artifact
  generations. JSON digests now cover the exact bytes parsed, page dimensions and the page
  digest come from one raster buffer, and bindings are projected from that captured
  snapshot without re-reading the filesystem. `publish_page_quality_record()` holds the
  reentrant project lock across derivation and atomic publication; construction and direct
  writes are independently lock-covered, migration retains its transaction-wide lock, and
  unlocked validation is explicitly advisory rather than silently implying serialization.

- Typography preflight no longer decides shaping support by matching character names,
  which only recognized scripts someone had thought to spell out. Arabic and CJK were
  caught while Hebrew, Devanagari, Thai, Bengali, Tamil, Khmer, Myanmar, and conjoining
  Hangul jamo passed the gate and were judged on glyph coverage alone — so a project that
  supplied a covering `--font` would letter them as unshaped, unreordered, or
  reversed text. Classification now comes from the declared block table, and every refusal
  states the property that forbids it.
- CJK, kana, and precomposed Hangul syllables were refused as `unsupported-shaping`, which
  told an author the text was impossible when it was merely unbundled. None of them need
  reordering or joining, so they now report `missing-glyph` with remediation naming the
  licensed face that resolves them. Georgian is classified through its uppercased Mtavruli
  form as well, which lands in a different Unicode block than the Mkhedruli a storyboard
  authors.
- The README no longer describes `.notdef` fallback boxes as the outcome for uncovered
  characters. Preflight refuses the batch before any panel is written, and has done since
  it was introduced; the documented behaviour contradicted the implemented gate.
- Codepoints belonging to no classified block are refused rather than assumed to place
  linearly. The undeclared BMP blocks are dominated by scripts that need joining or
  reordering — Syriac Supplement, Arabic Extended-B, Devanagari Extended, Javanese,
  Balinese — so a permissive default handed exactly those a false pass whenever a
  covering face was configured. The linear symbol, Braille, and CJK compatibility blocks
  that captions legitimately use are declared explicitly so fail-closed costs no
  coverage.
- Combining marks are checked against the base they attach to. One mark over a base from
  the same face still places correctly and is admitted, but a mark with no base, a
  stack of two marks, and a mark resolved to a different face than its base are refused:
  each needs anchor geometry that nominal advances cannot express, and no font choice
  changes that. NFC normalization continues to compose the common accented forms before
  this check sees them.

- Fixed out-of-bounds balloon detection, which measured lettering boxes against the
  storyboard page rectangle instead of the panel's own clean raster. For a downscaled hero
  panel that made `clipped-text` about twice as permissive as intended, so a box could run
  well past the artwork and still pass. Page QA now reads the pixel space the geometry is
  actually written in from `panels/{panel-id}/normalization.json`. The renderer and the
  audit also share one clearance constant, which makes `balloon-subject-obstruction` exact
  for the pair the renderer resolves — a dialogue balloon against the anchor it speaks
  from — while still reporting a caption or a second balloon landing on another line's
  speaker, which placement never considers.
- `bubble-tail-geometry` recomputes a tail's `source_gap` from its retained tip instead of
  trusting the recorded value, and a corrupt or non-finite `speaker_anchor` now fails the
  check closed rather than raising out of page-QA construction.
- Page QA records now bind ordered `normalization_sha256s`, because `clean.size` defines the
  pixel space every balloon verdict is measured in. Re-normalizing a panel makes the record
  stale even when the composed page image is unchanged.

### Changed

- **Lettering geometry `schema_version` moves from `"1.0"` to `"1.1"` now that every
  placement carries an `attribution` record.** Geometry is fully derived from the clean
  raster, the storyboard, and the font policy, so there is nothing to migrate: a `"1.0"`
  record is reported as `lettering-record-stale: geometry predates speaker attribution and
  must be lettered again` and is re-lettered rather than rewritten in place. The lettering
  stage cache version in `templates/manifest.json` moves from `"2"` to `"3"` for the same
  reason, so a project carrying the new version reruns lettering instead of reusing a
  cached result written in the previous shape.
- **The page-QA record `schema_version` moves from `"2.0"` to `"2.1"`, and `"2.0"` records
  migrate in place.** The page check set grows from seven entries to ten and `bindings` gains
  `normalization_sha256s`, so the record carries a new version instead of changing shape
  underneath one. A `"2.0"` record is now reported as
  `quality-migration-required: schema 2.0 page QA must be migrated to 2.1` rather than as
  `page-quality-stale: quality-check-ids`, which named the reviewer's check IDs for what was
  really a superseded check set. `migrate_page_quality_record()` runs the registered
  `("2.0", "2.1")` hook from `PAGE_QA_MIGRATIONS` inside the project transaction: the seven
  deterministic checks and all bindings are re-derived from current artifacts, and the three
  reviewer-supplied checks and the original review are preserved only while the bound
  `page_sha256` still matches the page on disk. A record with no registered migration path
  fails closed with `UnsupportedSchemaVersionError`, and a refused or interrupted migration
  leaves the project byte-for-byte unchanged. `templates/page-qa.json` starts at `"2.1"` and
  still stubs the new checks as `migration-required`. No `project.json` schema version is
  affected. This supersedes the note that these records must simply be re-derived per page.

- **The `dialogue_correctness` benchmark metric now measures a wider check set.**
  `DIALOGUE_PAGE_CHECK_IDS` in `scripts/benchmark.py` gains `balloon-subject-obstruction`
  and `bubble-tail-geometry`, so the metric covers every deterministic, error-severity page
  check that verifies dialogue geometry rather than the three it counted before a tail or
  speaker-clearance regression was enforced by the pipeline. `balloon-crowding` stays
  excluded by design: it never fails, only warns, so counting it would conflate reading
  comfort with correctness. Both committed cases still report `1.0`, but the denominator per
  dialogue-bearing page grows by two, so recorded numerators and denominators — including
  `observations.dialogue_checks_passed`/`dialogue_checks_total` — change even where the
  ratio does not.

- **`HARNESS_VERSION` is now `"2"`, so benchmark result records produced by the previous
  harness are no longer comparable.** The result schema is unchanged, which is exactly the
  hazard: a harness-1 record still validates while measuring a narrower
  `dialogue_correctness`, so pooling or diffing it against a current run would report a
  definition change as a metric change. `summarize_results()` already rejected foreign
  harness versions; `diff_results()` now does too, and reports the stale side as an
  exception instead of a clean verdict. Re-run the benchmark rather than reusing archived
  pre-bump results; `.github/workflows/benchmark.yml` already benchmarks the baseline
  revision with the current harness, so CI needs no change.

- **One page validation now reads and digests each bound artifact once instead of twice,
  with no change to the issues it reports.** `validate_page_quality()` deliberately checks
  provenance twice: once at the path the record itself names, to separate a missing bound
  artifact from one whose bytes changed, and once re-derived from the current storyboard, to
  catch a binding that is well-formed and current but wrong. Both passes hashed every
  artifact independently, so validating the three-panel one-page fixture performed 18 hash
  computations over 9 distinct files, including the 1600x2400 page raster twice. The reads
  now flow through one `_ArtifactSnapshots` cache per operation, memoized on the resolved
  path, which drops that to 9 — one per file — and halves the bytes read on the two-page
  benchmark case from 1642.9 KiB to 821.5 KiB. Wall time is unchanged within measurement
  noise on that project, because hashing was never the dominant cost of validation; the
  saving is in reads and digests, and it scales with page and panel count. The two passes
  are untouched: each still resolves its own paths and makes its own comparison, so a
  record binding a decoy raster whose digest matches is still reported only by the
  re-derived pass, and the per-panel traversal still walks the record's panel IDs while
  `_page_context()` walks the storyboard's, which is what catches a record naming the wrong
  panel set. `_page_bindings()` keeps its single I/O-free derivation path shared by
  construction, migration, and validation. Because a parse now consumes the same buffer
  that was digested even across passes, a validation can also no longer observe two
  generations of one file; the advisory unlocked contract is otherwise unchanged.

### Removed

- Removed the unwired `comic_sol_product.providers` Python API. Integrations must
  keep image generation in the agent capability plane and use the documented
  project CLI/MCP lifecycle for retained raster artifacts.

## 2.0.0rc4 — 2026-07-30

Speaker-aware balloon-tail prerelease.

### Added

- Validated `speaker`, `voice_source`, and `speaker_anchor` semantics for every dialogue tail.
- Deterministic `organic-cubic-v1` geometry provenance and artifact-bound per-dialogue page-QA regions.

### Fixed

- Replaced short triangular wedge tails with merged-body, non-crossing cubic silhouettes that preserve a durable white core and point toward the declared voice source.
- Captions, system status, and SFX now fail closed if dialogue-only tail fields are present.
- Legacy free-coordinate `tail_target` inputs require explicit migration instead of silent reinterpretation.

### Distribution and limitations

- Linux, macOS, and Windows x86_64 bundles, CycloneDX SBOMs, SHA-256 manifests, and a keyless Sigstore bundle for `SHA256SUMS` remain available.
- Native archives use keyless Sigstore verification but are not Authenticode-signed or Apple-notarized; verify `SHA256SUMS.sigstore.json` and the archive digest before use.

## 2.0.0rc3 — 2026-07-30

Focused PDF fidelity prerelease.

### Fixed

- Encode composed page rasters at JPEG quality 95 before deterministic PDF publication.
- Preserve detailed, high-frequency comic artwork under the unchanged full-content verification thresholds.
- Add a deterministic regression reproducing the live Codex App dogfood failure.

### Distribution notes

- Linux, macOS, and Windows x86_64 archives remain available with wheel, sdist, installers, checksums, and CycloneDX SBOMs.
- Native artifacts remain **unsigned**; verify downloads against `SHA256SUMS`.

## 2.0.0rc2 — 2026-07-30

Comic Quality release candidate.

### Added

- Deterministic panel normalization with format, orientation, dimension, and SHA-256 provenance.
- Typography preflight and transactional lettering geometry.
- Immutable layout registry with canonical `four-grid` composition and schema 2.0 page QA.
- Full-content PDF verification bound to ordered source pages and page-QA hashes.
- Local-only deterministic quality matrix covering continuity, layouts, text, image formats, typography, retry/warning/failure outcomes, and interrupted resume.
- Explicit deterministic versus retained live-visual evidence disclosure in QA reports.

### Security and limitations

- Linux, macOS, and Windows x86_64 artifacts remain **unsigned** and are not notarized.
- Every native bundle includes SHA-256 checksums and a CycloneDX SBOM.
- Deterministic evidence proves mechanics only; it does not claim live visual quality.
- Provider credentials and network provider SDKs remain outside the base package.

## 2.0.0rc1 — 2026-07-29

First Native Distribution release candidate.

### Added

- Installable `comic-sol` CLI with deterministic engine, fonts, templates, Skill, and references.
- Stable stdio MCP launcher with the exact 17-tool lifecycle surface.
- Native bundled runtimes and portable ZIP archives for Linux, macOS, and Windows x86_64.
- Transactional user-local installers with checksum verification, health checks, upgrade rollback, idempotent reinstall, and project-preserving uninstall.
- Non-root OCI image and Compose example using `/data` for persistent projects.
- Deterministic unsigned release metadata, SHA-256 manifests, and CycloneDX SBOM files.
- Cross-platform release CI and clean-install/package gates.

### Security and limitations

- All native artifacts are **unsigned**: no Authenticode signature, Apple notarization, or GPG signature is provided in this prerelease.
- Linux, macOS, and Windows archives are built independently on native GitHub runners.
- x86_64 is the release architecture for `v2.0.0rc1`; arm64 naming is reserved but no arm64 artifact is claimed.
- Image generation still depends on an agent-exposed provider capability; deterministic fixtures do not claim live visual quality.
- Provider credentials and provider SDKs remain outside the base package.

