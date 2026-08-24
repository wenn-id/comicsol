# Changelog

## Unreleased

`docs/releases/milestone-delivery.md` records what each milestone delivered and which tag
carries it.

### Added

- Added automatic, provider-neutral image-capability reporting to `doctor`. The
  active agent inspects its exposed tools and supplies one sanitized observation;
  source, installed CLI, and MCP doctor surfaces now distinguish healthy,
  partially capable, missing, and unknown/detection-failed sessions while
  preserving the legacy readiness fields and offline default. Capability names
  and reference-image/dimension feature flags are reported, but no credential,
  provider payload, project state, provider SDK, or automatic provider setup is
  introduced.
- Added an opt-in `comic-sol init --interactive` wizard for project name, 1–4
  page scope, story prompt or source file, and output location. The wizard
  validates choices before allocation and uses the same schema and atomic init
  path as the non-interactive CLI. `--page-count` is also available to the
  source CLI and existing `comic_init` MCP tool, so automation and AI agents
  retain a prompt-free equivalent with the existing 2-page default.

- Added a diagnostic-driven client integration repair workflow. `comic-sol repair
  --dry-run` previews the verified config path, intended `comic-sol` MCP entry, action,
  and backup requirement without writing. Applying repair recomputes under the config
  lock, verifies backup and persisted mutation, verifies rollback after failure, is
  idempotent when repeated, and returns explicit per-client `success`, `no-op`, or
  `failure` states with `CS-INSTALL-002`/`CS-INSTALL-003` doctor guidance.

- Aligned the user-facing documentation contract from issue #213. New
  `docs/surfaces.md` separates the Skill checkout, Codex Plugin, source
  development, installed CLI wheel, native portable archive, MCP server, and OCI
  image workflows, and states each surface's default project output root —
  including that the MCP server has none (explicit `--root` only) and the OCI
  image writes under `/data`. New `docs/support-matrix.md` publishes the
  platform × install-mode × architecture × runtime matrix, including the WSL2,
  Intel-macOS, MCP-extra, and OCI rows. `docs/onboarding.md` now names its
  surface, pins an explicit `--output-root` in the happy path, and links both
  documents. `references/image-provider-setup.md` was rewritten to be
  capability-based, credential-safe, and vendor-neutral: vendor pointers are
  isolated in a dated, explicitly non-normative appendix, and no setup step
  puts an API key in a prompt or names a recommended vendor.

- Expanded `SUPPORT.md`, `PRIVACY.md`, and `TERMS.md` from plugin-only documents
  to cover every surface (Skill, plugin, source, installed CLI, native archive,
  MCP, OCI). SUPPORT now asks for version, install mode, platform/runtime, the
  structured `CS-<NAMESPACE>-<NNN>` error code, and `--json doctor` output, and
  defines a private route for sensitive reports (security follows `SECURITY.md`;
  privacy reports that cannot be sanitized use the same private channel).
  PRIVACY names where each surface writes and TERMS states the local-first
  boundary per surface.

- Added the accessibility and localization limitations to the README: exported
  PDFs are image-based, untagged, not PDF/UA, and carry no alt text; the
  CLI/Skill surface is English-only; typography refuses shaping-dependent
  scripts. The README artifact listing now includes the v2.2 artifacts
  (`plan/character-identity-pack.json`, `logs/reference-selection.json`,
  `logs/repair-plan.json`, `qa/pages/page-NNN.json` page-QA 2.1 records,
  `panels/*/sfx-audit.json`) and states the evidence limits of QA artifacts, and
  the README links SUPPORT, PRIVACY, TERMS, typography, accessibility,
  surfaces, and the support matrix. `tests/test_user_docs.py` pins these
  contracts.

- Added project URLs, classifiers, keywords, and maintainer metadata to
  `pyproject.toml`, and a wheel-METADATA acceptance contract:
  `comic_sol_product.release` now validates the built wheel's METADATA (project
  URLs, classifier families, keywords, maintainer, `Requires-Python >=3.11`,
  SPDX `License-Expression`, and rejection of deprecated `License ::`
  classifiers) alongside the existing member checks, with offline tests in
  `tests/test_clean_install.py`.

### Security

