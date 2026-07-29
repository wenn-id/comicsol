# Comic Sol Portable Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers/subagent-driven-development (recommended) or superpowers/executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing deterministic Comic Sol source tree into an installable Python product with a stable `comic-sol` CLI, complete MCP launcher, provider-neutral generation contract, transactional client configuration, and clean-install CI evidence.

**Architecture:** Keep `scripts/comic_sol.py` and its sibling modules as the deterministic engine during this milestone. Add a focused `comic_sol_product` package that owns installed-user concerns: command routing, configuration, provider records, client adapters, and package resource lookup. The package delegates deterministic operations to the existing engine; no lifecycle, validation, retry, hashing, or path rules are duplicated.

**Tech Stack:** Python 3.11, stdlib (`argparse`, `dataclasses`, `json`, `pathlib`, `tomllib`), Pillow 12.3.0, optional MCP 1.28.1, setuptools build backend, unittest.

## Global Constraints

- Python runtime floor is 3.11.
- Base install works without MCP or provider extras.
- Pillow is pinned to 12.3.0; MCP is pinned to 1.28.1.
- No system Python bundling or native installers in this milestone; those belong to Native Distribution.
- Existing schema-v1 projects remain readable.
- Engine functions remain the only owners of deterministic state, validation, retry, hashing, and containment rules.
- Every behavior change follows RED → GREEN → REFACTOR.
- Human output is concise; `--json` output is stable JSON.
- Errors use stable categories and non-zero exit codes and never expose secrets, prompt bodies, raw provider payloads, or private absolute paths.
- MCP sampling remains disabled.
- Client configuration is backup-first, atomic, idempotent, verified, and rolled back on failure.
- Projects are preserved by setup, repair, upgrade, and uninstall operations.

---

### Task 1: Installable package and stable CLI router

**Files:**
- Create: `pyproject.toml`
- Create: `comic_sol_product/__init__.py`
- Create: `comic_sol_product/cli.py`
- Create: `comic_sol_product/config.py`
- Create: `tests/test_product_cli.py`
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md`

**Interfaces:**
- Produces: `comic_sol_product.cli:main(argv: list[str] | None = None) -> int`
- Produces: `comic_sol_product.config.default_output_root(platform: str | None = None, home: Path | None = None) -> Path`
- Produces console script: `comic-sol`
- Consumes engine interface: `scripts.comic_sol.main(argv)`, `doctor(output_root)`, and project lifecycle functions without reimplementation.

- [ ] **Step 1: Write failing CLI/package tests**

Add tests that assert:

```python
self.assertEqual("comic-sol", cli.build_parser().prog)
self.assertEqual(Path.home() / "Comic Sol", default_output_root("linux", Path.home()))
self.assertEqual(Path.home() / "Documents" / "Comic Sol", default_output_root("darwin", Path.home()))
self.assertEqual(Path.home() / "Documents" / "Comic Sol", default_output_root("win32", Path.home()))
```

Test `comic-sol --json doctor` returns one JSON object with `ok`, `command`, `data`, and `error`; an invalid source extension returns category `invalid-input`, exit code 2, and no project directory.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_product_cli -v`
Expected: import failure because `comic_sol_product` does not exist.

- [ ] **Step 3: Implement package metadata, config, and CLI router**

`pyproject.toml` must declare:

```toml
[build-system]
requires = ["setuptools==80.9.0"]
build-backend = "setuptools.build_meta"

[project]
name = "comic-sol"
version = "2.0.0.dev0"
requires-python = ">=3.11"
dependencies = ["Pillow==12.3.0"]

[project.optional-dependencies]
mcp = ["mcp==1.28.1"]

[project.scripts]
comic-sol = "comic_sol_product.cli:main"
```

The router exposes `doctor`, `init`, `status`, `validate`, `resume`, `finalize`, and `mcp`. It emits stable envelopes in JSON mode:

```python
{"ok": True, "command": command, "data": data, "error": None}
{"ok": False, "command": command, "data": None,
 "error": {"category": category, "message": safe_message}}
```

