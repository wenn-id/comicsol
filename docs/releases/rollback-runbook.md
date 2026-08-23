# Release rollback and yank runbook

Status: **operational runbook** for orchestration states 8 and 9 of
[`v2.0-stable-criteria.md`](v2.0-stable-criteria.md)

Immutable releases cannot be rewritten — that is the control being protected.
Rollback and withdrawal therefore work by **adding evidence, never replacing
bytes**: the tag, assets, Sigstore signature, attestations, and the locked
prerelease field of the affected release stay exactly as published, and every
action below appends a public record explaining what changed operationally.

The same rule applies in both directions: never delete the signed annotated tag,
never change its captured tag-object SHA, never re-upload or "fix" an asset
under the same name, and never rebuild the old version as a new candidate of
the same version number. A replacement is always a **fresh version and a fresh
signed annotated tag** through the normal release workflow. The active matching tag rulesets must collectively restrict creation, updates,
and deletions and enable `required_signatures` throughout incident handling.
Their only approved bypass actor is repository admin
(`actor_type=RepositoryRole`, `actor_id=5`, `bypass_mode=always`), and every
matching ruleset containing `creation` must contain exactly that actor; write or
maintain roles, teams, integrations, and pull-request-only bypasses are not
trusted release authority.

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
4. **If annotation is insufficient, escalate — never delete the release yourself.**
   Deleting an immutable release removes GitHub's immutable-release binding between
   that release and its tag, which re-enables tag deletion that only the separately
   configured tag ruleset then prevents. Removing a public release entry is therefore
   an administrator-only escalation: confirm the active `refs/tags/v*` ruleset
   restricting updates and deletions is enforced before and after the action, perform
   the removal under that recorded review, and capture the API audit trail. Before
   and after it, verify that the matching rulesets collectively restrict
   creation, updates, and deletions, enable `required_signatures`, and contain
   no bypass actor other than repository admin (`RepositoryRole`, actor ID `5`,
   mode `always`), with every matching ruleset containing `creation` granting
   exactly that actor. The signed
   annotated tag itself is never deleted or recreated, so its captured tag-object SHA,
   attestations, Sigstore identity, direct tag-object SHA and peeled target commit, and run evidence remain
   verifiable against the same identity. Run the policy validator from a fresh
   evidence directory immediately before and after the separately reviewed
   administrator action. Both invocations must exit zero; retain both evidence
   trees with the incident and API audit record.
   ```bash
   set -euo pipefail
   evidence_root="release-removal-rulesets-vX"
   mkdir "$evidence_root"

   capture_and_validate_tag_rulesets() {
     local phase="$1"
     local phase_dir="$evidence_root/$phase"
     test ! -e "$phase_dir"
     mkdir -p "$phase_dir/full"

     gh api "repos/wenn-id/comicsol/rulesets?includes_parents=true&per_page=100" \
       > "$phase_dir/index.json"
     jq -r '.[] | select(.target == "tag" and .enforcement == "active") | .id' \
       "$phase_dir/index.json" | while read -r ruleset_id; do
         gh api "repos/wenn-id/comicsol/rulesets/${ruleset_id}" \
           > "$phase_dir/full/${ruleset_id}.json"
       done
     python3 scripts/release_identity.py rulesets \
       --rulesets-dir "$phase_dir/full" \
       --release-ref "refs/tags/vX" | tee "$phase_dir/validation.json"
   }

   capture_and_validate_tag_rulesets before
   # An administrator performs the separately reviewed release-entry removal.
   capture_and_validate_tag_rulesets after
   ```
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
- **Reuse the tag after deletion**: forbidden; the signed annotated tag's object
  SHA is immutable, and the ruleset restricts creation, updates, and deletions
  and requires signatures. Recreating even a tag that peels to the same commit
  creates a different tag object and breaks the recorded identity.
- **Yank external registries**: out of scope because nothing is published
  there; state that plainly rather than implying coverage.

## User-side rollback

User-facing rollback (reinstalling the previous verified archive locally) is
documented in [`docs/install.md`](../install.md#upgrade-and-rollback) and is
separate from this repository-side runbook.
