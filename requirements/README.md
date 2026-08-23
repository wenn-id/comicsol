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
python -m piptools compile --allow-unsafe --generate-hashes --strip-extras \
  --output-file=requirements/locks/base-linux-x86_64.txt requirements/base.in
python -m piptools compile --allow-unsafe --generate-hashes --strip-extras \
  --output-file=requirements/locks/runtime-linux-x86_64.txt requirements/runtime.in
python -m piptools compile --allow-unsafe --generate-hashes --strip-extras \
  --output-file=requirements/locks/release-linux-x86_64.txt requirements/release.in
```

Repeat with the `-macos-` output names on macOS and the `-windows-` output names
on Windows. `quality-linux-x86_64.txt` and `audit-python311.txt` are
platform-independent and may be regenerated on any of the three platforms:

```bash
python -m piptools compile --allow-unsafe --generate-hashes --strip-extras \
  --output-file=requirements/locks/quality-linux-x86_64.txt requirements/quality.in
python -m piptools compile --allow-unsafe --generate-hashes --strip-extras \
  --output-file=requirements/locks/audit-python311.txt requirements/audit.in
```

`--strip-extras` is required with pip-tools 7.6.1: without it the resolver emits
extras-qualified pins such as `pyjwt[crypto]==...` instead of the `pyjwt==...`
form every committed lock uses.

## Reviewing regeneration diffs

Per-platform resolution differs where dependencies declare environment markers
(for example `pywin32`, `pefile`, and `colorama` appear only in the Windows
release lock), so a lock must be regenerated **on its own platform**. When
changing any pin, regenerate the affected family on Linux, macOS, and Windows,
then review the three diffs together:

- `base-*` and `runtime-*` locks must remain byte-identical across the three
  platforms; a platform-specific diff there means an environment marker leaked
  into a supposedly portable environment and must be investigated, not committed.
- `release-*` diffs must differ only by marker-gated packages and their hashes.
- Version changes must be intentional: raising a direct pin in the `.in` file is
  a deliberate change; unattended transitive drift should be reviewed with the
  same care as a direct bump and must keep `pip-audit` green.
- Hash-only changes with identical versions indicate a re-publish or a resolver
  anomaly and must be explained in the pull request.

`tests/test_lock_provenance.py` runs on every platform in CI and fails when a
lock loses a direct pin declared by its canonical input, when the same family
disagrees about a pinned version across platforms, or when a lock stops
documenting its input and regeneration command. It cannot prove that a fresh
`pip-compile` run reproduces a committed lock byte-for-byte (the resolver moves
as indexes change); the cross-platform regeneration and diff review above is
the procedure that does.
