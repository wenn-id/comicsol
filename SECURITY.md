# Security Policy

## Supported versions

The latest published prerelease or stable release receives security fixes. Older release candidates may be superseded without backports.

## Reporting a vulnerability

Do not open a public issue for a vulnerability, leaked credential, unsafe archive behavior, path-containment bypass, arbitrary file write, or dependency compromise.

Use GitHub's private vulnerability reporting for `wenn-id/comicsol` when available. If that interface is unavailable, contact the repository owner privately through the contact method shown on the owner's GitHub profile and include:

- affected version or commit;
- reproduction steps and impact;
- operating system and installation method;
- whether credentials or private project data may have been exposed;
- any proposed mitigation.

Do not include live secrets. Revoke exposed credentials before reporting and replace values with `[REDACTED]`.

## Scope

Security-sensitive areas include project path containment, archive extraction, transactional configuration writes, provider metadata sanitization, release checksums, bundled runtimes, MCP output-root isolation, and generated artifact validation.

### MCP trust boundary

The optional MCP server uses local `stdio` and has no authentication. Any local process able to launch the configured command can invoke the complete tool surface inside its configured `--root`. Use a dedicated absolute output root containing only Comic Sol projects, and run it only from a trusted client configuration. Do not point MCP at a home directory, repository root, or shared multi-user folder. Containment and symlink rejection limit filesystem reach; they do not authenticate clients. CLI commands accepting `project_dir` should likewise use paths under the intended output root.

Comic Sol does not bundle image-provider credentials or send data by itself. When an agent invokes an external image capability, that provider's privacy and retention policy applies.

### Resource limits on untrusted project input

Everything a caller can place inside the project root is untrusted, so the engine bounds every project-relative input before reading or decoding it. The central limits live in `scripts/input_limits.py` and `scripts/raster_limits.py`, and every violation fails with the stable structured error `CS-SEC-002`:

- A project JSON document is at most 2 MiB of UTF-8, at most 64 levels of nesting, at most 4096 entries per array or object, and at most 65,536 characters per string. The append-only `logs/events.jsonl` log is capped at 8 MiB as a whole.
- A raster is checked against a 128 MiB encoded-byte ceiling before decode, in addition to the existing decoded-pixel ceiling (`MAX_DECODED_PIXELS`).
- All project reads go through the no-follow bounded readers in `scripts/project_io.py` (`read_contained_bytes`, `read_contained_json`, `read_bytes_nofollow`, `read_json_nofollow`); file size is checked before any read or decode.

### Narrative field limits and hygiene

Titles (200 characters), transition warnings (500 characters), and panel override reasons (1000 characters) are persisted operator notes, not story content. They are validated on every CLI and MCP write path and re-checked on validation: over-length or credential-shaped values are rejected, and validation flags them when found in persisted artifacts. Narrative fields must never contain source text, PII, or credentials; obvious secrets are rejected outright rather than redacted, because a redacted note would still claim to be an accepted override.

## Repository hardening

The repository uses GitHub vulnerability alerts, Dependabot security updates,
secret scanning with push protection, and CodeQL analysis on pull requests and
the `main` branch. Changes to `main` require an independent approving review,
dismiss stale approvals, and keep the existing required CI checks and
conversation-resolution rule.

All third-party GitHub Actions are pinned to full commit SHAs. If an action
cannot be pinned, the pull request must document the action, the reason, the
owner responsible for the exception, and an expiry date before merge.
