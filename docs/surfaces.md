# Distribution surfaces and output roots

## Creator path

The creator path starts with the installed Agent Skill, a natural-language comic
request, and inspection of the editable project, page PNGs, QA report, and PDF. Comic
Sol is a provider-neutral, local-first comic production pipeline. The deterministic
engine is the product; Agent Skills, CLI, and MCP are adapters.

```bash
comic-sol skill-install --target codex --scope user
```

The core CLI does not create artwork by itself. It validates, persists, resumes, repairs,
letters, composes, and exports around an agent-supplied compatible image generator.
Comic Sol stores no provider credentials. Follow [getting started](user/getting-started.md)
for the complete creator route; the surfaces below are retained integration choices.

Comic Sol is one product distributed through several installable surfaces: a
Codex Skill checkout, a Codex Plugin bundle, a source checkout for development,
an installed Python CLI, native portable archives, an optional MCP server, and
an OCI image. The deterministic engine is the same on every surface; what
differs is how you start it and where your projects land by default.

This page separates those workflows one per section and states the default
output root for each, so no surface has to be inferred from another's
instructions. The platform and runtime combinations each surface supports are
published separately in [`docs/support-matrix.md`](support-matrix.md).

The default **project output root** is resolved by the engine per platform:

| Platform | Default output root |
|---|---|
| Linux, WSL | `$HOME/Comic Sol` |
| macOS | `$HOME/Documents/Comic Sol` |
| Windows | `%USERPROFILE%\Documents\Comic Sol` |

Every CLI command also accepts `--output-root PATH` to select a different
explicit root; the happy path in [`docs/onboarding.md`](onboarding.md) pins one
so the first run does not depend on the default at all.

## Surface-freeze governance

A **new surface** is a new distribution, installation, integration, or execution surface.
It requires exactly one of these review records:

1. a link to a published adoption summary that satisfies the real evidence gate; or
2. an explicit waiver from a named maintainer, recorded in both the relevant issue and the
   pull request.

The adoption gate requires at least 10 verified external comic creators and 20–50 valid,
consented, non-duplicate real-project reports. Current tooling, fixtures, CI runs,
maintainers, automated identities, fabricated submissions, and deterministic samples do
not satisfy the adoption gate. No qualifying adoption summary exists merely because these
tools or samples exist.

Security, correctness, compatibility, and maintenance work on existing surfaces remains
allowed without adoption evidence or a waiver.

Two approved boundaries are not additional standalone surfaces:

