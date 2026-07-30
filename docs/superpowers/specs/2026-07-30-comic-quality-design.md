# Comic Sol Comic Quality Design

**Date:** 2026-07-30

**Status:** Approved design awaiting implementation plan

**Repository:** `wenn-id/comic-sol-lab` (private)

**Development branch:** `ai/comic-quality`

**Target release:** `v2.0.0rc2`

## 1. Purpose

Comic Quality is the fourth Comic Sol Lab v2 milestone. It turns the existing deterministic comic pipeline into a quality-gated publishing workflow whose panel, page, and PDF claims are bound to current artifacts and specific evidence.

The milestone improves visible output and verification together. A comic must not look better only in a curated demo while stale, substituted, clipped, or incomplete artifacts can still reach `COMPLETE`.

## 2. Product outcome

A user can create a comic in which:

1. every accepted panel has documented normalization and seven check-specific QA observations;
2. authored text is proven renderable before lettering starts;
3. every composed page has current layout and lettering QA;
4. the exported PDF is decoded and compared across full page content;
5. at least one four-panel layout is supported and regression-tested;
6. curated samples distinguish deterministic mechanics from live visual quality;
7. existing schema 1.0 projects remain readable and receive actionable migration guidance.

## 3. Delivery approach

Implementation proceeds as four vertical slices:

1. **Trustworthy Panel** — raw image through normalization, panel QA, typography preflight, lettering, and stale-artifact validation.
2. **Trustworthy Page** — composition, four-panel layout, reading order, obstruction and lettering QA, and current page records.
3. **Trustworthy PDF** — deterministic export followed by full-content decoded verification.
4. **Quality Matrix** — deterministic fixtures, golden outputs, and clearly labeled live visual samples.

Each slice must leave the repository green and produce an independently demonstrable improvement. Public behavior begins with a failing test. A later slice may consume an earlier slice's contracts but must not duplicate its validation rules.

## 4. Scope

### 4.1 Included

- Panel QA provenance bound to raw, clean, storyboard, reference, and generation artifacts.
- Deterministic image normalization for PNG, JPEG, and WebP, including EXIF orientation.
- Typography preflight with exact text-item and code-point diagnostics.
- Current page QA with seven normative page checks.
- At least one deterministic four-panel layout.
- Full-page PDF decode and comparison with documented tolerance.
- Negative verification for erased, missing, swapped, duplicated, corrupt, and dimension-mismatched pages.
- Golden tests for every supported layout.
- Curated deterministic and live visual quality sample matrices.
- Schema migration and invalidation rules needed by these contracts.
- Version progression to `2.0.0rc2` only after every milestone gate is green.

### 4.2 Non-goals

- A GUI or hosted service.
- New image-provider integrations.
- Model selection, prompt optimization, or mandatory provider accounts.
- Automatic visual-similarity scores as hard acceptance gates.
- Face recognition or biometric identity scoring.
- Replacing human or agent visual judgment with deterministic heuristics.
- Broad font shaping support that has not been verified by tests.
- A mechanical source-tree rewrite.
- Stable `v2.0.0`; RC2 remains a prerelease.

## 5. Architecture and ownership

The canonical engine remains `scripts/*.py`. Installed package code continues to bundle and call this engine rather than fork business logic.

New responsibilities are separated by contract:

- normalization owns source decoding, orientation, crop, resize, clean-image publication, and normalization provenance;
- panel QA owns seven visual review records and their artifact bindings;
- typography preflight owns renderability policy and diagnostics;
- lettering consumes a successful preflight and records output geometry needed by page QA;
- composition owns deterministic page pixels and layout geometry;
- page QA owns page-level review evidence bound to the composed page and its source descriptors;
- PDF verification owns decoded comparison between exported pages and source PNGs;
- validation owns freshness and state-transition gates, not artifact production.

CLI and MCP remain thin adapters. This milestone does not add an MCP tool unless a new user operation cannot be expressed safely through an existing canonical lifecycle tool. Exact 17-tool parity is preserved by default.

## 6. Compatibility and schema evolution

### 6.1 Read compatibility

Schema 1.0 projects remain readable. Existing records are not silently treated as satisfying RC2 quality gates.

When an old project is opened:

- schema 1.0 panel QA can still be displayed and reported;
- missing RC2 provenance produces a stable, actionable validation issue;
- existing raw, clean, lettered, page, and PDF artifacts are preserved until the user chooses resume or regeneration;
- migration never fabricates reviewer evidence, timestamps, hashes, crop data, or check observations.

### 6.2 Write policy

New or mutated quality records use schema `2.0`. Migration is transactional and project-locked. A failed migration leaves the original bytes unchanged.

Schema migration may derive only facts that can be recomputed deterministically from retained artifacts, such as dimensions and SHA-256. Human or agent observations must be re-reviewed.

