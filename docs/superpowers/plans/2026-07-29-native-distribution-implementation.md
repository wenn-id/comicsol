# Native Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers/executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a verifiable `v2.0.0rc1` prerelease with self-contained Comic Sol executables, portable archives, one-command installers, an OCI image, checksums, SBOMs, and clean install/upgrade/uninstall evidence.

**Architecture:** PyInstaller builds one-folder runtimes independently on each target OS. A deterministic Python release module owns artifact naming, metadata, checksum, SBOM, archive, and verification rules. Thin PowerShell and POSIX installers unpack a verified portable archive into a user-local immutable version directory, update a stable launcher, run doctor/MCP smoke, and preserve projects during upgrade or uninstall. GitHub Actions builds artifacts on their native runners and a release workflow attaches only artifacts that pass lifecycle verification.

**Tech Stack:** Python 3.11, PyInstaller 6.15.0, Pillow 12.3.0, MCP 1.28.1, CycloneDX JSON, GitHub Actions, Docker/OCI.

## Global Constraints

- Release version is exactly `2.0.0rc1`; Git tag is exactly `v2.0.0rc1`.
- No system Python is required after installing a portable/native artifact.
- Windows, macOS, and Linux portable archives are built on native GitHub runners.
- External CLI and exact 17-tool MCP surface remain unchanged.
- Installer writes only user-local application/config locations and never deletes projects.
- Upgrade preserves projects, client backups, and the prior installed version for rollback.
- Uninstall removes binaries and integrations only; project deletion is outside this command.
- Every artifact has SHA-256, version metadata, CycloneDX SBOM, and explicit `unsigned` status.
- No signing claim is made without platform signing credentials.
- OCI image runs as non-root with `/data` as output root.
- All production behavior is developed test-first.

---

### Task 1: Release Contract and Version

**Files:**
- Modify: `pyproject.toml`
- Modify: `comic_sol_product/__init__.py`
- Create: `comic_sol_product/distribution.py`
- Create: `tests/test_distribution.py`

**Interfaces:**
- Produces: `ReleaseIdentity`, `artifact_name()`, `write_release_metadata()`, `write_checksums()`, `write_sbom()`, `verify_release_directory()`.

- [ ] Write failing tests requiring canonical version/tag/platform/architecture names, sorted SHA-256 output, CycloneDX metadata, explicit unsigned state, and rejection of missing or mismatched artifacts.
- [ ] Run `python -m unittest tests.test_distribution -v`; expect import failure for `comic_sol_product.distribution`.
- [ ] Implement immutable release records and deterministic writers/verifiers without network access.
- [ ] Set package version to `2.0.0rc1` in both package metadata locations.
- [ ] Run targeted tests and full regression; expect all green.
- [ ] Commit `feat: define native release contract`.

### Task 2: Bundled Runtime and Portable Archives

**Files:**
- Create: `packaging/comic-sol.spec`
- Create: `scripts/build_portable.py`
- Create: `scripts/portable_smoke.py`
- Modify: `tests/test_distribution.py`

**Interfaces:**
- Consumes: release naming and verification from Task 1.
- Produces: native one-folder runtime and deterministic `.zip`/`.tar.gz` portable archives.

- [ ] Write failing tests for PyInstaller data/resource coverage, launcher location, archive traversal rejection, and required runtime members.
- [ ] Run targeted tests and confirm missing build interfaces cause RED.
- [ ] Implement a PyInstaller spec that bundles engine, Skill, references, templates, fonts, licenses, Pillow, and MCP.
- [ ] Implement native archive creation and portable smoke (`--version`, doctor, init/status, MCP 17-tool discovery).
- [ ] Build and exercise the Linux portable runtime outside the checkout.
- [ ] Run targeted tests and full regression.
- [ ] Commit `feat: build portable bundled runtime`.

### Task 3: Transactional User-Local Installer

**Files:**
- Create: `installers/install.sh`
- Create: `installers/install.ps1`
- Create: `comic_sol_product/install_lifecycle.py`
- Create: `scripts/installer_smoke.py`
- Create: `tests/test_install_lifecycle.py`

**Interfaces:**
- Produces: `install_archive()`, `activate_version()`, `rollback_install()`, `uninstall_integration()` and stable launcher semantics.

- [ ] Write failing tests for fresh install, idempotent reinstall, upgrade preserving previous version/projects, failed verification rollback, and uninstall preserving projects.
- [ ] Confirm RED due to missing lifecycle module.
- [ ] Implement safe archive extraction, versioned directories, atomic active-version pointer, verification callback, and rollback.
- [ ] Add thin POSIX/PowerShell download/install entry points with checksum verification and explicit unsigned reporting.
- [ ] Exercise install → doctor → upgrade simulation → uninstall in temporary HOME.
- [ ] Run targeted tests and full regression.
- [ ] Commit `feat: add transactional native installer`.

### Task 4: OCI and Release CI

**Files:**
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.dockerignore`
- Create: `.github/workflows/release.yml`
- Modify: `.github/workflows/tests.yml`
- Modify: `tests/test_distribution.py`

**Interfaces:**
- Produces: non-root OCI image and native release artifacts from three OS runners.

- [ ] Write failing static contract tests for non-root container, `/data`, healthcheck, pinned actions, native runner matrix, artifact verification, and prerelease upload.
- [ ] Confirm RED against absent files/workflow.
- [ ] Implement multi-stage OCI build and Compose example.
- [ ] Implement manually dispatched/tag-triggered release workflow: native PyInstaller builds, smoke, metadata/SBOM/checksum, artifact aggregation, verification, prerelease publication.
- [ ] Extend PR CI with distribution contract tests and Linux bundled-runtime smoke.
- [ ] Build and smoke OCI locally when Docker is available; otherwise rely on GitHub container job and report local unavailability.
- [ ] Run targeted tests and full regression.
- [ ] Commit `ci: add native distribution release gates`.

### Task 5: Documentation and Release Candidate

**Files:**
- Modify: `README.md`
- Create: `docs/install.md`
- Create: `CHANGELOG.md`
- Create: `docs/releases/v2.0.0rc1.md`

**Interfaces:**
- Documents exact install, upgrade, rollback, uninstall, container, checksum, SBOM, and unsigned-state behavior.

- [ ] Add documentation contract tests for commands and preservation guarantees.
- [ ] Confirm RED before documentation changes.
- [ ] Document platform install paths, one-command installers, manual portable install, container usage, upgrade/rollback/uninstall, security metadata, and RC limitations.
- [ ] Run complete tests, compile checks, diff checks, package build, bundled runtime smoke, installer lifecycle smoke, and artifact verification.
- [ ] Commit `docs: prepare v2.0.0rc1 release`.
- [ ] Push branch, open PR, wait for all CI, fix failures, and squash merge.
- [ ] Re-run release gates on merged default branch and prove anonymous clean clone.
- [ ] Create immutable tag `v2.0.0rc1` through the release workflow and verify prerelease assets, hashes, SBOM, metadata, tag equality, and remote branch cleanup.