- Completed the container runtime hardening gap from issue #214. The image now
  carries the fixed numeric identity `10001:10001` (`USER 10001:10001`, created
  with a pinned `useradd`/`groupadd` pair instead of an auto-allocated system
  account), and `compose.yaml` adds `cap_drop: [ALL]`, `pids_limit: 64`,
  `user: "10001:10001"`, and `init: true` on top of the existing read-only
  root filesystem, `network_mode: none`, tmpfs, resource limits, and
  `no-new-privileges`. The effective seccomp policy is documented and tested:
  the image ships no custom profile and must never run `seccomp=unconfined` —
  CI asserts the engine's default profile is active and that the container
  itself is in seccomp filter mode. New
  `scripts/container_runtime_audit.py` runs fail-closed checks on every build
  and again during qualification of the published bytes: engine seccomp
  profile, image `Config.User`, reported CLI version against the canonical
  release version, runtime UID/GID, zero effective capabilities,
  `NoNewPrivs`, the process limit, an `EROFS` root-filesystem write probe, an
  absent network interface beyond loopback, and working `doctor` and MCP
  initialize handshake under the full hardening set with only `/data` and
  `/tmp` writable.

- Removed the Docker release identity drift from issue #214. The release
  workflow no longer passes the unused `DOCKER_BASE_DIGEST` build argument;
  the Dockerfile's single `ARG PYTHON_BASE` is now the one canonical,
  digest-pinned base reference consumed by both stages, and the release image
  tag continues to be derived from `needs.prepare.outputs.version` (verified
  against `comic_sol_product/version.py` by the audit). The container release
  payload now also includes `comic-sol-<version>-linux-x86_64.container.sbom.json`:
  a CycloneDX 1.6 SBOM generated by inventorying the image's own
  `site-packages` via `docker cp` into a mirror environment, recording the
  pinned base image, validated against the pinned schema, signed and attested
  through `SHA256SUMS` like every other payload, and verified during
  qualification. The release container job additionally blocks on a `pip-audit`
  scan of the image's exact hash-locked dependency set. `docs/install.md`,
  `docs/support-matrix.md`, and `docs/releases/release-trust-chain.md`
  document the hardening contract, the seccomp policy, and the container
  support limitations.

- Completed the supply-chain provenance gap from issue #211. The complete release subject
  set and trust chain is now defined in `docs/releases/release-trust-chain.md`: every payload
  named by the signed `SHA256SUMS` with a build-provenance attestation, the manifest and its
  Sigstore bundle bound through `candidate-identity.json`, and the qualification order of
  verification. Release qualification now downloads the published candidate identity on every
  platform leg and verifies, together with its inputs, the tag, the exact candidate commit,
  the manifest and signature-bundle digests, and each downloaded payload digest — so the
  wheel, sdist, every native archive, both installers, and the container tar are checked
  against both the attestation and the identity record, not either alone. The source leg's
  manifest coverage check excludes the manifest itself, its Sigstore bundle, and both
  candidate-identity files — the four files `SHA256SUMS` deliberately does not name — with
  a functional regression test pinning that filter contract.

- Documented an external pre-execution verification path for `install.sh` and `install.ps1`
  in `docs/install.md`: verify the Sigstore bundle over `SHA256SUMS`, confirm the installer's
  own digest appears in the signed manifest, and optionally verify its build-provenance
  attestation with `gh attestation verify` before executing any installer code. Also fixed
  the doubled backslashes in the existing manual `cosign verify-blob` example.

- Recorded the OCI distribution decision: OCI is an official channel delivered as the
  attested `comic-sol-<version>-linux-x86_64.container.tar` release asset, not a registry
  image. `docs/install.md` now shows loading and running the verified asset, and the trust
  chain document records what a registry distribution would additionally require before
  that decision can change.

- Added canonical inputs and reproducible regeneration commands for every dependency lock:
  `requirements/{base,runtime,release}.in` join the existing `audit.in`/`quality.in`, each
  lock header now names its input and command, and `requirements/README.md` documents the
  per-platform regeneration procedure with pip-tools 7.6.1 (`--strip-extras` is required)
  and the required cross-platform diff review. `tests/test_lock_provenance.py` runs on every
  platform and fails when a lock loses a direct pin, disagrees across platforms, or stops
  documenting its provenance.

- Added `docs/releases/rollback-runbook.md`: step-by-step withdrawal/yank and rollback
  procedures that preserve immutable evidence — title/notes-only edits, evidence capture
  before any mutation, deployment-state marking, and verification of the fallback release.
  Removing a public release entry is an administrator-only escalation guarded by the active
  tag ruleset (deleting an immutable release removes GitHub's immutable-release tag
  binding), never a standard withdrawal step. Linked from the stable criteria and install
  docs.

