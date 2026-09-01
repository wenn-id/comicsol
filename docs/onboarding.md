# First run: install to first comic

This is the provider-neutral creator path: install the Agent Skill, request a comic,
let the active session render through a declared image capability or portable handoff,
and inspect the editable project, pages, QA report, and PDF. Comic Sol is a local-first
production pipeline; the deterministic engine is the product, and Agent Skills, CLI,
and MCP are adapters.

Follow the path top to bottom and stop at the first step that fails. Retained advanced
integrations stay linked after the creator path. The
[user-guide index](user/index.md) covers resume, repair, and export after the first run.

## 1. Install the Agent Skill

`skill-install` requires Comic Sol `v2.0.0rc6` or later. Because `v2.0.0rc6`
is not yet published, run this command only after installing an rc6-or-later package
or native distribution whose release assets are available, or from a trusted rc6
source checkout installed as a package. The shortest explicit user-scope placement is:

```bash
comic-sol skill-install --target codex --scope user
```

Choose the placement that matches your host and scope; unsupported combinations fail
rather than silently changing scope:

| Target | User scope | Project scope |
|---|---|---|
| `codex` | supported | not supported |
| `claude` | supported | supported |
| `antigravity` | not supported | supported |
| `zcode` | supported | not supported |

For a project placement, add `--project-root /absolute/project`. `--target auto` is
truthful only when exactly one supported destination already exists; zero or multiple
matches report the candidates without writing. Installation copies and verifies the one
canonical synchronized Agent Skill payload. Placement mechanics do not prove live host
compatibility.

