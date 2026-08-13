# PR #56 Review Remediation Design

## Goal

Resolve every still-valid review finding on `wenn-id/comicsol#56`, remove the
CodeRabbit docstring warning, preserve compatibility with existing Comic Sol
projects, and push a verified fix set to `feat/unify-codex-plugin`.

## Current state

- GitHub reports 17 unresolved inline threads containing 18 distinct findings.
- The CodeRabbit review body contains 13 minor and 15 nitpick findings.
- CodeRabbit reports 39.85% docstring coverage against an 80% target.
- All GitHub Actions checks currently pass.
- The local baseline passes all 367 tests, with 26 platform-specific skips on
  Windows.
- Every Python file under `scripts/` is currently byte-identical to its bundled
  counterpart under `skills/comic-sol/scripts/`.

## Architectural decisions

### Schema 2.0 is canonical

The bundled panel and page QA templates and the shared quality-record code
already implement schema 2.0. The remediation will make schema 2.0 the
documented and emitted contract instead of reverting templates to schema 1.0.
Schema 1.0 remains readable as a legacy format so existing projects and reports
can be migrated or resumed safely.

Canonical schema-2.0 panel and page records use these top-level fields:

- `schema_version`, `kind`, and `subject_id` identify the contract and subject.
- `bindings` contains current artifact paths, dimensions, and hashes.
- `checks` contains the exact ordered check sequence and bounded evidence.
- `review` contains a non-empty method and reviewer plus an ISO-8601 timestamp.
- `decision` and `unresolved_warnings` agree with check results and severity.

Panel schema 2.0 uses `accept`, `accept-warning`, and `regenerate`. Legacy
schema 1.0 continues to use `accept`, `accept_with_warnings`, and `regenerate`.
Resume and reuse logic will dispatch on `schema_version`, read `subject_id` for
schema 2.0 and `panel_id` for schema 1.0, and reject conflicting identifiers.

Page schema 2.0 owns the complete seven-check visual record. The
`bubble-tail-direction` check owns one region per dialogue with `panel_id`,
`text_id`, `speaker`, `voice_source`, `speaker_anchor`, `tip`, and `result`.
Bindings, ordered checks, review provenance, decisions, and warnings remain
part of the normative contract. The obsolete five-field page record remains a
legacy schema-1.0 input only.

### Fix roots, not callers

Shared helpers will own shared rules:

- one ordered quality-check declaration per artifact kind;
- one decoded-raster ceiling used by normalization, lettering, and PDF QA;
- one file-descriptor stream-mode selector for read, write, and read/write;
- one display-text transformation used by typography preflight and lettering;
- containment helpers for every manifest-derived or user-selected path.

The implementation will not add speculative abstractions. Helpers are added
only where at least two existing callers currently duplicate or disagree on a
rule.

### Transactions publish consistent snapshots

Composition will use the same source bytes for pixel composition and source
hashes. PDF export will re-read `project.json` after acquiring the project lock
and will update only the PDF descriptors on that locked snapshot. Path
validation and required-input checks occur before creating output directories
or staging transaction data.

### Source and plugin bundle remain identical

Runtime changes are authored in the root `scripts/` copy, tested there, and
mechanically mirrored to `skills/comic-sol/scripts/`. Verification fails if
any corresponding files differ. Documentation and templates are updated in the
surface where they are shipped.

## Remediation workstreams

### 1. QA contracts and resume behavior

- Rewrite panel/page schema documentation and templates around canonical v2.
- Validate ordered check IDs directly; allow explicit ordered subsets only for
  the three subjective page checks.
- Remove fabricated deterministic checks from subjective validation.
- Record no regions for passing deterministic checks.
- Make a warning tail result produce a warning-aware decision and
  `unresolved_warnings`, or reject inconsistent region/check combinations.
- Accept reviewer identity and timestamp when building page QA and validate
  both on read.
- Enforce error-failure and warning-decision consistency for schema-2.0 panel
  records at every validation stage.
- Handle v1 and v2 panel identities in resume/reuse without silently skipping
  valid v2 records.
- Retain legacy page records in reports and label them
  `quality-migration-required` instead of dropping them.

