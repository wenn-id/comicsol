# PR #56 Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all still-valid findings on `wenn-id/comicsol#56`, make QA schema 2.0 canonical with schema-1.0 read compatibility, restore integrity under malformed/concurrent inputs, reach at least 80% docstring coverage, and push a fully verified branch update.

**Architecture:** Root runtime modules under `scripts/` remain the authoring source and are mirrored byte-for-byte into `skills/comic-sol/scripts/`. QA v2 records use ordered shared check IDs, bounded evidence, artifact bindings, review provenance, and schema-specific decision vocabulary; legacy v1 records remain readable. Transactional writers operate on one source snapshot and re-read shared manifests only after acquiring the project lock.

**Tech Stack:** Python 3.11, Pillow 12.3.0, `unittest`, GitHub CLI, Codex plugin JSON/Markdown assets.

## Global Constraints

- Preserve Python 3.11 compatibility and the existing Pillow 12.3.0 pin.
- Add no new runtime or test dependency.
- Follow strict TDD for every behavioral fix: observe the focused regression test fail before editing production code.
- Keep `scripts/*.py` and matching `skills/comic-sol/scripts/*.py` files byte-identical after every task.
- Keep schema-1.0 panel/page records readable; emit and document schema 2.0 as canonical.
- Do not reply to or manually resolve GitHub review threads.
- Do not weaken containment, symlink, transaction, safety/IP, or visual-QA gates.
- Keep changes limited to the 46 unresolved findings and the 80% docstring warning.

---

### Task 1: Ordered shared quality-check validation

**Files:**
- Modify: `scripts/quality_records.py`
- Modify: `scripts/page_quality.py`
- Modify: `scripts/validate_project.py`
- Modify: `scripts/render_report.py`
- Modify: `tests/test_page_quality.py`
- Modify: `tests/test_validation.py`
- Modify: `tests/test_report.py`
- Mirror: `skills/comic-sol/scripts/quality_records.py`
- Mirror: `skills/comic-sol/scripts/page_quality.py`
- Mirror: `skills/comic-sol/scripts/validate_project.py`
- Mirror: `skills/comic-sol/scripts/render_report.py`

**Interfaces:**
- Consumes: `PANEL_CHECK_IDS`, `PAGE_CHECK_IDS`, and existing check dictionaries.
- Produces: `validate_quality_checks(checks, expected_ids)` with exact sequence validation; an immutable `PageContext`; no fabricated check records.

- [ ] **Step 1: Add failing order and subset tests**

Add focused cases equivalent to:

```python
def test_quality_checks_require_normative_order(self):
    record = valid_panel_record_v2()
    record["checks"] = list(reversed(record["checks"]))
    self.assert_issue(validate_panel_record(record), "quality-check-ids")

def test_subjective_page_checks_use_their_normative_subset(self):
    checks = reviewer_checks(self.project)
    checks[0], checks[1] = checks[1], checks[0]
    with self.assertRaisesRegex(ValueError, "quality-check-ids"):
        build_page_quality_record(
            self.project,
            1,
            checks,
            reviewer="fixture-reviewer",
            reviewed_at="2026-08-14T00:00:00Z",
        )
```

Update `reviewer_checks` to order its three entries by their position in
`PAGE_CHECK_IDS`, not alphabetically.

- [ ] **Step 2: Add failing deterministic-evidence and strict-pairing tests**

```python
def test_passing_deterministic_checks_have_no_failure_regions(self):
    record = self._build_record()
    deterministic = {
        check["id"]: check for check in record["checks"]
        if check["id"] in DETERMINISTIC_PAGE_CHECK_IDS
    }
    self.assertTrue(all(check["regions"] == [] for check in deterministic.values()))

def test_page_context_rejects_lettering_panel_count_mismatch(self):
    geometry = self.project / "panels/p01-01/lettering.json"
    geometry.unlink()
    with self.assertRaisesRegex(ValueError, "lettering|panel"):
        build_page_quality_record(
            self.project,
            1,
            reviewer_checks(self.project),
            reviewer="fixture-reviewer",
            reviewed_at="2026-08-14T00:00:00Z",
        )
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_validation.StrictSchemaValidationTests.test_panel_record_v2_uses_shared_quality_contract `
  tests.test_page_quality.PageQualityTests -v
```

Expected: failures show reversed IDs are accepted, passing checks contain layout
regions, or a mismatched pairing is silently truncated.

- [ ] **Step 4: Implement exact ordered validation and real subset validation**

Change the shared validator to use direct sequence comparison:

```python
ids = tuple(
    item.get("id") if isinstance(item, dict) else None
    for item in checks
)
if ids != tuple(expected_ids):
    issues.add("quality-check-ids")
