# Troubleshooting by error code

Find the `CS-…` code in terminal output or the JSON `error.code` field. Save the
full redacted error and run `comic-sol status PROJECT_DIR` when a project exists.
The steps below preserve project data; do not delete a project as a repair step.

## Command and project errors

### `CS-CLI-001`

The command or option placement is invalid. Run `comic-sol --help` or
`comic-sol COMMAND --help`, correct the command, and retry. `--json` is a global
installed-CLI option, for example `comic-sol --json status PROJECT_DIR`.

### `CS-PROJ-001`

Project or request data is invalid. Run `comic-sol validate PROJECT_DIR` and use
its issue list to identify the file or field. Restore a known-good file or ask
the Skill to repair the project; do not guess at schema fields. Retry validation
before resuming.

### `CS-PROJ-002`

A project or required file was not found. Confirm the exact project path and
that the whole folder was copied. If this is a new project, initialize it; if it
previously existed, restore the missing file from backup rather than creating an
empty replacement.

### `CS-PROJ-003`

Comic Sol cannot read or write the selected path. Close programs locking the
file, verify that your account owns the project/output folder and can write to
it, then retry. Do not solve this by running an untrusted project as an
administrator.

### `CS-PROJ-004`

The filesystem operation failed. Check free space, mount/network availability,
path length, and filesystem health. Keep transaction or backup files in place,
restore storage access, then retry; Comic Sol recovers interrupted atomic writes.

### `CS-PROJ-005`

An unexpected CLI failure reached the safety boundary. Retry once. If it repeats,
collect the version, command, redacted error detail, `comic-sol status` output,
and install mode, then follow [SUPPORT.md](../../SUPPORT.md). Never send story
content or credentials in a public report.

## Safety and input errors

### `CS-SEC-001`

A path escaped the trusted project/output boundary or used a symlink or similar
unsafe indirection. Move the project to a normal local directory, use a contained
relative project path, remove the unsafe indirection, and retry. Do not disable
the containment check.

### `CS-SEC-002`

An input exceeded a documented size, depth, collection, or text limit. Reduce the
reported input; source stories must be UTF-8 `.txt` or `.md` files no larger than
200 KiB. Retry with the smaller input instead of raising engine limits.

## Image, quality, font, and export errors

### `CS-IMG-001`

An image is missing, unreadable, unsupported, too small, or too large. Use a
local readable PNG, JPEG, or WebP at least 512 px in each dimension. Ask the
Skill to redraw or re-import only the failed panel, then validate the project.

### `CS-QA-001`

A deterministic or visual check failed. Run `comic-sol validate PROJECT_DIR`,
read `qa/report.md`, and repair the named panel/page while preserving accepted
work. If finalization asks for page QA, have the Skill or agent visually review
the composed pages; do not manufacture a passing QA record.

### `CS-FONT-001`

A required font is missing, unreadable, or unsupported for the text. Restore the
bundled fonts or select a supported TrueType/OpenType font, review the
[typography limits](../typography.md), and rerun lettering/finalization. Do not
replace rejected complex-script layout with misleading output.

### `CS-EXPORT-001`

The PDF could not be rendered, verified, or published. Run
`comic-sol validate PROJECT_DIR`, confirm every composed page has current visual
QA, check output-folder space and permissions, then run
`comic-sol finalize PROJECT_DIR` again. Existing pages and project files remain
available while export is blocked.

## Installation and integration errors

### `CS-INSTALL-001`

An optional component is missing. Use the installer or lock file for your exact
install mode and platform, reinstall the named extra, and rerun `doctor`. Do not
mix dependencies from another platform or surface.

### `CS-INSTALL-002`

Client integration repair failed safely. Run JSON `doctor`, preview the change,
and retry only after correcting its reported path or format problem:

```bash
comic-sol --json doctor --output-root "/path/to/output-root"
comic-sol --json repair --dry-run --output-root "/path/to/output-root"
```

A missing or unverified third-party config is not rewritten by force.

### `CS-INSTALL-003`

Comic Sol could not verify configuration rollback. Stop the affected client,
leave the failed file untouched, restore the backup path named in the error, and
run `doctor`. If backup verification still fails, use the private reporting path
in [SUPPORT.md](../../SUPPORT.md) before making more changes.

## MCP errors

### `CS-MCP-001`

An MCP tool argument is invalid. Check the tool schema, project identifier, stage,
and trusted MCP root, correct the request, and retry. Do not pass an absolute or
parent-traversal path where a project-relative value is required.

### `CS-MCP-002`

The MCP server hit an unexpected safety boundary. Retry once. If it repeats,
restart the client, run `comic-sol --json doctor`, and collect the Comic Sol
version plus redacted server diagnostics for [support](../../SUPPORT.md).
Credentials and raw provider responses must not be included.

## Still blocked?

Report the Comic Sol version, operating system, install mode, exact command,
error code, redacted detail, and JSON doctor output as described in
[SUPPORT.md](../../SUPPORT.md). Use the private security route for credentials,
private story material, unsafe paths, or suspected vulnerabilities.