- Regenerated `requirements/locks/audit-python311.txt` with the documented pip-tools
  7.6.1 command so it matches its canonical provenance: `cachecontrol[filecache]==0.14.4`
  became the extras-stripped `cachecontrol==0.14.4`; all pins and hashes are otherwise
  unchanged, and a regression test now rejects extras-qualified pins in every lock.

### Fixed

- Reconciled the release and project-schema documentation with implementation authorities.
  The current native archive matrix is Linux x86_64, macOS arm64, and Windows x86_64.
  WSL2 uses the Linux x86_64 archive; it has no separate native archive. Source installation
  supports Linux, macOS, Windows, and WSL2 on Python 3.11+. Intel macOS is
  source-install-only; it has no native archive. Candidate notes now enumerate every archive,
  metadata, and SBOM filename. The schema reference now derives allowed descriptors,
  canonical paths, ownership, terminal requirements, and stage versions from the validator,
  stage registry, and manifest template, including export-owned `pdf_verification` at
  `exports/pdf-verification.json` and lettering stage version `3`.

- Restored the macOS and Windows legs of the native release build. All three legs pinned
  CPython `3.11.15`, but the 3.11 line is in security-only maintenance and
  `actions/python-versions` publishes no macOS or Windows binary past `3.11.9` — only Linux
  builds continue. `setup-python` therefore failed outright on both platforms, which skipped
  `publish` and left the `v2.0.0rc5` tag with no release attached. Each leg now resolves its
  own interpreter: Linux stays on `3.11.15` to match the Dockerfile base image, while macOS
  and Windows pin `3.11.9`, the newest build that exists for them. A regression test asserts
  the native matrix never shares one patch pin again and that neither cross-platform leg
  outruns the last release carrying its binary.

- Moved the macOS release from x86_64 to arm64, and corrected what the artifact name claims.
  `cryptography` removed x86_64 macOS support in 49.0.0 and now publishes arm64 wheels only,
  so the x86_64 leg had to build it from source against an OpenSSL that the frozen runtime
  could not then load — `dlopen` failed on `_SSL_get0_group_name`, the bundled MCP smoke test
  failed, and `publish` was skipped. The runner, the uploaded bundle name, the assembled
  architecture, and the publish-side release identity now all agree on `arm64`, and the
  qualification workflow resolves the architecture per platform instead of assuming x86_64.

  This also corrects a mislabelling that predates the change: `2.0.0rc1` through `2.0.0rc4`
  published their macOS archives as `-macos-x86_64.zip` while building them on an arm64
  runner. All 106 Mach-O binaries inside `comic-sol-2.0.0rc4-macos-x86_64.zip` are arm64, and
  its metadata, SBOM, and checksum entries all record `x86_64`. Anyone who took the archive at
  its word on an Intel Mac received a binary that cannot run natively. Apple silicon is now
  the only macOS native-archive target, and the name says so. Intel macOS remains supported
  through source installation on Python 3.11+. `docs/install.md` records the discrepancy for
  the archives already published.

  The lock files are unchanged: they are per-platform but arch-agnostic, and
  `requirements/locks/release-macos-x86_64.txt` already carried the hash for
  `cryptography-50.0.0-cp311-abi3-macosx_11_0_arm64.whl`, so the arm64 leg installs a
  prebuilt, already-trusted wheel. Their `-x86_64` filenames are now legacy; renaming them is
  left as separate work because `CONTRIBUTING.md`, `docs/onboarding.md`, `README.md`, and the
  lock-completeness test all reference those paths.

- Disclosed the macOS architecture mislabelling in the release notes of the four prereleases
  that carry it. `docs/install.md` recorded it only for `2.0.0rc4`, the archive it tells people
  to download, so `2.0.0rc1` through `2.0.0rc3` still described themselves as publishing
  `x86_64` archives with no correction anywhere near them. Each of
  `docs/releases/v2.0.0rc1.md` … `v2.0.0rc4.md` now carries a `Published archive correction`
  section naming its own archive, stating that the contents are arm64, and saying the immutable
  asset was not replaced. The distribution and known-limitation lines in those same notes no
  longer claim the macOS archive is x86_64 or that "no arm64 artifact is claimed"; the archive
  that shipped was the arm64 one.

  The published assets are deliberately left untouched. These are prereleases; a release tag
  and its assets name exact bytes, and rewriting a name or a `metadata.json` after publication
  would cost more than retroactively accurate naming buys. No `SHA256SUMS` digest is wrong —
  only the filename recorded beside it. Sigstore checksum signing landed after `rc4`, so no
  signature covers these four archives and none is invalidated by leaving them alone.
  The release pipeline targets macOS arm64 from `2.0.0rc6` onward; binary-architecture
  verification during assembly remains tracked in #206. Nothing here relies on the historical
  publisher's `--architecture` argument being honest.