### 6.3 Invalidation

Changes invalidate only downstream dependents:

- raw image change invalidates normalization, panel QA, lettering, composition, page QA, report, and PDF;
- normalization contract or clean-image change invalidates panel QA onward;
- storyboard or character-reference change invalidates affected panel QA onward;
- authored text or font configuration change invalidates typography preflight, lettering, composition, page QA, report, and PDF;
- lettered panel change invalidates affected pages, page QA, report, and PDF;
- layout or composition change invalidates affected page QA, report, and PDF;
- source page change invalidates PDF verification and finalization.

Invalidation reports preserved and removed artifacts before mutation through the existing resume contract.

## 7. Slice 1: Trustworthy Panel

### 7.1 Normalization input boundary

Supported source formats are PNG, JPEG, and WebP. Decoding is fail-closed for unreadable data, decompression bombs, unsupported dimensions, or files outside the project root.

Normalization performs, in order:

1. decode source;
2. apply EXIF orientation exactly once;
3. convert to the canonical color mode;
4. compute target aspect-ratio operation;
5. crop or fit according to the explicit mode;
6. resize with the configured deterministic resampler;
7. encode canonical PNG bytes;
8. atomically publish the clean image and record.

A normalization record contains:

- schema version;
- panel ID;
- source path, format, dimensions, EXIF orientation, and SHA-256;
- target dimensions;
- operation mode: `fit`, `crop`, or `exact`;
- crop box in source-oriented pixel coordinates, or `null`;
- clean path, dimensions, mode, and SHA-256;
- normalization implementation version.

Undocumented crop, stale clean bytes, unexpected substitution, wrong dimensions, or mismatched hashes blocks `QA_READY`.

### 7.2 Panel QA record

Every panel has exactly seven checks:

1. `character-identity`;
2. `anatomy`;
3. `action`;
4. `composition`;
5. `continuity`;
6. `text-free`;
7. `technical`.

Each check records:

- result: `pass`, `warning`, or `fail`;
- severity;
- non-empty observed evidence specific to that check;
- optional bounded region coordinates when useful;
- reviewer method and identity.

A panel QA record also binds:

- raw and clean paths, hashes, and dimensions;
- normalization-record hash;
- storyboard hash;
- ordered character-reference hashes;
- generation provenance and retained-attempt hash;
- review timestamp in UTC;
- attempt counters and retry reason;
- decision and unresolved warnings.

Evidence is rejected when all seven values normalize to one identical phrase or when it is only a generic assertion such as `verified`, `looks good`, `ok`, or `pass`. Deterministic validation checks evidence structure and distinctness; it does not claim to understand image semantics.

Warnings require explicit acceptance through the existing override flow. Failures require regeneration or a new review; they cannot be overridden as passes.

### 7.3 Typography preflight

Preflight runs before any output image mutation. It walks every authored text item, including dialogue, caption, and SFX, after the same NFC/control-character normalization used by lettering.

For every rendered code point and style span it records:

- text item ID;
- Unicode code point and printable representation;
- selected font path identifier, not a private absolute path;
- regular or bold style;
- support result;
- shaping-policy result.

Missing coverage blocks lettering before temporary or final images are written. The error reports the code point, item ID, fonts checked, and supported remediation.

Advertised RC2 support is limited to scripts proven by bundled-font and rendering tests. Latin, Greek, and Cyrillic are supported when coverage tests pass. Combining marks and bold spans require explicit tests. Arabic, CJK, and emoji are reported as unsupported unless the implementation includes and verifies both glyph coverage and required shaping/rendering behavior. Unsupported does not silently fall back to `.notdef`.

Whitespace and control characters that do not require visible glyphs are handled by explicit policy rather than accidental cmap behavior.

### 7.4 Lettering output contract

Lettering consumes the clean image and successful preflight record. It preserves authored storyboard text and creates display text separately.

The lettering record captures enough deterministic geometry for page review:

- text item and panel IDs;
- kind and reading order;
- selected anchor;
- bubble, caption, or SFX bounding box;
- tail origin and target when applicable;
- rendered font runs;
- source clean hash and output lettered hash.

The existing classic American print-comic style remains: uppercase inked dialogue, compact white balloons, smooth outlines, and short tails that avoid faces. This milestone improves validation and layout behavior without replacing the established visual direction.

A project cannot enter `LETTERED` with a missing preflight, stale clean hash, unsupported code point, `.notdef` result, or incomplete lettering geometry.

## 8. Slice 2: Trustworthy Page

### 8.1 Layout registry

Supported layouts are explicit named definitions with:

- page dimensions;
- ordered panel slots;
- integer rectangles;
- reading order;
- gutter and border rules.

RC2 adds at least one four-panel layout. All layouts must fit within the canonical page, use non-overlapping positive rectangles, and have a complete unique reading order.

