# Comic Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers/executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `v2.0.0rc2` with artifact-bound panel/page QA, deterministic normalization and typography preflight, four-panel composition, full-content PDF verification, and reproducible quality evidence.

**Architecture:** Canonical business rules remain in focused `scripts/*.py` engine modules; CLI, MCP, package, and Skill surfaces consume those rules without duplication. Work proceeds in vertical slices so each gate leaves a usable, fully tested pipeline: shared quality records → trustworthy panel → trustworthy page → trustworthy PDF → quality matrix and RC2 release.

**Tech Stack:** Python 3.11, Pillow 12.3.0, MCP 1.28.1, `unittest`, canonical JSON/SHA-256, raster PDF, PyInstaller 6.15.0, GitHub Actions.

## Global Constraints

- Target package version is exactly `2.0.0rc2`; target tag is exactly `v2.0.0rc2`.
- Public behavior is implemented RED → GREEN → REFACTOR.
- Canonical engine remains `scripts/*.py`; installed package bundling must not fork business logic.
- Existing schema 1.0 projects remain readable; migration never invents review evidence.
- New or mutated quality records use schema `2.0`.
- Exact 17-tool MCP surface remains unchanged unless a separately approved design changes it.
- No provider, network, GUI, biometric, or hard visual-similarity dependency is added.
- PNG, JPEG, and WebP normalization is local and deterministic.
- Arabic, CJK, and emoji remain unsupported unless glyph coverage and shaping are both verified by tests.
- Mutations take the project lock and publish transactionally; failed operations preserve previous valid bytes.
- Structured errors never expose secrets, prompts, raw provider payloads, or private absolute paths.
- RC1 install, rollback, uninstall, native runtime, OCI, checksum, SBOM, and unsigned-status guarantees must remain green.
- Commits stage only named files; never use `git add .`.

---

### Task 1: Shared Quality Record Contract and Schema Compatibility

**Files:**
- Create: `scripts/quality_records.py`
- Create: `tests/test_quality_records.py`
- Modify: `templates/panel-record.json`
- Modify: `templates/page-qa.json`
- Modify: `scripts/validate_project.py`
- Modify: `tests/test_validation.py`
- Modify: `setup.py`
- Modify: `comic_sol_product/release.py`

**Interfaces:**
- Produces: `QualityCheck`, `QualityBinding`, `validate_quality_checks(checks, expected_ids) -> tuple[str, ...]`, `quality_record_hash(record) -> str`, `read_quality_record(path, kind) -> dict`, and `migrate_quality_record(record, kind, recomputed) -> dict`.
- Consumes: `canonical_artifact_bytes()`, `sha256_file()`, path containment, project lock, and atomic writer from existing engine modules.
- Later tasks rely on exact check result values `pass`, `warning`, and `fail`, and on stable validation categories `quality-record-stale`, `quality-evidence-generic`, and `quality-migration-required`.

- [ ] **Step 1: Write failing schema and evidence-policy tests**

Add tests that construct seven checks and prove accepted records require exact IDs, non-empty evidence, valid result/severity values, and non-generic distinct observations:

```python
PANEL_CHECK_IDS = (
    "character-identity", "anatomy", "action", "composition",
    "continuity", "text-free", "technical",
)


def passing_checks():
    return [
        {
            "id": check_id,
            "result": "pass",
            "severity": "error",
            "evidence": f"Observed {check_id} against panel bounds",
            "method": "agent-review",
            "reviewer": "fixture-reviewer",
            "regions": [],
        }
        for check_id in PANEL_CHECK_IDS
    ]


def test_generic_or_identical_evidence_is_rejected():
    checks = passing_checks()
    for check in checks:
        check["evidence"] = "verified"
    issues = validate_quality_checks(checks, PANEL_CHECK_IDS)
    self.assertIn("quality-evidence-generic", issues)
```