- [ ] **Step 4: Verify GREEN and legacy compatibility**

Run:

```bash
python -m unittest tests.test_product_cli -v
python -m unittest discover -s tests -v
python -m comic_sol_product.cli --json doctor
```

Expected: all pass; doctor JSON reports `ok: true`.

- [ ] **Step 5: Add wheel and clean-venv smoke gate**

CI builds a wheel, creates a temporary venv, installs `dist/*.whl`, runs `comic-sol --json doctor`, and verifies `comic-sol --help` without importing from the checkout.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml comic_sol_product tests/test_product_cli.py .github/workflows/tests.yml README.md
git commit -m "feat: add installable Comic Sol CLI"
```

### Task 2: Installed MCP launcher and lifecycle parity

**Files:**
- Create: `comic_sol_product/mcp.py`
- Modify: `scripts/mcp_server.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_product_cli.py`

**Interfaces:**
- Produces: `comic_sol_product.mcp:main(argv: list[str] | None = None) -> int`
- Produces CLI command: `comic-sol mcp --root PATH`
- Consumes the exact 17-tool FastMCP surface from `scripts.mcp_server.mcp`.

- [ ] **Step 1: Write failing installed-launcher protocol test**

Start `comic-sol mcp --root <temp>` through `StdioServerParameters`; initialize a client; assert the exact 17 tools and call `comic_doctor`, `comic_init`, `comic_status`, and the two-pass `comic_finalize` lifecycle.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_mcp_server.McpInstalledProtocolTests -v`
Expected: failure because the `mcp` CLI command is not wired.

- [ ] **Step 3: Implement launcher without duplicating tools**

The product launcher imports the existing server module, calls `_configure_root`, and runs its FastMCP instance over stdio. If the optional extra is absent, return category `missing-extra` with `pip install 'comic-sol[mcp]'` remediation.

- [ ] **Step 4: Verify GREEN**

Run base and MCP suites separately. Base must skip only MCP-specific tests; MCP environment must expose exactly 17 tools and complete the protocol fixture.

- [ ] **Step 5: Commit**

```bash
git add comic_sol_product/mcp.py scripts/mcp_server.py tests/test_mcp_server.py tests/test_product_cli.py
git commit -m "feat: launch MCP through stable CLI"
```

### Task 3: Provider-neutral generation contract

**Files:**
- Create: `comic_sol_product/providers.py`
- Create: `tests/test_providers.py`
- Modify: `README.md`

**Interfaces:**
- Produces immutable `GenerationRequest`, `GenerationResult`, and `GenerationFailure` dataclasses.
- Produces protocol `GenerationProvider.generate(request: GenerationRequest) -> GenerationResult`.
- Produces `retain_generation_result(project_dir, panel_id, kind, result) -> dict[str, int]`, delegating path validation and retry accounting to engine functions.

- [ ] **Step 1: Write failing contract tests**

Tests cover canonical sanitized serialization, optional provider/model/request ID/seed fields, dimensions, references requested/used, SHA-256, and rejection of secret-like fields or absolute attempt paths. A fake provider returns a real 512×512 PNG and is retained through engine accounting.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_providers -v`
Expected: import failure.

- [ ] **Step 3: Implement minimal dataclasses and fake-provider seam**

No HTTP SDK enters the base package. Failures expose only stable category and sanitized message. Serialization excludes `None` and never accepts arbitrary provider payload dictionaries.

- [ ] **Step 4: Verify GREEN**

Run provider tests and the full base suite.

- [ ] **Step 5: Commit**

```bash
git add comic_sol_product/providers.py tests/test_providers.py README.md
git commit -m "feat: define provider-neutral generation contract"
```

### Task 4: Transactional client detection and configuration

**Files:**
- Create: `comic_sol_product/clients.py`
- Create: `comic_sol_product/setup.py`
- Create: `tests/test_client_setup.py`
- Modify: `comic_sol_product/cli.py`
- Modify: `README.md`

**Interfaces:**
- Produces `ClientAdapter` protocol with `detect`, `load`, `mutate`, `verify`, and `remove`.
- Produces `SetupResult(client, status, config_path, backup_path, message)`.
- Produces `setup_clients(output_root, selected=None, home=None) -> list[SetupResult]`.
- Produces `repair_clients(...)` and `uninstall_clients(...)`; uninstall never deletes project output.

- [ ] **Step 1: Write failing fixture tests**

Use temporary fake homes for Codex, Hermes, Claude Desktop, Claude Code, Cursor, VS Code, and Windsurf. Assert detection, malformed-config refusal, timestamped backup, atomic write, exact `comic-sol mcp --root <path>` registration, idempotence, verification rollback, and integration-only uninstall.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_client_setup -v`
Expected: import failure.

