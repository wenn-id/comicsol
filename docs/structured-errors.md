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

The initial namespaces are `CS-IMG`, `CS-PROJ`, `CS-QA`, `CS-FONT`, `CS-MCP`, `CS-INSTALL`, `CS-EXPORT`, and `CS-SEC`. Within `CS-SEC`, `CS-SEC-001` reports a containment failure (path, symlink, or boundary crossing) while `CS-SEC-002` reports that an otherwise contained input exceeded a documented resource limit (JSON size/depth/collection/string, encoded raster bytes, or narrative field length); the engine raises the latter as `scripts.input_limits.InputResourceLimitError`, whose message always begins with `security-error: input exceeds`.

## Adding a code

1. Confirm the failure is a public, actionable boundary; do not expose every internal exception.
2. Choose the existing namespace that owns the boundary. Add a namespace only when ownership is genuinely new.
3. Search `ERROR_DEFINITIONS` and the repository history before choosing the numeric suffix. **Never reuse an old identifier**, even if its definition is removed or its wording changes.
4. Add one `ErrorDefinition` with all five contract fields and update the classifier only at the owning boundary.
5. Add CLI and MCP parity coverage, including safe redaction and a recovery assertion.
6. Update this document only when the public contract or namespace set changes, then run the full test suite and package build.

Codes are append-only public API. Renaming a category, message, reason, or recovery may affect consumers; prefer additive fields or a new code when semantics change.
