# Security Hardening for Issues #61 and #62

## Scope

This change resolves GitHub issues #61 and #62 in one pull request. It prevents
MCP tool errors from exposing arbitrary exception text and preserves restrictive
POSIX permissions while client configuration files are backed up, updated, or
rolled back.

The change is limited to `scripts/mcp_server.py`,
`comic_sol_product/setup.py`, and their focused tests. It adds no dependency and
does not refactor unrelated MCP tools or client adapters.

## MCP error boundary

`ToolError` becomes a fail-closed boundary. Exceptions caught from internal
operations are mapped by exception category to stable, allowlisted error codes
and user-facing messages. The original exception string is never included in a
`ToolError`.

The category mapping is ordered from specific to general:

- `FileNotFoundError` → `not-found: required project data was not found`
- `PermissionError` → `permission-denied: project data could not be accessed`
- other `OSError` → `io-error: project data operation failed`
- `UnicodeError` → `invalid-data: project data encoding is invalid`
- `ValueError` or `TypeError` → `invalid-data: tool request or project data is invalid`
- every other exception → `internal-error: tool operation failed`

Known request-validation failures retain useful static messages through an
explicit allowlist. Dynamic suffixes such as setting names are not forwarded.
Project containment checks continue to return specific, static security
messages. Unexpected filesystem, validation, and internal exceptions return a
stable category message instead of sanitized arbitrary text.

The request-setting allowlist keeps only the fixed categories `sensitive
request setting is not allowed`, `request setting keys must be strings`,
`unsupported request setting`, invalid mode, invalid language, and invalid
title. Project containment messages keep the existing invalid-ID,
outside-root, uninitialized-project, and symlink distinctions, but each is
constructed from a static literal rather than an exception string.

Because arbitrary exception text no longer crosses the MCP boundary, the
credential regex sanitizer and its sensitive-name pattern are removed. This
also removes the risk of future credential formats bypassing a regex. Absolute
paths remain protected because neither raw filesystem exception text nor a raw
path is emitted.

## Client configuration permissions

Client setup copies the original configuration to its timestamped backup with
metadata preservation. On POSIX, a source mode such as `0600` is therefore
retained by the backup. The existing atomic writer creates its temporary file
with restrictive permissions before replacing the config; the same writer is
used for successful updates and rollback, so both paths remain restrictive.

No chmod is applied to the parent directory, and Windows keeps using the same
functional copy/write flow without assertions based on POSIX mode bits.

## Tests

MCP tests use table-driven cases covering quoted and unquoted assignments,
spaces, `:` and `=` separators, mixed casing, nested JSON, multiple sensitive
fields, and Authorization/Bearer headers. Each case is passed through the
actual `ToolError` conversion and asserts that every supplied secret is absent.
Separate checks confirm stable error codes/messages and continued path hiding.

POSIX-only client setup tests start from a `0600` configuration and verify that
the updated config and backup remain `0600`. A verification-failure case checks
that rollback also leaves both files at `0600`. The parent directory mode is
recorded before setup and must remain unchanged. Existing platform-neutral
tests continue to cover Windows behavior.

## Delivery

The implementation and regression tests are committed on
`fix/issues-61-62-security-hardening`, pushed to `origin`, and proposed against
`main` in one pull request that closes #61 and #62.
