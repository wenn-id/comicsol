# Issues 63–65 Setup and Imports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist a launchable absolute MCP command, use Claude Desktop's native macOS config path, and make every engine/test import independent of test execution order.

**Architecture:** Client setup resolves one real launcher before transaction work and stores structured command/argument values. Platform adapter selection is explicit for Windows, macOS, and Linux. Engine source and bundled engine copies use one package-relative import model, while product loaders import an explicit checkout or installed package without mutating `sys.path`.

**Tech Stack:** Python 3.11+, standard library (`importlib`, `pathlib`, `shutil`, `subprocess`, `tomllib`, `unittest`), GitHub Actions, existing MCP 2.0 smoke client.

## Global Constraints

- Resolve issues #63, #64, and #65 in one branch and pull request based on `origin/main`.
- Add no dependency.
- Do not alter project data, MCP tool behavior, the MCP tool surface, or supported client formats.
- Persist command and arguments as separate structured values; never shell-quote or join them.
- Preserve transactional backup, atomic write, verification, rollback, and POSIX mode behavior.
- Keep documented direct runtime-script execution working.
- Run on Windows, macOS Intel, and Ubuntu with Python 3.11 or newer.

## File Map

- `comic_sol_product/setup.py`: launcher resolution and platform-native adapter locations.
- `comic_sol_product/cli.py`: pass the current console/native launcher and import checkout/installed engine packages.
- `comic_sol_product/mcp.py`: import checkout/installed MCP packages without `sys.path` mutation.
- `scripts/__init__.py`: canonical checkout engine package marker.
- `scripts/*.py`: package-relative engine imports and direct-entrypoint bootstrap where needed.
- `scripts/installed_mcp_smoke.py`: start an exact persisted command and argument list.
- `scripts/clean_install_smoke.py`: space-containing install path, generated-config parsing, minimal-PATH exact launch, and macOS Claude smoke.
- `scripts/portable_release_smoke.py`: call the generalized installed MCP smoke interface.
- `scripts/sync_plugin_bundle.py`: include the package marker in the synchronized plugin engine.
- `skills/comic-sol/scripts/`: generated synchronized engine package; never edit these files by hand.
- `tests/test_client_setup.py`: launcher, repair, and platform adapter regressions.
- `tests/test_product_cli.py`: launcher forwarding and package-loader regressions.
- `tests/test_mcp_server.py`: package MCP loader and protocol regressions.
- `tests/test_*.py`: consistent `scripts.*` imports without shared `sys.path` state.
- `tests/test_isolated_discovery.py`: fresh-process discovery gate for every test module.
- `.github/workflows/tests.yml`: run isolated discovery before full discovery.

---

### Task 1: Resolve and Persist the Current MCP Launcher

**Files:**
- Modify: `comic_sol_product/setup.py`
- Modify: `comic_sol_product/cli.py`
- Modify: `tests/test_client_setup.py`
- Modify: `tests/test_product_cli.py`

**Interfaces:**
- Produces: `_resolve_executable(executable: str | os.PathLike[str] | None) -> str` in `comic_sol_product.setup`.
- Produces: `setup_clients(..., executable: str | os.PathLike[str] | None = None)` and the same forwarded interface through `repair_clients`.
- Consumes: existing `mcp_entry(executable: str, output_root: Path) -> dict[str, Any]`.

- [ ] **Step 1: Add launcher and repair regressions**

In `ClientSetupTests.setUp`, create a real launcher whose parent contains spaces:

```python
self.launcher = self.home / "bin with spaces" / ("comic-sol.exe" if os.name == "nt" else "comic-sol")
self.launcher.parent.mkdir()
self.launcher.write_bytes(b"launcher")
if os.name != "nt":
    self.launcher.chmod(0o755)
```

Import `repair_clients` and `mock`, then add tests with these assertions:

```python
def test_setup_resolves_bare_launcher_and_repair_is_idempotent(self):
    config = self.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"comic-sol": {
            "command": "comic-sol",
            "args": ["mcp", "--root", str(self.output.resolve())],
        }}}),
        encoding="utf-8",
    )
    adapter = JsonClientAdapter("cursor", config, "mcpServers")

    with mock.patch("comic_sol_product.setup.shutil.which", return_value=str(self.launcher)):
        repaired = repair_clients(self.output, adapters=[adapter], executable="comic-sol")[0]
        repaired_bytes = config.read_bytes()
        repeated = repair_clients(self.output, adapters=[adapter], executable="comic-sol")[0]

    entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["comic-sol"]
    self.assertEqual("configured", repaired.status)
    self.assertEqual("unchanged", repeated.status)
    self.assertEqual(str(self.launcher.resolve()), entry["command"])
    self.assertEqual(["mcp", "--root", str(self.output.resolve())], entry["args"])
    self.assertEqual(repaired_bytes, config.read_bytes())
```

