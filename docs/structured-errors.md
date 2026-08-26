# Structured Comic Sol errors

Comic Sol exposes one structured error taxonomy to the CLI and MCP surfaces. The canonical registry is `comic_sol_product.errors.ERROR_DEFINITIONS`; adapters must classify exceptions through `classify_exception()`/`error_payload()` instead of defining surface-specific constants.

## Public contract

Every public definition contains:

- `code`: immutable identifier in the form `CS-<NAMESPACE>-<NNN>`.
- `category`: stable machine category such as `invalid-data` or `not-found`.
- `message`: short, safe human-readable summary.
- `reason`: technical explanation without secrets or local paths.
- `recovery`: actionable remediation guidance.

CLI JSON errors keep the existing envelope (`ok`, `command`, `data`, `error`) and add the canonical fields inside `error`. Human CLI output remains readable and renders the code, category, message, reason, and recovery. MCP `ToolError` carries the same fields as a JSON string because the installed MCP SDK transports a string error value.

The namespaces are `CS-IMG`, `CS-PROJ`, `CS-QA`, `CS-FONT`, `CS-MCP`, `CS-INSTALL`, `CS-EXPORT`, `CS-SEC`, `CS-CLI`, and `CS-HANDOFF`. Within `CS-SEC`, `CS-SEC-001` reports a containment failure (path, symlink, or boundary crossing) while `CS-SEC-002` reports that an otherwise contained input exceeded a documented resource limit (JSON size/depth/collection/string, encoded raster bytes, or narrative field length); the engine raises the latter as `scripts.input_limits.InputResourceLimitError`, whose message always begins with `security-error: input exceeds`. Handoff processing does not absorb those security boundaries: path-containment and symlink failures remain `CS-SEC-001`, and resource-limit failures remain `CS-SEC-002`.

## Fail-closed behavior

Both surfaces are fail-closed: every failure path emits exactly one canonical result instead of argparse usage text, `SystemExit`, or a raw traceback.

- **Parse errors.** Invalid or missing arguments raise `comic_sol_product.errors.CliUsageError` instead of argparse's `SystemExit`. When `--json` is requested (the flag may appear anywhere in the invocation), `stdout` receives exactly one envelope classified `CS-CLI-001` with `command: null`, the argparse diagnostic in `error.detail`, and exit status `2`; `stderr` stays empty. Human mode prints the canonical error block to `stderr` and exits `2`. `--help` and `--version` remain successful exits with their usual text on `stdout`.
- **Unexpected exceptions.** A final top-level boundary catches every exception the typed handlers do not. It classifies as `CS-PROJ-005` on the CLI surface (`CS-MCP-002` on MCP), adds a redacted `error.detail`, exits `1`, and never prints a traceback.
- **Validation failures.** `comic-sol validate` fails closed: a completed inspection that reports issues returns `ok: false`, exit status `2`, and the canonical `CS-QA-001` payload, while `data` still carries the full issue list for parity with the MCP `comic_validate` tool (which returns that same list as its tool result). An empty issue list remains `ok: true` with exit status `0`. Human mode prints the issue list to `stdout` and the error block to `stderr`.

### Exit statuses

| Status | Meaning |
| --- | --- |
| `0` | Success (valid project, healthy doctor, completed lifecycle). |
| `1` | Operational failure: I/O, permission, security, internal, or unexpected errors. |
| `2` | Usage or input rejection: argument parse errors, invalid request data, or a project that fails validation. |

### Redaction rules

