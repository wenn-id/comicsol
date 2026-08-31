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
- **in-memory / process-local** state does **not** survive: sessions
  (the authenticated session object lives on `app.state`), the
  in-memory half of the generation queue, and any FastAPI background
  task that was consuming a job at the moment of termination are lost;
- the **on-disk (SQLite-backed) generation queue is not "replayed"**.
  `web/comic_sol_web/app.py::create_app` registers **no** `lifespan` or
  `shutdown` handler that drains, flushes, or replays interrupted work.
  Recovery is by design: `DurableGenerationQueue` stores jobs
  transactionally and `expired-running` leases are recovered on the
  next process start rather than by an explicit replay step. A panel
  whose `running` lease expired because the process was killed will be
  re-claimed by the next consumer; an in-memory half that was mid-step
  is not recoverable.

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
3. Re-encrypt any persisted credentials that were written under a key
   the snapshot does not include; the credential-key rotation logic in
   `CredentialBroker` re-encrypts a credential under the active key the
   next time it is read.
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
     the new operator secret. For persisted BYOK, call the credential
     storage interface to delete the record. For session BYOK, end
     the affected session.
   - **Agent side, concretely.** Agent credentials live in the
     agent session and are not retrievable from Studio. End the
     external agent session by terminating the agent process (or
     revoking its session token) on the user's machine; do not rely
     on Studio to "contain" it.
3. **Re-encrypt every affected persisted BYOK record eagerly, not
   read-time.** Suspected compromise does not wait for lazy rotation.
   For each affected record:
   - if the plaintext can be re-supplied (the user re-enters the
     BYOK or re-issues the provider key), re-encrypt it under the
     new active key and persist immediately;
   - if the plaintext **cannot** be re-supplied (the user is
     unavailable, the key is lost, or the record cannot be safely
     verified), **delete and revoke** the record rather than
     leaving it under the suspect key. Do not rely on a "decrypt
     on next read" path; that path itself is a compromise surface.
4. **Retire the old key only after every reachable record has
   moved.** Keep the old key declared in
   `COMIC_SOL_WEB_CREDENTIAL_KEY_REFS` until a documented audit
   confirms that no remaining ciphertext is encrypted under it.
   The retirement condition is *zero remaining records under the
   old key*, not a wall-clock window. Removing the old key
   before the audit is a recovery failure, not a precaution.
5. **Audit receipts** for the affected owner and window. Receipts
   are sanitized (see [Security and privacy](security.md)) and
   never contain a raw provider payload, but they do show which
   actions took place under the affected credential.

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
  persisted job recovers on the next poll. Manual requeueing is
  reserved for jobs that have exhausted their retries or reached a
  terminal state — enqueuing a duplicate otherwise causes duplicate
  generation and duplicate provider spend.
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
