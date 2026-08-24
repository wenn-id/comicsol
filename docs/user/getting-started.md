# Getting started

This page separates installation from comic creation so you know which tool is
doing what. Choose one path and one output folder; do not mix commands from
another surface unless its guide tells you to.

## 1. Install

### End-to-end creation: Codex Skill checkout

Use the [first-run walkthrough](../onboarding.md). It includes platform-specific
Python 3.11+ installation commands, a readiness check with an explicit output
folder, and the first natural-language request. Start a fresh Codex session
after installing the Skill.

### Native core CLI

Use the [verified native installer guide](../install.md). Its pinned
`v2.0.0rc6` commands become usable only when that tag appears on the GitHub
Releases page. Until then, follow the manual path documented for an available
published release; do not replace the tag with a branch download.

Source, wheel, local archive, and OCI installation are advanced paths in the
[manual installation guide](../install-manual.md). The [support matrix](../support-matrix.md)
lists available platform, architecture, and runtime combinations.

## 2. Check readiness

Use the exact doctor command printed by your installer. For a Skill checkout,
run the source command from the checkout and choose an explicit folder:

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
contact the service. Confirm a suitable tool is present **before** starting the
first comic. If an existing project is already blocked because no drawing tool
was available, preserve the whole project folder and read the
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
is a useful visual example. Other samples may intentionally demonstrate only
project mechanics; read each sample's evidence tier.

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
