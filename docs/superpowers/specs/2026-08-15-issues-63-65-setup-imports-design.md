# Issues 63–65: Reliable Client Setup and Isolated Engine Imports

## Scope

This change resolves GitHub issues #63, #64, and #65 in one branch and pull
request. It makes persisted MCP launch commands independent of a desktop
client's `PATH`, uses Claude Desktop's native macOS configuration location,
and removes test-order coupling from engine imports.

The change adds no dependency and does not alter project data, MCP tool
behavior, or supported client formats.

## Issue #63: Absolute MCP Launcher

`comic-sol setup` and `comic-sol repair` will persist the launcher that is
actually running rather than the bare string `comic-sol`.

- The CLI passes its current console-script or frozen native launcher into the
  setup operation.
- `setup_clients` has no bare-command fallback; when the executable is omitted,
  it resolves the current launcher through the same path.
- A shared resolver accepts an absolute launcher directly and uses
  `shutil.which` for a bare or relative launcher. It canonicalizes the result
  to an absolute path and requires a runnable regular file before any client
  configuration is changed.
- A frozen distribution uses its native executable. An ordinary console
  script uses the resolved `sys.argv[0]` launcher.
- Programmatic callers may still supply an executable explicitly, but it is
  resolved and validated by the same helper.
- Repair uses the newly resolved entry, so an existing bare command is updated
  transactionally. A second repair is unchanged and creates no extra backup.
- Failure to resolve a runnable launcher is reported before configuration
  mutation; the existing backup, atomic-write, verification, and rollback
  behavior remains authoritative after mutation begins.

The persisted arguments remain `mcp --root <absolute-output-root>`. Paths are
stored as single structured command/argument values, never shell-quoted or
joined into a command string.

## Issue #64: Native macOS Claude Desktop Path

`default_adapters` will distinguish three platform families:

- Windows: `%APPDATA%/Claude/claude_desktop_config.json`.
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`.
- Other supported POSIX systems: `~/.config/Claude/claude_desktop_config.json`.

Cursor, Windsurf, and Codex locations remain unchanged. Unit tests patch the
platform explicitly and assert Windows, macOS, and Linux paths independently
so the host running the test cannot mask a branch.

The clean-install smoke creates an existing Claude Desktop configuration on
macOS and selects that adapter during setup. It then verifies that the
existing file is detected, preserved through a backup, and updated with the
same absolute MCP entry as Codex.

## Issue #65: One Engine Package Model

The canonical engine source under `scripts/` will become a Python package by
adding `scripts/__init__.py`. Engine modules will import engine siblings only
with relative imports. Tests will import engine modules through `scripts.*`
and will no longer depend on another test module adding `scripts/` to
process-global `sys.path`.

The installed build already copies engine files into
`comic_sol_product/engine`; its generated `__init__.py` remains the installed
package boundary. Product loaders will import one of these explicit package
paths:

- Checkout: `scripts.comic_sol` and `scripts.mcp_server`.
- Installed wheel: `comic_sol_product.engine.comic_sol` and
  `comic_sol_product.engine.mcp_server`.

The loaders will use `importlib.import_module` and will stop inserting the
engine directory into `sys.path`. Other engine modules will be loaded relative
to the selected package, preventing duplicate top-level and package module
identities.

Direct execution of documented runtime scripts remains supported. When a
runtime file is launched directly and has no package context, a small
entrypoint bootstrap adds only the package parent, sets the canonical package
name, and then executes the same relative imports. Module execution remains
the preferred internal form. Build-only scripts that do not import engine
siblings are unchanged.

## Clean-Install Launch Verification

The clean-install environment directory will contain spaces on every platform.
After setup, the smoke test will:

1. Parse the generated Codex configuration with the standard library.
2. Assert that `command` is absolute and that `args` exactly match the
   persisted MCP entry.
3. Replace the child `PATH` with a platform-minimal system path that excludes
   the virtual environment and every Comic Sol install directory.
4. Pass the parsed command and arguments unchanged to the installed MCP smoke
   client.
5. Complete an MCP initialize/list/call sequence through that exact entry.

This proves the generated configuration launches without desktop-client PATH
assumptions. Windows and macOS matrix jobs exercise launcher paths containing
spaces. The macOS job additionally verifies Claude Desktop's native path.

## Test Isolation Gate

A standard-library isolation test will enumerate every `tests/test_*.py` file.
For each file it starts a fresh Python subprocess that asks `unittest` to
discover that file and asserts that at least one test is found. Discovery
loads the module but does not execute its tests, keeping the gate fast while
exposing import-order dependencies.

GitHub Actions will run the isolation test explicitly before the existing full
discovery command. Full discovery remains unchanged and also retains the
isolation test as a regression. Focused verification will run
`test_layouts.py` alone from the repository root.

## Verification

Required verification is:

- Red/green unit coverage for launcher resolution, repair idempotency, and
  platform-specific adapter paths.
- Red/green isolated discovery for `test_layouts.py` and the all-file gate.
- Client setup, product CLI, clean-install contract, MCP server, and packaging
  suites on the development host.
- Full unittest discovery.
- Wheel/sdist validation and clean-install smoke with MCP enabled.
- Git diff checks and plugin-bundle synchronization checks.
- GitHub Actions success for Windows, macOS Intel, and Ubuntu base/MCP matrix
  jobs, native distribution, and code scanning.

## Non-Goals

- Adding new MCP clients or changing their configuration schemas.
- Changing the output-root layout or MCP protocol surface.
- Introducing a general plugin loader or import compatibility framework.
- Supporting arbitrary Python source-file launchers as persisted desktop MCP
  commands; setup persists a real console script or native launcher.