Also prove schema 1.0 records remain readable, migration derives only supplied hashes/dimensions, omitted reviewer evidence remains explicitly unresolved, canonical hashing ignores no fields, and a failed migration leaves source bytes unchanged.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest tests.test_quality_records -v
```

Expected: import failure for `quality_records`.

- [ ] **Step 3: Implement immutable shared records and validators**

Use frozen dataclasses only for internal typed values; persisted records remain canonical JSON dictionaries. Normalize evidence with NFC, collapsed whitespace, and lowercase comparison solely for generic/duplicate detection. Reject unknown/missing/duplicate check IDs and private absolute paths. Implement migration as a pure function; the caller performs atomic publication.

The schema 2.0 common envelope is:

```python
{
    "schema_version": "2.0",
    "kind": "panel-qa" or "page-qa",
    "subject_id": str,
    "bindings": {str: str | int | list},
    "checks": list[dict],
    "review": {
        "method": str,
        "reviewer": str,
        "reviewed_at": str,
    },
    "decision": "accept" | "accept-warning" | "regenerate",
    "unresolved_warnings": list[str],
}
```

Keep kind-specific fields outside the common envelope only when the other kind cannot interpret them.

- [ ] **Step 4: Upgrade templates and validation gates**

Make new templates schema 2.0 with the exact panel/page check IDs. Extend validation so schema 1.0 records report `quality-migration-required` at RC2 gates but remain readable by status/report paths. Do not mutate during validation.

Update wheel resource contracts so `quality_records.py` is bundled and build-only scripts remain excluded.

- [ ] **Step 5: Run focused and regression tests**

Run:

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_quality_records tests.test_validation tests.test_clean_install -v
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass; only the existing native-Windows symlink skip is permitted locally.

- [ ] **Step 6: Commit the contract**

```bash
git add scripts/quality_records.py tests/test_quality_records.py \
  templates/panel-record.json templates/page-qa.json \
  scripts/validate_project.py tests/test_validation.py setup.py \
  comic_sol_product/release.py
git commit -m "feat: define Comic Quality record contract"
```

### Task 2: Deterministic Normalization and Panel Provenance

**Files:**
- Create: `scripts/normalize_panels.py`
- Create: `tests/test_normalization.py`
- Modify: `scripts/comic_sol.py`
- Modify: `scripts/validate_project.py`
- Modify: `scripts/render_report.py`
- Modify: `tests/test_finalization.py`
- Modify: `tests/test_report.py`
- Modify: `setup.py`

**Interfaces:**
- Produces: `NormalizationSpec`, `normalization_geometry(source_size, target_size, mode) -> NormalizationGeometry`, `normalize_panel(project_dir, panel_id, source_relative, target_size, mode) -> Path`, and `validate_panel_provenance(project_dir, record) -> tuple[ValidationIssue, ...]`.
- Persists: `panels/<panel-id>/normalization.json` and canonical `panels/<panel-id>/clean.png`.
- Consumes: Task 1 quality hashing/evidence contract and existing transaction/path APIs.

- [ ] **Step 1: Write failing normalization matrix tests**

Generate synthetic PNG, JPEG, and WebP fixtures with distinguishable colored regions. Cover landscape, portrait, exact aspect ratio, EXIF orientations 3/6/8, crop boundaries, invalid mode, unreadable image, decompression limit, traversal, and batch preflight.

Assert explicit geometry, for example:

```python
def test_center_crop_records_oriented_source_box(self):
    geometry = normalization_geometry((1200, 800), (600, 600), "crop")
    self.assertEqual((200, 0, 1000, 800), geometry.crop_box)
    self.assertEqual((600, 600), geometry.target_size)
```

Test that a second identical run is byte-identical and that any failure leaves an existing `clean.png` and record unchanged.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest tests.test_normalization -v
```

Expected: missing module `normalize_panels`.

- [ ] **Step 3: Implement geometry and canonical publication**

Decode with Pillow safety limits, call `ImageOps.exif_transpose()` exactly once, convert to RGB, compute integer source-oriented crop coordinates, resize with `Image.Resampling.LANCZOS`, and encode PNG using fixed options. Publish record and clean image in one `ProjectTransaction`.

Normalization record fields must include source path/format/size/orientation/hash, target size, mode, crop box, clean path/mode/size/hash, and `implementation_version: "1"`.

- [ ] **Step 4: Bind panel QA and state progress to normalization**

Extend panel validation to recompute raw/clean/record/storyboard/reference/attempt hashes. Raw change invalidates normalization onward; clean or record mismatch reports `quality-record-stale`. `QA_READY` requires current normalization and schema 2.0 panel QA for every panel.

Report normalization mode and dimensions without absolute paths or authored prompts.

- [ ] **Step 5: Prove targeted invalidation and transactionality**

Add tests where one panel changes in a two-panel project. Assert only its downstream records and affected page chain become invalid while unrelated retained files stay byte-identical. Assert resume reports affected artifacts before applying invalidation.