```

Define the subjective subset once in page order:

```python
SUBJECTIVE_PAGE_CHECK_IDS = tuple(
    check_id for check_id in PAGE_CHECK_IDS
    if check_id not in DETERMINISTIC_PAGE_CHECK_IDS
)
```

Validate reviewer checks directly against `SUBJECTIVE_PAGE_CHECK_IDS`. Delete
the four synthetic pass records. Set deterministic `regions` to `failures`.
Replace the heterogeneous `_page_context` mapping with a frozen `PageContext`
dataclass and use `zip(..., strict=True)` where lettering and panels must pair.

Remove local `CHECK_IDS` tuples from `validate_project.py` and
`render_report.py`; import and use `PANEL_CHECK_IDS`.

- [ ] **Step 5: Mirror changed runtime files**

```powershell
Copy-Item scripts\quality_records.py skills\comic-sol\scripts\quality_records.py
Copy-Item scripts\page_quality.py skills\comic-sol\scripts\page_quality.py
Copy-Item scripts\validate_project.py skills\comic-sol\scripts\validate_project.py
Copy-Item scripts\render_report.py skills\comic-sol\scripts\render_report.py
```

- [ ] **Step 6: Run focused tests and confirm GREEN**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_page_quality tests.test_validation tests.test_report -v
```

Expected: all selected tests pass with no warnings or errors.

- [ ] **Step 7: Commit Task 1**

```powershell
git add scripts skills/comic-sol/scripts tests/test_page_quality.py tests/test_validation.py tests/test_report.py
git commit -m "fix: enforce ordered quality evidence"
```

### Task 2: Canonical page-QA v2 provenance and warning semantics

**Files:**
- Modify: `scripts/page_quality.py`
- Modify: `scripts/validate_project.py`
- Modify: `scripts/render_report.py`
- Modify: `templates/page-qa.json`
- Modify: `skills/comic-sol/templates/page-qa.json`
- Modify: `skills/comic-sol/references/schemas.md`
- Modify: `skills/comic-sol/references/visual-qa.md`
- Modify: `tests/test_page_quality.py`
- Modify: `tests/test_validation.py`
- Modify: `tests/test_report.py`
- Mirror: matching runtime files under `skills/comic-sol/scripts/`

**Interfaces:**
- Consumes: current composition, storyboard, lettering, and three subjective reviewer checks.
- Produces: `build_page_quality_record(..., *, reviewer: str, reviewed_at: str)` and fully validated schema-2.0 page records.

- [ ] **Step 1: Add failing provenance tests**

```python
def test_page_record_uses_supplied_review_provenance(self):
    record = build_page_quality_record(
        self.project,
        1,
        reviewer_checks(self.project),
        reviewer="alwan-review",
        reviewed_at="2026-08-14T01:02:03Z",
    )
    self.assertEqual("alwan-review", record["review"]["reviewer"])
    self.assertEqual("2026-08-14T01:02:03Z", record["review"]["reviewed_at"])

def test_page_validation_rejects_fixture_or_invalid_provenance(self):
    record = self._build_record()
    record["review"]["reviewed_at"] = "fixture-deterministic"
    write_page_quality_record(self.project, 1, record)
    self.assertTrue(any(issue.field == "review.reviewed_at"
                        for issue in validate_page_quality(self.project, 1)))
```

- [ ] **Step 2: Add failing warning and tail-consistency tests**

```python
def test_tail_warning_requires_failed_region_and_records_warning(self):
    checks = reviewer_checks(self.project)
    tail = next(check for check in checks
                if check["id"] == "bubble-tail-direction")
    tail["result"] = "warning"
    tail["severity"] = "warning"
    tail["evidence"] = "One tail terminates slightly wide of its speaker."
    tail["regions"][0]["result"] = "fail"
    record = self._build_record(checks)
    self.assertEqual("accept-warning", record["decision"])
    self.assertEqual([tail["evidence"]], record["unresolved_warnings"])

def test_tail_warning_cannot_hide_all_passing_regions(self):
    checks = reviewer_checks(self.project)
    tail = next(check for check in checks
                if check["id"] == "bubble-tail-direction")
    tail.update({"result": "warning", "severity": "warning"})
    with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
        self._build_record(checks)
```

- [ ] **Step 3: Run the page-QA suite and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest tests.test_page_quality -v
```

Expected: the builder rejects the new keyword arguments or still emits fixture
provenance and silently accepts a warning result.

- [ ] **Step 4: Implement provenance and decision validation**

Add keyword-only `reviewer` and `reviewed_at` arguments. Require a non-empty
reviewer and validate the timestamp with the same ISO-8601 rule used by panel
v2 records. Build decisions as:

```python
failures = [check for check in checks if check["result"] == "fail"
            and check["severity"] == "error"]
warnings = [check for check in checks if check["result"] == "warning"
            or check["severity"] == "warning"]
