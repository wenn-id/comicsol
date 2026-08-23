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

Run checks through same `.venv` interpreter used for dependency installation. Before requesting merge, the complete deterministic suite and the blocking quality policy must pass. The quality gate runs on Linux with the hash-locked toolchain:

```bash
.venv/bin/python -m pip install --require-hashes -r requirements/locks/quality-linux-x86_64.txt
.venv/bin/python -m ruff check scripts comic_sol_product tests
.venv/bin/python -m ruff format --check scripts comic_sol_product tests
.venv/bin/python -m mypy
.venv/bin/python -m coverage run -m unittest discover -s tests
.venv/bin/python -m coverage json -o coverage.json
.venv/bin/python scripts/check_coverage.py coverage.json
```

Coverage measures application modules under `scripts/` and `comic_sol_product/`, not tests. The independently enforced floors are **82% line coverage** and **72% branch coverage**; lowering either floor requires an explicit, reviewed policy change. Continue with the platform validation commands:

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

The deterministic test suite runs offline and does not require image-provider
credentials.

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

## Changing dependencies

Every hash lock in `requirements/locks/` is generated from exactly one canonical input
file in `requirements/`. To change a dependency, edit the `.in` file, then regenerate
that family's locks **on each platform it ships** and review the three diffs together —
the exact commands, the expected per-platform differences, and the review checklist are
in [`requirements/README.md`](requirements/README.md). Never hand-edit a lock file, and
never commit a lock that no longer carries its input and regeneration command header.

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

All Linux, macOS, and Windows CI checks must pass before merge. Branch protection for `main` must require the stable `Quality gates` status in addition to the platform test and native-distribution statuses. Releases are created only from reviewed commits already merged into `main`; exact-candidate publication and promotion depend on the same blocking quality workflow.

## Reporting defects

Use GitHub Issues for reproducible non-security bugs and feature requests. For vulnerabilities or accidental exposure of sensitive information, follow `SECURITY.md` instead of opening a public issue.