The storyboard may provide explicit rectangles for compatibility, but validation maps them to a named layout or labels them `custom`. A custom layout must satisfy the same geometry and reading-order invariants.

### 8.2 Composition

Composition remains deterministic and transactional. The composition cache binds each page to:

- layout definition/version;
- storyboard page hash;
- ordered lettered-panel hashes;
- page settings;
- output PNG hash and dimensions.

No page is published when any source is missing, stale, unreadable, or outside the project root.

### 8.3 Page QA record

Every composed page has seven normative checks:

1. `clipped-text`;
2. `text-overlap`;
3. `face-action-obstruction`;
4. `bubble-tail-direction`;
5. `reading-order`;
6. `accidental-text-watermark`;
7. `layout-border-integrity`.

Each check uses the same structured result, severity, specific evidence, optional regions, reviewer method, and reviewer identity rules as panel QA.

The page record binds:

- page number, path, hash, dimensions, and composition-cache hash;
- layout name and version;
- ordered source panel and lettering-record hashes;
- review timestamp;
- decision and unresolved warnings.

Deterministic checks may prove clipping, overlap, geometry, ordering, and border invariants from recorded boxes and layout data. Face/action obstruction, accidental visual text, and subjective tail quality remain agent or human review claims. The record identifies which method produced each observation.

Missing or stale page QA blocks `EXPORTED` and `COMPLETE`.

## 9. Slice 3: Trustworthy PDF

### 9.1 Export boundary

Export continues to require contiguous canonical page PNGs with expected dimensions. The PDF is written to a temporary file, flushed, decoded, verified, and only then atomically published.

### 9.2 Full-content verification

Four-corner sampling is removed as the content gate. Each decoded PDF page is normalized to canonical RGB and dimensions, then compared against the corresponding source PNG across the full page.

The comparison records:

- source page hash;
- decoded page dimensions and mode;
- mean absolute channel error;
- high-error pixel ratio at a documented threshold;
- maximum regional error over a deterministic grid;
- pass/fail tolerance version.

Tolerance is calibrated against PDFs produced by the pinned export stack. It must accept the expected lossy round trip while rejecting meaningful missing or substituted content. Exact threshold values are established by RED fixtures during implementation and stored as named constants with rationale; they are not configurable per project in RC2.

### 9.3 Required negative cases

Verification must reject, before publication:

- erased or replaced center content;
- missing lettering;
- swapped pages;
- duplicate pages;
- corrupt or undecodable PDF;
- page-count mismatch;
- page dimension or mode mismatch;
- decoded content outside the calibrated tolerance.

The previous good canonical PDF and descriptor remain byte-for-byte unchanged after any failed export or verification.

### 9.4 Verification record

The canonical PDF descriptor binds:

- ordered source page hashes;
- PDF hash;
- exporter and tolerance versions;
- decoded page metrics;
- page count and dimensions;
- verification timestamp.

A changed PDF, page, exporter contract, or tolerance version makes the verification stale and blocks `COMPLETE`.

## 10. Slice 4: Quality Matrix

### 10.1 Deterministic fixture matrix

CI fixtures cover:

- at least two recurring characters;
- identity, wardrobe, prop, palette, and scene continuity records;
- every supported named layout;
- dense dialogue, caption, and SFX;
- portrait and landscape sources;
- PNG, JPEG, and WebP;
- EXIF rotation and aspect-ratio boundaries;
- regular, bold, combining-mark, and verified non-Latin fallback cases;
- one transient repeat;
- one visual retry;
- one accepted warning;
- one hard failure;
- interrupted and resumed execution.

Fixtures use local deterministic assets and never require a network provider.

### 10.2 Golden outputs

Golden composition and export tests exist for every named layout. Golden review is hash-based only where cross-platform bytes are proven stable. Otherwise it compares canonical dimensions, geometry, regions, and bounded pixel metrics to avoid false platform failures.

Any golden update requires an explicit reason and visual inspection of the changed output; tests must not auto-bless new baselines.

### 10.3 Live visual samples

At least one sample comic exercises the complete workflow with generated artwork. Live samples record provider/model identity when exposed, references, attempts, reviewer method, and known limitations.

Live visual samples are not required for every CI run. They are release evidence and are labeled separately from deterministic mechanics. A passing deterministic fixture never claims that character identity or anatomy is visually good.

## 11. State, validation, and finalization

The existing state order remains unchanged:

```text
INIT → PLANNED → SCRIPTED → STORYBOARDED → REFERENCES_READY
→ PANELS_READY → QA_READY → LETTERED → COMPOSED → EXPORTED → COMPLETE
```

RC2 strengthens gates:

