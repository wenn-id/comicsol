# Comic Sol Studio — deployment

This page covers how to operate Comic Sol Studio: the one-process runtime, the
durable data volume, the required environment secrets, the health endpoint,
TLS expectations, and the limits of a single-process deployment.

## Deployment status

**Not deployed.** No production deployment has been performed as part of
this work package; no external deployment URL is claimed. The guidance
below is the operator-facing contract for any future deployment, qualified
against the merged code, but not yet rehearsed against a live target.

## One-process runtime

Comic Sol Studio runs as **one process**. There is no separate worker, no
separate scheduler, and no background indexer. The process exposes the Web
UI, the WebMCP surface, the local-MCP-equivalent HTTP surface, and the
generation queue.

**Studio does embed a database.** `EngineGateway.open` creates
`application.sqlite3` under the data root and applies migrations
(`web/comic_sol_web/engine_gateway.py`), lazily, on the first authenticated
project request. `/healthz` deliberately does not touch it.

State therefore splits into two categories, and the distinction governs
backup, scaling, and recovery:

**Durable, SQLite-backed (survives process restart, must be backed up):**

- the **generation queue** — `DurableGenerationQueue` stores jobs in the
  `generation_jobs` table and claims them transactionally
  (`generation/queue.py`);
- **provider-switch approvals** — `ProviderSwitchApprovals` runs its own
  migrations against the same database (`generation/approvals.py`);
- **projects and their revisions** — owned by `EngineGateway` in the same
  database, plus the project files on the volume;
- **encrypted persisted BYOK credential ciphertext**.

**Genuinely process-local (lost on restart, never backed up):**

- the lazily constructed service objects cached on `app.state`
  (`projects`, `assets`, `generation`, `approvals`);
- FastAPI background tasks that are consuming queue jobs at the moment of
  termination;
- in-request rate-limit and per-request caches.

**Horizontal scaling is still not a release surface, but not because the
queue is in memory.** Jobs are leased with an expiry, so a second process
would contend for the same `generation_jobs` rows rather than owning an
independent queue. That contention is not a qualified configuration: it has
not been tested, and SQLite under concurrent writers from multiple
processes is not a supported deployment for this release. Run a single
instance, or use agent-native handoff to coordinate across machines.

## Durable data volume

Studio writes its data under a single absolute path: the
**`COMIC_SOL_WEB_DATA_ROOT`** environment variable, which must be an
**absolute** path on a **durable** filesystem. The data volume is the only
state that survives process restart; the rest is regenerated from the
project files on disk.

Backup the data volume. Restore by replacing the contents of the data volume
with a known-good snapshot; never edit a project file in place while the
process is running.

## Environment secrets

The configuration module (`web/comic_sol_web/config.py`) defines exactly six
environment variables. Three are **required** and fail process start if
missing or too short; three are **optional** reference declarations used only
when hosted or persisted-BYOK credential routing is configured. All are listed
below under the heading the configuration contract uses.

### Required at start

- `COMIC_SOL_WEB_SESSION_SECRET` — server-side session signing key
  (consumed by `comic_sol_web.auth.SessionAuthenticator`).
- `COMIC_SOL_WEB_ENCRYPTION_SECRET` — required by the configuration
  module and held in `WebConfig.encryption_secret`, but in the current
  build it is **not passed to `CredentialBroker`**; persisted BYOK
  ciphertext is encrypted with the key references named by
  `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS` (selected by
  `COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID`). Operators who back up or
  rotate encryption material must treat `CREDENTIAL_KEY_REFS` as the
  authoritative source; the unused `ENCRYPTION_SECRET` value should not
  be relied on as the at-rest encryption key until it is wired into the
  encryption boundary.
- `COMIC_SOL_WEB_DATA_ROOT` — absolute path to a durable data volume.

The two secret values must be at least **`32`** characters (the
configuration module enforces this minimum and rejects shorter values),
contain no whitespace or control characters, and are never logged, echoed,
serialized, or included in an error message. The data-root value must be an **absolute** path. Any invalid value
fails the process start with a diagnostic that names the variable but never
its value.

### Optional reference declarations

- `COMIC_SOL_WEB_HOSTED_SECRET_REFS` — comma-separated
  `provider=ENVIRONMENT_VARIABLE` declarations naming hosted credentials.
- `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS` — comma-separated
  `key_id=ENVIRONMENT_VARIABLE` declarations naming encryption keys.
- `COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID` — which declared encryption key is
  the active one (`required` whenever `CREDENTIAL_KEY_REFS` is declared).