- [ ] **Step 6: Run focused, lifecycle, and full tests**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_normalization tests.test_validation tests.test_finalization \
  tests.test_report tests.test_resume -v
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all pass with no new skip.

- [ ] **Step 7: Commit normalization and provenance**

```bash
git add scripts/normalize_panels.py tests/test_normalization.py \
  scripts/comic_sol.py scripts/validate_project.py scripts/render_report.py \
  tests/test_finalization.py tests/test_report.py setup.py
git commit -m "feat: bind panel QA to normalized artwork"
```

### Task 3: Typography Preflight and Lettering Geometry

**Files:**
- Create: `scripts/typography.py`
- Create: `tests/test_typography.py`
- Modify: `scripts/letter_panels.py`
- Modify: `tests/test_lettering.py`
- Modify: `scripts/validate_project.py`
- Modify: `setup.py`

**Interfaces:**
- Produces: `TypographyIssue`, `preflight_text_items(items, font_policy) -> dict`, `write_typography_preflight(project_dir, panel_id, result) -> Path`, and `lettering_geometry_hash(record) -> str`.
- Persists: `panels/<panel-id>/typography.json` and `panels/<panel-id>/lettering.json`.
- Consumes: existing `normalize_content()`, cmap parser, font paths, clean-image descriptor, Task 1 canonical hashing, and Task 2 clean artifacts.

- [ ] **Step 1: Write failing preflight policy tests**

Cover regular/bold Latin, Greek, Cyrillic, combining marks, whitespace/control policy, missing glyph, and unsupported shaping for Arabic, CJK, and emoji. Assert failures identify code point, item ID, style, checked font identifiers, and remediation without absolute paths.

```python
def test_unsupported_codepoint_blocks_before_output_mutation(self):
    before = existing_lettered.read_bytes()
    with self.assertRaisesRegex(TypographyPreflightError, r"U\+1F600.*dialogue-1"):
        letter_project(project)
    self.assertEqual(before, existing_lettered.read_bytes())
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest tests.test_typography -v
```

Expected: missing `typography` module.

- [ ] **Step 3: Extract font policy without changing rendering**

Move reusable cmap/font-selection policy from `letter_panels.py` into `typography.py`. Preserve existing rendering output for supported text. Use package-relative font identifiers such as `ComicNeue-Regular.ttf`, never `Path.resolve()` values in persisted data.

A successful preflight records each normalized visible code point/style/font selection plus input storyboard and font-policy hashes. Unsupported shaping is distinct from missing glyph coverage.

- [ ] **Step 4: Publish lettering geometry transactionally**

Before any panel mutation, preflight every panel in the batch. During render, record text-item ID/kind/order, anchor, bounding box, tail origin/target, font runs, clean hash, and lettered hash. Publish image and geometry in one transaction.

Keep authored storyboard text unchanged; uppercase dialogue remains display-only.

- [ ] **Step 5: Strengthen LETTERED validation**

Require current preflight and geometry for each lettered panel. Reject `.notdef`, stale clean/storyboard/font hashes, missing boxes, invalid tail coordinates, duplicate reading order, and unsupported script policy.

- [ ] **Step 6: Run visual regressions and full suite**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_typography tests.test_lettering tests.test_validation -v
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: supported existing lettering snapshots/metrics remain within their established bounds; all tests pass.

- [ ] **Step 7: Commit typography slice**

```bash
git add scripts/typography.py tests/test_typography.py \
  scripts/letter_panels.py tests/test_lettering.py \
  scripts/validate_project.py setup.py
git commit -m "feat: preflight typography before lettering"
```

### Task 4: Layout Registry, Four-Panel Composition, and Page QA

**Files:**
- Create: `scripts/layouts.py`
- Create: `scripts/page_quality.py`
- Create: `tests/test_layouts.py`
- Create: `tests/test_page_quality.py`
- Modify: `scripts/compose_pages.py`
- Modify: `tests/test_composition.py`
- Modify: `tests/test_compose.py`
- Modify: `scripts/validate_project.py`
- Modify: `scripts/render_report.py`
- Modify: `setup.py`

**Interfaces:**
- Produces: `LayoutDefinition`, `get_layout(name) -> LayoutDefinition`, `match_layout(rectangles) -> str`, `compose_all_pages(project_dir) -> list[Path]` with enriched cache, and `validate_page_quality(project_dir, page_number) -> tuple[ValidationIssue, ...]`.
- Persists: schema 2.0 `qa/pages/page-<NNN>.json`.
- Consumes: Task 3 lettering boxes/hashes and Task 1 quality checks.