decision = "regenerate" if failures else "accept-warning" if warnings else "accept"
unresolved_warnings = [check["evidence"] for check in warnings]
```

Require a warning tail check to contain at least one failed bounded region.
Validate page `review`, decision/check consistency, and warning pairing in
`validate_page_quality`.

- [ ] **Step 5: Align templates and normative documentation**

Keep schema 2.0 in both page templates. Expand `bindings` to the exact keys the
builder emits, keep the seven ordered checks, use empty reviewer/timestamp
fields to force completion, and document the complete v2 record in
`schemas.md`. State that legacy five-field page records are schema 1.0 inputs
requiring migration. Document region ownership and warning behavior in
`visual-qa.md`.

- [ ] **Step 6: Retain legacy page records in reports**

Load page records whose shape is either schema 2.0 `page-qa` or legacy schema
1.0. Sort v2 by `subject_id` and v1 by zero-padded `page`; render v1 as
`quality-migration-required` rather than omitting it.

- [ ] **Step 7: Mirror runtime files and run GREEN verification**

```powershell
Copy-Item scripts\page_quality.py skills\comic-sol\scripts\page_quality.py
Copy-Item scripts\validate_project.py skills\comic-sol\scripts\validate_project.py
Copy-Item scripts\render_report.py skills\comic-sol\scripts\render_report.py
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_page_quality tests.test_validation tests.test_report -v
```

- [ ] **Step 8: Commit Task 2**

```powershell
git add scripts skills/comic-sol tests/test_page_quality.py tests/test_validation.py tests/test_report.py templates/page-qa.json
git commit -m "fix: make page QA v2 authoritative"
```

### Task 3: Canonical panel-QA v2 resume and decision behavior

**Files:**
- Modify: `scripts/comic_sol.py`
- Modify: `scripts/validate_project.py`
- Modify: `templates/panel-record.json`
- Modify: `skills/comic-sol/templates/panel-record.json`
- Modify: `skills/comic-sol/references/schemas.md`
- Modify: `tests/test_resume.py`
- Modify: `tests/test_validation.py`
- Mirror: `skills/comic-sol/scripts/comic_sol.py`
- Mirror: `skills/comic-sol/scripts/validate_project.py`

**Interfaces:**
- Consumes: legacy v1 records using `panel_id` and v2 records using `subject_id` plus `bindings`.
- Produces: schema-aware resume actions and accepted-artifact validation.

- [ ] **Step 1: Add a reusable valid v2 project record fixture**

Build a schema-2.0 record using the real project artifacts:

```python
def _panel_record_v2(self, panel_id="p01-01"):
    raw = self.project / f"panels/raw/{panel_id}.png"
    clean = self.project / f"panels/{panel_id}/clean.png"
    normalization = self.project / f"panels/{panel_id}/normalization.json"
    return {
        "schema_version": "2.0",
        "kind": "panel-qa",
        "subject_id": panel_id,
        "bindings": {
            "raw_path": f"panels/raw/{panel_id}.png",
            "raw_sha256": sha256_file(raw),
            "raw_width": 736,
            "raw_height": 1136,
            "clean_path": f"panels/{panel_id}/clean.png",
            "clean_sha256": sha256_file(clean),
            "clean_width": 736,
            "clean_height": 1136,
            "normalization_path": f"panels/{panel_id}/normalization.json",
            "normalization_sha256": sha256_file(normalization),
        },
        "checks": self._quality_checks(),
        "review": {
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-08-14T00:00:00Z",
        },
        "decision": "accept",
        "unresolved_warnings": [],
    }
```

- [ ] **Step 2: Add failing schema-dispatch and conflict tests**

```python
def test_resume_matches_valid_v2_record_by_subject_id(self):
    self._write_json("qa/panels/p01-01.json", self._panel_record_v2())
    actions = build_resume_plan(self.project)
    action = next(item for item in actions if item.artifact == "p01-01")
    self.assertEqual("reuse", action.action)

def test_v2_panel_record_rejects_legacy_panel_id(self):
    record = valid_panel_record_v2()
    record["panel_id"] = record["subject_id"]
    self.assert_issue(validate_panel_record(record), "panel_id")
```

Add cases showing error-severity fail requires `regenerate`, and a warning
check requires `accept-warning` or `regenerate`.

- [ ] **Step 3: Run focused tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_resume.ResumeTests `
  tests.test_validation.StrictSchemaValidationTests.test_panel_record_v2_uses_shared_quality_contract -v
```

- [ ] **Step 4: Implement schema-aware accepted-panel validation**

Dispatch identity, decisions, paths, hashes, and dimensions by version. For v2,
read `subject_id` and `bindings`; for v1, retain the current fields. Treat v2
`accept-warning` and v1 `accept_with_warnings` as accepted. Run
`validate_panel_record` before filesystem reuse checks and reject any extra
legacy identifier on v2 through the exact-field schema.

In `build_resume_plan`, select identity before the `isinstance` guard:

```python
panel_id = (
    record.get("subject_id")
    if record.get("schema_version") == "2.0"
    else record.get("panel_id")
)
```

- [ ] **Step 5: Enforce v2 decision/check consistency**

Apply the same rules at every stage:

```python
if any(check["result"] == "fail" and check["severity"] == "error"
       for check in checks) and decision != "regenerate":
    issue("decision", "error-level failures require regenerate")