### Changed

- Added a fail-closed publication contract for v2.2 live visual evidence. The local-only
  `scripts/live_visual_evidence.py` validator binds an exact candidate, sanitized
  provider/model/reviewer provenance, hash-verified before/after renders, the complete 105-slot
  character-consistency scorecard, retained attempts, defects, repairs, retries, accepted
  warnings, all nine visual review categories, and explicit limitations. Promotion requires
  reviewer approval, 105/105 coverage, an overall identity mean of at least 3.5/4, every
  aggregate axis at least 3/4, and no individual score below 3. The repository does not contain
  a real provider bundle, so `docs/releases/v2.2-live-visual-evidence.md` records the current
  state as BLOCKED rather than converting deterministic green results into an artistic-quality
  claim.

- Recorded the versioning strategy for the pending candidate and re-cut it as `2.0.0rc6`.
  `v2.0`, `v2.1`, and `v2.2` remain **milestone names, not version tags**; the distribution
  version is `2.0.0rc6`, and `comic_sol_product/version.py`, the Compose image tag, package
  metadata, native archive and SBOM names, `docs/releases/v2.0.0rc6.md`, this changelog, and
  the candidate installer fixtures all agree on it. Published-install examples intentionally
  remain on `2.0.0rc4`, the latest available prerelease, until `2.0.0rc6` is published. The
  prepared-but-failed `v2.0.0rc5` tag is neither moved nor reused: its Native Release runs
  failed with no release attached, and `main` advanced two release fixes past its target. Moving
  an immutable release tag would break the guarantee that a tag names the exact reviewed bytes. `v2.0.0rc6` will therefore be annotated
  fresh at the reviewed `main` commit after this PR lands. No `2.2.0` distribution exists or is
  planned by this candidate.

## 2.0.0rc6 — unreleased

Prepared for the `v2.0.0rc6` tag and **not published**. No archive, checksum, SBOM, or
signature exists until the release workflow runs, and the release qualification gate has not
been executed. The published prerelease is still `2.0.0rc4`.

Prerelease carrying milestones v2.0 — Stability, v2.1 — Reliability & DX, and v2.2 — Comic
Quality in full: 28 issues, none of which had shipped before. The v2.0 milestone belongs here
rather than in the earlier prereleases — those shipped the v2.0 product line on 2026-07-29/30,
but the milestone that hardened and qualified it merged on 2026-08-18/19, after `2.0.0rc4` was
published. `docs/releases/milestone-delivery.md` records which issue and pull request
delivered each change.

This remains a **prerelease**. `docs/releases/v2.0-stable-criteria.md` is the authoritative
gate for a stable `2.0.0` tag: it requires per-platform qualification from the release
artifacts themselves plus a recorded maintainer decision, and it is not yet satisfied.

### Added

- Made SFX handling explicit enough to verify or replace. SFX was the one text kind Comic
  Sol handed to the image model, and nothing recorded that: an authored `KRAK!` could come
  back misspelled, mirrored, doubled, or missing, and the project held no evidence that the
  effect had never been deterministic in the first place. An SFX item now declares
  `render_mode` — `generated-visual`, the default, or `deterministic-lettering`, which
  Pillow draws as outlined bold display type that reserves its rectangle like any balloon.
  Every panel's lettering geometry (now schema `1.2`) carries an `sfx` provenance block
  naming each effect's `origin` as `image-model` or `comic-sol-lettering`, so a reviewer can
  attribute a suspect effect instead of guessing. `validate_project.py` recomputes that
  block from the storyboard rather than reading it back, so a record cannot claim an effect
  was lettered while the plan still hands it to the model. New `scripts/sfx_verification.py`
  also reports the risks that *are* deterministically knowable without reading pixels:
  `sfx-glyph-risk`, `sfx-duplicate-content`, `sfx-legibility-budget`, and
  `sfx-unprohibited-generation`. Nothing performs OCR, and semantic accuracy of arbitrary
  artwork remains a reviewer judgement.
