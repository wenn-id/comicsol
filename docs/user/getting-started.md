# Getting started

Comic Sol is a provider-neutral, local-first comic production pipeline. The
deterministic engine is the product; Agent Skills, CLI, and MCP are adapters. This page
starts with the creator goal and keeps native CLI and other integration routes advanced.

## 1. Install the Agent Skill

`skill-install` requires Comic Sol `v2.0.0rc6` or later. Because `v2.0.0rc6`
is not yet published, run this command only after installing an rc6-or-later package
or native distribution whose release assets are available, or from a trusted rc6
source checkout installed as a package. Place the one canonical synchronized Skill
with the WP2 interface:

```bash
comic-sol skill-install --target codex --scope user
```

Supported placements are Codex user, Claude user/project, Antigravity project, and
ZCode user. Add `--project-root /absolute/project` for project scope. `--target auto`
requires exactly one existing supported destination; it reports ambiguity without
writing. Path-placement tests prove mechanics, not live host compatibility. Codex,
Claude, Antigravity, and ZCode remain experimental until a real host smoke record is
linked. Follow the [first-run walkthrough](../onboarding.md) for exact placement and
readiness details.

### Native core CLI

The core CLI does not create artwork by itself. It validates, persists, resumes,
repairs, letters, composes, and exports around an agent-supplied compatible image
generator. Comic Sol stores no provider credentials.

Use the [verified native installer guide](../install.md). Its pinned
`v2.0.0rc6` commands become usable only when that tag appears on the GitHub
Releases page. Until then, follow the manual path documented for an available
published release; do not replace the tag with a branch download.

Source, wheel, local archive, and OCI installation are advanced paths in the
[manual installation guide](../install-manual.md). The [support matrix](../support-matrix.md)
lists available platform, architecture, and runtime combinations.

## 2. Check readiness

For the installed package or native distribution used by the primary Skill route, choose
an explicit folder and run the installed launcher:

```bash
comic-sol doctor --output-root "$HOME/Comic Sol"
```

For an advanced Skill source checkout, run the source launcher from that checkout:

```bash
"$PYTHON" scripts/comic_sol.py doctor --output-root "$HOME/Comic Sol"
```

On macOS, a conventional choice is `$HOME/Documents/Comic Sol`; on Windows use
`%USERPROFILE%\Documents\Comic Sol`. A successful core check means Comic Sol can
create and validate local projects. Artwork also requires a drawing tool in the
current agent session.

## How drawings work

A **drawing tool** (sometimes called an image provider) is the outside tool or
service that turns a text description into an image and saves it locally. Comic
Sol only needs to know what the tool can do:

- **Required:** create an image from text and save a readable PNG, JPEG, or WebP.
- **Helpful:** use reference pictures and honor a requested image size.
- **Not inspected by Comic Sol:** credentials, account details, or provider
  responses.

Detection checks the tool's declared features; it does not make a test image or
contact the service. Route selection is: compatible declared native image tool,
compatible declared external adapter/API tool, portable handoff, then an actionable
`BLOCKED` state preserving editable intermediates. Never infer capability from a
provider, model, host, or tool name. Host support and image-generator support are
separate claims. Confirm a suitable route is present **before** starting the first
comic. If an existing project is already blocked because no drawing tool was available,
 preserve the whole project folder and read the
[current recovery limitation](resume-repair-export.md#drawing-tool-blocks); do
not edit `project.json`. Advanced setup lives in the
[provider guide](../../references/image-provider-setup.md).

## 3. Make a first comic

With the Skill or Plugin active, ask in ordinary language. For example:

> Make a 2-page manga about a courier delivering sunlight to an underground
> city. Save it under my Comic Sol output folder.

Comic Sol may ask for scope or style details. It then plans, draws through the
available tool, checks the result, repairs failed panels where possible, adds
lettering, composes pages, and exports the PDF. Progress uses `WORKING`,
`BLOCKED`, `FAILED`, and `COMPLETE` so an interrupted run is not mistaken for a
finished one.

When it completes, open:

- `exports/<project-id>.pdf` for the comic;
- `pages/page-001.png` (and later pages) for individual page images;
- `qa/report.md` for checks and warnings.

The live-generated [Sunlight Courier sample](../../samples/sunlight-courier/README.md)
is the only visual-quality sample. One retained sample does not prove broad illustration
quality. Other samples and deterministic fixtures are mechanics-only evidence, never
visual-quality proof.

After completing a normal real project, creators may separately choose the
[opt-in creator dogfood program](../dogfood.md). It uses local preview and validation,
manual report submission, and purpose-specific consent; participation is never required
to create or export a comic.

## About `init` and starters

`comic-sol init --interactive` creates a project boundary and records its source;
it does **not** create finished artwork. A starter command such as:

```bash
comic-sol init --title "My first comic" --starter minimal-one-page
```

creates a prewritten storyboard and stops at `STORYBOARDED`. Use the Skill or an
agent workflow with a drawing tool to continue it. Do not treat a starter as an
art style or a completed sample.

Next: [resume, repair, and export](resume-repair-export.md).