- [ ] **Step 3: Implement JSON adapters first, then YAML/TOML adapters only where formats are verified**

Unknown or unverified formats return `unsupported`; no guessed config is written. Shared transaction code owns backup, atomic replace, verification, and rollback.

- [ ] **Step 4: Wire `setup`, `repair`, and `uninstall` commands**

All commands support `--json`, `--output-root`, and repeat execution without duplicate entries. Output distinguishes `configured`, `unchanged`, `skipped`, `unsupported`, `failed`, and `rolled-back`.

- [ ] **Step 5: Verify GREEN**

Run client tests, full suite, and a CLI smoke using a temporary HOME.

- [ ] **Step 6: Commit**

```bash
git add comic_sol_product/clients.py comic_sol_product/setup.py comic_sol_product/cli.py tests/test_client_setup.py README.md
git commit -m "feat: add transactional client setup"
```

### Task 5: Portable Product clean-install and release gates

**Files:**
- Create: `tests/test_clean_install.py`
- Modify: `.github/workflows/tests.yml`
- Modify: `README.md`
- Modify: `SKILL.md`

**Interfaces:**
- Consumes wheel, console script, MCP extra, provider contract, and client fixture setup from Tasks 1–4.
- Produces current release evidence for Portable Product acceptance criteria.

- [ ] **Step 1: Add package-content and isolated-install tests**

Build wheel and sdist, inspect archives for package modules, Skill, references, templates, and fonts, install into a fresh venv, run doctor, initialize a project, inspect status JSON, discover MCP tools with the extra, and uninstall while proving the project remains.

- [ ] **Step 2: Verify RED for missing packaged assets**

Run the clean-install test before adding package-data rules. Expected: missing template/font/Skill failure.

- [ ] **Step 3: Add explicit package-data rules and resource lookup**

Use `importlib.resources` for installed assets while preserving checkout execution. No runtime dependency on repository paths.

- [ ] **Step 4: Expand CI gates**

On Ubuntu, macOS, and Windows: base wheel smoke and MCP wheel smoke. Add provider fake tests and client fixture tests. Keep actions pinned to immutable SHAs.

- [ ] **Step 5: Final verification**

Run:

```bash
python -m unittest discover -s tests -v
python -m build
python -m venv /tmp/comic-sol-clean
/tmp/comic-sol-clean/bin/pip install 'dist/comic_sol-*.whl[mcp]'
/tmp/comic-sol-clean/bin/comic-sol --json doctor
```

Expected: zero failures, package install succeeds, doctor healthy, MCP exposes exactly 17 tools, and no repository path is required.

- [ ] **Step 6: Documentation and acceptance audit**

Update README and Skill with only verified commands. Check every Portable Product criterion in the approved v2 spec; explicitly leave Native Distribution and Comic Quality-only gates open.

- [ ] **Step 7: Commit, PR, and merge**

```bash
git add pyproject.toml comic_sol_product tests .github/workflows/tests.yml README.md SKILL.md
git commit -m "test: gate portable product releases"
git push -u origin ai/portable-product
gh pr create --base ai/post-event-development --head ai/portable-product
gh pr checks --watch
gh pr merge --squash --delete-branch
```
