# Resume, repair, and export

Comic Sol preserves accepted work and editable project files across interrupted
runs. Work from the existing project folder; do not create another project or
hand-edit `project.json` to clear a failure.

## Resume an interrupted comic

### With the Skill or Plugin

Ask the agent:

> Resume this Comic Sol project: `/path/to/project`.

### Drawing-tool blocks

A project blocked with `image-capability-unavailable` is preserved, but the
current public CLI and Skill cannot yet attach a newly available drawing-tool
observation to that existing project. Running `doctor` checks the current setup
but does not update the project, so another bare `resume` remains blocked.

Do not hand-edit `project.json` or create placeholder artwork. Keep the whole
project folder and check [support](../../SUPPORT.md) for a version with a
supported capability-refresh path. This is a current product limitation, not a
reason to discard the project.

### With the installed CLI

```bash
comic-sol status "/path/to/project"
comic-sol resume "/path/to/project"
comic-sol status "/path/to/project"
```

`status` shows `complete`, `stale`, `blocked`, and `pending` stages, failed-panel
counts, warnings, and the next action. For a resolved `BLOCKED` project,
`resume` restores the last valid state, keeps reusable stages, and invalidates
only stale downstream work. For any other project it returns a read-only resume
plan; follow the reported agent action or deterministic command. Repeating a
bare `resume` is safe.

If the same block remains, use the reported `CS-…` code in
[troubleshooting](troubleshooting.md).

## Repair

“Repair” has two different meanings. Choose the one that matches the problem.

### Repair Comic Sol setup or client integration

Use this when `doctor` reports a broken MCP entry for Codex, Claude, Cursor, VS
Code, Windsurf, or another supported client. Preview first:

```bash
comic-sol --json doctor --output-root "/path/to/output-root"
comic-sol --json repair --dry-run --output-root "/path/to/output-root"
comic-sol --json repair --output-root "/path/to/output-root"
```

Add `--client CLIENT` to limit repair to one client. Setup repair changes only
the verified, product-owned `comic-sol` entry. It does not reinstall Comic Sol,
create a missing third-party config, change unrelated client settings, touch a
comic project, or delete user work. It backs up configuration and rolls back if
verification fails.

### Repair comic artwork or lettering

With the Skill or Plugin, ask:

> Repair the failed panels in `/path/to/project` and keep accepted panels
> unchanged.

The agent reads QA evidence and retries only failed or stale work when possible.
A full panel redraw may be necessary when the drawing tool cannot edit a bounded
region. Accepted panels remain untouched, and retry limits prevent an endless
loop. Review `qa/report.md` after repair. Internal repair scripts are agent and
contributor tools, not installed creator commands.

## Export a finished comic

With the Skill or Plugin, ask:

> Finish and export `/path/to/project`.

For a project whose artwork and QA evidence are already ready, the installed CLI
uses `finalize`:

```bash
comic-sol finalize "/path/to/project"
```

There is no installed `comic-sol export` command. `finalize` letters panels,
composes pages, checks current page-level visual QA, and publishes the canonical
PDF. It may pause after composing pages so the agent can visually inspect them.
If that happens, ask the Skill or agent to review the pages and continue; a bare
CLI cannot make or fabricate that visual judgment. Run `finalize` again only
after the required review exists.

Successful finalization writes:

```text
pages/page-001.png
exports/<project-id>.pdf
exports/pdf-verification.json
qa/report.md
```

A terminal project is `COMPLETE` or `COMPLETE_WITH_WARNINGS`. Read the QA report
before sharing a result with warnings. If a page changes later, its old QA
record no longer matches; review the new page and finalize again.

Direct `scripts/export_pdf.py` usage is an advanced source workflow. Its custom
`--output` copy is not equivalent to the canonical, manifest-recorded export.

Back to the [user-guide index](index.md).