if any(check["result"] == "warning" or check["severity"] == "warning"
       for check in checks) and decision not in {"accept-warning", "regenerate"}:
    issue("decision", "warnings require accept-warning or regenerate")
```

- [ ] **Step 6: Align panel templates and schema documentation**

Keep panel templates on schema 2.0 with canonical `subject_id`, `bindings`,
seven ordered rich checks, `review`, `decision`, and `unresolved_warnings`.
Replace placeholder `panel-id` paths with canonical `p01-01` paths. Rewrite the
panel section of `schemas.md` as v2 and retain a compact legacy-v1 migration
section.

- [ ] **Step 7: Mirror, verify, and commit**

```powershell
Copy-Item scripts\comic_sol.py skills\comic-sol\scripts\comic_sol.py
Copy-Item scripts\validate_project.py skills\comic-sol\scripts\validate_project.py
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_resume tests.test_validation -v
git add scripts skills/comic-sol templates/panel-record.json tests/test_resume.py tests/test_validation.py
git commit -m "fix: resume canonical panel QA records"
```

### Task 4: Consistent composition and PDF transaction snapshots

**Files:**
- Modify: `scripts/compose_pages.py`
- Modify: `scripts/export_pdf.py`
- Modify: `tests/test_composition.py`
- Modify: `tests/test_export_pdf.py`
- Modify: `tests/test_concurrency.py`
- Mirror: matching files under `skills/comic-sol/scripts/`

**Interfaces:**
- Consumes: lettered panel bytes, current project manifest, composed pages, and page-QA records.
- Produces: composition cache hashes for exactly composed bytes and locked-manifest PDF descriptors.

- [ ] **Step 1: Add failing single-read composition test**

Patch the contained read helper so a second read returns different pixels, then
assert each source is read once and the cache hashes the first payload:

```python
def test_composition_hashes_the_exact_source_bytes_it_composes(self):
    source = self.project / "panels/p01-01/lettered.png"
    original = source.read_bytes()
    with patch("compose_pages.read_contained_bytes", wraps=lambda *_: original) as read:
        compose_all_pages(self.project)
    cache = json.loads((self.project / "cache/composition.json").read_text("utf-8"))
    self.assertEqual(1, read.call_count)
    self.assertIn(hashlib.sha256(original).hexdigest(),
                  cache["pages"][0]["ordered_lettered_sha256s"][0])
```

- [ ] **Step 2: Add failing export race, containment, and missing-QA tests**

```python
def test_guarded_export_preserves_manifest_change_made_before_lock(self):
    def mutate_manifest(*args, **kwargs):
        manifest = read_json(self.project / "project.json")
        manifest["warnings"] = ["concurrent warning"]
        atomic_write_json(self.project / "project.json", manifest)
        return self._valid_pdf_payload_and_metrics()
    with mock.patch("export_pdf._render_verified_payload", side_effect=mutate_manifest):
        guarded_export(self.project)
    self.assertEqual(["concurrent warning"],
                     read_json(self.project / "project.json")["warnings"])

def test_guarded_export_rejects_traversal_before_creating_directory(self):
    outside = self.project.parent / "outside" / "comic.pdf"
    with self.assertRaisesRegex(PdfExportError, "inside the project"):
        guarded_export(self.project, self.project / ".." / "outside" / "comic.pdf")
    self.assertFalse(outside.parent.exists())

def test_guarded_export_reports_missing_page_qa_as_pdf_error(self):
    (self.project / "qa/pages/page-001.json").unlink()
    with self.assertRaisesRegex(PdfExportError, "qa/pages/page-001.json"):
        guarded_export(self.project)
```

- [ ] **Step 3: Run focused tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_composition tests.test_export_pdf tests.test_concurrency -v
```

- [ ] **Step 4: Read panel sources once and carry paired payloads**

Use `read_contained_bytes` during preflight, compose from `io.BytesIO(payload)`,
and hash the same bytes. Store `(page_number, relative, payload, metadata)` in
one prepared list; remove `payloads`, `payload_by_number`, and the unstrict
`zip`.

Remove undeclared `page_background`/`background` settings and always create an
RGB white page, matching the manifest validator.

- [ ] **Step 5: Validate export inputs before side effects and patch locked manifest**

Resolve the project and destination before `mkdir`, require destination
membership through `contained_project_path`, check every page-QA file before
hashing, and raise `PdfExportError` with its relative path.

Inside `ProjectTransaction`, re-read `project.json`, copy its artifacts mapping,
update only `pdf` and `pdf_verification`, and stage that current manifest.

- [ ] **Step 6: Mirror, verify, and commit**

```powershell
Copy-Item scripts\compose_pages.py skills\comic-sol\scripts\compose_pages.py
Copy-Item scripts\export_pdf.py skills\comic-sol\scripts\export_pdf.py
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_composition tests.test_export_pdf tests.test_concurrency -v
git add scripts skills/comic-sol/scripts tests/test_composition.py tests/test_export_pdf.py tests/test_concurrency.py
git commit -m "fix: publish consistent composition and PDF snapshots"
```

