# Comic Sol Studio — rollback and recovery

This page documents how an operator recovers a Comic Sol Studio deployment
when something goes wrong: a bad release, a corrupted data volume, a
suspected credential compromise, or a downstream provider-side failure.

> **What has actually happened**
>
> **No production rollback has been performed** as part of this work package.
> No real rollback drill has been run against a deployed target, and the
> recovery steps below are the merged-code contract — what the runtime is
> actually capable of — not a record of an executed drill. A future
> deployment that wants to qualify these steps must rehearse them against
> a non-production target and record the result; until that is done the
> steps remain **described, not exercised**.

## Rollback

A deployment rollback is the operation of stopping the current Studio
process and starting a previous, known-good build against the existing
data volume. The data volume under `COMIC_SOL_WEB_DATA_ROOT` is preserved
across rollback, so:

- **durable, SQLite-backed** project state, generation history, and
  persisted BYOK credentials survive;
- **in-memory / process-local** state does **not** survive: all
  process-local `app.state` caches are recreated on restart — the
  `AuthService` object, the cached `generation` service, the cached
  `generation_credentials` broker, and any session-BYOK state held by
  that broker — and any FastAPI background task that was consuming a
  job at the moment of termination is lost. The in-memory half of the
  generation queue (if any in-flight lease was mid-step) is also lost.
  Authenticated **session records** are themselves persisted in the
  SQLite `sessions` table and survive a rollback that retains the data
  volume (see the dedicated bullet below);
- the **on-disk (SQLite-backed) generation queue is not "replayed"**.
  `web/comic_sol_web/app.py::create_app` registers **no** `lifespan` or
  `shutdown` handler that drains, flushes, or replays interrupted work.
  Recovery is by design: `DurableGenerationQueue` stores jobs
  transactionally and `expired-running` leases are reclaimed on the
  **next queue-consumer lease attempt** (`lease_next()`), not by a
  startup or lifespan recovery handler — `create_app` registers no
  such handler. A panel whose `running` lease expired because the
  process was killed will be re-claimed on the next poll; an
  in-memory half that was mid-step is not recoverable.
- **authenticated sessions are persisted, not invalidated.** A rollback
  that retains the data volume and the session secret keeps the
  SQLite `sessions` rows intact, and `AuthService.authenticate_token()`
  reads them back after restart; only the `AuthService` object on
  `app.state` is process-local. A runbook that needs to log users out
  as part of the rollback must therefore call
  `AuthService.revoke(session_token)` explicitly, rotate
  `COMIC_SOL_WEB_SESSION_SECRET`, or both — the rollback itself does
  not invalidate any session.

