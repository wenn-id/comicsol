# Contributing to Comic Sol

`wenn-id/comicsol` is the canonical public repository for Comic Sol. Development is review-first: create a focused branch, open a pull request into `main`, and keep unrelated changes separate.

## Development setup

Use Python 3.11+ and the pinned dependencies:

# Linux
```bash
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
```

# macOS
```bash
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/base-macos-x86_64.txt
```

# Windows PowerShell
```powershell
py -3 -m venv .venv
& .venv\Scripts\python.exe -m pip install --require-hashes -r requirements/locks/base-windows-x86_64.txt
```

## Required validation

Run the checks relevant to the change. Before requesting merge, the complete deterministic suite must pass:

```bash
python -m unittest discover -s tests -v
python scripts/comic_sol.py doctor --output-root /tmp/comic-sol-doctor
python -m build --no-isolation
python -m comic_sol_product.release dist/*.whl dist/*.tar.gz
```

Changes to packaging, runtime freezing, MCP, release automation, or distribution must also pass clean-install and portable-runtime smoke tests. Visual output changes require an actual before/after render and visual inspection; green geometry tests alone are insufficient.

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