### Task 5: Filesystem, raster, and malformed-artifact hardening

**Files:**
- Create: `scripts/raster_limits.py`
- Create: `skills/comic-sol/scripts/raster_limits.py`
- Modify: `scripts/project_io.py`
- Modify: `scripts/normalize_panels.py`
- Modify: `scripts/letter_panels.py`
- Modify: `scripts/pdf_quality.py`
- Modify: `scripts/render_report.py`
- Modify: `scripts/validate_project.py`
- Modify: `tests/test_project_io.py`
- Modify: `tests/test_normalization.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_validation.py`
- Mirror: all matching runtime files under `skills/comic-sol/scripts/`

**Interfaces:**
- Produces: `MAX_DECODED_PIXELS = 1600 * 2400 * 16`, `_stream_mode(flags)`, and structured validation issues instead of tracebacks.

- [ ] **Step 1: Add failing stream-mode and lock-error tests**

```python
def test_open_path_nofollow_honors_write_and_readwrite_flags(self):
    target = (self.project / "mode.bin").resolve()
    with project_io.open_path_nofollow(
        target, flags=os.O_WRONLY | os.O_CREAT, mode=0o600
    ) as stream:
        stream.write(b"write")
    with project_io.open_path_nofollow(target, flags=os.O_RDWR) as stream:
        self.assertEqual(b"write", stream.read())
        stream.seek(0)
        stream.write(b"W")

def test_empty_lock_metadata_reraises_nonretryable_error(self):
    (self.project / ".comic-sol.lock").write_bytes(b"")
    error = OSError(errno.EINVAL, "invalid lock")
    with mock.patch.object(project_io.ProjectLock, "_lock", side_effect=error):
        with self.assertRaises(OSError) as raised:
            with project_io.ProjectLock(self.project, timeout=0.01):
                pass
    self.assertEqual(errno.EINVAL, raised.exception.errno)
```

- [ ] **Step 2: Add failing raster and malformed-record tests**

```python
def test_raster_modules_share_one_decode_ceiling(self):
    self.assertEqual(raster_limits.MAX_DECODED_PIXELS,
                     normalize_panels.MAX_DECODED_PIXELS)
    self.assertEqual(raster_limits.MAX_DECODED_PIXELS,
                     letter_panels.MAX_DECODED_PIXELS)
    self.assertEqual(raster_limits.MAX_DECODED_PIXELS,
                     pdf_quality.MAX_DECODED_PIXELS)

def test_non_object_normalization_record_is_a_validation_issue(self):
    (self.project / "panels/p01-01/normalization.json").write_text("[]\n", "utf-8")
    issues = validate_project(self.project, "panels")
    self.assertTrue(any("normalization record must be an object" in issue.message
                        for issue in issues))
```

Add a corrupt-raster test whose header parses but whose pixel payload is
truncated; expect validation to report an unreadable raster after `load()`.
Patch `Image.open` in report integrity to raise
`Image.DecompressionBombError`; expect `valid page: no` instead of an exception.

- [ ] **Step 3: Add failing manifest path containment tests**

Use valid manifests whose `panels` or `project_id` values attempt `../` or whose
artifact descriptors point through a symlink. Assert validation returns an
issue and does not call `sha256_file`, `is_file`, or PDF verification on the
escaped path. Also assert `qa_report` and `pdf_verification` expected paths are
checked even when `project_id` is invalid.

- [ ] **Step 4: Run focused tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_project_io tests.test_normalization tests.test_report tests.test_validation -v
```

- [ ] **Step 5: Implement shared raster ceiling and error handling**

Create:

```python
#!/usr/bin/env python3
"""Shared decoded-raster safety limit for Comic Sol image readers."""

MAX_DECODED_PIXELS = 1600 * 2400 * 16
```

Import it in normalization, lettering, and PDF QA. Remove process-wide
`Image.MAX_IMAGE_PIXELS` assignments and enforce the explicit limit before
decode. Catch Pillow bomb errors at report boundaries.

- [ ] **Step 6: Implement stream, lock, JSON, and containment fixes**

Use one selector:

```python
def _stream_mode(flags: int) -> str:
    if flags & os.O_RDWR:
        return "r+b"
    if flags & os.O_WRONLY:
        return "wb"
    return "rb"