These are only consulted when the matching feature is used; they are optional
for a session-only, agent-only, or hostless deployment.

## Health endpoint

Studio exposes a single, deterministic, in-process health endpoint:

- `GET /healthz` returns `{"status":"ok"}` with a 200 response.

The endpoint is a **liveness** probe only: it confirms the process is
running and able to respond. **There is no readiness endpoint** in this
release. Do not invent one or add one in a derivative deployment; relying
on `/healthz` for readiness would conflate "alive" with "ready to take
work" and would mask a half-started process.

## TLS and reverse proxy

TLS termination belongs to a reverse proxy in front of Studio. The proxy
**terminates** TLS and forwards plain HTTP to the Studio process. The
Studio process itself does not negotiate TLS and does not own certificates.

**Plain HTTP is trusted only on same-host loopback or a Unix socket.** The
proxy-to-Studio listener must not be reachable from an untrusted network:
bind it to loopback or use a Unix socket, firewall the port, and do not
publish it as a second public endpoint. If the proxy and Studio run on
different hosts, plain HTTP is not an acceptable transport; use authenticated
encryption (for example, mTLS or an equivalent private encrypted tunnel)
and restrict both ends to the intended peers.

The proxy is expected to:

- present a valid certificate for the public hostname;
- restrict access to `/healthz` only as the operator sees fit;
- set conservative body-size limits and request timeouts;
- forward the original client IP and `X-Forwarded-Proto` so Studio can log
  accurately.

## Backup and restore

The data volume under `COMIC_SOL_WEB_DATA_ROOT` is the primary state.
Backup is performed by snapshotting that volume while the process
is either running (with a snapshot-aware filesystem) or stopped. Restore
replaces the volume with a known-good snapshot and starts the process
against it.

**The data volume is not the sole source of truth for persisted encrypted
BYOK credentials.** Ciphertext is stored in SQLite under the volume, but
its master keys are read from **external environment variables** named by
`COMIC_SOL_WEB_CREDENTIAL_KEY_REFS`. If those key values are absent after a
restore, `CredentialBroker` cannot derive the cipher that decrypts the
stored ciphertext. A restore must therefore:

1. snapshot the data volume as above;
2. **retain the external key material** (the environment values behind
   `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS`) in the same release/backup unit as
   the volume snapshot;
3. keep the **prior key configured as decrypt-only** until every restored
   credential has been re-encrypted under the new active key (rotation
   below), rather than pointing restore at a new key first.

Restoring the volume alone, or targeting a different key without first
rotating through the old cipher, is insufficient and silently yields
undecryptable credentials.

## Credential-key rotation

Credential keys are rotated on a documented operator schedule and on
demand after a suspected compromise. Rotation declares a new active key,
then re-encrypts each persisted BYOK credential under the new key as it is
next read. The active key is named by
`COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID`; the references are named by
`COMIC_SOL_WEB_CREDENTIAL_KEY_REFS`, and the active key must be one of the
declared references.

**Rotation is gradual and read-time, not a bulk rewrite.** Ciphertext is
re-encrypted only when a credential is next read. Until every stored
credential has been re-read and re-encrypted, the **previous key must remain
decrypt-only** in `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS`; removing it early
makes the remaining ciphertext undecryptable. See
[security.md](security.md) "Credential modes" and the recovery boundaries in
[rollback.md](rollback.md).

## Startup and shutdown

Startup fails closed: a missing or invalid environment variable stops the
process before it accepts a request. A successful start brings the queue
online and begins serving `/healthz` immediately.

**There is no coordinated graceful-shutdown drain.** The merged
`web/comic_sol_web/app.py::create_app` registers no lifespan or shutdown
handler that drains in-flight generation or flushes a queue, so an operator
cannot rely on those steps during deployment or rollback. Recovery is
instead provided by design:

- the generation queue is SQLite-backed through `GenerationStore`, so the
  durable half survives process restart without a replay hook;
- in-flight work is protected by **expiring leases** that recover on next
  start rather than by an explicit drain step.

A hard termination loses only the in-memory half of the queue; the
SQLite-backed half is authoritative on restart. The absence of a drain
handler is a documented limitation, not a promise. See
[Rollback and recovery](rollback.md) for the recovery boundary.

## Rollback

A deployment rollback is a coordinated, documented operation; the
recovery boundaries are described in [Rollback and recovery](rollback.md).
No production rollback has been performed as part of this work package.

## Related documents

- [Rollback and recovery](rollback.md)
- [Security and privacy](security.md)
- [User guide](index.md)