- [ ] **Step 1: Write failing layout registry tests**

Define existing supported layouts from current fixtures plus `four-grid`, whose ordered rectangles are:

```python
FOUR_GRID_RECTS = (
    (50, 50, 740, 1135),
    (810, 50, 740, 1135),
    (50, 1215, 740, 1135),
    (810, 1215, 740, 1135),
)
```

Assert positive integer dimensions, canonical page containment, no overlap, unique complete order, deterministic lookup, custom-layout validation, and border/gutter invariants.

- [ ] **Step 2: Write failing page-QA tests**

Require exact IDs:

```python
PAGE_CHECK_IDS = (
    "clipped-text", "text-overlap", "face-action-obstruction",
    "bubble-tail-direction", "reading-order",
    "accidental-text-watermark", "layout-border-integrity",
)
```

Prove clipping, overlap, reading order, and border checks can be derived from geometry while subjective checks require explicit reviewer evidence/method. Missing, generic, or stale page evidence blocks export.

- [ ] **Step 3: Verify RED**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_layouts tests.test_page_quality -v
```

Expected: missing `layouts` and `page_quality` modules.

- [ ] **Step 4: Implement registry and enriched composition cache**

Move rectangle invariants into `layouts.py`. Retain explicit storyboard rectangles for compatibility and label valid unmatched geometry `custom`. Bind each page cache entry to layout/version, storyboard page hash, ordered lettering hashes, settings, output dimensions, and PNG hash.

Compose all pages only after complete preflight; publish pages/cache/manifest transactionally.

- [ ] **Step 5: Implement page QA and state gates**

Create deterministic geometry observations first, then merge bounded agent/human observations through Task 1 validation. Bind record to page/cache/layout and ordered lettering hashes. Require current page QA before `EXPORTED` and `COMPLETE`.

Report method identity per check so deterministic mechanics never masquerade as visual judgment.

- [ ] **Step 6: Add golden layout coverage**

For each named layout, render synthetic high-contrast panels and assert dimensions, slot sample regions, border pixels, reading order, and bounded whole-image metrics. Use byte hashes only after proving cross-platform stability.

- [ ] **Step 7: Run page slice and full regression**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_layouts tests.test_page_quality tests.test_composition \
  tests.test_compose tests.test_validation tests.test_report -v
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all pass; four-panel fixture produces a valid canonical page.

- [ ] **Step 8: Commit trustworthy pages**

```bash
git add scripts/layouts.py scripts/page_quality.py \
  tests/test_layouts.py tests/test_page_quality.py \
  scripts/compose_pages.py tests/test_composition.py tests/test_compose.py \
  scripts/validate_project.py scripts/render_report.py setup.py
git commit -m "feat: add trustworthy four-panel pages"
```

### Task 5: Full-Content PDF Verification

**Files:**
- Create: `scripts/pdf_quality.py`
- Create: `tests/test_pdf_quality.py`
- Modify: `scripts/export_pdf.py`
- Modify: `tests/test_export_pdf.py`
- Modify: `scripts/validate_project.py`
- Modify: `tests/test_finalization.py`
- Modify: `scripts/render_report.py`
- Modify: `setup.py`

**Interfaces:**
- Produces: `PdfPageMetrics`, `compare_full_page(source, decoded) -> PdfPageMetrics`, `verify_pdf_payload(payload, source_pages) -> dict`, and `PDF_TOLERANCE_VERSION = "1"`.
- Persists: `exports/pdf-verification.json` and canonical PDF descriptor bindings.
- Consumes: contiguous source page paths/hashes and current page-QA descriptors from Task 4.

- [ ] **Step 1: Write failing metric calibration tests**

Generate pinned-stack PDF round trips from synthetic pages containing corner, center, fine lettering, and large flat regions. Record mean absolute channel error, high-error pixel ratio, maximum deterministic-grid regional error, size, and mode.

Tests establish named fixed thresholds by accepting expected Pillow output and rejecting mutations. Threshold constants must be derived from observed fixture bounds plus a documented margin, not user configuration.

- [ ] **Step 2: Write required corruption tests**

Construct failures for center erasure, lettering erasure, swapped pages, duplicate pages, truncation/corruption, page-count mismatch, dimension mismatch, and content beyond tolerance. In every case seed a previous canonical PDF/descriptor and assert both remain byte-identical after failure.

- [ ] **Step 3: Verify RED**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest tests.test_pdf_quality -v
```