```python
def test_unresolvable_launcher_fails_before_config_mutation(self):
    config = self.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = b"{}\n"
    config.write_bytes(original)
    adapter = JsonClientAdapter("cursor", config, "mcpServers")

    with mock.patch("comic_sol_product.setup.shutil.which", return_value=None):
        with self.assertRaises(FileNotFoundError):
            setup_clients(self.output, adapters=[adapter], executable="missing-comic-sol")

    self.assertEqual(original, config.read_bytes())
    self.assertEqual([], list(config.parent.glob("*.bak-*")))
```

Update existing setup tests to pass `executable=self.launcher` whenever the persisted command is not the subject of that test. Replace the old nonexistent `/opt/...` fixture with `self.launcher`.

- [ ] **Step 2: Add a CLI forwarding regression**

In `tests/test_product_cli.py`, patch the imported setup operation and `sys.argv`:

```python
def test_setup_passes_the_current_console_launcher(self):
    arguments = cli.build_parser().parse_args([
        "setup", "--output-root", "/tmp/projects", "--client", "codex",
    ])
    with (
        mock.patch.object(cli.sys, "argv", ["/opt/Comic Sol/bin/comic-sol"]),
        mock.patch("comic_sol_product.setup.setup_clients", return_value=[]) as setup,
    ):
        self.assertEqual([], cli._run(arguments))
    setup.assert_called_once_with(
        arguments.output_root,
        selected=["codex"],
        executable="/opt/Comic Sol/bin/comic-sol",
    )
```