- `error.detail` is produced by `safe_error_detail()` (parse errors use argparse's own path-free message). Absolute POSIX and Windows paths — quoted or bare — are replaced with `<path>` before any detail leaves the process.
- Canonical `message`, `reason`, and `recovery` fields are static registry text and never embed user content.
- `legacy_category`/`legacy_message` fields exist only for pre-existing consumers and follow the same redaction.

## Code reference

| Code | Category | Boundary and recovery |
| --- | --- | --- |
| `CS-CLI-001` | `invalid-request` | The command line is invalid; [check usage and retry](user/troubleshooting.md#cs-cli-001). |
| `CS-HANDOFF-001` | `handoff-contract-error` | Handoff data, locked scope, or the current job binding is invalid or stale; [inspect or prepare the handoff again](../references/schemas.md#portable-handoff-contracts). |
| `CS-HANDOFF-003` | `handoff-result-error` | Executor metadata, attempt identity, raster evidence, or reference activation was rejected; [correct and resubmit the current result](../references/schemas.md#generation-receipt-generationreceiptsattempt-idjson). |
| `CS-PROJ-001` | `invalid-data` | Project or request data violates the schema; [validate or restore it](user/troubleshooting.md#cs-proj-001). |
| `CS-PROJ-002` | `not-found` | Required project data is missing; [locate or restore it](user/troubleshooting.md#cs-proj-002). |
| `CS-PROJ-003` | `permission-denied` | The process cannot access project data; [restore safe access](user/troubleshooting.md#cs-proj-003). |
| `CS-PROJ-004` | `io-error` | The filesystem failed during a project operation; [check storage and retry](user/troubleshooting.md#cs-proj-004). |
| `CS-PROJ-005` | `internal-error` | Unexpected runtime failure; [retry once and collect diagnostics](user/troubleshooting.md#cs-proj-005). |
| `CS-SEC-001` | `security-error` | Containment failure; [remove the unsafe path or indirection](user/troubleshooting.md#cs-sec-001). |
| `CS-SEC-002` | `security-error` | Input exceeded a documented resource limit; [reduce the reported input](user/troubleshooting.md#cs-sec-002). |
| `CS-IMG-001` | `image-error` | Image asset missing, unreadable, or outside limits; [repair or replace the asset](user/troubleshooting.md#cs-img-001). |
| `CS-QA-001` | `quality-error` | Deterministic or visual QA failed; [review evidence and repair the named artifact](user/troubleshooting.md#cs-qa-001). |
| `CS-FONT-001` | `font-error` | Font missing, invalid, or unloadable; [restore a supported font](user/troubleshooting.md#cs-font-001). |
| `CS-EXPORT-001` | `export-error` | Export could not be rendered or published; [validate and finalize again](user/troubleshooting.md#cs-export-001). |
| `CS-INSTALL-001` | `missing-extra` | Optional component missing; [reinstall the correct extra](user/troubleshooting.md#cs-install-001). |
| `CS-INSTALL-002` | `repair-failed` | Client integration repair failed safely; [preview and retry the repair](user/troubleshooting.md#cs-install-002). |
| `CS-INSTALL-003` | `rollback-failed` | Client config rollback could not be verified; [stop and restore the reported backup](user/troubleshooting.md#cs-install-003). |
| `CS-MCP-001` | `invalid-request` | An MCP tool argument violated the request contract; [correct the request](user/troubleshooting.md#cs-mcp-001). |
| `CS-MCP-002` | `internal-error` | The MCP tool hit an unexpected failure; [retry once and inspect diagnostics](user/troubleshooting.md#cs-mcp-002). |

`CS-HANDOFF-002` is reserved and deferred for WP3 archive failures. WP2 does not define, emit, or classify that code; preserving the gap prevents a different failure from reusing its future public identifier.

## Classifier lookup surfaces

`classify_exception()` maps an exception to one definition through exactly two boundary registries plus the type-based fallback chain:

- **Typed boundaries** (`_BOUNDARY_TYPE_NAMES`): the engine's typed exception classes are matched by class name so `comic_sol_product.errors` never imports the engine. `PdfExportError` → `CS-EXPORT-001`; `PdfQualityError`, `PageQualityMigrationError`, `ProjectValidationError`, and `ValidationFailureError` → `CS-QA-001`; `TypographyPreflightError` → `CS-FONT-001`; `HandoffContractError` and `StaleLockedScopeError` → `CS-HANDOFF-001`; `HandoffResultError` → `CS-HANDOFF-003`; `IntegrationRepairError` → `CS-INSTALL-002`; `IntegrationRollbackError` → `CS-INSTALL-003`; `CliUsageError` → `CS-CLI-001`.
- **Message-prefix boundaries** (`_BOUNDARY_MESSAGE_PREFIXES`): stable lowercase prefixes the owning engine module raises — `security-error: input exceeds` → `CS-SEC-002`, `security-error` and the no-follow path messages (`project path must not contain symlinks`, `path must not contain symlinks or reparse points`) → `CS-SEC-001`, `page_qa_required:` → `CS-QA-001`, the raster messages (`panel is not a readable image`, `source is not a readable image`, `source image format must be`, `source image exceeds the decoded pixel limit`, `missing required lettered panel image`) → `CS-IMG-001`, and the font messages (`font policy`, `font script override`, `font is not a readable TrueType/OpenType file`) → `CS-FONT-001`.
- **Type fallback chain**: `FileNotFoundError`, `PermissionError`, `UnicodeError`, `ValueError`/`TypeError`, `OSError`, then `RuntimeError` (with the missing-extra prefixes → `CS-INSTALL-001`). Request-rejection prefixes (`invalid project ID`, `unknown validation stage`, `attempt path`) classify as `CS-MCP-001` only on the MCP surface or when `request=True`. Anything else is the internal-error definition for the surface (`CS-PROJ-005` on CLI, `CS-MCP-002` on MCP).

## CLI and MCP parity

The same registry and classifier serve both surfaces, so one failure class carries the same code, category, and recovery everywhere. Differences are only in transport and documented above: the CLI wraps the payload in the `ok`/`command`/`data`/`error` envelope and adds exit statuses; MCP transports the same fields as a `ToolError` JSON string. The CLI `validate` command and the MCP `comic_validate` tool report the same issue list — the CLI marks it fail-closed (`ok: false`, exit `2`, `CS-QA-001`), while the MCP tool returns the list as its result because MCP tools report inspection data as results and failures as `ToolError`.

## Adding a code

1. Confirm the failure is a public, actionable boundary; do not expose every internal exception.
2. Choose the existing namespace that owns the boundary. Add a namespace only when ownership is genuinely new.
3. Search `ERROR_DEFINITIONS` and the repository history before choosing the numeric suffix. **Never reuse an old identifier**, even if its definition is removed or its wording changes.
4. Add one `ErrorDefinition` with all five contract fields and update the classifier only at the owning boundary.
5. Add CLI and MCP parity coverage, including safe redaction and a recovery assertion.
6. Update this document only when the public contract or namespace set changes, then run the full test suite and package build.

Codes are append-only public API. Renaming a category, message, reason, or recovery may affect consumers; prefer additive fields or a new code when semantics change.
