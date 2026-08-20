# Changelog

## Unreleased

### Added

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

- **Existing `qa/pages/page-{NNN}.json` records must be re-derived.** The page check set
  grows from seven entries to ten and `bindings` gains `normalization_sha256s`, and page QA
  requires the exact check tuple, so a previously accepted record now reports
  `page-quality-stale: quality-check-ids` until it is written again. The page-QA
  `schema_version` stays `"2.0"`; these records are re-derived per page after review rather
  than migrated in place, and `templates/page-qa.json` stubs the new checks as
  `migration-required`. No `project.json` schema version is affected.

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