```

Use it in both `open_path_nofollow` branches. Apply `_retryable` to the
zero-length lock branch. Guard normalization JSON with `isinstance(..., dict)`.
Use `image.load()` for raster integrity. Resolve all manifest-derived paths via
`contained_project_path`; record a `ValidationIssue` and skip downstream access
when containment fails. Remove the unused `REQUIRED_ARTIFACT_DESCRIPTORS`.

- [ ] **Step 7: Mirror, verify, and commit**

```powershell
$names = @('raster_limits.py','project_io.py','normalize_panels.py','letter_panels.py','pdf_quality.py','render_report.py','validate_project.py')
foreach ($name in $names) { Copy-Item "scripts\$name" "skills\comic-sol\scripts\$name" }
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_project_io tests.test_normalization tests.test_report tests.test_validation -v
git add scripts skills/comic-sol/scripts tests
git commit -m "fix: harden project paths and raster decoding"
```

### Task 6: Lettering, typography, and layout correctness

**Files:**
- Modify: `scripts/letter_panels.py`
- Modify: `scripts/typography.py`
- Modify: `scripts/layouts.py`
- Modify: `scripts/comic_sol.py`
- Modify: `tests/test_lettering.py`
- Modify: `tests/test_typography.py`
- Modify: `tests/test_layouts.py`
- Modify: `tests/test_validation.py`
- Mirror: matching files under `skills/comic-sol/scripts/`

**Interfaces:**
- Produces: source-preserving empty/SFX-only lettering; shared visible-text normalization; margin-compliant `four-grid` geometry.

- [ ] **Step 1: Add failing empty and SFX-only lettering tests**

```python
def test_no_renderable_text_still_writes_the_base_panel(self):
    before = Image.open(self.panel).convert("RGB")
    for items in ([], [sfx()]):
        output = self.root / f"out-{len(items)}.png"
        summary = letter_panel(
            str(output), 800, 1000, items, self.characters,
            source_bytes=self.panel.read_bytes(),
        )
        self.assertTrue(output.is_file())
        with Image.open(output) as actual:
            self.assertIsNone(ImageChops.difference(before, actual.convert("RGB")).getbbox())
        self.assertEqual(0, summary["rendered_text_count"])
```

- [ ] **Step 2: Add failing display-transform and font-cache tests**

```python
def test_dialogue_preflight_checks_uppercase_display_codepoints(self):
    result = preflight_text_items([item("straße")], FONT_POLICY)
    displayed = "".join(entry["character"] for entry in result["glyphs"])
    self.assertIn("STRASSE", displayed)
    self.assertNotIn("ß", displayed)

def test_font_hashes_are_cached_by_path(self):
    with mock.patch("typography._hash_font_file",
                    wraps=typography._hash_font_file) as hashed:
        preflight_text_items([item("First")], FONT_POLICY)
        preflight_text_items([item("Second")], FONT_POLICY)
    self.assertEqual(3, hashed.call_count)
```

- [ ] **Step 3: Update the failing four-grid expectation**

Change the approved geometry test to:

```python
self.assertEqual(
    (
        (64, 64, 720, 1120),
        (816, 64, 720, 1120),
        (64, 1216, 720, 1120),
        (816, 1216, 720, 1120),
    ),
    FOUR_GRID_RECTS,
)
self.assertEqual(
    [dict(zip(("x", "y", "width", "height"), rectangle))
     for rectangle in FOUR_GRID_RECTS],
    layout_rects("four-grid"),
)
```

- [ ] **Step 4: Run focused tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_lettering tests.test_typography tests.test_layouts tests.test_validation -v
```

- [ ] **Step 5: Implement the minimum fixes**

Encode and atomically write `base.convert("RGB")` before returning an
empty-render summary. Move the shared normalization/display transform into
`typography.py` and import it into lettering so both paths uppercase dialogue
identically. Add `@lru_cache(maxsize=None)` to the font-file hash helper keyed
by the path string.

Pass the canvas `Image.Image` explicitly to `_draw_antialiased_balloon`; remove
all `draw._image` access in that helper. Add `four-grid` to
`comic_sol.layout_rects` and set `FOUR_GRID_RECTS` to the four margin-compliant
rectangles.

- [ ] **Step 6: Mirror, verify, and commit**

```powershell
$names = @('letter_panels.py','typography.py','layouts.py','comic_sol.py')
foreach ($name in $names) { Copy-Item "scripts\$name" "skills\comic-sol\scripts\$name" }
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_lettering tests.test_typography tests.test_layouts tests.test_validation -v
git add scripts skills/comic-sol/scripts tests
git commit -m "fix: align lettering preflight and layouts"
```

### Task 7: Finalization, CLI, report, and hashing cleanup

**Files:**
- Modify: `scripts/comic_sol.py`
- Modify: `scripts/compose_pages.py`
- Modify: `scripts/render_report.py`
- Modify: `scripts/quality_sample.py`
- Modify: `tests/test_resume.py`
- Modify: `tests/test_composition.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_quality_matrix.py`
- Mirror: matching files under `skills/comic-sol/scripts/`

**Interfaces:**
- Produces: finalization that follows emitted actions, explicit CLI selection, safe template substitution, and shared streaming hashes.

- [ ] **Step 1: Add failing finalization tests**

```python
def test_finalize_treats_regenerate_and_rerun_as_stale(self):
    plan = [ResumeAction("lettering", "rerun", "stage", "stale")]
    with patch("comic_sol.build_resume_plan", return_value=plan), \
         patch("letter_panels.letter_project") as letter:
        self._run_finalize()
    letter.assert_called_once_with(self.project)

def test_finalize_does_not_accept_empty_manifest_panels_vacuously(self):
    manifest = read_json(self.project / "project.json")
    manifest["panels"] = []
    atomic_write_json(self.project / "project.json", manifest)
    (self.project / "panels/p01-01/lettered.png").unlink(missing_ok=True)
    with patch("letter_panels.letter_project") as letter:
        self._run_finalize()
    letter.assert_called_once()
```

