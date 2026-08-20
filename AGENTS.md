# Comic Sol agent development constitution

This file is the non-negotiable contract for any AI agent that changes code,
tests, packaging, or documentation in `wenn-id/comicsol`. It records the
architectural invariants that would otherwise live only in maintainer memory, so
delegated implementation work cannot quietly violate them.

Read it before planning a change, and treat every article as a hard gate.

## Scope and precedence

- **Applies to:** agents doing repository work — the deterministic engine under
  `scripts/` and `comic_sol_product/`, tests, packaging, installers, CI, and docs.
- **Does not apply to:** producing a comic. `SKILL.md` governs that workflow and
  deliberately forbids the production agent from reading or editing engine
  source. When producing a comic, follow `SKILL.md`; when developing this
  repository, follow this file.
- This file **does not replace** human code review, `CONTRIBUTING.md`,
  `SECURITY.md`, `PRIVACY.md`, or the release gate in
  `docs/releases/v2.0-stable-criteria.md`. Where a documented policy is stricter,
  the stricter policy wins. Never resolve a conflict by relaxing either side:
  stop and report it.
- An article may be waived only by an explicit maintainer decision recorded in
  the pull request. An agent never waives one on its own authority.

## Article 1 — Project schemas never change silently

`scripts/schema.py` is the only compatibility gate for `project.json`.

- Never change the shape or meaning of a persisted project artifact without
  bumping the version constants in `scripts/schema.py` and registering a hook in
  `PROJECT_MIGRATIONS`, keyed by `(source_version, target_version)`.
- Never widen `SUPPORTED_PROJECT_SCHEMA_VERSIONS` just to make an unsupported
  project load. A project with no registered migration path fails closed with
  `UnsupportedSchemaVersionError`.
- Reading a project never mutates it. A manifest without `schema_version` is the
  legacy representation and is normalized in memory only.
- A migration runs inside `ProjectTransaction` or not at all: it either publishes
  a complete manifest for the current version, or it leaves the project
  byte-for-byte unchanged.
- Every schema change ships with a fixture project and a migration test.
- `references/schemas.md` and `templates/` document the same contract; update them
  in the same change.

## Article 2 — User projects are never deleted

Uninstall and upgrade touch product-owned files only.

- `uninstall` removes the Comic Sol runtime and the `comic-sol` MCP integration
  entry. It must never delete, move, or rewrite a user project directory, a
  generated artifact, or unrelated content beneath an output root.
- `comic_sol_product/setup.py` and `comic_sol_product/clients.py` may remove only
  the `comic-sol` server entry, and only at a verified client-config location.
  Surrounding configuration bytes are preserved; an unverified native format or
  location is reported as `unsupported` rather than guessed or rewritten.
- Uninstall is idempotent and safe to repeat.
- Upgrade and rollback preserve existing projects and existing client
  configuration; every failure path restores the original bytes.
- `repair` may recreate product-owned state; it may never discard user work.
- `docs/install.md` and `docs/releases/v2.0-stable-criteria.md` treat project
  preservation as a release gate. Any change to install, upgrade, rollback, or
  uninstall keeps that preservation coverage green.

## Article 3 — Provider credentials and SDKs stay outside the engine

Image generation is agent-managed; the deterministic engine is provider-neutral.

- Code under `scripts/` and `comic_sol_product/` must never import a provider SDK,
  read a provider credential, call a provider over the network, or embed an API
  key, endpoint, or account identifier.
- The engine accepts only a local raster produced by the agent session, then
  validates, normalizes, retains, and records it.
- Raw provider payloads and responses are never persisted. Logs keep sanitized
  paths, hashes, categories, and state changes — never credentials or story
  content.
- A missing image capability is an actionable, resumable `BLOCKED` state, not a
  reason to add a provider integration.
- Any new dependency needs pinned, hash-locked entries in `requirements/locks/`;
  a provider SDK is not an acceptable engine dependency.

## Article 4 — Path containment and security invariants hold

`scripts/project_io.py` owns the filesystem trust boundary. Use it; never
reimplement it.

- Every project-relative path resolves through `contained_project_path()`, or the
  `open_contained()` / `open_path_nofollow()` helpers. Absolute paths, drive
  letters, and `..` segments are rejected.
- Symlinks and Windows reparse points are refused on every path component. No
  helper may be changed to follow them.
- A write never escapes the project directory, and MCP never escapes its
  configured `--root`. Containment is not authentication: the MCP `stdio` surface
  has no auth layer, so never introduce logic that assumes a trusted caller.
- Source input stays bounded: at most `MAX_SOURCE_BYTES` of valid UTF-8, and only
  `.txt` or `.md`.
