# Contributing to Comic Sol

`wenn-id/comicsol` is the canonical public repository for Comic Sol. Development is review-first: create a focused branch, open a pull request into `main`, and keep unrelated changes separate.

## Development setup

Use Python 3.11+ and the pinned dependencies:

# Linux
```bash
PYTHON=python  # replace with resolved Python 3.11+ launcher
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
```

# macOS
```bash
PYTHON=python  # replace with resolved Python 3.11+ launcher
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/base-macos-x86_64.txt
```

# Windows PowerShell
```powershell
$PYTHON = "py"  # resolve Python 3.11+ first; use `py -3`
& $PYTHON -3 -m venv .venv
& .venv\Scripts\python.exe -m pip install --require-hashes -r requirements/locks/base-windows-x86_64.txt
```

## Required validation

Run checks through same `.venv` interpreter used for dependency installation. Before requesting merge, complete deterministic suite must pass:

```bash
# POSIX
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/comic_sol.py doctor --output-root /tmp/comic-sol-doctor
.venv/bin/python -m build --no-isolation
.venv/bin/python -m comic_sol_product.release dist/*.whl dist/*.tar.gz
```

```powershell
# Windows PowerShell
& .venv\Scripts\python.exe -m unittest discover -s tests -v
& .venv\Scripts\python.exe scripts\comic_sol.py doctor --output-root "$env:TEMP\comic-sol-doctor"
& .venv\Scripts\python.exe -m build --no-isolation
$artifacts = Get-ChildItem -Path dist\*.whl, dist\*.tar.gz |
  Select-Object -ExpandProperty FullName
& .venv\Scripts\python.exe -m comic_sol_product.release @artifacts
```

Changes to packaging, runtime freezing, MCP, release automation, or distribution must also pass clean-install and portable-runtime smoke tests. Visual output changes require an actual before/after render and visual inspection; green geometry tests alone are insufficient.

CLI lifecycle progress is a human-only `stderr` contract: use concise `WORKING`, `BLOCKED`, `FAILED`, and `COMPLETE` stage lines with known completed/remaining counts. Never write progress to `stdout`; `--json` must remain a single parseable JSON envelope with no human-only output on either stream.

Changes to the lifecycle engine, lettering, composition, or export should also be
measured against the benchmark framework so a quality regression is visible rather
than argued. `docs/benchmark.md` documents the case contract, the metrics, and how to
diff two engine revisions:

```bash
.venv/bin/python scripts/benchmark.py --all \
  --output-root build/benchmark/projects --results build/benchmark/results
```

Deterministic benchmark runs prove pipeline and geometry mechanics only; they never
substitute for the visual inspection required above.

The root `SKILL.md`, deterministic scripts, templates, fonts, and non-host-specific
references are canonical. Synchronize and verify the self-contained plugin bundle with:

```bash
.venv/bin/python scripts/sync_plugin_bundle.py
.venv/bin/python scripts/sync_plugin_bundle.py --check
```

On Windows, use `.venv\Scripts\python.exe` for the same commands. The capability
detection and image-provider setup references are intentionally host-specific; other
bundle differences fail validation.

## Pull requests

- Target `main`.
- Explain user-visible behavior and migration impact.
- Add or update deterministic tests for behavior changes.
- Preserve the exact 17-tool MCP surface unless a separately reviewed contract change explicitly updates it.
- Do not commit credentials, provider payloads, generated projects, build outputs, or private source material.
- Do not weaken validation thresholds merely to make a fixture pass.

All Linux, macOS, and Windows CI checks must pass before merge. Releases are created only from reviewed commits already merged into `main`.

## Reporting defects

Use GitHub Issues for reproducible non-security bugs and feature requests. For vulnerabilities or accidental exposure of sensitive information, follow `SECURITY.md` instead of opening a public issue.