Add the equivalent frozen-launcher assertion with `mock.patch.object(cli.sys, "frozen", True, create=True)` and `mock.patch.object(cli.sys, "executable", launcher)`.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_client_setup tests.test_product_cli -v
```

Expected: launcher tests fail because `setup_clients` still persists its input unchanged and CLI does not pass `executable`.

- [ ] **Step 4: Implement the launcher resolver**

Add `shutil.which`-based resolution to `comic_sol_product/setup.py`:

```python
def _resolve_executable(executable: str | os.PathLike[str] | None) -> str:
    launcher = os.fspath(
        executable
        if executable is not None
        else (sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    )
    located = launcher if Path(launcher).is_absolute() else shutil.which(launcher)
    if located is None:
        raise FileNotFoundError("Comic Sol executable could not be resolved")
    resolved = Path(located).expanduser().resolve(strict=True)
    if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
        raise FileNotFoundError("Comic Sol executable is not runnable")
    return str(resolved)
```

Change the setup signature and entry construction:

```python
def setup_clients(
    output_root: Path,
    selected: Iterable[str] | None = None,
    home: Path | None = None,
    *,
    adapters: Iterable[ClientAdapter] | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> list[SetupResult]:
    ...
    entry = mcp_entry(_resolve_executable(executable), output_root)
```

Resolve the entry before the adapter loop so failure cannot create a backup or mutate a config.

- [ ] **Step 5: Forward the CLI launcher only for setup and repair**

In `comic_sol_product/cli.py`, keep uninstall's existing signature and build kwargs explicitly:

```python
operation_arguments: dict[str, Any] = {"selected": arguments.clients}
if arguments.command != "uninstall":
    operation_arguments["executable"] = (
        sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    )
return [
    asdict(result)
    for result in operation(arguments.output_root, **operation_arguments)
]
```

- [ ] **Step 6: Run focused setup and CLI suites**

Run:

```powershell
python -m unittest tests.test_client_setup tests.test_product_cli -v
```

Expected: all tests pass, including launcher paths containing spaces and idempotent repair.

- [ ] **Step 7: Commit Task 1**

```powershell
git add comic_sol_product/setup.py comic_sol_product/cli.py tests/test_client_setup.py tests/test_product_cli.py
git commit -m "fix: persist absolute MCP launcher"
```

---

### Task 2: Select Claude Desktop's Native Platform Path

**Files:**
- Modify: `comic_sol_product/setup.py`
- Modify: `tests/test_client_setup.py`

**Interfaces:**
- Produces: `default_adapters(home: Path | None = None) -> list[ClientAdapter]` with explicit `win32`, `darwin`, and other-POSIX behavior.
- Consumes: the absolute launcher setup contract from Task 1.

- [ ] **Step 1: Write platform-specific adapter path tests**

Import `comic_sol_product.setup` as `client_setup`. Add a helper that maps adapter names to paths, then assert all three platform branches:

```python
def adapter_paths(self, platform: str) -> dict[str, Path]:
    with mock.patch.object(client_setup.sys, "platform", platform):
        return {
            adapter.name: adapter.config_path
            for adapter in client_setup.default_adapters(self.home)
        }

def test_default_adapter_paths_are_platform_native(self):
    with mock.patch.dict(os.environ, {"APPDATA": str(self.home / "Roaming")}):
        windows = self.adapter_paths("win32")
    macos = self.adapter_paths("darwin")
    linux = self.adapter_paths("linux")

    self.assertEqual(
        self.home / "Roaming/Claude/claude_desktop_config.json",
        windows["claude-desktop"],
    )
    self.assertEqual(
        self.home / "Library/Application Support/Claude/claude_desktop_config.json",
        macos["claude-desktop"],
    )
    self.assertEqual(
        self.home / ".config/Claude/claude_desktop_config.json",
        linux["claude-desktop"],
    )
    shared = {
        "codex": self.home / ".codex/config.toml",
        "cursor": self.home / ".cursor/mcp.json",
        "windsurf": self.home / ".codeium/windsurf/mcp_config.json",
    }
    for paths in (windows, macos, linux):
        for name, expected in shared.items():
            self.assertEqual(expected, paths[name])
```

- [ ] **Step 2: Run the adapter test and verify RED**

Run:

```powershell
python -m unittest tests.test_client_setup.ClientSetupTests.test_default_adapter_paths_are_platform_native -v
```

Expected: FAIL because Darwin currently uses `.config/Claude`.

- [ ] **Step 3: Add the explicit Darwin branch**

Refactor only the Claude path decision in `default_adapters`:

```python
if sys.platform == "win32":
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    claude = roaming / "Claude" / "claude_desktop_config.json"
elif sys.platform == "darwin":
    claude = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
else:
    claude = home / ".config" / "Claude" / "claude_desktop_config.json"

adapters.extend(
    [
        JsonClientAdapter("claude-desktop", claude),
        JsonClientAdapter("cursor", home / ".cursor" / "mcp.json"),
        JsonClientAdapter("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json"),
    ]
)
```

- [ ] **Step 4: Run the full client setup suite**

Run:

```powershell
python -m unittest tests.test_client_setup -v
```

Expected: all client setup tests pass on the current host while explicitly exercising all three platform values.

- [ ] **Step 5: Commit Task 2**

```powershell
git add comic_sol_product/setup.py tests/test_client_setup.py
git commit -m "fix: use native macOS Claude config path"
```

---

### Task 3: Launch the Exact Generated Entry in Clean-Install Smoke

**Files:**
- Modify: `scripts/installed_mcp_smoke.py`
- Modify: `scripts/clean_install_smoke.py`
- Modify: `scripts/portable_release_smoke.py`
- Modify: `tests/test_clean_install.py`

**Interfaces:**
- Produces: `smoke(command: str, arguments: list[str]) -> None` in `scripts/installed_mcp_smoke.py`.
- Produces: CLI flags `--command <absolute-path>` and `--args-json <JSON-array>` for the installed MCP smoke helper.
- Consumes: absolute persisted entries from Task 1 and the Darwin adapter path from Task 2.

- [ ] **Step 1: Add clean-install source contracts**

Extend `tests/test_clean_install.py` with a contract that prevents the smoke from reverting to an explicit executable unrelated to generated config:

```python
def test_clean_install_launches_the_persisted_entry_with_minimal_path(self):
    source = (Path(__file__).resolve().parents[1] / "scripts/clean_install_smoke.py").read_text(
        encoding="utf-8"
    )
    self.assertIn("tomllib.loads", source)
    self.assertIn('environment_root = root / "venv with spaces"', source)
    self.assertIn('"--command", entry["command"]', source)
    self.assertIn('"--args-json", json.dumps(entry["args"])', source)
    self.assertIn('minimal_env["PATH"]', source)
```

Add a macOS contract assertion for `Library/Application Support/Claude/claude_desktop_config.json` and selection of `claude-desktop` in the smoke source.

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```powershell
python -m unittest tests.test_clean_install -v
```

Expected: FAIL because the smoke uses `venv`, does not parse TOML, and passes an explicit executable to the helper.

- [ ] **Step 3: Generalize the installed MCP smoke helper**

Import `json` and change the helper to consume exact command/args:

```python
async def smoke(command: str, arguments: list[str]) -> None:
    server = StdioServerParameters(command=command, args=arguments)
    ...

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--args-json", required=True)
    parsed = parser.parse_args()
    arguments = json.loads(parsed.args_json)
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ValueError("MCP arguments must be a JSON string array")
    asyncio.run(smoke(parsed.command, arguments))
    return 0
```

Keep the existing tool-surface and doctor protocol assertions unchanged.

- [ ] **Step 4: Parse and validate the generated Codex entry**

In `scripts/clean_install_smoke.py`, import `tomllib`, change the venv path, and read the generated entry:

```python
environment_root = root / "venv with spaces"
...
codex_record = tomllib.loads(codex.read_text(encoding="utf-8"))
entry = codex_record["mcp_servers"]["comic-sol"]
if not Path(entry["command"]).is_absolute():
    raise RuntimeError("installed setup persisted a non-absolute MCP command")
if entry["args"] != ["mcp", "--root", str(output_root.resolve())]:
    raise RuntimeError("installed setup persisted unexpected MCP arguments")
```

Create a platform-minimal child environment that keeps home/temp/system values but excludes the venv and installation directories:

```python
minimal_env = {
    key: env[key]
    for key in ("HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG")
    if key in env
}
minimal_env["PATH"] = (
    str(Path(env["SYSTEMROOT"]) / "System32")
    if os.name == "nt"
    else "/usr/bin:/bin"
)
```

Call the helper only in MCP jobs using the persisted values unchanged:

```python
run(
    [
        str(python),
        str(repository / "scripts" / "installed_mcp_smoke.py"),
        "--command", entry["command"],
        "--args-json", json.dumps(entry["args"]),
    ],
    cwd=root,
    env=minimal_env,
)
```

- [ ] **Step 5: Add the native macOS Claude smoke**

Before setup, create an existing config only on Darwin:

```python
clients = ["codex"]
claude = home / "Library/Application Support/Claude/claude_desktop_config.json"
if sys.platform == "darwin":
    claude.parent.mkdir(parents=True)
    claude.write_text('{"mcpServers":{"other":{"command":"other"}}}\n', encoding="utf-8")
    clients.append("claude-desktop")
```

Build CLI arguments with one `--client` pair per item. Assert both selected results are `configured`, parse Claude JSON on Darwin, preserve `other`, and assert its `comic-sol` entry equals the Codex entry.

- [ ] **Step 6: Update portable release smoke for the generalized helper**

In `scripts/portable_release_smoke.py`, import `json` and replace `--executable/--output-root` with:

```python
[
    sys.executable,
    str(helper),
    "--command", str(executable),
    "--args-json", json.dumps(["mcp", "--root", str((temporary / "mcp").resolve())]),
]
```

- [ ] **Step 7: Run contract and syntax verification**

Run:

```powershell
python -m unittest tests.test_clean_install tests.test_client_setup -v
python -m compileall -q scripts comic_sol_product tests
```

Expected: both suites and compilation pass.

- [ ] **Step 8: Commit Task 3**

```powershell
git add scripts/installed_mcp_smoke.py scripts/clean_install_smoke.py scripts/portable_release_smoke.py tests/test_clean_install.py
git commit -m "test: launch persisted MCP entry in clean install"
```

---

### Task 4: Convert the Engine and Tests to One Package Import Model

**Files:**
- Create: `scripts/__init__.py`
- Modify: `comic_sol_product/cli.py`
- Modify: `comic_sol_product/mcp.py`
- Modify: `scripts/comic_sol.py`
- Modify: `scripts/compose_pages.py`
- Modify: `scripts/export_pdf.py`
- Modify: `scripts/letter_panels.py`
- Modify: `scripts/mcp_server.py`
- Modify: `scripts/normalize_panels.py`
- Modify: `scripts/page_quality.py`
- Modify: `scripts/pdf_quality.py`
- Modify: `scripts/quality_sample.py`
- Modify: `scripts/render_report.py`
- Modify: `scripts/typography.py`
- Modify: `scripts/validate_project.py`
- Modify: `scripts/sync_plugin_bundle.py`
- Create: `tests/test_isolated_discovery.py`
- Modify: all tests currently inserting `ROOT / "scripts"` into `sys.path` or importing engine modules without the `scripts.` prefix
- Generated: `skills/comic-sol/scripts/`

**Interfaces:**
- Produces: checkout modules `scripts.<module>` and installed modules `comic_sol_product.engine.<module>`.
- Produces: `_engine_package() -> str` and `_load_engine_module(name: str) -> Any` in the product loader layer.
- Consumes: existing public engine functions without signature changes.

- [ ] **Step 1: Capture the import-order failure and loader requirements**

Run the issue reproduction before editing:

```powershell
python -m unittest discover -s tests -p test_layouts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'project_io'`.

Create `tests/test_isolated_discovery.py` before changing imports:

```python
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISCOVER_ONE = (
    "import sys, unittest; "
    "suite = unittest.defaultTestLoader.discover('tests', pattern=sys.argv[1]); "
    "raise SystemExit(0 if suite.countTestCases() else 2)"
)


class IsolatedDiscoveryTests(unittest.TestCase):
    def test_each_test_module_discovers_in_a_fresh_process(self):
        for test_file in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(test_file=test_file.name):
                completed = subprocess.run(
                    [sys.executable, "-c", DISCOVER_ONE, test_file.name],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(
                    0,
                    completed.returncode,
                    f"{test_file.name}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
```

Run:

```powershell
python -m unittest tests.test_isolated_discovery -v
```

Expected: FAIL on the `test_layouts.py` subtest with
`ModuleNotFoundError: No module named 'project_io'`.

Add product loader assertions:

```python
def test_checkout_engine_loader_uses_a_package_without_sys_path_mutation(self):
    before = list(sys.path)
    engine = cli._load_engine()
    self.assertEqual("scripts.comic_sol", engine.__name__)
    self.assertEqual(before, sys.path)
```

Add the corresponding MCP loader assertion in `tests/test_mcp_server.py` for `scripts.mcp_server` when the MCP extra is available.

- [ ] **Step 2: Add the checkout package marker and direct-entrypoint bootstrap**

Create `scripts/__init__.py`:

```python
"""Canonical deterministic Comic Sol engine package."""
```

For checkout runtime files that have relative imports and a direct `__main__` entry (`comic_sol.py`, `compose_pages.py`, `export_pdf.py`, `letter_panels.py`, `mcp_server.py`, `quality_sample.py`, `render_report.py`, and `validate_project.py`), place this after standard-library `sys` and `Path` imports but before sibling imports:

```python
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"
```

This bootstrap is local to direct-file execution. Imported modules and tests must not execute it.

- [ ] **Step 3: Convert every engine sibling import to relative form**

Apply this exhaustive mapping, including imports inside functions:

| Module | Relative sibling imports |
| --- | --- |
| `comic_sol.py` | `.project_io`, `.raster_limits`, `.validate_project`, `.letter_panels`, `.compose_pages`, `.page_quality`, `.export_pdf`, `.render_report` |
| `compose_pages.py` | `.comic_sol`, `.layouts`, `.project_io` |
| `export_pdf.py` | `.comic_sol`, `.pdf_quality`, `.project_io`, `.validate_project` |
| `letter_panels.py` | `.comic_sol`, `.project_io`, `.raster_limits`, `.typography`, `.font_cmap` |
| `mcp_server.py` | `.comic_sol`, `.validate_project`, `.letter_panels`, `.compose_pages`, `.export_pdf`, `.render_report` |
| `normalize_panels.py` | `.project_io`, `.raster_limits` |
| `page_quality.py` | `.comic_sol`, `.layouts`, `.project_io`, `.quality_records` |
| `pdf_quality.py` | `.raster_limits` |
| `quality_sample.py` | `.project_io`, `.comic_sol` |
| `render_report.py` | `.comic_sol`, `.project_io`, `.quality_records` |
| `typography.py` | `.comic_sol`, `.font_cmap`, `.project_io` |
| `validate_project.py` | `.project_io`, `.raster_limits`, `.page_quality`, `.quality_records`, `.typography`, `.comic_sol` |

For example:

```python
from .project_io import ProjectTransaction, contained_project_path
from .raster_limits import MAX_DECODED_PIXELS
```

Do not add `try/except ImportError` fallbacks; the direct bootstrap and package loaders are the only compatibility paths.

- [ ] **Step 4: Replace product `sys.path` loaders with explicit packages**

In `comic_sol_product/cli.py`, import `importlib` and select a package:

```python
def _engine_package() -> str:
    package_root = Path(__file__).resolve().parent
    if (package_root / "engine" / "comic_sol.py").is_file():
        return "comic_sol_product.engine"
    if (package_root.parent / "scripts" / "comic_sol.py").is_file():
        return "scripts"
    raise RuntimeError("Comic Sol engine files are missing; reinstall the package")

def _load_engine_module(name: str) -> Any:
    return importlib.import_module(f"{_engine_package()}.{name}")

def _load_engine() -> Any:
    return _load_engine_module("comic_sol")
```

Change validation loading to:

```python
validation = _load_engine_module("validate_project")
try:
    issues = validation.validate_project(arguments.project_dir, arguments.stage)
except validation.ProjectValidationError as error:
    issues = error.issues
```

In `comic_sol_product/mcp.py`, import the shared loader with
`from .cli import _load_engine_module` and call:

```python
return _load_engine_module("mcp_server")
```

Remove `sys.path.insert` from both product modules.

- [ ] **Step 5: Migrate all test imports atomically**

Remove every test-level `sys.path` insertion, including the `ROOT / "tests"`
insertion in `test_finalization.py`. Import shared fixtures as `tests.support`.
Change imports and lazy imports from top-level engine names to package names,
for example:

```python
from scripts.compose_pages import compose_project
from scripts.export_pdf import guarded_export
from scripts.letter_panels import letter_project
from scripts.validate_project import validate_project
```

Apply this to:

```text
tests/test_compose.py
tests/test_composition.py
tests/test_concurrency.py
tests/test_export_pdf.py
tests/test_finalization.py
tests/test_lettering.py
tests/test_manifest.py
tests/test_mcp_server.py
tests/test_normalization.py
tests/test_page_quality.py
tests/test_pdf_quality.py
tests/test_plugin_bundle.py
tests/test_quality_matrix.py
tests/test_report.py
tests/test_resume.py
tests/test_typography.py
tests/test_validation.py
```

Keep existing `scripts.*` imports in `test_layouts.py` and `test_project_io.py`. In subprocess source strings in `test_concurrency.py`, add the repository root—not `ROOT/scripts`—to `sys.path` and import `scripts.project_io`.

- [ ] **Step 6: Synchronize the plugin engine package**

Add `"__init__.py"` to `BUNDLED_SCRIPTS`, then run:

```powershell
python scripts/sync_plugin_bundle.py
python scripts/sync_plugin_bundle.py --check
```

Expected: `skills/comic-sol/scripts/__init__.py` exists and every bundled runtime module exactly matches its canonical source.

- [ ] **Step 7: Verify direct entrypoints and focused package suites**

Run:

```powershell
python scripts/comic_sol.py doctor --output-root "$env:TEMP/comic-sol-package-doctor"
python -m unittest discover -s tests -p test_layouts.py -q
python -m unittest tests.test_product_cli tests.test_mcp_server tests.test_providers -v
python -m compileall -q scripts comic_sol_product tests skills/comic-sol/scripts
```

Expected: direct doctor succeeds; isolated layouts discovery passes; loader/protocol/provider tests pass; compilation succeeds.

- [ ] **Step 8: Run the full suite before committing the atomic import migration**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: full discovery passes without any test depending on an earlier module's `sys.path` mutation.

- [ ] **Step 9: Commit Task 4**

```powershell
git add comic_sol_product/cli.py comic_sol_product/mcp.py scripts tests skills/comic-sol/scripts
git commit -m "refactor: use package-relative engine imports"
```

---

### Task 5: Enforce Fresh-Process Discovery in CI

**Files:**
- Modify: `.github/workflows/tests.yml`

**Interfaces:**
- Consumes: `IsolatedDiscoveryTests.test_each_test_module_discovers_in_a_fresh_process` and the package import model from Task 4.
- Produces: a required matrix step that runs the isolation gate before full discovery.

- [ ] **Step 1: Add the explicit CI step**

Insert this immediately before `Test with unittest` in `.github/workflows/tests.yml`:

```yaml
    - name: Verify isolated test discovery
      run: python -m unittest tests.test_isolated_discovery -v
```

Keep the existing full command unchanged:

```yaml
    - name: Test with unittest
      env:
        COMIC_SOL_ENABLE_MCP_TESTS: ${{ matrix.extras == 'mcp' && '1' || '0' }}
      run: python -m unittest discover -s tests -v
```

- [ ] **Step 2: Run isolation and full discovery together**

Run:

```powershell
python -m unittest tests.test_isolated_discovery -v
python -m unittest discover -s tests -v
```

Expected: both commands pass.

- [ ] **Step 3: Commit Task 5**

```powershell
git add .github/workflows/tests.yml
git commit -m "test: enforce isolated unittest discovery"
```

---

### Task 6: Integrated Distribution and Acceptance Verification

**Files:**
- Verify only; modify files only to address a specific failing acceptance check.

**Interfaces:**
- Consumes: all interfaces produced by Tasks 1–5.
- Produces: evidence that issues #63, #64, and #65 meet their acceptance criteria on source and built artifacts.

- [ ] **Step 1: Run focused source suites**

Run:

```powershell
python -m unittest tests.test_client_setup tests.test_product_cli tests.test_clean_install tests.test_isolated_discovery -v
python -m unittest discover -s tests -p test_layouts.py -q
python -m unittest tests.test_mcp_server tests.test_providers -v
```

Expected: all commands pass.

- [ ] **Step 2: Run source consistency checks**

Run:

```powershell
git diff --check
python scripts/sync_plugin_bundle.py --check
python -m compileall -q comic_sol_product scripts tests skills/comic-sol/scripts
```

Expected: all commands exit zero and plugin bundle drift is empty.

- [ ] **Step 3: Run full discovery**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: full discovery passes.

- [ ] **Step 4: Build and validate distributions**

Run:

```powershell
python -m build --no-isolation
python -m comic_sol_product.release (Get-ChildItem dist -Filter *.whl | Select-Object -First 1).FullName (Get-ChildItem dist -Filter *.tar.gz | Select-Object -First 1).FullName
```

Expected: wheel and sdist build and validate without forbidden build-only content.

- [ ] **Step 5: Run clean-install smoke from the wheel**

Run:

```powershell
$wheel = (Get-ChildItem dist -Filter *.whl | Select-Object -First 1).FullName
python scripts/clean_install_smoke.py --wheel $wheel --mcp
```

Expected: `clean-install-ok: mcp=True`; the MCP protocol was reached through the command/args parsed from generated config under a minimal PATH.

- [ ] **Step 6: Review the complete branch diff**

Run:

```powershell
git status --short --branch
git diff origin/main...HEAD --stat
git log --oneline origin/main..HEAD
```

Expected: only the design, plan, issue implementation, tests, workflow, and generated synchronized bundle are present; no unrelated files are modified.

- [ ] **Step 7: Request independent code review and address findings**

Review against `origin/main` for correctness, platform behavior, security boundaries, import identity, test isolation, and distribution compatibility. For every accepted finding, add a focused failing regression, implement the smallest fix, rerun the affected suite, and commit with a descriptive `fix:` or `test:` message.

- [ ] **Step 8: Push and open the combined pull request**

After all verification is green:

```powershell
git push -u origin fix/issues-63-65-setup-imports
$prBody = @'
## Summary
- persist the resolved absolute Comic Sol launcher in MCP client configs
- use Claude Desktop's native macOS config path
- use package-relative engine imports and fresh-process test discovery

## Verification
- `python -m unittest tests.test_client_setup tests.test_product_cli tests.test_clean_install tests.test_isolated_discovery -v`
- `python -m unittest discover -s tests -p test_layouts.py -q`
- `python -m unittest discover -s tests -v`
- `python scripts/sync_plugin_bundle.py --check`
- wheel/sdist validation and MCP clean-install smoke

Closes #63
Closes #64
Closes #65
'@
gh pr create --repo wenn-id/comicsol --base main --head fix/issues-63-65-setup-imports --title "fix: make setup and engine imports platform reliable" --body $prBody
```

- [ ] **Step 9: Monitor required checks to completion**

Run `gh pr checks --watch --interval 10` and require success for CodeQL, Python analysis, native distribution, and all Windows/macOS/Ubuntu base/MCP jobs. If a check fails, retrieve the exact failed job log, reproduce locally where possible, add a regression, fix, recommit, push, and monitor the new run.
