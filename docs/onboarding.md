# First run: install to first comic

This is the single supported happy path for a brand-new user. Follow it top to
bottom and stop at the first step that fails.

This page covers **one surface**: the Codex Skill checkout with the development
script. The other surfaces — the Codex Plugin bundle, the installed CLI wheel,
native portable archives, the MCP server, and the OCI image — start differently
and some use different output roots. They are deliberately **not** on this page;
[`docs/surfaces.md`](surfaces.md) separates them one per section, and links at
the bottom cover them once your first comic exists.

## 1. Install (shortest supported path)

Comic Sol is a Codex Skill plus a portable Python CLI. The shortest path is to
clone this repository straight into your Codex skills directory and install the
one pinned dependency.

You need **Python 3.11+**. Resolve one launcher, store it as `PYTHON`, and reuse
it for every command below.

### Linux and WSL

```bash
git clone https://github.com/wenn-id/comicsol.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
cd "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
PYTHON=python  # replace with your resolved Python 3.11+ launcher
"$PYTHON" -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
```

### macOS

```bash
git clone https://github.com/wenn-id/comicsol.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
cd "${CODEX_HOME:-$HOME/.codex}/skills/comic-sol"
PYTHON=python3  # replace with your resolved Python 3.11+ launcher
"$PYTHON" -m pip install --require-hashes -r requirements/locks/base-macos-x86_64.txt
```

### Windows PowerShell

```powershell
git clone https://github.com/wenn-id/comicsol.git `
  "$env:USERPROFILE\.codex\skills\comic-sol"
Set-Location "$env:USERPROFILE\.codex\skills\comic-sol"
$PYTHON = "py"  # resolve Python 3.11+ first; invoked below as: py -3
& $PYTHON -3 -m pip install --require-hashes -r requirements/locks/base-windows-x86_64.txt
```

Keep `SKILL.md`, `scripts/`, `references/`, `templates/`, and `assets/` together
in that one `comic-sol` folder. Nothing else is required to continue.

## 2. Run `comic-sol doctor` now

Do this before writing any story. `doctor` is the authoritative readiness check,
and it tells you exactly what is missing instead of failing later mid-comic.

From the folder you just cloned, pass an **explicit output root** so the first
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

`doctor` prints a readiness summary plus one prefixed line per check. Read it by
prefix, not by position:

- `READY`, with no `FAIL` line — the deterministic engine, fonts, templates,
  references, and your output directory are all usable. Go to step 3.
- Any `FAIL` line — fix it using the table in step 5, then run `doctor` again.
- A `WARN` or `INFO` line about MCP or image capability — expected on a fresh
  install. MCP is optional, and image capability is checked in your agent session
  rather than here; step 3 explains it.

The exit code is the definitive signal: `doctor` exits `0` when ready and `1`
when it is not, so you can rely on it in a script.

## 3. Understand the one requirement `doctor` cannot check

**Comic Sol does not generate images itself, and it ships no provider
credentials.** It plans the story, writes the prompts, letters the balloons,
composes the pages, and exports the PDF — all deterministically and offline. The
actual drawing is done by an image-generation tool that your *agent session*
exposes to it.

In practice:

- **Using Codex?** Image generation is built in. You are already done; continue to
  step 4.
- **Using another agent?** You must give that session an image tool that can
  create a picture from text and save it as a local file. Until then, Comic Sol
  will plan your comic, stop with a clear message, mark the project `BLOCKED`, and
  keep every file so you can resume later. It will never invent a placeholder
  image. See [`references/image-provider-setup.md`](../references/image-provider-setup.md).

An editing-only image tool is not enough — it must be able to create the first
image from text alone.

## 4. Make your first comic

Start a fresh Codex session so the newly installed skill is loaded, then say
exactly this:

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
message reported, then ask Codex to **resume that Comic Sol project**.

## 5. If `doctor` reported a failure

| Failed check | What it means | How to fix it |
|---|---|---|
| `runtime` | Your Python is older than 3.11. | Install Python 3.11+, then redo step 1 with that launcher. See [`README.md` → Install](../README.md#install). |
| `pillow` | The one pinned dependency is missing or the wrong version. | Re-run the `pip install --require-hashes` command for your platform in step 1. |
| `fonts` | A bundled font face is missing or unreadable. | Your copy is incomplete. Re-clone or reinstall so `assets/fonts/` is intact; see [`assets/README.md`](../assets/README.md). |
| `templates` | A project template is missing, empty, or malformed JSON. | Reinstall to restore `templates/`; see [`docs/install.md` → Upgrade and rollback](install.md#upgrade-and-rollback). |
| `references` | The `references/` guidance files are missing. | Reinstall as in step 1 and keep `references/` beside `SKILL.md`. |
| `output-root` | The output directory cannot be created or written to. | Pick a writable location: `doctor --output-root "$HOME/Comic Sol"`. Do not use a path that already exists as a file. |

Warnings are not failures:

| Warned check | Why it is fine |
|---|---|
| `mcp` | The MCP server is optional. Skip it for your first comic; see [`README.md` → MCP Server](../README.md#mcp-server-optional) later. |
| `image-capability` | Always a warning here by design — capability lives in the agent session. Step 3 covers it. |

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
- [`README.md` → MCP Server](../README.md#mcp-server-optional) — expose the
  deterministic lifecycle as MCP tools.
- [`docs/structured-errors.md`](structured-errors.md) — the stable error code
  taxonomy shared by the CLI and MCP surfaces.