- [#245](https://github.com/wenn-id/comicsol/issues/245) is one universal, host-neutral
  Agent Skills portability initiative, not a separate surface for every AI host.
- [#244](https://github.com/wenn-id/comicsol/issues/244) ComfyUI support is one reference
  executor under the existing `external-tool` contract, not a new standalone product
  surface.

An agent, bot, reviewer, or implementation tool cannot infer a waiver, cannot
self-authorize a waiver, and cannot treat its own comment as maintainer approval. This is
governance for human review. A keyword scanner, blocking CI grep, bot, network check, or
automated waiver inference must not be implemented as enforcement.

## Codex Skill placement

The installed package places the canonical verified Agent Skill payload with
`comic-sol skill-install --target codex --scope user`. A fresh agent session then
orchestrates `SKILL.md`; the deterministic engine runs through the installed launcher
or, for an advanced source checkout, `"$PYTHON" scripts/comic_sol.py`.

- **How you start it:** a natural-language request in a fresh session; use
  `comic-sol doctor` for deterministic readiness.
- **Default output root:** the platform default in the table above.
- **Claim boundary:** Codex remains experimental until **both** a linked real host
  execution smoke record **and** durable, inspectable links to the required sanitized
  output artifacts exist. Installer tests prove placement mechanics, not live host
  compatibility.
- **Details:** [`README.md` → Install](../README.md#install-the-agent-skill) and
  [`docs/onboarding.md`](onboarding.md).

## Codex Plugin bundle

The same repository doubles as a skills-only Codex Plugin
(`codex plugin add comic-sol@comic-sol`). The bundle under `skills/comic-sol/`
is synchronized from the canonical root files, so behavior and defaults are
identical to the Skill checkout.

- **How you start it:** install the plugin through Codex's repo marketplace and
  start a fresh session.
- **Default output root:** the platform default in the table above.
- **Details:** [`README.md` → Codex Plugin](../README.md#codex-plugin-same-repository).

## Source checkout (development)

Developing on this repository is the same engine invoked from a checkout.
Development commands — tests, benchmarks, and smoke checks — must always pass an
explicit `--output-root` under a temporary or disposable directory so a test run
never writes into the real platform default. The deterministic test suite needs
no image provider.

- **How you start it:** `"$PYTHON" scripts/comic_sol.py doctor --output-root <temporary path>`.
- **Default output root:** the platform default applies only when you omit
  `--output-root`; development workflows should not.
- **Details:** [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Installed CLI (wheel)

`"$PYTHON" -m pip install .` installs the `comic-sol` launcher and the bundled
engine, fonts, templates, Skill, and references. The optional `.[mcp]` extra
adds the MCP SDK for the same launcher.

- **How you start it:** `comic-sol --json doctor`.
- **Default output root:** the platform default in the table above; override
  with `--output-root`.
- **Project initialization:** humans may run `comic-sol init --interactive` for
  the guided flow; automation uses fully specified `comic-sol --json init`
  flags and never receives a prompt.
- **Details:** [`README.md` → Install](../README.md#install).

## Native portable archive

The release archives bundle Python 3.11, Pillow, MCP, fonts, templates, the
Skill, and references, and install transactionally into a runtime root that is
**separate from your projects**: `$HOME/.local/share/comic-sol` on Linux and
macOS, `$HOME\AppData\Local\ComicSol` on Windows. Installed execution needs no
system Python.

- **How you start it:** the verified `installers/install.sh` or
  `installers/install.ps1`, then `comic-sol doctor` from the installed `bin/`.
- **Default output root:** the platform default in the table above. The
  installer-managed runtime root never holds user projects; uninstall removes
  only the runtime and preserves every output root.
- **Details:** [`docs/install.md`](install.md).

## MCP server

The optional `stdio` MCP server exposes the deterministic lifecycle as 17
`comic_*` tools. It has **no default output root**: `--root` is an explicit,
absolute, dedicated directory containing only Comic Sol projects, and every tool
invocation stays inside it. MCP uses no authentication — any process that can
launch the configured command can use the tools — so the root must not be a
home directory, repository root, or shared folder.

- **How you start it:** `comic-sol mcp --root /absolute/path/to/comic-sol-output`
  (installed) or `"$PYTHON" scripts/mcp_server.py --root …` (source).
- **Default output root:** none; the `--root` argument is required and must be
  absolute.
- **Details:** [`README.md` → MCP Server](../README.md#mcp-server-optional).

## OCI image

The release publishes an attested `comic-sol-<version>-linux-x86_64.container.tar`
(not a registry image) and the repository carries a non-root image definition.
Projects live under `/data` inside the container — a named volume under
`docker compose` — with a read-only root filesystem and no network. Nothing
persists outside that volume.

- **How you start it:** `docker load --input <verified container.tar>`, then run
  `comic-sol doctor --output-root /data/...`; or `docker compose up`.
- **Default output root:** `/data` inside the container; map it to a host volume
  to keep projects.
- **Details:** [`docs/install-manual.md` → OCI image](install-manual.md#oci-image).

## Comic Sol Studio (Web)

The Web distribution (`web/comic_sol_web/`) is a separately installed FastAPI
application that exposes the same deterministic lifecycle through a browser
and a WebMCP client (`5` read + `9` write tools). The local `stdio` MCP server
remains exactly `17` tools. The Web surface is **not** a replacement for the
core CLI: the engine, the local MCP server, the wheel, and the native archive
are unchanged. The Web surface has no default output root; an explicit
`COMIC_SOL_WEB_DATA_ROOT` volume is required and the process fails fast if it
is missing.

- **How you start it:** the Web package exports no dedicated console script.
  The factory `comic_sol_web.app.create_app` requires a `WebConfig`; the
  intended invocation is the bundled ASGI server with an explicit config
  (for example `python -c "import os, uvicorn; from
  comic_sol_web.config import WebConfig; from comic_sol_web.app import
  create_app; uvicorn.run(create_app(WebConfig.from_env(os.environ)),
  host='127.0.0.1', port=8000)"`), after setting the three required
  environment variables. The `/healthz` endpoint returns
  `{"status":"ok"}`; there is no readiness endpoint.
- **Default output root:** none; `COMIC_SOL_WEB_DATA_ROOT` is required and
  is a separate durable volume from the runtime. The hosted process must
  never contact a user's localhost.
- **Provider model:** the merged build registers only the WebMCP
  `agent` route, and it is selectable only when the startup invocation
  supplies trusted `text_to_image` capability; the documented bare
  start command exposes no executable route. `hosted`,
  `session BYOK`, and `encrypted persisted BYOK` are **offline
  adapter contracts**, not routes the merged application serves. The
  agent route is agent-native; the hosted route (when wired) would be
  the only route that does not require a credential in the browser.
- **Details:** [`docs/web/index.md`](web/index.md), [`docs/web/deployment.md`](web/deployment.md),
  [`docs/web/rollback.md`](web/rollback.md), [`docs/web/security.md`](web/security.md),
  [`docs/web/providers.md`](web/providers.md).

## Surface summary

| Surface | Start command | Default project output root |
|---|---|---|
| Codex Skill placement | fresh agent session; `comic-sol doctor` for readiness | Platform default (table above) |
| Codex Plugin bundle | fresh Codex session | Platform default (table above) |
| Source (development) | `scripts/comic_sol.py --output-root <temp>` | None expected — explicit root required by convention |
| Installed CLI | `comic-sol doctor` | Platform default (table above) |
| Native archive | installed `comic-sol doctor` | Platform default (table above); runtime lives separately |
| MCP server | `comic-sol mcp --root <absolute path>` | None — explicit `--root` required |
| OCI image | container entrypoint | `/data` inside the container |
| Comic Sol Studio (Web) | `python -c "import os, uvicorn; from comic_sol_web.config import WebConfig; from comic_sol_web.app import create_app; uvicorn.run(create_app(WebConfig.from_env(os.environ)), host='127.0.0.1', port=8000)"` (three required env vars must be set) | None — `COMIC_SOL_WEB_DATA_ROOT` is required |