Before any rollback, snapshot the data volume. After the rollback, run
the full [Verification](#verification) checklist. **`/healthz` is a
liveness probe only**; it confirms the process is up, not that it is
ready to serve traffic. Do **not** start serving traffic on `/healthz`
alone — run the deployment-specific readiness checks (port reachable,
data root mounted, environment secrets valid) before opening ingress.

## Restore from backup

If the data volume is corrupted or lost, restore from the most recent
known-good snapshot. The procedure is:

1. Stop the Studio process.
2. Replace the contents of `COMIC_SOL_WEB_DATA_ROOT` with the snapshot.
3. Verify the snapshot retains every key id listed in the prior
   `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS` value, including the active key
   the snapshot was encrypted under. Without the source key,
   `CredentialBroker.resolve()` raises `CredentialKeyUnavailableError`
   before any decrypt or re-encrypt step can run; automatic re-encryption
   is therefore not promised. If the source key is unavailable, the
   operator must require credential re-entry and revoke the affected
   persisted credentials before the new process can serve traffic.
4. Start the Studio process against the restored data volume.
5. Run [Verification](#verification).

A restore that re-keys the encryption material is **not** equivalent to a
restore that keeps it; document which key was active at snapshot time
before you commit to a restore.

## Credential-key rotation and revocation

A suspected credential compromise is a security incident. The recovery
steps differ by credential mode; record the mode and the time the
incident was detected before starting.

1. **Identify the affected mode.** The four credential modes (agent,
   hosted, session BYOK, encrypted persisted BYOK) have different blast
   radii; agent and session BYOK credentials are scoped to a session and
   expire naturally, while hosted and persisted BYOK credentials persist
   until the operator revokes them.
2. **Revoke the affected credential at every layer it lives in.**
   - **Provider side first, where supported.** Hosted credentials
     (`COMIC_SOL_WEB_HOSTED_SECRET_REFS`) live on the operator's side
     of the proxy and are managed by a provider dashboard. Rotate,
     disable, or revoke the upstream secret there; do not rely on a
     Studio-side delete alone. Persisted BYOK credentials may also
     have provider-side state (e.g. an API key with usage logs) that
     must be rotated or revoked at the provider.
   - **Studio side second.** For hosted credentials, redeploy with
     the new operator secret. For persisted BYOK, call
     `CredentialBroker.revoke(user_id, provider)` (which sets
     `revoked_at` and clears the session state); do not call a delete
     method — `CredentialBroker` does not expose one. For **session
     BYOK, ending the Studio session is not revocation**: the broker
     keys session credentials by `(user_id, provider)` with an
     independent TTL (`credentials.py:238-258`), while
     `AuthService.revoke()` only deletes the SQLite `sessions` row
     (`auth.py:300-305`). The credential stays resolvable after
     logout — and to a fresh login by the same user — until its TTL
     expires. Call `CredentialBroker.revoke(user_id, provider)` for
     session BYOK too.
   - **Agent side, concretely.** Agent credentials live in the
     agent session and are not retrievable from Studio. End the
     external agent session by terminating the agent process (or
     revoking its session token) on the user's machine; do not rely
     on Studio to "contain" it.
3. **Re-encrypt what can be re-supplied; revoke what cannot. There is
   no implemented purge.** Suspected compromise does not wait for lazy
   rotation, but the merged `CredentialBroker` exposes **no delete or
   purge operation** — `revoke(user_id, provider)`
   (`credentials.py:296-324`) sets `revoked_at` and drops the session
   entry, and the ciphertext row **stays in the `credentials` table
   under its original `key_id`**. For each affected record:
   - if the plaintext can be re-supplied (the user re-enters the
     BYOK or re-issues the provider key), re-authorize it: the
     upsert path re-encrypts under the current active key and clears
     `revoked_at`;
   - if the plaintext **cannot** be re-supplied (the user is
     unavailable, the key is lost, or the record cannot be safely
     verified), call `CredentialBroker.revoke(user_id, provider)` and
     rotate or delete the credential **at the provider** — that is
     the only step that actually ends the upstream token's usefulness.
     Do not rely on a "decrypt on next read" path; that path itself
     is a compromise surface.
4. **Retire the old key only when no record still needs it, and
   accept that revoked ciphertext keeps its old key.** Because
   `revoke()` does not remove ciphertext or re-key it, a revoked
   record's row remains decryptable-in-principle under the key id it
   was written with. The old key must therefore stay declared in
   `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS` for as long as any row —
   revoked or active — still carries that `key_id`, unless the
   operator removes those rows out-of-band at the database level
   (which is an operator action, not a documented Studio API). The
   condition for dropping the old key is *no remaining row carries
   that `key_id`*, not a wall-clock window. Removing the old key
   while rows still reference it makes that ciphertext permanently
   undecryptable; that is acceptable only if the operator has
   decided those records are to be abandoned, and it should be a
   recorded decision rather than an accident.
5. **Audit the right table for the right fact.** Generation
   **receipts** are appended only for an accepted raster and carry
   only provider, model, auth mode, sanitized usage, and the raster
   checksum (see [Security and privacy](security.md#receipts-and-redaction)),
   so they show only *successful* generations under the affected
   credential. Failed attempts must be audited from the attempt
   history, and provider-switch decisions from the proposal and
   decision tables. Receipts are not an action log and cannot answer
   "what was attempted" on their own.

## Incident response

A Studio incident follows the published `SECURITY.md` private reporting
route. The expected response procedure is:

- the operator stops the affected process or routes it offline;
- the data volume is preserved and snapshotted for forensics;
- if the incident is credential-related, see
  [Credential-key rotation and revocation](#credential-key-rotation-and-revocation);
- if the incident is data-related, see [Restore from backup](#restore-from-backup);
- if the incident is deployment-related, see [Rollback](#rollback);
- a public issue tracker is never used for exploit detail; only the
  private route in `SECURITY.md` is acceptable.

## What rollback cannot recover

Rollback and restore are powerful, but they have hard limits. The
following are **not recoverable by rollback**:

- **in-flight generation** that was lost when the process was stopped
  is not restored; the panel state is preserved, but the work the
  in-flight task was doing is not. Do **not** queue a replacement job
  for it: `DurableGenerationQueue.lease_next()` selects expired
  `running` rows and reclaims them while retries remain, so the
  persisted job recovers on the next poll. Jobs that have exhausted
  their retries (`retry_count >= max_retries`) or reached a terminal
  state are **not recoverable** — neither `/retry` nor `/queue` reopens
  them. The only recovery path is to start a new generation request
  from the same project revision. Enqueuing a duplicate of an active
  job causes duplicate generation and duplicate provider spend.
- **provider-side state** (model deprecation, billing changes, account
  suspension, provider outages) is outside Studio. Rolling back Studio
  does not roll back the provider.
- **already-spent** provider budget. A generation that completed and
  consumed budget is spent whether Studio is rolled back or not.
- **revoked credential** state for credentials the user has not provided
  again. A rolled-back instance cannot recreate a credential that was
  revoked; the user must authorize it again.

## Verification

After any rollback, restore, or rotation, the following should pass:

- `GET /healthz` returns `{"status":"ok"}` with a 200 response.
- the documented test suite passes against the restored volume (see
  the verification log in the submission).
- the process answers on its documented port and the reverse proxy in
  front of it reports healthy.

A failure on any of these is a recovery failure, not a successful
rollback.

## Related documents

- [Deployment](deployment.md)
- [Security and privacy](security.md)
- [User guide](index.md)