- `QA_READY` requires current normalization and panel QA for every panel;
- `LETTERED` requires current typography preflight and lettering records;
- `COMPOSED` requires current composition descriptors for every page;
- `EXPORTED` requires current page QA and a verified canonical PDF;
- `COMPLETE` requires all prior descriptors, the current report, and no unaccepted warning or stale verification.

Validation reports stable issue categories, exact artifact identifiers, and remediation. It never prints prompt content, secrets, raw provider payloads, or private absolute paths.

## 12. Error handling and transactionality

All mutations use the existing project lock and transaction primitives.

- Batch preflight validates all inputs before publishing the first output.
- Normalization, lettering, composition, QA-record updates, and PDF publication use atomic writes.
- A failure preserves the previous valid artifact and descriptor.
- Temporary files are confined to controlled directories and removed on failure.
- Structured errors distinguish unsupported text, stale provenance, invalid geometry, unreadable media, review failure, and PDF mismatch.
- Resume chooses the last valid stage from hashes and descriptors; it does not infer success from filenames alone.

## 13. CLI, MCP, and report behavior

Existing `comic-sol run`, `validate`, `status`, and `resume` surfaces expose the stronger gates without duplicating engine logic.

Human output summarizes the first actionable issue and count. JSON output includes stable category, artifact ID, stage, and remediation.

The report includes:

- normalization summaries;
- panel and page QA method/provenance;
- accepted warnings and reasons;
- typography support summary;
- PDF verification metrics and tolerance version;
- explicit labels for deterministic versus live visual evidence.

No new persisted log stores authored dialogue, prompts, or private paths beyond the project artifacts that already legitimately contain authored content.

## 14. Testing strategy

Every public behavior follows RED → GREEN → REFACTOR.

Required layers:

1. unit tests for normalization geometry, evidence policy, glyph coverage, layout registry, and PDF metrics;
2. transactional tests proving old artifacts survive failed mutation;
3. integration tests for each vertical slice;
4. validation and finalization negative tests for every stale dependency;
5. golden layout/export tests;
6. clean-wheel and frozen-runtime smoke tests after resource/schema additions;
7. Linux, macOS, and Windows CI;
8. one end-to-end sample from init through verified PDF and `COMPLETE`.

Tests use controlled synthetic images for exact failure localization. PDF corruption fixtures include center erasure, lettering erasure, swap, duplicate, and truncation.

## 15. Release gates

Comic Quality may ship as `v2.0.0rc2` only when all gates pass.

### Gate 1: Trustworthy Panel

- normalization matrix passes for PNG, JPEG, WebP, EXIF, portrait, landscape, and boundaries;
- panel QA binds all required hashes and rejects generic duplicated evidence;
- typography preflight blocks unsupported output before mutation;
- stale normalization, review, and lettering records block state progress.

### Gate 2: Trustworthy Page

- at least one four-panel layout is implemented;
- every layout has deterministic composition coverage;
- all seven page checks have specific current evidence;
- missing or stale page QA blocks export and completion.

### Gate 3: Trustworthy PDF

- full-content decoded comparison replaces corner-only verification;
- all required corruption fixtures fail before canonical publication;
- expected pinned-stack lossy output passes documented tolerance;
- prior valid PDF survives every failure case.

### Gate 4: Quality Matrix

- deterministic fixture matrix is complete;
- all layouts have reviewed goldens;
- one live sample demonstrates the complete flow;
- evidence clearly distinguishes mechanics from visual judgment.

### Gate 5: Distribution regression

- full test suite passes on Linux, macOS, and Windows;
- wheel, sdist, frozen runtime, MCP 17-tool smoke, and non-root OCI gates remain green;
- install, upgrade, rollback, and uninstall behavior from RC1 is unchanged;
- release metadata remains factual about unsigned artifacts.

## 16. Rollout and release

Implementation uses branch `ai/comic-quality` and PRs into `ai/post-event-development`. No direct push to the default branch is authorized.

The final RC2 release process is:

1. pass all five Comic Quality gates;
2. update package version and release notes to `2.0.0rc2`;
3. pass clean installation and native distribution regression;
4. merge a green PR;
5. tag the merge commit `v2.0.0rc2`;
6. publish a GitHub prerelease with portable archives, wheel, sdist, installers, checksums, CycloneDX SBOM, and factual signing metadata;
7. download the public assets and repeat checksum, install, doctor, MCP, PDF, and uninstall smoke tests.

## 17. Completion definition

The milestone is complete only when a comic can reach `COMPLETE` through the strengthened gates and the evidence proves:

- accepted panels are bound to current normalized artwork and specific reviews;
- every authored visible glyph is supported by verified rendering policy;
- composed pages have current page-level QA;
- the PDF reproduces complete ordered page content within calibrated tolerance;
- layouts and quality workflows are protected by deterministic fixtures;
- a live sample visibly demonstrates the intended comic quality;
- RC1 installation and portability guarantees have not regressed.