- Added `scripts/sfx_repair.py`, the supported path for replacing one faulty generated
  effect without re-rolling a whole panel on a hope. In one transaction it routes that item
  to deterministic lettering, adds the missing generated-SFX prohibition to the panel,
  archives the rejected clean and lettered rasters under `panels/{panel-id}/sfx-audit/`, and
  records the transition, the reason, and every preserved hash in
  `panels/{panel-id}/sfx-audit.json` — a repair that destroyed the artifact it diagnosed
  could not be reviewed. Only `generated-visual` SFX is generation material, so the edit
  invalidates generation and lettering while planning and the storyboard stage keep their
  cache; restating the default render mode explicitly is normalized out of both stages' cache
  material, so no existing project is regenerated or re-rolled by the mechanism. Lettered SFX
  is also excluded from `balloon-subject-obstruction` and `balloon-crowding`, which encode
  rules about speech: an effect is placed over the action deliberately, and counting it would
  fail a correct page while telling the reviewer to shorten dialogue that is not the cause.
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
- Defined the authoritative v2.0 stable release gate in
  `docs/releases/v2.0-stable-criteria.md`: supported clean-install targets, the required CLI
  and project lifecycle, interrupted-generation and resume behaviour, project-data
  preservation, release artifact integrity and provenance, and the no-open-P0/P1 rule.
  `.github/workflows/release.yml` verifies the document is present, so the gate cannot be
  removed and released around, and `tests/test_release_docs.py` asserts it stays complete.
- Added golden end-to-end pipeline coverage over a committed `tests/golden/mini-comic`
  project, so the deterministic half of the pipeline — plan, storyboard, lettering,
  composition, export, and validation — is exercised as one path rather than only as
  independent units.
- Added a lifecycle failure-injection suite (`tests/test_lifecycle_failures.py`) that faults
  the operations a project depends on rather than trusting the happy path.
- Added clean-install verification across the supported platforms, so a release is qualified
  from an installed runtime rather than only from a source checkout.
- Audited installer lifecycle safety: upgrade rollback restores the previous runtime, a failed
  upgrade leaves external state untouched, reinstall is idempotent, and uninstall removes only
  the runtime while user projects and separately managed client configuration survive.
  `tests/test_installers.py` covers each of those as a contract rather than as a manual check.
- Repeated `resume` calls are now read-only and idempotent after a recovery. A first recovery
  correctly cleared `BLOCKED`, but a second resume then raised
  `resume requires a BLOCKED project`, which made recovery a one-shot operation and a retry
  an error. A non-blocked resume now derives its summary without mutating `project.json` or
  `logs/events.jsonl`, and regression coverage pins the accepted artifact hashes and the
  provenance reuse events.
- `doctor` gained an authoritative structured report. `doctor_report()` returns stable check
  IDs with a `status` of `pass`, `warn`, or `fail`, a message, and remediation, covering
  runtime, Pillow, fonts, templates, references, the selected output root, MCP installation,
  and image capability. The same report is exposed through the CLI and the MCP adapter, and
  the legacy tuple API plus the `healthy` and `messages` JSON fields are preserved, so an
  existing consumer keeps working while a new one can branch on a check ID instead of parsing
  prose.
- Added the `comic_sol_product.errors` registry of canonical `CS-*` error codes, surfaced as
  `code`, `category`, `message`, `reason`, and `recovery` in CLI JSON and MCP `ToolError`
  envelopes. The previous CLI envelope and the legacy-readable MCP error text are retained as
  compatibility fields. Code allocation is append-only and documented in
  `docs/structured-errors.md`, with CLI/MCP parity, redaction, and registry contract tests, so
  a caller can branch on a stable identifier rather than on a message string.
- Made the project schema an explicit compatibility contract rather than an implied one.
  `scripts/schema.py` owns `CURRENT_PROJECT_SCHEMA_VERSION`,
  `MIN_READER_PROJECT_SCHEMA_VERSION`, and `SUPPORTED_PROJECT_SCHEMA_VERSIONS`, and every
  reader goes through it: an unsupported or future version is refused with
  `UnsupportedSchemaVersionError` instead of being guessed at, downgraded, or rewritten. A
  migration is permitted only through an explicit `(source, target)` hook in
  `PROJECT_MIGRATIONS`, published through the journal-backed project transaction, so a refused
  or interrupted migration leaves `project.json`, source files, logs, and user artifacts
  byte-for-byte unchanged. A manifest that needs no migration opens no transaction, and a
  manifest with no `schema_version` is normalized in memory only, leaving the file on disk
  untouched. `references/schemas.md` states the policy normatively.