- Archive extraction, release checksums, and provider metadata sanitization are
  security-sensitive; changing one requires the matching negative test.
- Path-containment tests may be skipped only on unprivileged Windows, which
  cannot create file symlinks. CI enforces this through
  `COMIC_SOL_REQUIRE_SYMLINK_TESTS`; a skip anywhere else is a coverage loss and
  must fail.
- A suspected vulnerability follows `SECURITY.md` private reporting. Never open a
  public issue or publish exploit detail in a pull request.

## Article 5 — Behavior changes require regression tests

- Every behavior change adds or updates a deterministic test that fails before
  the fix and passes after it. A bug fix without a reproducing test is
  incomplete.
- Never weaken a validation threshold, delete an assertion, or loosen a fixture
  to turn a failing check green. If a threshold is genuinely wrong, propose that
  as its own reviewed change with justification.
- Never add a skip or expected-failure marker to reach green.
- Tests stay offline and provider-free, and every `tests/test_*.py` module must
  import cleanly under isolated discovery.
- Shared CLI and MCP behavior gets parity coverage on both surfaces.
- A visual output change requires an actual before/after render and human visual
  inspection. Passing geometry tests are never sufficient; deterministic and
  benchmark runs prove mechanics only.

## Article 6 — Deterministic behavior and atomic writes

- Identical inputs produce identical bytes. No wall-clock value, locale, random
  seed, hash ordering, or filesystem iteration order may leak into an artifact.
  Serialize through the canonical helpers in `scripts/core_primitives.py`.
- MCP sampling stays disabled, and deterministic stages never call a model.
- Published files are written atomically: `durable_atomic_write()` for a single
  file, `ProjectTransaction` for a multi-file change. A partially written
  artifact is never visible.
- A multi-file change is all-or-nothing: journal first, then publish, and restore
  backups in reverse order on any failure.
- Concurrent access is serialized by `ProjectLock`, and an interrupted
  transaction is rolled back by `ProjectTransaction.recover()`. Never bypass the
  lock or hand-roll a temp-file rename.
- Installer and client-config writes follow the same rule: back up, write
  atomically, verify, and restore the original bytes when verification fails.

## Article 7 — Resumability and public JSON compatibility survive

- `scripts/stage_registry.py` is the single source of truth for resume stages:
  `planning`, `storyboard`, `generation`, `lettering`, `composition`, and
  `export`. Change stage metadata there only, never by duplicating it in a CLI,
  MCP, or validation surface.
- Every interrupted run stays resumable. Editable intermediates are preserved,
  invalidation stays honest, and a project that cannot safely complete stops at
  `BLOCKED` instead of fabricating success.
- Never fabricate an artifact, a QA record, provider capability, or a terminal
  success status.
- The public CLI JSON envelope `ok`, `command`, `data`, `error` is stable. Prefer
  additive fields, and keep documented legacy fields such as `data.healthy` and
  `data.messages` alongside `data.ready`.
- Human progress (`WORKING`, `BLOCKED`, `FAILED`, `COMPLETE`) goes to `stderr`
  only. With `--json`, `stdout` stays exactly one parseable JSON envelope.
- Error codes in `comic_sol_product/errors.py` are append-only public API in the
  form `CS-<NAMESPACE>-<NNN>`. Never reuse a retired identifier; add a new code
  when semantics change.
- The MCP surface is exactly 17 `comic_*` tools. Never add, rename, or remove a
  tool without a separately reviewed contract change.

## Article 8 — Full verification before claiming completion

Run the real gates from `CONTRIBUTING.md`, through the same interpreter used to
install dependencies, and report actual output:

- the complete suite — `python -m unittest discover -s tests -v`;
- `python scripts/comic_sol.py doctor --output-root <temp>`;
- `python -m build --no-isolation`, then
  `python -m comic_sol_product.release dist/*.whl dist/*.tar.gz`;
- `python scripts/sync_plugin_bundle.py --check` when a canonical bundle source
  changes;
- clean-install and portable-runtime smoke tests for packaging, runtime freezing,
  MCP, release automation, or distribution changes;
- `scripts/benchmark.py` for lifecycle, lettering, composition, or export
  changes.

Rules:

- A command that exits zero is evidence only for what it actually covered. Never
  describe an unrun check as passing.
- Never claim work is complete while a relevant gate is failing, skipped, or
  unrun. Report the gap instead.
- If a gate cannot run in the current environment, name it, say why, and leave
  verification to CI rather than implying local success.
- Never commit credentials, provider payloads, generated projects, build outputs,
  or private source material.
