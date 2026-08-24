# Comic Sol

[![Tests](https://github.com/wenn-id/comicsol/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/wenn-id/comicsol/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/wenn-id/comicsol?include_prereleases&label=release)](https://github.com/wenn-id/comicsol/releases)
[![License](https://img.shields.io/github/license/wenn-id/comicsol)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP tools](https://img.shields.io/badge/MCP_tools-17-brightgreen)](#mcp-server-optional)
[![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-blue)](docs/install.md)

Comic Sol is an installable Codex Skill and portable Python CLI that turns a short prompt, pasted story,
or `.txt`/`.md` file into an original manga/anime comic. One natural-language
invocation drives planning, character consistency, image generation, visual QA,
selective repair, deterministic lettering and composition, and PDF export. It is
not a web app or hosted product. No build service is required.

This repository, [`wenn-id/comicsol`](https://github.com/wenn-id/comicsol),
is the canonical, independent home of Comic Sol. New development, issues, pull
requests, documentation, and releases happen here.

> **New here? Read [`docs/onboarding.md`](docs/onboarding.md) first.** It is one
> short first-run path from installation to your first finished comic, including
> the readiness check and where the output lands. The rest of this README is the
> full reference.

## Install

### Native core CLI (recommended)

For the bundled core CLI, use the exact-tag installer path in
[`docs/install.md`](docs/install.md). The guide is prepared for the unpublished
`v2.0.0rc6` candidate and becomes executable when that tag's assets appear on
the Releases page. It automatically selects the supported native asset, verifies
the tag-bound Sigstore manifest and archive digest, runs staged `doctor`, and
prints one absolute doctor command to run next. Advanced local-archive, source,
wheel, and OCI procedures live separately in
[`docs/install-manual.md`](docs/install-manual.md).

### Codex Skill checkout

The Skill surface requires Python 3.11+ and `Pillow==12.3.0`. Resolve one Python
3.11+ launcher per device, store it as `PYTHON`, then use `"$PYTHON"`
consistently for each run. Image creation additionally
requires an image-generation capability exposed to the active agent session; Comic
Sol never embeds provider credentials. Comic Sol requires no Comic Sol account or
demo credentials, although a Codex session and the selected image provider may
require their own account or access.

Clone the public repository directly into the Codex skills directory, then install
the one pinned dependency:

```bash
git clone https://github.com/wenn-id/comicsol.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
cd "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
PYTHON=python  # replace with resolved Python 3.11+ launcher
"$PYTHON" -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
```

The host-agnostic rule is: clone or copy this repository as one `comic-sol` folder
beneath the Codex skills directory configured by your Codex installation. Keep
`SKILL.md`, `scripts/`, `references/`, `templates/`, and `assets/` together.

### Codex Plugin — same repository

This repository is also a skills-only Codex Plugin. The plugin manifest lives at
`.codex-plugin/plugin.json`; its self-contained upload bundle lives under
`skills/comic-sol/`. The root skill files are canonical; the bundle is synchronized
with `scripts/sync_plugin_bundle.py`. No second repository is required.

Test the same repository through Codex's repo marketplace:

```bash
codex plugin marketplace add wenn-id/comicsol --ref main
codex plugin list --available --json
codex plugin add comic-sol@comic-sol --json
```

The plugin includes the workflow, deterministic scripts, references, templates,
fonts, and legal assets. Start a fresh Codex session after installation. The
optional MCP/CLI engine remains in this repository as a separate local surface;
plugin installation does not require a hosted service or MCP server.

Windows PowerShell:

```powershell
git clone https://github.com/wenn-id/comicsol.git `
  "$env:USERPROFILE\.codex\skills\comic-sol"
Set-Location "$env:USERPROFILE\.codex\skills\comic-sol"
$PYTHON = "py"  # use: py -3; resolve Python 3.11+ first
& $PYTHON -3 -m pip install --require-hashes -r requirements/locks/base-windows-x86_64.txt
```

Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+.
The pinned runtime dependency is Pillow 12.3.0. Intel macOS is source-install-only; it has no native archive.
The native archive matrix is Linux x86_64, macOS arm64, and Windows x86_64.
WSL2 uses the Linux x86_64 archive; it has no separate native archive. On WSL,
follow the Linux instructions above; the PowerShell steps apply only when running
Comic Sol directly on native Windows. The deterministic test suite does not need
an image provider.

Comic Sol ships as several surfaces — Skill checkout, Codex Plugin, source
checkout, installed CLI wheel, native portable archive, MCP server, and OCI
image — each with its own start command and default output root;
[`docs/surfaces.md`](docs/surfaces.md) separates them, and
[`docs/support-matrix.md`](docs/support-matrix.md) publishes the full
platform × install-mode × architecture × runtime matrix. Source and wheel
installation are advanced core-CLI paths documented in
[`docs/install-manual.md`](docs/install-manual.md#source-and-wheel-installation).

### Recommended companion: Superpowers

For structured brainstorming, planning, debugging, and verification workflows,
we recommend installing [Superpowers](https://github.com/obra/superpowers)
alongside Comic Sol. Superpowers is optional, installed separately, and is not
required for Comic Sol to run.

Machine-readable doctor output keeps the stable CLI envelope (`ok`, `command`,
`data`, `error`). `data.ready` is the authoritative readiness boolean; the legacy
`data.healthy` and `data.messages` fields remain available for existing consumers.
`data.checks` contains stable check objects with `id`, `status` (`pass`, `warn`, or
`fail`), `message`, and `remediation`. Runtime, Pillow, fonts, templates, references,
and the selected output root fail closed when broken. MCP installation and a missing,
partial, or unknown image-generation capability are actionable warnings because both
remain optional for deterministic project editing.

Before an agent-driven doctor call, the Comic Sol Skill inspects only the image-tool
metadata exposed in that session. It supplies the fixed neutral name
`agent-image-generation` with declared reference-image and dimension support, reports
`unavailable` when the
inspectable inventory has no usable tool, or supplies no capability flags when inspection
is unavailable or fails. A fully capable observation passes; a partial, unavailable, or
unknown observation warns without changing deterministic readiness. Detection invokes no
provider, reads no credential, and installs or enables nothing.

The CLI exposes `doctor`, `init`, `status`, `validate`, `resume`, `finalize`,
`setup`, `repair`, and `uninstall`, plus the optional `mcp` launcher. Machine-readable
responses use one stable envelope containing `ok`, `command`, `data`, and `error`.

Lifecycle commands report concise stage progress only on the human-readable surface:
`WORKING`, `BLOCKED`, `FAILED`, or `COMPLETE`, followed by the current stage and known
completed/remaining counts. Progress is written to `stderr`, so scripts can consume
`stdout` safely. With `--json`, `stdout` remains one parseable JSON envelope and both
human progress and error details stay out of the machine stream. The contract is
fail-closed: argument-parse failures emit a `CS-CLI-001` envelope instead of argparse
text, unexpected internal failures emit a redacted `CS-PROJ-005` envelope instead of a
traceback, and `validate` reports a project with issues as `ok: false` with exit
status `2` while keeping the issue list in `data`. Exit statuses are `0` for success,
`1` for operational failures, and `2` for usage or input rejection. Every public code,
category, exit status, redaction rule, and CLI/MCP parity behavior is documented in
[`docs/structured-errors.md`](docs/structured-errors.md).

## Native Distribution (`v2.0.0rc4`)

The published `v2.0.0rc4` prerelease provides bundled portable archives labelled
for Linux x86_64, macOS x86_64, and Windows x86_64, plus transactional user-local
installers and a non-root OCI image. The macOS archive was built for arm64 despite
its historical x86_64 filename; the current native matrix corrects that name to
macOS arm64. Native archives include Python, Pillow, MCP, fonts, templates, the
Skill, and references, so installed execution does not require a system Python.

Every native bundle includes `SHA256SUMS`, a Sigstore bundle for that manifest,
deterministic metadata, and a CycloneDX SBOM. The RC artifacts use keyless
Sigstore verification (not Authenticode-signed or Apple-notarized); verify the
manifest signature and archive digest before execution. The recommended path,
upgrade, rollback, and uninstall instructions are in
[`docs/install.md`](docs/install.md); bootstrap verification and advanced
installation procedures are in
[`docs/install-manual.md`](docs/install-manual.md). The authoritative
stable-release checklist is [`docs/releases/v2.0-stable-criteria.md`](docs/releases/v2.0-stable-criteria.md),
the complete release subject set and trust chain — including the OCI distribution
decision — is [`docs/releases/release-trust-chain.md`](docs/releases/release-trust-chain.md), and the
immutable-evidence rollback/yank procedures are [`docs/releases/rollback-runbook.md`](docs/releases/rollback-runbook.md).

What each milestone delivered, and which issue and pull request delivered it, is recorded in
[`docs/releases/milestone-delivery.md`](docs/releases/milestone-delivery.md). Milestones v2.0,
v2.1, and v2.2 are complete and unreleased: they are **prepared for `v2.0.0rc6`, which is not
yet published**, so the
current published prerelease remains `v2.0.0rc4` and the archives described above are its own.
The `2.0.0rc*` prereleases shipped the v2.0 product line, not the v2.0 milestone, which merged
afterwards.

Container and Compose deployment commands are documented in
[`docs/install-manual.md`](docs/install-manual.md#oci-image); they are optional
and are not required for the normal local CLI workflow.

Native uninstall removes only the runtime. User projects and separately managed
MCP client configurations remain preserved.

## MCP Server (Optional)

Comic Sol includes an optional `stdio` MCP server that exposes the deterministic pipeline as standard tools for Codex-compatible MCP clients.

Sampling should remain disabled to preserve deterministic execution.

### MCP trust boundary

MCP uses local `stdio` and has no authentication layer. Any process that can
launch the configured MCP command can invoke every deterministic tool and write
inside its configured `--root`. Treat the MCP client and its configuration as
trusted local code. Use a dedicated absolute output root containing only Comic
Sol projects; do not point it at a home directory, repository root, or shared
multi-user folder. The server rejects project traversal and symlinks, but this
is containment, not user authentication.

The CLI `status`, `validate`, `resume`, and `finalize` commands accept a project
path directly. For the same containment model, keep project paths beneath the
output root and use MCP when an explicit fixed root is required.

To run the MCP adapter, install the SDK alongside Pillow. Use matching command
and lockfile for host platform:

```bash
# Linux
"$PYTHON" -m venv ~/.venvs/comic-sol-mcp
~/.venvs/comic-sol-mcp/bin/pip install --require-hashes -r requirements/locks/runtime-linux-x86_64.txt
```

```bash
# macOS
"$PYTHON" -m venv ~/.venvs/comic-sol-mcp
~/.venvs/comic-sol-mcp/bin/pip install --require-hashes -r requirements/locks/runtime-macos-x86_64.txt
```

```powershell
# Windows
& $PYTHON -3 -m venv $HOME\.venvs\comic-sol-mcp
& $HOME\.venvs\comic-sol-mcp\Scripts\pip.exe install --require-hashes -r requirements/locks/runtime-windows-x86_64.txt
```

From the repository root, start the development server with a repository-relative
script path and an absolute selected output root:

```bash
OUTPUT_ROOT="$(pwd)/comic-sol-output"
"$PYTHON" scripts/mcp_server.py --root "$OUTPUT_ROOT"
```

For an MCP client configuration, lock the server to one absolute output root and
keep sampling disabled. An installed package uses the stable launcher:

```bash
~/.venvs/comic-sol-mcp/bin/pip install --no-deps '.[mcp]'
~/.venvs/comic-sol-mcp/bin/comic-sol mcp --root /absolute/path/to/comic-sol-output
```

On native Windows PowerShell:

```powershell
& "$HOME\.venvs\comic-sol-mcp\Scripts\pip.exe" install --no-deps ".[mcp]"
& "$HOME\.venvs\comic-sol-mcp\Scripts\comic-sol.exe" mcp --root C:\absolute\path\to\comic-sol-output
```

Transactional client integration is available through:

```bash
comic-sol --json setup --output-root /absolute/path/to/comic-sol-output
comic-sol --json repair --dry-run --output-root /absolute/path/to/comic-sol-output
comic-sol --json repair --output-root /absolute/path/to/comic-sol-output
comic-sol --json uninstall --output-root /absolute/path/to/comic-sol-output
```

`repair --dry-run` diagnoses each detected integration and previews the intended
`comic-sol` command, arguments, config path, action, and backup requirement without
writing. Apply recomputes the plan under the config lock, verifies the backup, writes
atomically, verifies the persisted entry, and verifies rollback after failure. Each
client returns `success`, `no-op`, or `failure`; selected clients that cannot be
verified return a structured `failure`, while unselected or undetected clients
may return `no-op` with `skipped` status and unverified native formats return
`no-op` with `unsupported` status. The compatible `status` values are `planned`
for preview, `configured` for an applied change, `unchanged` for a repeat/no-op,
`rolled-back` when a failed mutation was restored, and `rollback-failed` when
restoration could not be verified. Repair exits nonzero if any client fails.
Repeating repair is idempotent.

Repair changes only the `comic-sol` MCP entry at verified Codex TOML and detected
JSON config locations. It does not create missing third-party config files, repair
unsupported clients, or reinstall runtime components; those cases return diagnostic
and `doctor` guidance. Uninstall removes only the MCP integration and preserves comic
projects. Clients whose native format or location has not been verified are reported
as `unsupported` rather than guessed.

During source development, `"$PYTHON" scripts/mcp_server.py --root PATH` remains
available. Both entry points expose the same protocol-tested deterministic lifecycle
as exactly 17 `comic_*` tools.

## Image-provider boundary

Image generation is agent-managed: the active agent session selects and invokes
its available image capability, then the deterministic CLI/MCP lifecycle validates,
retains, normalizes, and records the resulting raster. Provider credentials, SDKs,
and raw provider payloads remain outside Comic Sol. Before generation, the Skill
automatically inspects exposed tool metadata and passes a provider-neutral observation to
`doctor`; the deterministic engine never discovers or calls a provider itself.

## Invoke

Open Codex with the skill installed and say, for example:

> Make a 2-page manga about a courier delivering sunlight to an underground city.

That is the complete user invocation. Comic Sol detects prompt, pasted-story,
source-file, or resume mode; applies documented defaults; and asks only materially
missing questions. It reports an explicit error if the agent session cannot return
a local raster image. See
[`references/capability-detection.md`](references/capability-detection.md) for the
exact capability check and preserved-project recovery procedure. Platform-specific,
provider-neutral image setup is documented in
[`references/image-provider-setup.md`](references/image-provider-setup.md).

For deterministic diagnostics:

```bash
"$PYTHON" scripts/comic_sol.py doctor --output-root /tmp/comic-sol-doctor
# Installed equivalent:
comic-sol --json doctor --output-root /tmp/comic-sol-doctor
```

These direct calls omit agent capability flags. Human output says
`INFO image capability: inspect in agent session`; JSON reports
`details.readiness` as `unknown`. The Skill supplies
`--image-capability-status available --image-capability-name agent-image-generation` and the declared `--supports-reference-images` and
`--supports-dimensions` flags, or
`--image-capability-status unavailable` when it can conclusively inspect an inventory
with no usable text-to-image tool.

## Inspect the result

Each run preserves editable semantic and visual intermediates beneath its generated
project directory. The important outputs are:

```text
project.json                       project manifest and current state
plan/                              story plan, character bible, and storyboard
plan/character-identity-pack.json  per-character identity traits for generation (v2.2)
prompts/                           editable reference and panel prompts
references/                        canonical character and scene images
panels/raw/                        generated panel PNGs
panels/clean/                      accepted clean panel PNGs
panels/*/lettered.png              deterministic lettered panel PNGs
panels/*/sfx-audit.json            authored-SFX authorization records
pages/page-001.png                 ordered 1600×2400 page PNGs
exports/<project-id>.pdf           ordered comic PDF
exports/pdf-verification.json      PDF verification (`pdf_verification` descriptor)
qa/panels/*.json                   seven-check panel QA records
qa/pages/page-001.json             ten-check page QA records (schema 2.1)
qa/report.md                       human-readable QA report
logs/reference-selection.json      which reference images each panel uses (v2.2)
logs/repair-plan.json              selective panel-repair decisions (v2.2)
logs/                              sanitized events, cache, and retry accounting
```

Treat the QA artifacts as evidence with limits, not as guarantees: deterministic
offline builds prove schema, lettering, composition, export, and validation
mechanics — including the page-QA 2.1 records and the PDF verification descriptor —
while artwork-dependent checks on placeholder builds are recorded as unreviewed
warnings, and live visual quality is established only by the
[v2.2 live-visual-evidence contract](docs/releases/v2.2-live-visual-evidence.md),
not by a green test suite.

### Official examples

To see that structure filled in before running anything yourself, read
[`samples/README.md`](samples/README.md). It catalogs three reference projects and
states which of them is evidence of visual quality:

| Example | Pages | Panels | Tier |
|---|---:|---:|---|
| [`first-light-signal`](samples/first-light-signal) | 1 | 3 | Deterministic |
| [`sunlight-courier`](samples/sunlight-courier) | 2 | 4 | Live-generated |
| [`the-quiet-ledger`](samples/the-quiet-ledger) | 4 | 11 | Deterministic |

`sunlight-courier` tracks real generated artwork and its exported PDF; it is the
one to look at for image output. The two deterministic examples commit only
editable inputs — story plan, character bible, storyboard, and prompts — and build
their pages, QA records, and PDF locally with no provider call:

```bash
"$PYTHON" scripts/build_examples.py
```

Each build is validated at the `final` stage. Deterministic builds prove schema,
lettering, composition, export, and validation mechanics; their placeholder panel
artwork is not a claimed live sample. Because a placeholder has no artwork to
inspect, those builds record the artwork-dependent QA checks as unreviewed
warnings and terminate as `COMPLETE_WITH_WARNINGS` rather than claiming a visual
review that never happened. The committed
[one-page synthetic fixture](tests/fixtures/valid-one-page) remains available as
minimal test data.

## Architecture

`SKILL.md` orchestrates ten agent stages and progressively loads guidance from
`references/`. Agent reasoning creates the editable story, character, storyboard,
prompt, and QA decisions. Python scripts handle only deterministic project state,
validation, resume planning, lettering, page composition, PDF export, and report
rendering. Provider access remains in the agent capability plane; secrets and
provider SDKs do not enter deterministic scripts.

## Hybrid lettering

Dialogue is rendered in uppercase, while authored caption casing is preserved.

Dialogue uses bundled Comic Neue Regular, and inline `**bold**` emphasis uses Comic
Neue Bold with wrapping and centering measured across the mixed runs. Font selection
is per-character: bundled Noto Sans covers Greek and Cyrillic when Comic Neue does not,
and an optional per-script face given as `--font-script SCRIPT=PATH` is consulted last,
which is how CJK, kana, Hangul syllables, Armenian, Georgian, and Ethiopic letter
without bundling their fonts. Typography preflight refuses text that no configured face
covers before any panel is written, so `.notdef` fallback boxes are not a pipeline
outcome; `letter_panel()` called directly still preserves a character absent from every
face as a Noto Sans `.notdef` rather than silently dropping it. The `--font` option
still overrides the regular dialogue font; its bold counterpart may fall back to
Comic Neue Bold. Run `scripts/font_coverage.py` for the coverage inventory, and see
[`docs/typography.md`](docs/typography.md) for the supported scripts, the selected
extension fonts, and the fallback order.

Pillow fits dialogue into adaptive oval balloons, attaches each tail at the nearest
oval boundary toward a validated speaker or device anchor, and renders a tapered
organic cubic tail that stops before the voice source. It also draws compact light caption
strips sized to their text.
Authored SFX takes the other half of the hybrid pipeline: the image model draws
the exact SFX into the artwork, and visual QA verifies spelling, count, and authorization.
Pillow validates and counts SFX but reserves no placement and changes no pixels for it.

## Test

Run the complete offline suite:

```bash
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" scripts/comic_sol.py doctor
```

### Base environment (Pillow only, MCP tests skipped)

Create a clean virtual environment and install only the base dependency:

```bash
"$PYTHON" -m venv /tmp/comic-sol-base
/tmp/comic-sol-base/bin/pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
/tmp/comic-sol-base/bin/python -m unittest discover -s tests -v
```

MCP tests (`test_mcp_server.py`) are skipped gracefully when the `mcp` package is absent.

### MCP-extra environment (Pillow + MCP, all tests run)

```bash
"$PYTHON" -m venv /tmp/comic-sol-mcp-extra
/tmp/comic-sol-mcp-extra/bin/pip install --require-hashes -r requirements/locks/runtime-linux-x86_64.txt
/tmp/comic-sol-mcp-extra/bin/python -m unittest discover -s tests -v
```

Clean-room Linux/macOS/WSL check:

```bash
tmp_dir=$(mktemp -d)
"$PYTHON" -m venv "$tmp_dir/venv"
"$tmp_dir/venv/bin/python" -m pip install Pillow==12.3.0
"$tmp_dir/venv/bin/python" scripts/comic_sol.py doctor --output-root "$tmp_dir/output"
```

Windows clean-room equivalent:

```powershell
$TempRoot = Join-Path $env:TEMP "comic-sol-clean-room"
& $PYTHON -3 -m venv "$TempRoot\venv"
& "$TempRoot\venv\Scripts\python.exe" -m pip install Pillow==12.3.0
& "$TempRoot\venv\Scripts\python.exe" scripts\comic_sol.py doctor --output-root "$TempRoot\output"
```

## Support matrix

| Area | Supported | Notes |
|---|---|---|
| Inputs | Short prompt, pasted prose, UTF-8 `.txt` and `.md`, resume | Source limit and defaults are documented in the workflow reference. |
| Output | Panel PNGs, page PNGs, comic PDF, manifest, QA report | Editable intermediate artifacts remain local. |
| Lettering | Comic Neue Regular/Bold; per-character Noto Sans fallback | Adaptive oval dialogue, actual inline bold emphasis, compact captions, and hybrid authored SFX are supported. Font licenses and digests are in `assets/README.md`. |
| Image generation | Agent-exposed image model returning a local raster | References and exact dimensions are used when supported; exact authored SFX is checked by visual QA. |
| Deterministic scripts | Python 3.11+ and Pillow 12.3.0 | Offline and provider-neutral. |
| Native MCP | Python 3.11+ and MCP SDK 2.0.0 via `stdio` | Exposes 17 tools covering the full deterministic lifecycle safely locked to one output root. |

## Privacy, IP, and Limitations

Project artifacts stay in the selected local output directory. Prompts and reference
images sent to a selected image tool are governed by its external provider policies.
Minimize private source material and never place secrets in story text or
prompts. Logs contain sanitized paths, hashes, categories, and state changes rather
than raw credentials or story content.

Comic Sol requests original manga/anime direction and translates disallowed artist
or franchise imitation into high-level visual traits. It does not promise perfect
character continuity: results depend on the available image capability, especially
its reference-image and dimension support. Lettering places glyphs by nominal advance
without a shaping engine, so it refuses scripts that need contextual joining, cluster
reordering, mark stacking, or bidirectional runs — Arabic, Hebrew, the Indic scripts,
and Thai among them — because no font choice renders them correctly; see
[`docs/typography.md`](docs/typography.md). Image-model SFX
spelling is not deterministic, so visual QA and bounded retries remain required.
Offline fixtures prove deterministic stages, not live image quality. Large projects
beyond four pages or twelve panels require an explicit scope decision.

### Accessibility and localization limitations

- Exported PDFs are **image-based**: each page is a rasterized PNG embedded in
  the PDF. There is no extractable text layer, the document is untagged, it is
  **not PDF/UA** or otherwise standards-conformant for accessibility, and it
  carries **no alt text** for panels or pages. Screen readers cannot read the
  dialogue out of the exported file; use the editable `prompts/` and `plan/`
  intermediates when you need machine-readable text.
- Lettering places glyphs by nominal advance without a shaping engine, so
  scripts that need contextual joining, cluster reordering, mark stacking, or
  bidirectional runs are refused rather than rendered incorrectly; see
  [`docs/typography.md`](docs/typography.md) for the supported-script contract
  and extension fonts. No font choice adds shaping support.
- The CLI and Skill surface — command names, progress, QA reports, and
  documentation — are English-only. Story content may use any script the
  typography preflight accepts, but Comic Sol does not localize its own
  interface or messages.

For support, run `doctor`, retain the printed project path, and inspect
`project.json` plus `qa/report.md`. A `BLOCKED` project is intentionally resumable;
restore the missing capability or correct the reported artifact, then ask Codex to
resume that Comic Sol project.

## License

Comic Sol's original code and documentation are available under the MIT License in
[`LICENSE`](LICENSE). The bundled Comic Neue and Noto Sans fonts remain separately
licensed under the SIL Open Font License 1.1; see
[`assets/README.md`](assets/README.md).

## Contributing, support, and security

Development is review-first through pull requests into `main`. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the required validation gates.

- **Support** — how to report a problem with the right diagnostics (version,
  install mode, error code, JSON `doctor` output) and when to use the private
  route for sensitive reports: [`SUPPORT.md`](SUPPORT.md).
- **Privacy** — what each surface (Skill, plugin, CLI, native archive, MCP,
  OCI) keeps local and what leaves the machine through your image provider:
  [`PRIVACY.md`](PRIVACY.md).
- **Terms** — the terms of use for every distribution surface:
  [`TERMS.md`](TERMS.md).
- **Security** — report security issues privately as described in
  [`SECURITY.md`](SECURITY.md).
- **Typography and accessibility limits** — supported scripts, extension
  fonts, and the accessibility/localization limitations of exported PDFs:
  [`docs/typography.md`](docs/typography.md) and
  [Accessibility and localization limitations](#accessibility-and-localization-limitations).
