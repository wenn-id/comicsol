# Release rollback and yank runbook

Status: **operational runbook** for orchestration states 8 and 9 of
[`v2.0-stable-criteria.md`](v2.0-stable-criteria.md)

Immutable releases cannot be rewritten — that is the control being protected.
Rollback and withdrawal therefore work by **adding evidence, never replacing
bytes**: the tag, assets, Sigstore signature, attestations, and the locked
prerelease field of the affected release stay exactly as published, and every
action below appends a public record explaining what changed operationally.

The same rule applies in both directions: never delete the tag, never re-upload
or "fix" an asset under the same name, and never rebuild the old version as a
new candidate of the same version number. A replacement is always a **fresh
version and a fresh tag** through the normal release workflow.

## 1. Withdrawal (yank) of a published candidate

Use when a published candidate must stop being recommended (for example: a
qualification failure discovered after publication, or a security report).

1. **Record the incident.** Open an issue (or use the private vulnerability
   report while the detail is embargoed per `SECURITY.md`) and note the release
   tag, the candidate commit from `candidate-identity.json`, and the reason.
2. **Capture the evidence before any mutation.** Download and preserve, outside
   the repository if necessary: `candidate-identity.json` and its sidecar,
   `SHA256SUMS`, `SHA256SUMS.sigstore.json`, the qualification summaries, and
   the promotion `release-evidence.{json,md,sha256}` if promotion already ran.
   These remain downloadable from the immutable release and the run artifacts —
   the copy exists so the record survives even a later release-entry removal.
3. **Edit only the title and notes.** The release API still permits title and
   body edits on an immutable release:
   ```bash
   gh api "repos/wenn-id/comicsol/releases/tags/vX" --jq .id   # capture the release id
   gh api "repos/wenn-id/comicsol/releases/<id>" --method PATCH \
     -f name="Comic Sol X — WITHDRAWN" \
     --raw-field body="$(cat withdrawn-notes.md)"
   ```
   `withdrawn-notes.md` must state: the reason, the incident link, that all
   bytes remain published for audit, that the tag is not reused, and which
   version users should install instead (or that none exists yet).
4. **If mere annotation is insufficient**, remove the public release entry while
   retaining the immutable evidence:
   ```bash
   gh api "repos/wenn-id/comicsol/releases/<id>" --method DELETE
   ```
   This hides the release page; **it must never be accompanied by tag deletion**
   (`git push --delete` / ruleset bypass). The protected tag keeps pointing at
   the candidate commit so the attestations, the Sigstore identity, and the run
   evidence remain verifiable against it forever.
5. **Mark the deployment state** if promotion had already happened: set the
   `release-production` deployment for that commit to `blocked`/`inactive`
   through an API status that links the incident, so the audit trail shows the
   production decision was reversed after approval.
6. **Never claim registry effects.** This project does not publish PyPI or OCI
   registry payloads (see the trust chain document's OCI decision); a withdrawal
   statement must not imply those systems were yanked.

## 2. Rollback of a production promotion

Use when the last promoted release is bad and users must be directed back to the
previous qualified release.

1. Follow evidence-capture steps 1–2 above for the bad release.
2. Patch **only** the title and notes of the bad release to say
   `ROLLED BACK`, linking the incident, the preserved evidence, and the exact
   previous tag users should return to.
3. Set the `release-production` deployment for the bad candidate commit to
   `blocked`/`inactive` with the incident link.
4. Verify the previous release is still intact and verifiable before naming it:
   ```bash
   gh release download vPrevious --dir /tmp/check --repo wenn-id/comicsol
   cd /tmp/check && sha256sum -c SHA256SUMS   # spot-check at least the target platform archive
   cosign verify-blob --bundle SHA256SUMS.sigstore.json \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     --certificate-identity-regexp '^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/tags/v' \
     SHA256SUMS
   ```
5. Direct users to reinstall the previous verified archive with the documented
   installer commands in [`docs/install.md`](../install.md) — the installer's
   transactional upgrade/rollback handles the local runtime swap.
6. Record the rollback in the release issue: tag, commit, deployment status
   change, evidence links, and the decision text
   (`Decision: ROLLED BACK`) from the criteria checklist.

## What each action deliberately cannot do

- **Rebuild or replace `vX` bytes**: forbidden; a retry that changes published
  bytes requires a fresh version and tag (criteria state 3).
- **Change the prerelease flag or assets**: forbidden; immutable-release
  locking is the property that makes all other evidence binding.
- **Reuse the tag after deletion**: forbidden; the tag ruleset restricts
  updates and deletions, and attestations for a reused tag would be ambiguous.
- **Yank external registries**: out of scope because nothing is published
  there; state that plainly rather than implying coverage.

## User-side rollback

User-facing rollback (reinstalling the previous verified archive locally) is
documented in [`docs/install.md`](../install.md#upgrade-and-rollback) and is
separate from this repository-side runbook.
