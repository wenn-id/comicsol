# Comic Sol user guide

Comic Sol is a provider-neutral, local-first comic production pipeline. The
deterministic engine is the product; Agent Skills, CLI, and MCP are adapters. It turns
a short idea, pasted story, or `.txt`/`.md` file into an editable local comic project
around any compatible AI image generator.

The core CLI does not create artwork by itself. It validates, persists, resumes, repairs,
letters, composes, and exports around an agent-supplied compatible image generator.
Comic Sol stores no provider credentials. If no compatible native tool or external
adapter is declared, portable handoff is next; otherwise the project remains safely
`BLOCKED` with editable intermediates instead of fabricated artwork. See the
[current recovery limitation](resume-repair-export.md#drawing-tool-blocks).

## Start with your goal

| I want to… | Read… |
| --- | --- |
| Install Comic Sol and make a first comic | [Getting started](getting-started.md) |
| Continue an interrupted comic | [Resume, repair, and export](resume-repair-export.md#resume-an-interrupted-comic) |
| Fix Comic Sol or a failed panel | [Resume, repair, and export](resume-repair-export.md#repair) |
| Finish a comic and find its PDF | [Resume, repair, and export](resume-repair-export.md#export-a-finished-comic) |
| Recover from a `CS-…` error | [Troubleshooting by error code](troubleshooting.md) |

Install the canonical Agent Skill from a current package or native distribution with:

```bash
comic-sol skill-install --target codex --scope user
```

The [single first-run walkthrough](../onboarding.md) lists all supported WP2 host/scope
placements and follows the creator path from install through retained outputs.

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

## Advanced integrations

CLI automation, MCP, OCI, source/wheel installation, native archives, security, and
release trust remain supported. Start with the [surface guide](../surfaces.md),
[installation guide](../install.md), and [support matrix](../support-matrix.md).

## Contributor and architecture documentation

You do not need repository internals to follow this user guide. To contribute or
understand the engine, start with [CONTRIBUTING.md](../../CONTRIBUTING.md) and
[AGENTS.md](../../AGENTS.md). Files under `references/`, benchmark documents,
and release runbooks are agent, architecture, or maintainer references—not
required creator reading.