- Added the v2.0 release qualification gate: `scripts/release_qualification.py` and
  `.github/workflows/release-qualification.yml` check a candidate against the stable criteria
  as a workflow rather than as a checklist someone remembers to run.
- Human lifecycle commands now report stage-aware progress. `resume` and `finalize` emit
  `WORKING`, `BLOCKED`, `FAILED`, and `COMPLETE` lines naming the current stage and the
  completed/remaining counts, so a long run is legible instead of silent. Progress goes to
  `stderr` only and results stay on `stdout`, so a script consuming output is unaffected;
  `--json` remains exactly one parseable envelope with no progress on either stream. An
  intermediate resume result no longer prints as though the project had completed, and a
  progress stream that cannot be written cannot change the lifecycle result.
- Added `docs/onboarding.md`: one linear path from install to a first finished comic, linked
  from the README introduction and the top of `docs/install.md`. A first run previously had
  to be assembled from four documents, none of which said what to do first or that `doctor`
  exists before any story is written. The page carries the shortest supported install per
  platform, runs `doctor` before authoring, states in plain language that Comic Sol does not
  generate images itself, gives one example prompt with the per-platform output root, and
  maps every failing `doctor` check id to its recovery — while release archives, checksum
  verification, MCP setup, non-Codex providers, and containers stay linked rather than
  inlined so the happy path stays short.
- Published official example projects: `samples/README.md` indexes them with explicit
  evidence tiers, and two new projects join the existing `sunlight-courier` —
  `first-light-signal` (one page, three panels) and `the-quiet-ledger` (four pages, eleven
  panels, four distinct layouts, two recurring characters, and one authored SFX, which sits
  deliberately at the supported ceiling). `scripts/build_examples.py` replays the
  deterministic half of the pipeline over an example's committed inputs through the exported
  PDF and validates it at the `final` stage, synthesizing panel artwork locally so no
  provider, credential, or downloaded asset is involved. Exports are reproduced on demand
  rather than tracked, so the examples add zero new binaries and no placeholder geometry is
  presented as finished artwork; `tests/test_examples.py` keeps them honest.
- Added `AGENTS.md`, a development constitution for agents changing engine code, tests,
  packaging, CI, or docs. Its eight articles — schema change safety, project-data
  preservation, the provider credential boundary, path containment, regression-test
  requirements, deterministic and atomic writes, resumability and public JSON
  compatibility, and full verification before claiming completion — are each grounded in a
  gate that already exists rather than in invented policy. It defers to human review,
  `CONTRIBUTING.md`, `SECURITY.md`, `PRIVACY.md`, and the v2.0 stable criteria, with the
  stricter policy winning any conflict, and it separates its own scope from `SKILL.md`,
  which governs the production agent rather than repository development. A waiver requires a
  recorded maintainer decision. `tests/test_agent_constitution.py` derives the resume-stage
  list from `scripts.stage_registry.RESUME_STAGES`, so adding a stage without updating the
  constitution fails.
- Added the Character Identity Pack at `plan/character-identity-pack.json`: one versioned
  artifact per project holding every character's stable visual identity, which panel prompts
  now embed instead of paraphrasing the character bible again per panel. Nothing previously
  bound one panel's wording to the next, so identity drift was the expected outcome rather
  than an anomaly. The pack is *derived* rather than hand-authored, which is what makes it
  deterministic: `scripts/character_identity.py --derive` derives, validates, and publishes
  atomically through the project transaction, and re-running it on an unchanged project
  rewrites byte-identical content, so a resume or a retry embeds the same identity clause the
  accepted panels were generated against. `--check` fails closed before generation on a
  missing, invalid, stale, unbacked, or tampered pack, and `--panel PANEL_ID` prints the
  deterministic, provider-neutral `IDENTITY LOCK` block. Authored additions survive
  re-derivation — extra reference views and `proportions.notes` are preserved while every
  bible-derived field is rebuilt — so a pack can never quietly disagree with the bible.
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
