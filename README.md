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

Requirements are Python 3.11+ and `Pillow==12.3.0`. Resolve one Python 3.11+ launcher per device, store it as `PYTHON`, then use `"$PYTHON"` consistently for each run. Image creation additionally
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

Supported environments are Linux, macOS, Windows, and WSL with Python 3.11+ and
Pillow 12.3.0. On WSL, follow the Linux instructions above; the PowerShell
steps apply only when running Comic Sol directly on native Windows. The
deterministic test suite does not need an image provider.

Install the portable CLI from a checkout and verify the bundled deterministic
engine, fonts, and templates:

```bash
"$PYTHON" -m pip install .
comic-sol --json doctor
```

### Recommended companion: Superpowers

For structured brainstorming, planning, debugging, and verification workflows,
we recommend installing [Superpowers](https://github.com/obra/superpowers)
alongside Comic Sol. Superpowers is optional, installed separately, and is not
required for Comic Sol to run.

Machine-readable doctor output keeps the stable CLI envelope (`ok`, `command`, `data`, `error`). `data.ready` is the authoritative readiness boolean; the legacy `data.healthy` and `data.messages` fields remain available for existing consumers. `data.checks` contains stable check objects with `id`, `status` (`pass`, `warn`, or `fail`), `message`, and `remediation`. Runtime, Pillow, fonts, templates, references, and the selected output root fail closed when broken. MCP installation and image-generation capability are reported as actionable warnings when unavailable because they are optional for deterministic project editing.

The CLI exposes `doctor`, `init`, `status`, `validate`, `resume`, `finalize`,
`setup`, `repair`, and `uninstall`, plus the optional `mcp` launcher. Machine-readable
responses use one stable envelope containing `ok`, `command`, `data`, and `error`.

Lifecycle commands report concise stage progress only on the human-readable surface:
`WORKING`, `BLOCKED`, `FAILED`, or `COMPLETE`, followed by the current stage and known
completed/remaining counts. Progress is written to `stderr`, so scripts can consume
`stdout` safely. With `--json`, `stdout` remains one parseable JSON envelope and both
human progress and error details stay out of the machine stream.

## Native Distribution (`v2.0.0rc4`)

The `v2.0.0rc4` prerelease provides bundled portable archives for Linux, macOS,
and Windows x86_64, plus transactional user-local installers and a non-root OCI
image. Native archives include Python, Pillow, MCP, fonts, templates, the Skill,
and references, so installed execution does not require a system Python.

Every native bundle includes `SHA256SUMS`, deterministic metadata, and a
CycloneDX SBOM. The RC artifacts are explicitly **unsigned** and not notarized;
verify the SHA-256 digest before execution. Exact install, upgrade, rollback,
uninstall, and security instructions are in [`docs/install.md`](docs/install.md). The authoritative
stable-release checklist is [`docs/releases/v2.0-stable-criteria.md`](docs/releases/v2.0-stable-criteria.md).

Container and Compose deployment commands are documented in
[`docs/install.md`](docs/install.md); they are optional and are not required for
the normal local CLI workflow.

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
comic-sol --json repair --output-root /absolute/path/to/comic-sol-output
comic-sol --json uninstall --output-root /absolute/path/to/comic-sol-output
```

Setup refuses malformed native config, creates a timestamped backup before each
change, writes atomically, and restores the original bytes if verification fails.
Repeated setup is idempotent. Uninstall removes only the MCP integration and
preserves comic projects. Codex TOML and detected JSON client configs are mutated
only at verified locations; clients whose native format or location has not been
verified are reported as `unsupported` rather than guessed.

During source development, `"$PYTHON" scripts/mcp_server.py --root PATH` remains
available. Both entry points expose the same protocol-tested deterministic lifecycle
as exactly 17 `comic_*` tools.

## Image-provider boundary

Image generation is agent-managed: the active Codex session selects and invokes
its available image capability, then the deterministic CLI/MCP lifecycle validates,
retains, normalizes, and records the resulting raster. Provider credentials, SDKs,
and raw provider payloads remain outside Comic Sol.

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

## Inspect the result

Each run preserves editable semantic and visual intermediates beneath its generated
project directory. The important outputs are:

```text
project.json                  project manifest and current state
plan/                         story plan, character bible, and storyboard
prompts/                      editable reference and panel prompts
references/                   canonical character and scene images
panels/raw/                   generated panel PNGs
panels/clean/                 accepted clean panel PNGs
panels/*/lettered.png         deterministic lettered panel PNGs
pages/page-001.png            ordered 1600×2400 page PNGs
exports/<project-id>.pdf      ordered comic PDF
qa/panels/*.json              seven-check panel QA records
qa/report.md                  human-readable QA report
exports/pdf-verification.json PDF verification (`pdf_verification` descriptor)
logs/                         sanitized events, cache, and retry accounting
```

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
while a character absent from both fonts is preserved as Noto Sans `.notdef` rather
than silently dropped. The `--font` option still overrides the regular dialogue font;
its bold counterpart may fall back to Comic Neue Bold.

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
its reference-image and dimension support. The bundled font set does not cover CJK;
characters absent from both bundled fonts are preserved as visible `.notdef` fallback boxes
unless a compatible regular `--font` override covers them. Image-model SFX
spelling is not deterministic, so visual QA and bounded retries remain required.
Offline fixtures prove deterministic stages, not live image quality. Large projects
beyond four pages or twelve panels require an explicit scope decision.

For support, run `doctor`, retain the printed project path, and inspect
`project.json` plus `qa/report.md`. A `BLOCKED` project is intentionally resumable;
restore the missing capability or correct the reported artifact, then ask Codex to
resume that Comic Sol project.

## License

Comic Sol's original code and documentation are available under the MIT License in
[`LICENSE`](LICENSE). The bundled Comic Neue and Noto Sans fonts remain separately
licensed under the SIL Open Font License 1.1; see
[`assets/README.md`](assets/README.md).

## Contributing and security

Development is review-first through pull requests into `main`. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the required validation gates. Report
security issues privately as described in [`SECURITY.md`](SECURITY.md).
