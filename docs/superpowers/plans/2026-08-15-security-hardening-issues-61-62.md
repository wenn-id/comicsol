# Security Hardening for Issues #61 and #62 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent arbitrary exception text from crossing the MCP `ToolError` boundary and preserve restrictive POSIX permissions during client configuration backup, update, and rollback.

**Architecture:** Replace best-effort regex sanitization with a fail-closed exception-category mapping and a small allowlist for known request-validation messages. Preserve configuration metadata with the standard library while retaining the existing atomic writer for updates and rollback.

**Tech Stack:** Python 3.11+, `unittest`, MCP 2.0, Python standard library

## Global Constraints

- Resolve GitHub issues #61 and #62 in one branch and one pull request against `main`.
- Add no dependency and change only the MCP error boundary, client setup transaction, focused tests, and approved Superpowers documents.
- Never include an arbitrary exception string in `ToolError` output.
- Keep the exact 17-tool MCP surface unchanged.
- Do not apply permission changes to client configuration parent directories.
- Keep Windows behavior functional without POSIX mode assertions.

## File Structure

- `scripts/mcp_server.py`: own the fail-closed MCP exception boundary and static containment errors.
- `tests/test_mcp_server.py`: prove exception mapping, credential non-disclosure, request-message allowlisting, and path hiding.
- `comic_sol_product/setup.py`: preserve source metadata when creating client configuration backups.
- `tests/test_client_setup.py`: prove restrictive POSIX modes survive success and rollback without changing the parent directory.

---

### Task 1: Fail-closed MCP tool errors

**Files:**
- Modify: `scripts/mcp_server.py:1-155,188-223,229-461`
- Test: `tests/test_mcp_server.py:158-218`

**Interfaces:**
- Consumes: caught Python exceptions and the existing `ToolError` class.
- Produces: `_tool_error(error: Exception) -> ToolError` and `_request_error(error: Exception) -> ToolError`, both returning only static messages.

- [ ] **Step 1: Write failing mapping and non-disclosure tests**

Replace the sanitizer-specific assertions in `McpServerUnitTests` with tests that exercise the real error-conversion boundary:

```python
    def test_tool_error_maps_exception_categories_to_safe_messages(self):
        cases = (
            (FileNotFoundError("password=file-value"), "not-found: required project data was not found"),
            (PermissionError("password=permission-value"), "permission-denied: project data could not be accessed"),
            (OSError("password=os-value"), "io-error: project data operation failed"),
            (UnicodeError("password=unicode-value"), "invalid-data: project data encoding is invalid"),
            (ValueError("password=value-error"), "invalid-data: tool request or project data is invalid"),
            (TypeError("password=type-error"), "invalid-data: tool request or project data is invalid"),
            (RuntimeError("password=runtime-value"), "internal-error: tool operation failed"),
        )
        for error, expected in cases:
            with self.subTest(error_type=type(error).__name__):
                self.assertEqual(expected, str(mcp_server._tool_error(error)))
                self.assertNotIn("password=", str(mcp_server._tool_error(error)))

    def test_tool_errors_never_leak_credentials_or_paths(self):
        cases = (
            ('password="top secret"', ("top secret",)),
            ("PASSWORD='mixed case value'", ("mixed case value",)),
            ('{"outer":{"Api_Key":"nested json value"}}', ("nested json value",)),
            ("authorization: Bearer bearer-value", ("bearer-value",)),
            ("Bearer standalone-value", ("standalone-value",)),
            (
                "token=first-value; client_secret: second value",
                ("first-value", "second value"),
            ),
        )
        for raw, secrets in cases:
            with self.subTest(raw=raw):
                converted = str(mcp_server._tool_error(RuntimeError(raw)))
                self.assertEqual("internal-error: tool operation failed", converted)
                for secret in secrets:
                    self.assertNotIn(secret, converted)

        raw_path = str(self.root / "private payload.json")
        converted = str(mcp_server._tool_error(FileNotFoundError(raw_path)))
        self.assertEqual("not-found: required project data was not found", converted)
        self.assertNotIn(raw_path, converted)

    def test_init_uses_only_allowlisted_request_error_messages(self):
        with self.assertRaisesRegex(
            ToolError,
            "^invalid-request: sensitive request setting is not allowed$",
        ) as context:
            mcp_server.comic_init("Story", "source", {"api_key": "do-not-leak"})
        self.assertNotIn("api_key", str(context.exception))
        self.assertNotIn("do-not-leak", str(context.exception))

        with self.assertRaisesRegex(
            ToolError,
            "^invalid-request: request setting keys must be strings$",
        ):
            mcp_server.comic_init("Story", "source", {1: "short_prompt"})
```