Expected: missing `pdf_quality` module.

- [ ] **Step 4: Implement decoded full-page comparison**

Decode every frame, normalize to RGB/canonical dimensions, and compare all pixels. Compute metrics using integer accumulation to avoid platform-dependent floating reduction order. Divide only for final canonical decimal values.

Reject order/content mismatch using both source binding order and metrics. Remove four-corner sampling as an acceptance gate; corner values may remain diagnostic only.

- [ ] **Step 5: Publish PDF and verification atomically**

Export temporary PDF, flush with writable descriptor, verify it, then transactionally publish PDF, verification record, and manifest descriptor. Bind ordered source hashes, PDF hash, exporter version, tolerance version, metrics, count, dimensions, and UTC timestamp.

`EXPORTED` requires current page QA before export. `COMPLETE` requires current PDF verification and report.

- [ ] **Step 6: Run focused, finalization, and full tests**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_pdf_quality tests.test_export_pdf tests.test_finalization \
  tests.test_validation tests.test_report -v
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all corruption fixtures fail closed; pinned-stack output passes.

- [ ] **Step 7: Commit verified PDF pipeline**

```bash
git add scripts/pdf_quality.py tests/test_pdf_quality.py \
  scripts/export_pdf.py tests/test_export_pdf.py scripts/validate_project.py \
  tests/test_finalization.py scripts/render_report.py setup.py
git commit -m "feat: verify complete PDF page content"
```

### Task 6: Deterministic Quality Matrix and End-to-End Lifecycle

**Files:**
- Create: `tests/fixtures/quality-matrix/README.md`
- Create: `tests/test_quality_matrix.py`
- Create: `scripts/quality_sample.py`
- Modify: `tests/support.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `scripts/render_report.py`
- Modify: `SKILL.md`
- Modify: `references/workflow.md`
- Modify: `setup.py`
- Modify: `comic_sol_product/release.py`

**Interfaces:**
- Produces: `build_quality_fixture(root, scenario) -> Path` and a local-only sample runner that labels `deterministic` versus `live-visual` evidence.
- Consumes: Tasks 1–5 complete lifecycle and existing exact 17-tool MCP server.

- [ ] **Step 1: Write failing matrix contract tests**

Define scenario names for two recurring characters, wardrobe/prop/palette continuity, all layouts, dense dialogue/caption/SFX, portrait/landscape, PNG/JPEG/WebP/EXIF, regular/bold/combining/non-Latin fallback, transient repeat, visual retry, accepted warning, hard failure, and interrupted resume.

Assert the fixture manifest lists each required dimension and that every generated project is local-only and deterministic.

- [ ] **Step 2: Verify RED**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest tests.test_quality_matrix -v
```

Expected: missing quality-matrix fixture builder.

- [ ] **Step 3: Build compact programmatic fixtures**

Generate synthetic source images at test time rather than committing large binaries. Keep only small authored JSON/README assets when they clarify a scenario. Reuse one base project builder and explicit scenario transforms.

- [ ] **Step 4: Add complete engine and MCP lifecycle tests**

Drive init through normalization, panel QA, typography, lettering, composition, page QA, verified PDF, report, and `COMPLETE`. Add interrupted-resume coverage. Repeat the canonical lifecycle through MCP and assert discovery remains exactly 17 tools.

- [ ] **Step 5: Add evidence labels to report and Skill**

Document that deterministic checks prove mechanics only. Live visual evidence must record provider/model when exposed, attempt hashes, references, reviewer method, and known limitations. The sample runner must refuse live mode without an explicitly supplied retained attempt; it never calls a provider itself.

- [ ] **Step 6: Verify package resource boundaries**

Build wheel/sdist, validate required quality modules/templates/references, and reject fixture/test/build scripts from runtime engine contents. Run clean base/MCP install outside checkout.

