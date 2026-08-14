# Issues 58–60 Remediation Design

## Context

This change closes three high-severity audit findings against `main` at
`1532d9f324d9d7042ad98918e906906115ddaa79`:

- #58: native uninstallers recursively delete an unverified custom install root;
- #59: panel override supports legacy QA schema 1.0 but not canonical schema 2.0;
- #60: root-skill and plugin-bundle references expose different schema contracts.

OpenAI's skill documentation defines a skill as a directory containing `SKILL.md`
plus optional scripts, references, and assets, while plugins are the distribution
mechanism for reusable skills. Comic Sol therefore keeps a usable root skill and a
self-contained plugin bundle, but one must be generated from the other rather than
maintained as an independent contract.

## Goals

1. Make native uninstall fail closed unless the exact install root is demonstrably
   owned by Comic Sol, and preserve every foreign file.
2. Support an auditable, schema-valid override of an eligible schema-2.0 visual QA
   failure while preserving schema-1.0 compatibility.
3. Make the repository root the canonical skill source and mechanically synchronize
   the self-contained plugin bundle, with CI detecting drift.

## Non-goals

- No new dependency, packaging framework, migration service, or general schema system.
- No change to image-generation policy, retry budgets, or non-visual/safety refusal
  behavior.
- No automatic deletion or migration of pre-sentinel installations. A user must run
  the new installer successfully once before the new uninstaller will remove it.
- No forced parity for the host-specific `capability-detection.md` and
  `image-provider-setup.md` references.

## Design

### 1. Fail-closed native uninstall

Both installers will create `.comic-sol-install` only after the runtime has passed
`doctor` and activation is ready to commit. The marker uses a three-line format that
POSIX shell and PowerShell can parse without Python, `jq`, or another dependency:

```text
comic-sol-install-v1
<active version>
<resolved absolute install root>
```

The product identity and marker schema are encoded in the first line. The second line
must match `active-version`; the third must match the resolved root supplied to the
uninstaller. Marker publication uses a sibling `.new` file followed by atomic rename.
Failed upgrades leave an existing marker untouched and remove any staged marker.

Uninstall behavior is:

1. A missing root remains an idempotent success.
2. Filesystem roots, the user's home, the current working directory, Git worktrees,
   and Comic Sol project roots are rejected as sensitive targets.
3. A present root without an exact valid marker fails with remediation text instructing
   the user to reinstall/upgrade into the same root first.
4. A valid root removes only `bin`, `versions`, `.bin.rollback`, `bin.new`,
   `active-version`, `active-version.new`, and the marker/staged marker.
5. The install root is removed only with a non-recursive empty-directory operation.
   Any unrelated file keeps the root in place.

The lock remains outside the install root and continues to serialize lifecycle
mutations. PowerShell and POSIX tests exercise missing markers, valid markers, foreign
files, and sensitive targets on their native CI runners.

### 2. Schema-2.0 panel override

Schema 2.0 keeps its existing required top-level fields and permits one optional,
non-empty `override_reason`. It is valid only when:

- `decision` is `accept-warning`;
- at least one check is warning-level after override;
- the exact reason is present in `unresolved_warnings`.

This is backward compatible with existing v2 records because the new field is optional.
The explicit field distinguishes a user-authorized override from an ordinary warning;
the existing `panel.overridden` event supplies the timestamped audit trail.

`record_override` branches on `schema_version`:

- schema 1.0 retains its current category, artifact, decision, and field behavior;
- schema 2.0 identifies the panel through `subject_id`, requires `regenerate` plus an
  error-level failed check, and validates the canonical `bindings`, hashes, dimensions,
  normalization artifact, and readable rasters before any mutation.

For an eligible v2 record, failed error-level checks retain `result: fail` but change to
`severity: warning`; `decision` becomes `accept-warning`; the normalized reason is added
to `override_reason`, panel `unresolved_warnings`, and manifest warnings. The transaction
continues to stage the panel record, manifest, and event log atomically. Invalid/stale/
corrupt bindings, an already accepted decision, empty reasons, and records without an
error-level visual failure fail without modifying any artifact.

Regression coverage exercises the engine and MCP entry point with a real schema-2.0
fixture, validates the resulting record, confirms resume reuse, and proves stale/corrupt
bindings remain non-overridable. Existing schema-1.0 tests remain unchanged.

### 3. Canonical root and synchronized plugin bundle

The repository root is the canonical authoring source. A small standard-library script
will copy the bundled `SKILL.md`, deterministic scripts, templates, fonts/legal files,
and non-host-specific references into `skills/comic-sol/`. Its `--check` mode performs
byte comparisons and exits nonzero with the drifted paths instead of writing.

The explicit host-specific reference allowlist contains only:

- `references/capability-detection.md`;
- `references/image-provider-setup.md`.

All other root references, including `schemas.md`, `workflow.md`, and `visual-qa.md`,
must match the bundle. Root `schemas.md` and `visual-qa.md` will first be updated to the
current schema-2.0 contract, then the synchronizer will propagate them and the #59
runtime changes into the bundle.

The full unit-test job already runs on every supported platform. A focused contract test
will run the synchronizer in `--check` mode, making drift a CI failure. CONTRIBUTING will
document the sync and check commands so contributors do not hand-edit generated files.

## Test strategy

Every production change follows red-green-refactor:

1. Add installer tests that reproduce unsafe/no-marker deletion and bundle-contract
   tests that reproduce current drift; run them and record the expected failures.
2. Add schema-2.0 override engine/MCP tests and observe the current identity mismatch.
3. Implement only enough marker validation, v2 branching, and synchronization to make
   those tests pass.
4. Run the affected installer, resume, validation, MCP, release-doc, and distribution
   modules.
5. Run bundle `--check`, doctor, full `unittest discover`, build wheel/sdist, release
   content validation, and clean-install MCP smoke before push.

## Delivery

Work is performed on `fix/issues-58-60` in an isolated worktree. The PR will preserve
reviewable commits, reference all three issues, and use `Closes #58`, `Closes #59`, and
`Closes #60` in its body.
