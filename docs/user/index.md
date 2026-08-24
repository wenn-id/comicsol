# Comic Sol user guide

Comic Sol turns a short idea, pasted story, or `.txt`/`.md` file into an editable
local comic project. With a compatible drawing tool in the same agent session,
it plans the story, keeps character notes, creates and checks artwork, adds
lettering, lays out pages, and exports an image-based PDF.

Comic Sol is **not** a hosted web app, an image generator, or an image-provider
account. It does not read or store a drawing service's password or API key. A
missing drawing tool leaves the project safely `BLOCKED` instead of fabricating
artwork; see the [current recovery limitation](resume-repair-export.md#drawing-tool-blocks)
before starting without one.

## Start with your goal

| I want to… | Read… |
| --- | --- |
| Install Comic Sol and make a first comic | [Getting started](getting-started.md) |
| Continue an interrupted comic | [Resume, repair, and export](resume-repair-export.md#resume-an-interrupted-comic) |
| Fix Comic Sol or a failed panel | [Resume, repair, and export](resume-repair-export.md#repair) |
| Finish a comic and find its PDF | [Resume, repair, and export](resume-repair-export.md#export-a-finished-comic) |
| Recover from a `CS-…` error | [Troubleshooting by error code](troubleshooting.md) |

The older [single first-run walkthrough](../onboarding.md) remains the complete
copy-and-paste path for a Codex Skill checkout.

## Choose the right surface

- **Skill or Plugin:** the end-to-end creator experience. Ask for a comic in
  ordinary language; the agent coordinates Comic Sol and the drawing tool.
- **Installed `comic-sol` CLI:** creates and inspects projects, manages client
  integration, resumes deterministic work, and finalizes ready projects. It
  does not draw artwork by itself.
- **Source/development scripts, MCP, and OCI:** advanced integration or
  contributor surfaces. Their commands and default folders differ; see the
  [surface guide](../surfaces.md).

## What a completed project contains

```text
<project>/
├── pages/page-001.png       final page images
├── exports/<project-id>.pdf image-based PDF
└── qa/report.md             checks, warnings, and repair history
```

The project also keeps editable plans, storyboards, panel images, and QA records.
Keep the whole project folder if you may want to revise or resume it.

## Important limits

- Normal projects are **1–4 pages**. Larger projects need an explicit scope
  decision.
- Drawing quality and character consistency depend partly on the drawing tool.
  Reference-image and requested-size support improve results but are optional.
- Exported PDFs contain page images. They have no selectable text, tags, alt
  text, or PDF/UA support.
- The CLI and documentation are English-only. Scripts that require complex text
  shaping or right-to-left layout are rejected rather than rendered incorrectly;
  see [typography support](../typography.md).
- Project files stay under the output folder you choose. Prompts and images sent
  to an external drawing tool follow that tool's privacy and retention policy;
  see [privacy](../../PRIVACY.md).
- Starter projects contain plans and storyboards, not finished artwork. Official
  [samples](../../samples/README.md) identify whether they demonstrate live art
  or deterministic mechanics.

## Contributor and architecture documentation

You do not need repository internals to follow this user guide. To contribute or
understand the engine, start with [CONTRIBUTING.md](../../CONTRIBUTING.md) and
[AGENTS.md](../../AGENTS.md). Files under `references/`, benchmark documents,
and release runbooks are agent, architecture, or maintainer references—not
required creator reading.
