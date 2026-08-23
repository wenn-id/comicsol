# Comic Sol Support

Support channel:
[GitHub Issues](https://github.com/wenn-id/comicsol/issues)

Comic Sol ships as several surfaces, and support looks slightly different on
each one. Whichever you run, an actionable report names the surface, the
version, the platform, and the structured error — see
[`docs/surfaces.md`](docs/surfaces.md) for how the surfaces differ and
[`docs/support-matrix.md`](docs/support-matrix.md) for the supported platform,
architecture, and runtime combinations.

## Include in every report

- **Version.** Run `comic-sol --version` (installed CLI or native archive), or
  check `project.json` / the release tag you downloaded for source and Skill
  checkouts.
- **Install mode.** One of: Codex Skill checkout, Codex Plugin bundle, source
  checkout/development, installed CLI wheel, native portable archive, MCP
  server, or OCI image.
- **Platform and runtime.** Operating system, CPU architecture, and Python
  version (`"$PYTHON" --version`) — or "bundled runtime" for a native archive
  or the OCI image, which carry their own Python.
- **Exact command or prompt** you ran, and on which surface (CLI, MCP client,
  or agent session).
- **Error code.** Every failure carries a stable `CS-<NAMESPACE>-<NNN>` code in
  its message and JSON envelope; the complete code, category, exit-status, and
  redaction contract is [`docs/structured-errors.md`](docs/structured-errors.md).
- **Sanitized error output.** Copy the envelope or message, not a screenshot of
  a truncated terminal.
- A minimal reproduction when possible.

## Run the JSON doctor first

Before opening an issue, attach machine-readable diagnostics. `doctor` checks
runtime, Pillow, fonts, templates, references, and the output root, and reports
optional MCP/image-capability checks as warnings:

```bash
# Source or Skill checkout (POSIX) — resolve one Python 3.11+ launcher as PYTHON:
"$PYTHON" scripts/comic_sol.py --json doctor --output-root ./comic-sol-output

# Windows PowerShell:
# $PYTHON = "py"; & $PYTHON -3 scripts\comic_sol.py --json doctor --output-root .\comic-sol-output

# Installed CLI or native archive:
comic-sol --json doctor --output-root ./comic-sol-output
```

Read `data.ready` (the authoritative readiness boolean) and `data.checks[]`,
where each entry carries a stable `id`, `status` (`pass`/`warn`/`fail`),
`message`, and `remediation`. With `--json`, `stdout` is exactly one parseable
envelope; progress stays on `stderr`. `doctor` exits `0` when ready and `1`
when not.

For a `BLOCKED` project: a `BLOCKED` project is intentionally resumable.
Restore the missing capability (usually the session's image-generation tool;
see [`references/capability-detection.md`](references/capability-detection.md))
or correct the reported artifact, then resume that Comic Sol project. Retain
the printed project path and inspect `project.json` plus `qa/report.md`.

## Do not post publicly

Do not post API keys, passwords, private story text, private images, personal
contact data, or generated logs containing sensitive material in a public
issue.

## Private route for sensitive reports

Some reports cannot be made public even sanitized — for example a leaked
credential, a privacy concern that requires showing private source text or
generated images, or a vulnerability:

- **Security issues** (vulnerabilities, leaked credentials, containment
  bypasses, dependency compromise): follow
  [`SECURITY.md`](SECURITY.md) — GitHub's private vulnerability reporting for
  `wenn-id/comicsol`, or the private contact method on the owner's GitHub
  profile when that interface is unavailable. Revoke exposed credentials
  before reporting; replace secret values with `[REDACTED]`.
- **Privacy questions involving material you cannot share publicly**: use the
  same private route. Say it is a privacy report, name the surface, and
  describe the data involved without pasting the sensitive content itself.

Never open a public issue first and move it later — public issues are indexed
immediately.
