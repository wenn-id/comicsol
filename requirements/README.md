# Dependency lock provenance

Every hash-locked file in `requirements/locks/` is generated from exactly one
canonical input file in this directory. The input pins the direct requirements;
`pip-compile` resolves and freezes the complete transitive closure with
`--generate-hashes`, so `pip install --require-hashes -r <lock>` verifies every
byte it installs.

| Lock file(s) | Canonical input | Used for |
| --- | --- | --- |
| `locks/base-{linux,macos,windows}-x86_64.txt` | `base.in` | Development/bootstrap environment (README, CONTRIBUTING, tests) |
| `locks/runtime-{linux,macos,windows}-x86_64.txt` | `runtime.in` | MCP runtime extra installation and the Docker runtime stage |
| `locks/release-{linux,macos,windows}-x86_64.txt` | `release.in` | Release builds, the portable runtime, SBOM generation, qualification |
| `locks/quality-linux-x86_64.txt` | `quality.in` | Ruff, mypy, and coverage quality gates |
| `locks/audit-python311.txt` | `audit.in` | `pip-audit` security gate |

## Regeneration procedure

Install `pip-tools==7.6.1` on the target platform, then compile that platform's
locks from the repository root:

```bash
# Linux (also review macOS and Windows before committing; see below)
python -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements/locks/base-linux-x86_64.txt --strip-extras requirements/base.in
python -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements/locks/runtime-linux-x86_64.txt --strip-extras requirements/runtime.in
python -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements/locks/release-linux-x86_64.txt --strip-extras requirements/release.in
```

Repeat with the `-macos-` output names on macOS and the `-windows-` output names
on Windows. `quality-linux-x86_64.txt` and `audit-python311.txt` are
platform-independent and may be regenerated on any of the three platforms:

```bash
python -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements/locks/quality-linux-x86_64.txt --strip-extras requirements/quality.in
python -m piptools compile --allow-unsafe --generate-hashes \
  --output-file=requirements/locks/audit-python311.txt --strip-extras requirements/audit.in
```

`--strip-extras` is required with pip-tools 7.6.1: without it the resolver emits
extras-qualified pins such as `pyjwt[crypto]==...` instead of the `pyjwt==...`
form every committed lock uses. The flag order above is the order pip-compile
7.6.1 itself writes into each lock header, so a regenerated header matches the
committed one.

## Reviewing regeneration diffs

Per-platform resolution differs where dependencies declare environment markers
(`pywin32`, `pefile`, `colorama`, and `pywin32-ctypes` appear only in the
Windows release lock; `colorama` and `pywin32` also appear only in the Windows
runtime lock because `mcp` and `click` gate them on `sys_platform == "win32"`;
`macholib` appears only in the macOS release lock), so a lock must be
regenerated **on its own platform**. When changing any pin, regenerate the
affected family on Linux, macOS, and Windows, then review the three diffs
together:

- `base-*` locks must be identical across the three platforms except for the
  per-lock header, which names that lock's own output file. Any other
  platform-specific diff there means an environment marker leaked into a
  supposedly portable environment and must be investigated, not committed.
- `runtime-*` locks follow the same rule, except that the Windows lock may add
  exactly the documented `colorama` and `pywin32` marker packages.
- `release-*` diffs must differ only by marker-gated packages and their hashes
  (Windows: `colorama`, `pefile`, `pywin32`, `pywin32-ctypes`; macOS:
  `macholib`).
- Version changes must be intentional: raising a direct pin in the `.in` file is
  a deliberate change; unattended transitive drift should be reviewed with the
  same care as a direct bump and must keep `pip-audit` green.
- Hash-only changes with identical versions indicate a re-publish or a resolver
  anomaly and must be explained in the pull request.

`tests/test_lock_provenance.py` runs on every platform in CI and fails when a
lock loses a direct pin declared by its canonical input, when the same family
disagrees about a pinned version or package set across platforms beyond the
documented markers, when a lock stops documenting its input and regeneration
command, or when an extras-qualified pin such as `pyjwt[crypto]==` survives in
a `--strip-extras` lock. It cannot prove that a fresh `pip-compile` run
reproduces a committed lock byte-for-byte (the resolver moves as indexes
change); the cross-platform regeneration and diff review above is the
procedure that does.