The [native package guide](install.md) describes the pinned release route; its
unpublished candidate warning remains authoritative. Source/wheel installation is an
[advanced route](install-manual.md#source-and-wheel-installation). With a source checkout,
resolve Python 3.11+ as `PYTHON`, install the matching hash-locked base requirements, then
install the package before running `skill-install`.

## 2. Run `comic-sol doctor` now

Do this before writing any story. Each invocation runs one surface at a time;
[`docs/surfaces.md`](surfaces.md) keeps their launcher and output-root rules separate.
`doctor` is the authoritative readiness check, and it tells you exactly what is missing
instead of failing later mid-comic.

For an installed package or native distribution:

```bash
comic-sol doctor --output-root "$HOME/Comic Sol"
```

For the advanced source-checkout route, pass an **explicit output root** so the first
run never depends on a default you have not chosen:

```bash
"$PYTHON" scripts/comic_sol.py doctor --output-root "$HOME/Comic Sol"
```

macOS:

```bash
"$PYTHON" scripts/comic_sol.py doctor --output-root "$HOME/Documents/Comic Sol"
```

Windows PowerShell:

```powershell
& $PYTHON -3 scripts\comic_sol.py doctor --output-root "$env:USERPROFILE\Documents\Comic Sol"
```

These direct commands intentionally carry no agent capability observation. Human output
says `INFO image capability: inspect in agent session`; JSON reports
`details.readiness` as `unknown`. `doctor` prints a readiness summary plus one prefixed
line per check. Read it by prefix, not by position:

- `READY`, with no `FAIL` line — the deterministic engine, fonts, templates,
  references, and your output directory are all usable. Image readiness is stated
  separately by the `image-capability` line. Go to step 3.
- Any `FAIL` line — fix it using the table in step 5, then run `doctor` again.
- A `WARN` or `INFO` line about MCP or image capability — expected on a fresh
  direct install check. Both are optional for deterministic project editing; step 3
  explains how an agent session supplies an image-capability observation.

The exit code is the definitive signal: `doctor` exits `0` when ready and `1`
when it is not, so you can rely on it in a script.

## 3. Let the agent check image capability

**Comic Sol does not generate images itself, and it ships no provider
credentials.** It plans the story, writes the prompts, letters the balloons,
composes the pages, and exports the PDF — all deterministically and offline. The
actual drawing is done by an image-generation tool that your *agent session*
exposes to it.

When the Comic Sol Skill runs, the agent automatically inspects the metadata for tools
exposed in that session. It does not invoke a tool just to test it, inspect provider
configuration or credentials, install anything, or enable a third-party provider. It
chooses one best usable tool that can create an image from text and return or write a
local raster, then calls `doctor` with `--image-capability-status available`, the fixed
`--image-capability-name agent-image-generation`, and only the `--supports-reference-images` and
`--supports-dimensions` flags declared by the tool. If an inspectable inventory has no
usable tool, it passes status `unavailable`; if inspection is unavailable or fails, it
passes no capability flags rather than guessing.

Sessions differ even on the same agent host, so Comic Sol does not assume that
Codex, Claude, Antigravity, ZCode, or any other platform exposes image generation.
Those four named hosts remain experimental until **both** a linked real host
execution smoke record **and** durable, inspectable links to the required sanitized
output artifacts exist; a linked record without durable output evidence keeps the
host Experimental. Installer tests prove placement mechanics, not live compatibility. Host support and
image-generator support are separate claims. The resulting `image-capability` check is:

| Result | Meaning |
|---|---|
| `PASS` | One usable capability was found and declares both reference-image and dimension support. |
| `WARN` — partial | A usable capability was found, but one or both optional features are unsupported or undiscoverable. Comic Sol uses degraded mode. |
| `WARN` — unavailable | The exposed inventory was inspected and no usable capability was found. Deterministic editing remains ready. |
| `WARN`/`INFO` — unknown | Tool metadata could not be inspected or detection failed, so the agent supplied no observation rather than guessing. |

An editing-only image tool is not enough — it must be able to create the first
image from text alone. Route selection is always: a compatible declared native image
tool; a compatible declared external adapter/API tool; portable handoff; then an
actionable `BLOCKED` state preserving editable intermediates. Never infer capability
from provider, model, host, or tool names. If no usable capability is available when
panels are needed, Comic Sol keeps the plan and prompts, marks the project `BLOCKED`,
and tells you how to resume; it never invents a placeholder. See
[`references/image-provider-setup.md`](../references/image-provider-setup.md).

## 4. Make your first comic

Open a fresh session in the host where you installed the Skill so the newly installed
Skill is loaded, then say exactly this:

> Make a 2-page manga about a courier delivering sunlight to an underground city.

That is the whole invocation. There are no flags, config files, or setup
questions to answer up front. Comic Sol applies documented defaults and asks only
about things it genuinely cannot infer.

### Where your comic appears

Comic Sol creates one project directory per comic inside your output root. When
you do not pass `--output-root`, the default depends on the platform — and on
the surface you are running (the MCP server requires an explicit root; the OCI
image writes under `/data` inside the container; see
[`docs/surfaces.md`](surfaces.md)). For the Skill, source, installed CLI, and
native-archive surfaces the platform defaults are:

| Platform | Default output root |
|---|---|
| Linux, WSL | `$HOME/Comic Sol` |
| macOS | `$HOME/Documents/Comic Sol` |
| Windows | `%USERPROFILE%\Documents\Comic Sol` |

Comic Sol prints the resolved project path when it starts. Inside that project,
these are the three files worth opening first:

```text
exports/<project-id>.pdf   your finished comic
pages/page-001.png         the composed pages
qa/report.md               what visual QA checked and accepted
```

Everything else in the project — the story plan, character bible, storyboard,
prompts, and individual panels — stays on disk and is editable, so you can change
one panel and resume instead of regenerating the whole comic.

If the run stops early, the project is intentionally resumable. Fix what the
message reported, then ask the agent to **resume that Comic Sol project**.

## 5. If `doctor` reported a failure

| Failed check | What it means | How to fix it |
|---|---|---|
| `runtime` | Your Python is older than 3.11. | Install Python 3.11+, then redo step 1 with that launcher. See [source and wheel installation](install-manual.md#source-and-wheel-installation). |
| `pillow` | The one pinned dependency is missing or the wrong version. | Installed-package users should reinstall the package. Source users should repeat the matching command in [source and wheel installation](install-manual.md#source-and-wheel-installation). |
| `fonts` | A bundled font face is missing or unreadable. | Your copy is incomplete. Re-clone or reinstall so `assets/fonts/` is intact; see [`assets/README.md`](../assets/README.md). |
| `templates` | A project template is missing, empty, or malformed JSON. | Reinstall to restore `templates/`; see [`docs/install.md` → Upgrade and rollback](install.md#upgrade-and-rollback). |
| `references` | The `references/` guidance files are missing. | Reinstall as in step 1 and keep `references/` beside `SKILL.md`. |
| `output-root` | The output directory cannot be created or written to. | Pick a writable location: `doctor --output-root "$HOME/Comic Sol"`. Do not use a path that already exists as a file. |

Warnings are not failures:

| Warned check | Why it is fine |
|---|---|
| `mcp` | The MCP server is optional. Skip it for your first comic; see the [MCP server contract](surfaces.md#mcp-server) later. |
| `image-capability` | A partial, unavailable, or unknown image capability does not break deterministic editing. Step 3 explains why generation may still pause at `BLOCKED`. |

For machine-readable diagnostics, add `--json` and read `data.ready` plus
`data.checks[]`, where each entry carries a stable `id`, `status`, `message`, and
`remediation`.

## Next steps

Once your first comic exists:

- [`docs/surfaces.md`](surfaces.md) — how the Skill, plugin, source, installed
  CLI, native archive, MCP, and OCI surfaces differ, and the default output
  root each one uses.
- [`docs/support-matrix.md`](support-matrix.md) — the published platform ×
  install-mode × architecture × runtime support matrix.
- [`docs/install.md`](install.md) — native portable archives, checksum
  verification, upgrade, rollback, uninstall, and containers.
- [`references/image-provider-setup.md`](../references/image-provider-setup.md) —
  per-platform image provider setup for non-Codex agents.
- [`references/capability-detection.md`](../references/capability-detection.md) —
  the exact capability check and `BLOCKED`-project recovery procedure.
- [`docs/surfaces.md` → MCP server](surfaces.md#mcp-server) — expose the
  deterministic lifecycle as MCP tools.
- [`docs/dogfood.md`](dogfood.md) — the optional manual creator-report route,
  local privacy checks, and separate report/showcase consent boundaries.
- [`docs/structured-errors.md`](structured-errors.md) — the stable error code
  taxonomy shared by the CLI and MCP surfaces.