- [ ] **Step 7: Run matrix, protocol, package, and full tests**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_quality_matrix tests.test_mcp_server tests.test_clean_install -v
rm -rf dist build comic_sol.egg-info
/home/acer/.venvs/comic-sol-mcp/bin/python -m build
/home/acer/.venvs/comic-sol-mcp/bin/python -m comic_sol_product.release \
  dist/*.whl dist/*.tar.gz
/home/acer/.venvs/comic-sol-mcp/bin/python scripts/clean_install_smoke.py \
  --wheel dist/*.whl --mcp
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all pass and package validators report no forbidden members.

- [ ] **Step 8: Commit quality matrix**

```bash
git add tests/fixtures/quality-matrix/README.md tests/test_quality_matrix.py \
  scripts/quality_sample.py tests/support.py tests/test_mcp_server.py \
  scripts/render_report.py SKILL.md references/workflow.md setup.py \
  comic_sol_product/release.py
git commit -m "test: add Comic Quality evidence matrix"
```

### Task 7: RC2 Version, Distribution Regression, and Public Release Verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `comic_sol_product/__init__.py`
- Modify: `CHANGELOG.md`
- Create: `docs/releases/v2.0.0rc2.md`
- Modify: `README.md`
- Modify: `docs/install.md`
- Modify: `.github/workflows/tests.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_distribution.py`
- Modify: `tests/test_release_docs.py`

**Interfaces:**
- Produces: immutable `v2.0.0rc2` native/source release assets with factual unsigned metadata and CycloneDX SBOM.
- Consumes: all Comic Quality gates and RC1 distribution pipeline.

- [ ] **Step 1: Write failing RC2 release contracts**

Require package/tag/version consistency, release notes, all prior asset classes, quality modules in wheel/frozen runtime, exact 17-tool smoke, non-root OCI, and tag-only prerelease publication. Assert workflow actions remain pinned to full commit SHAs.

- [ ] **Step 2: Verify RED against RC1 identity**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest \
  tests.test_distribution tests.test_release_docs -v
```

Expected: failures showing `2.0.0rc1` where `2.0.0rc2` is required.

- [ ] **Step 3: Update version, docs, and workflows**

Set both package version sources to `2.0.0rc2`, workflow tag guard to `refs/tags/v2.0.0rc2`, image/version labels to RC2, and release notes to exact Comic Quality gates/limitations. Keep signing status `unsigned` unless real vendor signing is introduced separately.

- [ ] **Step 4: Run complete local acceptance**

```bash
/home/acer/.venvs/comic-sol-mcp/bin/python -m unittest discover -s tests -v
rm -rf dist build comic_sol.egg-info .native-dist/rc2
/home/acer/.venvs/comic-sol-mcp/bin/python -m build
/home/acer/.venvs/comic-sol-mcp/bin/python -m comic_sol_product.release \
  dist/*.whl dist/*.tar.gz
/home/acer/.venvs/comic-sol-mcp/bin/python scripts/clean_install_smoke.py \
  --wheel dist/*.whl
/home/acer/.venvs/comic-sol-mcp/bin/python scripts/clean_install_smoke.py \
  --wheel dist/*.whl --mcp
/home/acer/.venvs/comic-sol-mcp/bin/python scripts/build_portable.py \
  --wheel dist/*.whl --output .native-dist/rc2
/home/acer/.venvs/comic-sol-mcp/bin/python scripts/portable_release_smoke.py \
  --runtime .native-dist/rc2/comic-sol
git diff --check
```

Expected: full suite, package validation, both clean installs, frozen runtime, doctor, and 17-tool MCP smoke pass.

- [ ] **Step 5: Commit RC2 preparation**

```bash
git add pyproject.toml comic_sol_product/__init__.py CHANGELOG.md \
  docs/releases/v2.0.0rc2.md README.md docs/install.md \
  .github/workflows/tests.yml .github/workflows/release.yml \
  tests/test_distribution.py tests/test_release_docs.py
git commit -m "docs: prepare v2.0.0rc2 release"
```

- [ ] **Step 6: Push, open PR, and satisfy merge gates**

Push `ai/comic-quality`, open a PR to `ai/post-event-development`, and require every Linux/macOS/Windows base/MCP, quality, native runtime, and OCI check to pass. Fix platform failures test-first on the feature branch. Squash-merge only with a clean mergeable PR and all checks successful.

- [ ] **Step 7: Tag and verify public prerelease**

Tag the verified merge commit `v2.0.0rc2`. Wait for all native/source/container/publish jobs. Download release assets into a fresh temporary directory and verify:

```bash
sha256sum --check SHA256SUMS
```

Then verify metadata says `signature_status: unsigned`, SBOM says CycloneDX, Linux archive install/doctor/MCP/PDF/uninstall smoke passes, projects remain preserved, and remote annotated tag resolves to the merge commit.

- [ ] **Step 8: Clean merged worktree safely**

Remove the `.worktrees/comic-quality` worktree only after merge/release verification. Use safe branch deletion; retain the local branch if squash history makes `git branch -d` refuse. Never force-delete solely for housekeeping.
