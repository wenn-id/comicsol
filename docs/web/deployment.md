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
separate scheduler, no background indexer, and no embedded database. The
process exposes the Web UI, the WebMCP surface, the local-MCP-equivalent HTTP
surface, and the generation queue.

Because there is only one process, the following are inherently
**process-local**:

- the generation **queue** (in-flight generation lives in this process);
- WebMCP session state and revision history;
- per-user session memory (within the process lifetime);
- in-memory rate-limit and approval caches.

**Horizontal scaling is not a release surface.** A second process would
have its own queue, its own sessions, and its own revision history, with no
shared coordination. Do not place Studio behind a load balancer that
distributes across processes; run a single instance per project or use the
agent-native handoff to coordinate work across machines.

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

- `COMIC_SOL_WEB_SESSION_SECRET` — server-side session signing key.
- `COMIC_SOL_WEB_ENCRYPTION_SECRET` — encryption key for at-rest secrets.
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

The proxy is expected to:

- present a valid certificate for the public hostname;
- restrict access to `/healthz` only as the operator sees fit;
- set conservative body-size limits and request timeouts;
- forward the original client IP and `X-Forwarded-Proto` so Studio can log
  accurately.

## Backup and restore

The data volume under `COMIC_SOL_WEB_DATA_ROOT` is the single source of
truth. Backup is performed by snapshotting that volume while the process
is either running (with a snapshot-aware filesystem) or stopped. Restore
replaces the volume with a known-good snapshot and starts the process
against it. A restore that targets a different encryption key requires the
persisted credentials to be re-encrypted under the key the snapshot
expects; key rotation covers that.

## Credential-key rotation

Credential keys are rotated on a documented operator schedule and on
demand after a suspected compromise. Rotation declares a new active key,
then re-encrypts each persisted BYOK credential under the new key as it is
next read. The active key is named by
`COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID`; the references are named by
`COMIC_SOL_WEB_CREDENTIAL_KEY_REFS`, and the active key must be one of the
declared references.

## Startup and shutdown

Startup fails closed: a missing or invalid environment variable stops the
process before it accepts a request. A successful start brings the queue
online and begins serving `/healthz` immediately.

Shutdown is graceful: in-flight generation is drained, the queue is
flushed to disk, and the process exits. A hard termination may lose the
in-memory half of the queue; the on-disk half is replayed on next start.
See [Rollback and recovery](rollback.md) for the recovery boundary.

## Rollback

A deployment rollback is a coordinated, documented operation; the
recovery boundaries are described in [Rollback and recovery](rollback.md).
No production rollback has been performed as part of this work package.

## Related documents

- [Rollback and recovery](rollback.md)
- [Security and privacy](security.md)
- [User guide](index.md)