Keep the existing tests for invalid project IDs, traversal, symlinks, validation stages, relative paths, and oversized input. Remove direct `_safe_message` assertions because that helper will no longer exist.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest `
  tests.test_mcp_server.McpServerUnitTests.test_tool_error_maps_exception_categories_to_safe_messages `
  tests.test_mcp_server.McpServerUnitTests.test_tool_errors_never_leak_credentials_or_paths `
  tests.test_mcp_server.McpServerUnitTests.test_init_uses_only_allowlisted_request_error_messages -v
```

Expected: FAIL because `_tool_error` still forwards sanitized exception text and `_request_error` does not exist.

- [ ] **Step 3: Implement the static exception and request mappings**

In `scripts/mcp_server.py`, remove `PurePosixPath` and `PureWindowsPath` from the import, delete `_SENSITIVE_NAME`, and delete `_safe_message`. Add the request allowlist and both conversion functions:

```python
_REQUEST_ERROR_PREFIXES = (
    "title must be a non-empty string of at most 200 characters",
    "source must be at most 200 KiB as UTF-8 bytes",
    "request_settings must be a JSON object",
    "sensitive request setting is not allowed",
    "request setting keys must be strings",
    "unsupported request setting",
    "request mode must be one of short_prompt, pasted_story, source_file, or resume",
    "request language must be a non-empty language tag",
    "request title must be a non-empty string of at most 200 characters",
)


def _tool_error(error: Exception) -> ToolError:
    """Map an internal exception to a stable message without exposing its text."""
    if isinstance(error, FileNotFoundError):
        message = "not-found: required project data was not found"
    elif isinstance(error, PermissionError):
        message = "permission-denied: project data could not be accessed"
    elif isinstance(error, OSError):
        message = "io-error: project data operation failed"
    elif isinstance(error, UnicodeError):
        message = "invalid-data: project data encoding is invalid"
    elif isinstance(error, (ValueError, TypeError)):
        message = "invalid-data: tool request or project data is invalid"
    else:
        message = "internal-error: tool operation failed"
    return ToolError(message)


def _request_error(error: Exception) -> ToolError:
    """Allowlist known request errors without forwarding dynamic suffixes."""
    raw_message = str(error)
    for prefix in _REQUEST_ERROR_PREFIXES:
        if raw_message.startswith(prefix):
            return ToolError(f"invalid-request: {prefix}")
    return _tool_error(error)
```

In `_resolve_project`, replace each locally raised `ValueError` with `_reject` and the same static distinction prefixed by `security-error: `. Replace its catch with:

```python
    except ToolError:
        raise
    except Exception as error:
        raise _tool_error(error) from None
```

Change the `comic_init` catch to:

```python
    except Exception as error:
        raise _request_error(error) from None
```

Change every other MCP tool catch to suppress the original exception context:

```python
    except Exception as error:
        raise _tool_error(error) from None
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_mcp_server.McpServerUnitTests -v
```

Expected: all MCP unit tests pass, including the new mapping, credential, request, and path cases.

- [ ] **Step 5: Commit the MCP fix**

```powershell
git add scripts/mcp_server.py tests/test_mcp_server.py
git commit -m "fix: fail closed on MCP tool errors"
```

---

### Task 2: Preserve restrictive client configuration modes

**Files:**
- Modify: `comic_sol_product/setup.py:102-105`
- Test: `tests/test_client_setup.py:1-89`

**Interfaces:**
- Consumes: the existing source configuration path and timestamped backup path.
- Produces: a byte-identical backup with source metadata preserved and no parent-directory mode change.

- [ ] **Step 1: Write failing POSIX permission tests**

Add `os` and `stat` imports, then add these methods to `ClientSetupTests`:

```python
    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_setup_preserves_restrictive_config_backup_and_parent_modes(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.parent.chmod(0o700)
        config.write_text("{}\n", encoding="utf-8")
        config.chmod(0o600)
        parent_mode = stat.S_IMODE(config.parent.stat().st_mode)

        result = setup_clients(
            self.output,
            adapters=[JsonClientAdapter("cursor", config, "mcpServers")],
        )[0]

        self.assertEqual("configured", result.status)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(
            0o600,
            stat.S_IMODE(Path(result.backup_path).stat().st_mode),
        )
        self.assertEqual(parent_mode, stat.S_IMODE(config.parent.stat().st_mode))

    @unittest.skipIf(os.name == "nt", "POSIX mode semantics are unavailable on Windows")
    def test_posix_rollback_preserves_restrictive_config_and_backup_modes(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        config = self.home / ".cursor" / "mcp.json"
        config.parent.mkdir(parents=True)
        config.parent.chmod(0o700)
        original = b'{"theme":"dark"}\n'
        config.write_bytes(original)
        config.chmod(0o600)
        parent_mode = stat.S_IMODE(config.parent.stat().st_mode)
        adapter = JsonClientAdapter(
            "cursor",
            config,
            "mcpServers",
            verify_hook=lambda: False,
        )

        result = setup_clients(self.output, adapters=[adapter])[0]

        self.assertEqual("rolled-back", result.status)
        self.assertEqual(original, config.read_bytes())
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(
            0o600,
            stat.S_IMODE(Path(result.backup_path).stat().st_mode),
        )
        self.assertEqual(parent_mode, stat.S_IMODE(config.parent.stat().st_mode))
```

Production mutation caught: changing metadata-preserving backup copy back to `shutil.copyfile` makes each backup `0644` under the controlled `0022` umask.

- [ ] **Step 2: Run the POSIX tests and verify RED**

Run on Linux, macOS, or WSL:

```bash
python -m unittest \
  tests.test_client_setup.ClientSetupTests.test_posix_setup_preserves_restrictive_config_backup_and_parent_modes \
  tests.test_client_setup.ClientSetupTests.test_posix_rollback_preserves_restrictive_config_and_backup_modes -v
```

Expected: FAIL with backup mode `0644` instead of `0600`.

- [ ] **Step 3: Preserve backup metadata with the standard library**

In `_configure_one`, replace only the backup copy call:

```python
        shutil.copy2(path, backup)
```

- [ ] **Step 4: Run focused POSIX and platform-neutral tests and verify GREEN**

Run on POSIX:

```bash
python -m unittest tests.test_client_setup -v
```

Run on Windows:

```powershell
python -m unittest tests.test_client_setup -v
```

Expected: POSIX permission tests pass; Windows skips only the two mode-specific tests and all functional setup tests pass.

- [ ] **Step 5: Commit the permission fix**

```powershell
git add comic_sol_product/setup.py tests/test_client_setup.py
git commit -m "fix: preserve client config backup permissions"
```

---

### Task 3: Verify the combined branch and prepare delivery

**Files:**
- Verify: all tracked files and generated distribution artifacts

**Interfaces:**
- Consumes: the two focused implementation commits.
- Produces: fresh evidence that the combined branch meets repository validation requirements.

- [ ] **Step 1: Run focused regression suites**

```powershell
python -m unittest tests.test_mcp_server tests.test_client_setup -v
```

- [ ] **Step 2: Run the complete deterministic suite**

```powershell
python -m unittest discover -s tests -v
```

- [ ] **Step 3: Verify the canonical plugin bundle remains synchronized**

```powershell
python scripts/sync_plugin_bundle.py --check
```

- [ ] **Step 4: Run doctor, build, and release validation**

```powershell
python scripts/comic_sol.py doctor --output-root "$env:TEMP\comic-sol-doctor"
python -m build --no-isolation
python -m comic_sol_product.release dist\*.whl dist\*.tar.gz
```

- [ ] **Step 5: Check the final patch and commit history**

```powershell
git diff --check origin/main...HEAD
git status --short --branch
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors, no unintended tracked files, and the design,
implementation plan, and two focused implementation commits are present.

- [ ] **Step 6: Request code review, address valid findings, and rerun affected checks**

Review the full `origin/main...HEAD` diff against the approved design and both issue acceptance criteria. Fix every critical or important finding before delivery.

- [ ] **Step 7: Push and create one pull request**

```powershell
git push -u origin fix/issues-61-62-security-hardening
$prBody = @'
## Summary
- replace arbitrary MCP exception text with allowlisted error categories
- preserve restrictive client configuration modes across backup, update, and rollback

## Verification
- `python -m unittest tests.test_mcp_server tests.test_client_setup -v`
- `python -m unittest discover -s tests -v`
- `python scripts/sync_plugin_bundle.py --check`
- `python scripts/comic_sol.py doctor --output-root "$env:TEMP\\comic-sol-doctor"`
- `python -m build --no-isolation`
- `python -m comic_sol_product.release dist\\*.whl dist\\*.tar.gz`

Closes #61
Closes #62
'@
gh pr create --repo wenn-id/comicsol --base main --head fix/issues-61-62-security-hardening --title "fix: harden MCP errors and client config backups" --body $prBody
```

The prepared PR body must summarize both fixes, list fresh verification commands, and include `Closes #61` and `Closes #62`.
