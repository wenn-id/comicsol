# Comic Sol Studio — security and privacy

This document lists the trust boundaries Comic Sol Studio enforces and the
classes of information that never cross them. Every statement below is
grounded in the merged Web code and the WP16 qualification suite; the
documentation contract tests in `web/tests/test_web_docs.py` enforce these
claims.

> **One sentence, no exceptions**
>
> A provider credential is never exposed to the browser, written into a project archive, recorded in a receipt, emitted to a log, or included in this submission.
>
> That single rule is the non-negotiable contract every other section in this document supports.

## Request integrity

Request integrity in Studio is the combination of four separate guarantees,
each with its own section below and its own merged-code contract:

- **authentication** and **CSRF** — enforced for every state-changing
  request (see [Authentication and CSRF](#authentication-and-csrf));
- **ownership** and **opaque** identifiers — a request can never address a
  resource it does not own (see [Ownership and opaque IDs](#ownership-and-opaque-ids));
- **revision** and **idempotency** — the same logical action cannot be
  applied twice and a stale action cannot land on a later state (see
  [Revision and idempotency](#revision-and-idempotency));
- **approval replay** protection — a recorded approval cannot be replayed
  onto a later revision (see [Approval replay protection](#approval-replay-protection)).

A request that fails any of these is rejected before it mutates state.

## Authentication and CSRF

**Studio does not currently ship an OAuth sign-in route.** The merged
`web/comic_sol_web/app.py::create_app` composition root includes only
the `projects`, `generation`, `approvals`, and `assets` routers plus
`/healthz` and static; it does **not** register an authentication
router, so no `/api/auth/login` or OAuth callback is served by the
merged application. `WebConfig` has no GitHub-client configuration in
the merged build. The scope boundary is the same as in
[Sign in](index.md#sign-in): `/healthz` is unauthenticated and
deterministic; every project and generation route currently requires
a `SessionPrincipal`, but the route that issues one is not present
in the merged build. The session-and-CSRF machinery below is the
**documented intended end-state** of the build, not an active path
that the merged code exercises today. The security contract test
`RuntimeBoundaryContractTests::test_create_app_registers_no_auth_router`
locks the current scope by AST-scanning the composition root and
asserting no `auth` / `login` / `callback` / `oauth` / `github`
router is included.

**Intended end-state (once the auth router is wired)**, the flow is:

- the user opens the Studio in a browser and authenticates through
  GitHub OAuth (`web/comic_sol_web/auth.py` contains the construction
  logic, but the router is not registered by `create_app` in the
  merged build);
- the browser is bound to a one-time OAuth state and a separate
  cookie-level binding; the server records only keyed digests of
  those values, never the plaintext, and any replay is rejected on
  consume;
- after the OAuth callback succeeds, the browser holds an `HttpOnly`
  session cookie and a paired CSRF cookie; the session cookie is
  `HttpOnly` and is never returned to JavaScript; the CSRF header is
  paired against a per-session token via `AuthService.require_csrf`,
  and a missing, mismatched, or expired token is rejected before the
  handler mutates state;
- the same-origin policy plus the same-site cookie model isolate the
  session from third-party contexts.

## Ownership and opaque IDs

Every project, panel, asset, and archive is identified by an opaque ID. The
browser never sees a filesystem path. A user may own at most one project with
a given ID; an authenticated request whose path ID is not owned by the
authenticated principal is rejected with `not found` rather than `forbidden`,
so the existence of unrelated IDs is not leaked.

## Revision and idempotency

Generation, provider switch, raster promotion, and overwrite all bind to a
**revision**. The revision is monotonically increasing and is part of the
request's identity. Re-submissions whose request signature is
**deduplicated by `Idempotency-Key`** (parseable as a UUID) are rejected
or returned as the prior result server-side rather than executed twice;
operations that **parse but discard** the `Idempotency-Key` (plan updates
and `run_qa`) are not deduplicated and a duplicate replay therefore
re-runs the operation. A stale revision cannot be replayed onto a later
one for any operation. The export endpoint tracks replayed export calls
in an **in-memory set** scoped to the process: a duplicate `Idempotency-Key`
on the same `(user, key)` is rejected with `409 export replay rejected`,
and the set is lost on process restart (an export retried after a
restart with the same key is therefore not deduplicated across that
restart).

## Approval replay protection

A provider switch requires a **revision-bound, expiring user approval**. The
approval token is bound to the exact revision it was issued against and
expires on a short lifetime; a later revision cannot accept a token issued
against an earlier one. This stops a recorded approval from being replayed
after the project has moved on.

## Network trust boundary

Studio enforces a strict origin allowlist for any provider it dials. The
rules below are the **SSRF** defense the merged
`comic_sol_web/generation/providers/http.py` transport policy implements:

- the origin **scheme** must be `https`, or `http` against a `loopback` host
  (for the agent route only);
- the origin **host** is validated as a literal string: `localhost`
  and a parsed literal IP in `127.0.0.0/8` are `loopback`; a parsed
  literal IP in private, link-local, reserved, multicast, unspecified,
  or metadata ranges is refused for hosted usage;
- the transport performs **no DNS resolution** of the configured host
  string before connecting. A configured `https://attacker.example/`
  that resolves (or later DNS-rebinds) to a private, link-local, or
  metadata IP is not stopped by the literal-IP check above — only
  literal-IP origins are checked. An operator who accepts the SSRF
  exposure must pin the configured host's resolved addresses
  out-of-band (for example, by a reverse-proxy allowlist or by
  binding DNS to known public addresses) before deploying;
- redirects are not followed (`follow_redirects=False`), so a 3xx cannot
  bounce Studio into a disallowed origin;
- the transport applies a **timeout** at every stage: connect, read, write,
  pool, and total; and a bounded **response byte** cap.

A cleartext `http` origin that is not loopback raises `invalid provider
origin`. A literal IP that falls in a private, link-local, or metadata
range is refused the same way; a hostname string that resolves (or
rebinds) into such a range after configuration is **not** refused at
the transport layer today.

## Archive and image trust boundary

A portable archive is decoded into a bounded, page-owned working area. The
decoder never executes archive contents, never follows symlinks inside the
archive, and **fails closed** on any unexpected member. A raster that is
imported is decoded and validated against a media-type sniff; the file is
never executed, and a mismatch between declared and sniffed media-type
fails closed.

## Credential modes and lifetime

Studio supports four credential modes, each with its own lifetime:

- **Agent** — credentials live in the agent session. Studio does not see
  them; they are never written to disk by Studio; their lifetime is the
  agent session.
- **Hosted** — credentials are server-side secrets configured by the
  operator. The browser never receives them; the operator may revoke a
  hosted credential by rotating the operator-configured secret.
- **Session BYOK** — you paste a credential for one session. It is held in
  memory only, used within a single studio session, and discarded at session
  end. The session lifetime is bounded to **one hour** at the most.
- **Encrypted persisted BYOK** — you authorize a credential that is encrypted
  at rest and kept for future sessions, under a key the operator rotates.
  Revocation is explicit: rotating the encryption key alone does **not**
  end the credential's use — `CredentialBroker.resolve()` re-encrypts the
  same provider credential under the new active key, leaving the database
  record and the upstream API token valid. The implemented Studio-side
  revocation path is `CredentialBroker.revoke(user_id, provider)`, which
  sets `revoked_at` on the persisted record. For a suspected compromise,
  revoke the credential both **provider-side first** (rotate, disable, or
  delete the upstream API key) and **Studio-side second**
  (`CredentialBroker.revoke(user_id, provider)`), then re-encrypt any
  remaining records under the new active key.

Every credential mode supports a documented revocation path and a
documented rotation path (where applicable: agent credentials are scoped to
the agent session and need no rotation; persisted BYOK is the canonical
rotation surface). A credential value is never logged, never echoed back,
and never written into a receipt.

## Receipts and redaction

Generation receipts are appended only when an accepted raster is
recorded. A receipt records what was produced (provider, model, auth
mode, sanitized usage, and the raster checksum) but **not** the wider
audit trail: failed attempts appear in the attempt history, and
provider-switch decisions live in the proposal and decision tables.
Receipts are not a transaction log for actions, revisions, statuses,
or error categories — those live in their own tables. Provider
responses are referenced by sanitized identifiers; raw provider
payload bytes never appear in a receipt, and any value that would
identify a credential is replaced with **`[REDACTED]`** before
persistence.

## Private story and artifact destinations

The story, prompts, plans, panels, and exports are private to the project
**owner**. Studio does not publish them, index them, or transfer them
outside the deployment the operator runs. Exports are private by default:
nothing is uploaded to a third-party destination, and any networked
destination must be an operator-configured, page-owned handle. Assets and
archives are never published, never uploaded, and never made public unless
an operator explicitly configures a destination and a user explicitly
confirms an overwrite to that destination.

**Generation inputs leave the deployment for the selected execution
route.** The agent route serializes the user prompt into the
provider-neutral handoff package (`web/comic_sol_web/generation/providers/agent.py`
includes `"prompt": request.prompt` and `"negative_prompt"` in the
package). Hosted and BYOK adapters, when wired, send the prompt and
the negative prompt in the external provider request body. The raw
provider payload bytes are not persisted in Studio, and an exported
archive or receipt does not carry the prompt, but the prompt **does**
leave the Studio deployment and reach the execution route the
operator configured. A Studio operator who cannot accept that
disclosure should not enable any generation route that hands a
prompt to an external process.

## Backup and incident expectations

Backups are the operator's responsibility, not the user's. The Web
distribution expects:

- periodic, **off-site** snapshots of the durable data volume;
- **rotation** of the encryption key on a documented schedule;
- a documented procedure to **revoke** a compromised credential, with
  explicit **revocation** recorded for every affected mode across all
  four credential modes;
- a documented **incident** response that names the contact, the response
  time, and the rollback path (see [Rollback and recovery](rollback.md)).

A suspected credential compromise is a security incident; the dedicated
private route in `SECURITY.md` is the only acceptable reporting channel,
and public issue trackers are never used for exploit detail.

## What this document does not claim

- Studio does not claim a security certification; no third-party audit has
  been completed in this work package.
- Studio does not claim a hosted deployment has been performed; the
  deployment status is "not deployed" (see [Deployment](deployment.md)).
- Studio does not claim the local ComfyUI or active-agent paths are
  security-qualified; those are experimental and not release surfaces.