- [ ] **Step 2: Add failing CLI and report-token tests**

```python
def test_compose_main_honors_explicit_all_flag(self):
    with patch("compose_pages.compose_all_pages", return_value=[]) as compose_all:
        self.assertEqual(0, compose_main([str(self.project), "--all"]))
    compose_all.assert_called_once_with(self.project)

def test_report_preserves_double_braces_in_authored_evidence(self):
    self.records[0]["checks"][0]["evidence"] = "Observed {{sun gate}} intact."
    self._write_records()
    report = render_report(self.project).read_text("utf-8")
    self.assertIn("{{sun gate}}", report)
```

- [ ] **Step 3: Add failing shared-hash test**

Patch `quality_sample.sha256_file` and assert retained attempt hashing calls it
once with the contained attempt path. Remove the local `hashlib` use after the
test passes.

- [ ] **Step 4: Run focused tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_resume tests.test_composition tests.test_report tests.test_quality_matrix -v
```

- [ ] **Step 5: Implement explicit control flow**

Use stale actions `{"regenerate", "rerun"}`. Derive lettering panel IDs from
the storyboard when the manifest list is absent or empty. Replace
`text and [text] or [[]]` with `[text] if text else [[]]`.

In composition CLI, reject invocation with neither `--page` nor `--all`, call
the selected path explicitly, and preserve mutually exclusive parser behavior.

Validate the original report template by comparing its token set to the exact
replacement-key set before substitution. Remove the post-substitution `"{{"`
guard so authored content survives.

Use `sha256_file` for retained-attempt hashing.

- [ ] **Step 6: Mirror, verify, and commit**

```powershell
$names = @('comic_sol.py','compose_pages.py','render_report.py','quality_sample.py')
foreach ($name in $names) { Copy-Item "scripts\$name" "skills\comic-sol\scripts\$name" }
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_resume tests.test_composition tests.test_report tests.test_quality_matrix -v
git add scripts skills/comic-sol/scripts tests
git commit -m "fix: make finalization follow recorded state"
```

### Task 8: Plugin metadata, skill instructions, and public documentation

**Files:**
- Modify: `.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `references/capability-detection.md`
- Modify: `references/image-provider-setup.md`
- Modify: `skills/comic-sol/SKILL.md`
- Modify: `skills/comic-sol/references/workflow.md`
- Modify: `submission/listing.md`
- Modify: `submission/test-cases.md`
- Modify: `tests/test_distribution.py`
- Modify: `tests/test_release_docs.py`
- Modify: `tests/test_validation.py`

**Interfaces:**
- Produces: complete legal metadata and public guidance consistent with actual account, provider, safety, schema, and page-limit behavior.

- [ ] **Step 1: Add failing public-contract tests**

Add assertions that parse `plugin.json` and require:

```python
self.assertEqual(
    "https://github.com/wenn-id/comicsol/blob/main/PRIVACY.md",
    manifest["interface"]["privacyPolicyURL"],
)
self.assertEqual(
    "https://github.com/wenn-id/comicsol/blob/main/TERMS.md",
    manifest["interface"]["termsOfServiceURL"],
)
```

Add documentation contract checks for the literal `pdf_verification` path and
for the absence of the truncated `fal_ge...mage` identifier. Add a skill
contract check that the progressive-loading section names all triggers:
external prompts, people, minors, sensitive content, named styles, franchises,
refusals, and every JSON write/revision.

- [ ] **Step 2: Run focused contract tests and confirm RED**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_distribution tests.test_release_docs tests.test_validation.TemplateContractTests -v
```

- [ ] **Step 3: Apply metadata and documentation corrections**

Add the two legal URLs without changing existing plugin metadata. Describe the
root provider document consistently as platform-specific, and replace the
truncated FAL tool name with provider-neutral wording rather than inventing a
tool identifier.

State that Comic Sol requires no Comic Sol account or demo credentials, while a
Codex session and selected provider access may be required. Document
`pdf_verification` at `exports/pdf-verification.json`.

In `SKILL.md`, preserve every safety/IP trigger and require schema reading for
every JSON write/revision. Change the batch-map example to `Batch A pages 1-2,
Batch B pages 3-4`. Keep the already-resolved high-impact confirmation boundary
unchanged.

- [ ] **Step 4: Run contract tests and confirm GREEN**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest `
  tests.test_distribution tests.test_release_docs tests.test_validation.TemplateContractTests -v