### 2. Transaction, path, and raster integrity

- Hash exactly the bytes used for page composition.
- Re-read and patch the manifest under the PDF transaction lock.
- Reject traversal and symlink-parent PDF destinations before creating
  directories.
- Report missing page-QA inputs as `PdfExportError`.
- Route manifest-supplied panel, project, PDF, and artifact paths through
  containment helpers before filesystem access.
- Decode raster pixels with `load()` rather than metadata-only `verify()`.
- Catch `Image.DecompressionBombError` while rendering report integrity.
- Align the decoded-pixel ceiling across image-processing modules.
- Validate non-object normalization JSON as a structured stale-record issue.
- Preserve non-retryable lock errors instead of converting them into timeouts.
- Select correct binary stream modes on Windows and POSIX.

### 3. Pipeline correctness

- Publish the unmodified base raster when a panel has no renderable text.
- Keep `four-grid` geometry identical and margin-compliant in both layout
  registries.
- Apply the dialogue uppercase display transform before typography glyph
  coverage and policy hashing.
- Match finalization stale-stage filters to emitted `regenerate`/`rerun`
  actions and avoid vacuous success for an empty manifest panel list.
- Honor the composition CLI `--all` flag and simplify paired page payload
  handling.
- Remove undeclared page-background behavior so validation and composition use
  the same fixed white background contract.
- Pass the canvas image explicitly to balloon rendering instead of relying on
  Pillow internals.
- Add strict pairing where page-panel and lettering collections must match.
- Keep terminal artifact path validation independent of a valid `project_id`.

### 4. Metadata, documentation, and maintainability

- Add privacy and terms URLs to the plugin manifest.
- Correct provider-setup wording and the truncated FAL tool reference.
- Document the `pdf_verification` artifact and provider/account prerequisites.
- Keep all mandatory safety/IP and schema-loading triggers in `SKILL.md` and
  use a valid one-to-four-page batch example.
- Validate report template tokens before substitution so authored `{{` text is
  preserved.
- Use the shared streaming hash helper and shared panel-check declaration.
- Cache immutable font-file hashes.
- Add concise function docstrings until the same AST-based calculation used by
  CodeRabbit is at least 80%, without changing runtime behavior.

## Testing strategy

Every behavioral fix follows red-green-refactor:

1. Add the smallest regression test that reproduces the reviewed failure.
2. Run that test and confirm it fails for the expected reason.
3. Apply the smallest production change that fixes the root cause.
4. Run the focused test and its neighboring module suite.
5. Mirror runtime code to the plugin bundle only after the root copy is green.

The new tests cover schema dispatch, warning/decision consistency, evidence
ordering, source/hash snapshots, locked manifest preservation, traversal,
Windows stream modes, raster bombs, malformed normalization records, empty
text/SFX-only lettering, layouts, typography transformations, and finalization
stage selection. Documentation-only corrections use parse, manifest, and
distribution contract checks rather than source-text assertions unless the
literal metadata is itself the public contract.

Final verification consists of:

- the complete `unittest` suite;
- plugin/distribution discovery and manifest JSON parsing;
- focused CLI doctor and validation checks;
- root-versus-bundle SHA-256 equality for mirrored scripts;
- an AST docstring coverage calculation of at least 80%;
- `git diff --check` and a clean tracked working tree after commit;
- refreshed `gh pr checks 56` and review-thread inventory after push.

## GitHub write scope

The final verified commits will be pushed directly to the existing
`feat/unify-codex-plugin` branch for PR #56. Review comments will not be replied
to or manually resolved; CodeRabbit may mark addressed threads automatically
after the pushed code is re-reviewed. Any still-open thread after the new
review will be rechecked against the new head and fixed if it remains valid.

## Completion criteria

The work is complete only when all of the following are true:

- every one of the 46 unresolved review findings is implemented or documented
  with a specific technical reason it is already invalid;
- docstring coverage is at least 80%;
- canonical schema-v2 documentation, templates, validators, resume logic, and
  reports agree;
- all local verification commands pass on the final tree;
- the source and bundled runtime copies are identical;
- commits are pushed to PR #56 and its refreshed checks contain no actionable
  failure attributable to the change.
