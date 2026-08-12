# Contributing to Comic Sol

`wenn-id/comicsol` is the canonical public repository for Comic Sol. Development is review-first: create a focused branch, open a pull request into `main`, and keep unrelated changes separate.

## Development setup

Use Python 3.11 and the pinned dependencies:

```bash
python3.11 -m venv .venv
python3.11 -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

## Required validation

Run the checks relevant to the change. Before requesting merge, the complete deterministic suite must pass:

```bash
python3.11 -m unittest discover -s tests -v
python3.11 scripts/comic_sol.py doctor --output-root /tmp/comic-sol-doctor
python3.11 -m build --no-isolation
python3.11 -m comic_sol_product.release dist/*.whl dist/*.tar.gz
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
