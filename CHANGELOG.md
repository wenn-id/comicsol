# Changelog

## Unreleased

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

- Linux, macOS, and Windows x86_64 bundles, CycloneDX SBOMs, and SHA-256 manifests remain available.
- Native archives and installers remain unsigned; verify them against `SHA256SUMS`.

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
