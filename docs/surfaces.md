# Distribution surfaces and output roots

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

## Codex Skill checkout

Clone this repository as one `comic-sol` folder beneath the Codex skills
directory and install the pinned dependency with your resolved Python 3.11+
launcher. The agent session orchestrates `SKILL.md`; the deterministic scripts
run through `"$PYTHON" scripts/comic_sol.py`.

- **How you start it:** `"$PYTHON" scripts/comic_sol.py doctor` from the
  checkout, or a natural-language request in a fresh Codex session.
- **Default output root:** the platform default in the table above.
- **Details:** [`README.md` → Install](../README.md#install) and
  [`docs/onboarding.md`](onboarding.md).

## Codex Plugin bundle

The same repository doubles as a skills-only Codex Plugin
(`codex plugin add comic-sol@comic-sol`). The bundle under `skills/comic-sol/`
is synchronized from the canonical root files, so behavior and defaults are
identical to the Skill checkout.

- **How you start it:** install the plugin through Codex's repo marketplace and
  start a fresh session.
- **Default output root:** the platform default in the table above.
- **Details:** [`README.md` → Codex Plugin](../README.md#codex-plugin--same-repository).

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
- **Details:** [`docs/install.md` → OCI image](install.md#oci-image).

## Surface summary

| Surface | Start command | Default project output root |
|---|---|---|
| Codex Skill checkout | `"$PYTHON" scripts/comic_sol.py doctor` | Platform default (table above) |
| Codex Plugin bundle | fresh Codex session | Platform default (table above) |
| Source (development) | `scripts/comic_sol.py --output-root <temp>` | None expected — explicit root required by convention |
| Installed CLI | `comic-sol doctor` | Platform default (table above) |
| Native archive | installed `comic-sol doctor` | Platform default (table above); runtime lives separately |
| MCP server | `comic-sol mcp --root <absolute path>` | None — explicit `--root` required |
| OCI image | container entrypoint | `/data` inside the container |