```

- [ ] **Step 5: Commit Task 8**

```powershell
git add .codex-plugin README.md references skills/comic-sol/SKILL.md skills/comic-sol/references submission tests
git commit -m "docs: align plugin contracts and safety guidance"
```

### Task 9: Reach the docstring threshold without behavior changes

**Files:**
- Modify: Python modules under `scripts/` that remain below 80% function docstring coverage.
- Mirror: matching modules under `skills/comic-sol/scripts/`.

**Interfaces:**
- Produces: concise one-line function docstrings; no signature, branch, return, exception, or side-effect changes.

- [ ] **Step 1: Run the AST coverage gate and confirm RED**

```powershell
@'
import ast
from pathlib import Path
functions = []
for path in Path("scripts").glob("*.py"):
    tree = ast.parse(path.read_text("utf-8"))
    functions.extend(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
documented = sum(ast.get_docstring(node) is not None for node in functions)
coverage = documented * 100 / len(functions)
print(f"docstring coverage: {documented}/{len(functions)} = {coverage:.2f}%")
raise SystemExit(0 if coverage >= 80 else 1)
'@ | '..\.venv-comicsol-pr56\Scripts\python.exe' -
```

Expected: exit 1 and `39.85%` before prior tasks add any helper docstrings.

- [ ] **Step 2: Add concise semantic docstrings**

For each undocumented function, add a single sentence describing its observable
responsibility. Use forms such as:

```python
def _stream_mode(flags: int) -> str:
    """Return the binary file-object mode implied by low-level open flags."""

def _load_page_records(project_dir: Path) -> list[dict[str, object]]:
    """Load current and legacy page-QA records in deterministic page order."""
```

Do not describe implementation trivia, parameters already obvious from the
signature, or behavior not enforced by code.

- [ ] **Step 3: Mirror every changed module**

For each modified root module, copy it to the corresponding bundle path.

- [ ] **Step 4: Run the AST coverage gate and syntax compilation**

Re-run the Step 1 gate. Expected: at least `80.00%` and exit 0.

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m compileall -q scripts skills/comic-sol/scripts
```

- [ ] **Step 5: Commit Task 9**

```powershell
git add scripts skills/comic-sol/scripts
git commit -m "docs: document Comic Sol runtime functions"
```

### Task 10: Full verification, push, and refreshed PR audit

**Files:**
- Verify only; modify code only if a fresh failure identifies a remaining root cause.

**Interfaces:**
- Produces: pushed PR head with local and GitHub evidence.

- [ ] **Step 1: Verify root/bundle byte equality**

```powershell
$failed = @()
Get-ChildItem scripts -Filter *.py | ForEach-Object {
  $bundle = Join-Path 'skills\comic-sol\scripts' $_.Name
  if (-not (Test-Path $bundle) -or
      (Get-FileHash $_.FullName).Hash -ne (Get-FileHash $bundle).Hash) {
    $failed += $_.Name
  }
}
if ($failed) { throw "runtime bundle drift: $($failed -join ', ')" }
```

- [ ] **Step 2: Run the complete test suite**

```powershell
& '..\.venv-comicsol-pr56\Scripts\python.exe' -m unittest discover -s tests -v
```

Expected: all tests pass; only documented platform/dependency skips remain.

- [ ] **Step 3: Run plugin and deterministic smoke checks**

```powershell
npx --yes skills add . --list
& '..\.venv-comicsol-pr56\Scripts\python.exe' scripts\comic_sol.py doctor --output-root "$env:TEMP\comic-sol-pr56-doctor"
Get-Content .codex-plugin\plugin.json -Raw | ConvertFrom-Json | Out-Null
Get-Content .agents\plugins\marketplace.json -Raw | ConvertFrom-Json | Out-Null
git diff origin/feat/unify-codex-plugin...HEAD --check
```

Expected: skill discovery lists `comic-sol`, doctor reports PASS, JSON parsing
succeeds, and diff check emits no output.

- [ ] **Step 4: Re-run docstring coverage and inspect final diff**

Run Task 9 Step 1 again and require at least 80%. Then run:

```powershell
git status --short
git diff --stat origin/feat/unify-codex-plugin...HEAD
git log --oneline origin/feat/unify-codex-plugin..HEAD
```

Expected: only intentional tracked commits and no uncommitted changes.

- [ ] **Step 5: Push the verified branch**

```powershell
git push origin HEAD:feat/unify-codex-plugin
```

- [ ] **Step 6: Recheck PR checks and review inventory**

```powershell
gh pr checks 56
$env:PYTHONUTF8='1'
python 'C:\Users\acer\.codex\plugins\cache\openai-curated-remote\github\0.1.8-2841cf9749ae\skills\gh-address-comments\scripts\fetch_comments.py'
```

Classify every remaining unresolved thread against the pushed SHA. If a finding
remains technically valid, add a failing test, fix it, repeat the relevant task
verification, commit, and push. If a thread is stale or contradicted by the
verified canonical-v2 contract, record the exact file/test evidence in the
handoff without replying to or resolving the thread.

- [ ] **Step 7: Report completion evidence**

Report the pushed commit range, full-suite test count, docstring percentage,
plugin/doctor result, GitHub check state, and any thread left open solely
because manual resolution was outside the authorized write scope.
