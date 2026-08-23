# Release subject set and trust chain

Status: **authoritative provenance reference**

This document defines the complete set of subjects published by a Comic Sol
release and the chain of trust that binds them to one immutable tag, commit,
workflow run, and set of bytes. It is the reference for
[`v2.0-stable-criteria.md`](v2.0-stable-criteria.md) section 5; where the two
disagree, the criteria win and this file must be fixed. The installer-facing
verification commands live in [`docs/install.md`](../install.md).

## Release subject set

Every published release asset is either a **payload** named by the signed
checksum manifest or **supporting evidence** that stands outside it so its own
digest cannot create a cycle. For version `X` and tag `vX`:

| Subject | Role | Binding |
| --- | --- | --- |
| `comic-sol-X-linux-x86_64.zip` | Native archive | `SHA256SUMS` entry + build-provenance attestation |
| `comic-sol-X-linux-x86_64.metadata.json` | Native metadata | `SHA256SUMS` entry + attestation |
| `comic-sol-X-linux-x86_64.sbom.json` | CycloneDX 1.6 SBOM | `SHA256SUMS` entry + attestation |
| `comic-sol-X-macos-arm64.zip` | Native archive | `SHA256SUMS` entry + attestation |
| `comic-sol-X-macos-arm64.metadata.json` | Native metadata | `SHA256SUMS` entry + attestation |
| `comic-sol-X-macos-arm64.sbom.json` | CycloneDX 1.6 SBOM | `SHA256SUMS` entry + attestation |
| `comic-sol-X-windows-x86_64.zip` | Native archive | `SHA256SUMS` entry + attestation |
| `comic-sol-X-windows-x86_64.metadata.json` | Native metadata | `SHA256SUMS` entry + attestation |
| `comic-sol-X-windows-x86_64.sbom.json` | CycloneDX 1.6 SBOM | `SHA256SUMS` entry + attestation |
| `comic_sol-X-py3-none-any.whl` | Source wheel | `SHA256SUMS` entry + attestation |
| `comic-sol-X.tar.gz` | Source sdist | `SHA256SUMS` entry + attestation |
| `comic-sol-X-linux-x86_64.container.tar` | OCI image (Docker `save`) | `SHA256SUMS` entry + attestation |
| `installers/install.sh` → `install.sh` | POSIX installer | `SHA256SUMS` entry + attestation |
| `installers/install.ps1` → `install.ps1` | Windows installer | `SHA256SUMS` entry + attestation |
| `SHA256SUMS` | Signed checksum manifest | Sigstore keyless signature (`SHA256SUMS.sigstore.json`) + digest recorded in `candidate-identity.json` |
| `SHA256SUMS.sigstore.json` | Signature bundle | Digest recorded in `candidate-identity.json` |
| `candidate-identity.json` | Identity record: tag, version, signed annotated tag-object SHA, candidate commit, captured protected-main SHA, canonical matching ruleset IDs, approved bypass authority, run URL, Actions artifact digests, manifest/signature/payload digests | Sidecar `candidate-identity.json.sha256` + build-provenance attestation under the release workflow identity; re-downloaded, provenance-verified, and compared byte-for-byte at qualification and promotion |
| `candidate-identity.json.sha256` | Identity digest sidecar | Uploaded as a release asset; verified on every re-download |

Nothing else is a release asset. A release whose asset set differs from this
table fails qualification and promotion, which compare the published set against
`candidate-identity.json` exactly.

## Trust chain

Trust roots are public and pinned: the GitHub repository (`wenn-id/comicsol`),
the `release.yml` workflow OIDC identity, protected `main`, the signed annotated
tag object, and every active ruleset matching the exact release tag. The
workflow applies GitHub-compatible pathname matching (`*` does not cross `/`;
`**` may), applies exclusions first, and aggregates required rule types across
all matches. The effective policy must restrict creation, updates, and
deletions and enable `required_signatures`. Its **only trusted release-creation
authority** is the repository-admin role bypass identity
`actor_type=RepositoryRole`, `actor_id=5`, `bypass_mode=always`. Write or
maintain roles, teams, integrations, pull-request-only bypasses, malformed
actors, malformed patterns, and unsupported patterns all fail closed. Every
matching ruleset containing `creation` must name exactly that approved authority.
Verification walks the chain in this order, and every step is fail-closed:

1. **Tag, commit, and protected main.** The release run proves `github.ref` is exactly
   `refs/tags/vX`, requires a signed annotated tag, and obtains that exact tag
   object from the GitHub API. The API object SHA and target commit must equal
   the locally captured direct tag-object SHA and peeled commit, and its
   signature metadata must be present and verified. The triggering SHA and
   checkout identify the same candidate, the package version equals `X`, and
   the candidate must be an ancestor of a freshly fetched protected `main`.
   An active set of tag rulesets must collectively restrict creation, updates,
   and deletions and enable `required_signatures`; the canonical matching IDs
   and approved repository-admin bypass identity are captured as candidate
   evidence. At every mutation boundary, the workflow reads
   both remote refs: the direct ref must remain the captured tag-object SHA and
   the peeled ref must remain the candidate commit. A deleted, lightweight,
   moved, or same-commit/replaced tag therefore fails closed. Every draft,
   published, qualification, and promotion-boundary Release read must retain exact
   `tag_name=vX` and freshly prove the captured direct tag-object SHA and peeled
   candidate commit through remote refs. GitHub may report
   `target_commitish=main` for a Release whose tag already exists; that raw field
   is non-authoritative and is never used as target evidence.
2. **Workflow identity.** The run holds `id-token: write` only in the jobs that
   attest or sign, and both verifiers pin the certificate identity regexp
   `^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/tags/v…$`
   plus the issuer `https://token.actions.githubusercontent.com`.
3. **Payload attestations.** `actions/attest-build-provenance` attests every
   subject named by `SHA256SUMS` — each native archive, its metadata and SBOM,
   the wheel, the sdist, the container tar, and both installers — with the
   subject digest taken from the manifest itself, so an attestation can never
   cover bytes the manifest does not name.
4. **Signed manifest.** `cosign sign-blob` keyless-signs `SHA256SUMS` under the
   same workflow identity. Verifiers check the bundle, the issuer, the identity
   regexp, and the tag-shaped ref together; the installers refuse to proceed
   until this passes.
5. **Identity record.** `candidate-identity.json` binds the signed annotated
   tag-object SHA, tag, version, candidate commit, captured protected-main SHA,
   workflow run URL, canonical matching ruleset IDs, the approved
   repository-admin bypass identity, the current run's Actions artifact IDs
   and service digests, the manifest digest, the signature bundle digest, and
   every payload digest in one signed-run-produced record with its own digest
   sidecar. The workflow separately attests that identity record under the
   release workflow's exact source commit and tag, so manual qualification
   cannot substitute a caller-authored policy record with a matching sidecar.
6. **Qualification.** The qualification workflow downloads the published
   candidate and verifies, together: the candidate identity's tag, tag-object
   SHA, candidate commit, and protected-main SHA against its inputs; current
   protected-main ancestry; exact remote direct and peeled refs; the Release
   state `draft=false`, `prerelease=true`, and `immutable=true` on both normal
   and manual dispatch paths; exact Release `tag_name` plus its target resolved
   from the captured direct and peeled refs in both identity and aggregate jobs; authenticated
   build-provenance for `candidate-identity.json` under the release workflow's
   tag and source commit; the manifest and signature-bundle digests against the
   identity record; the Sigstore bundle; every payload checksum; a
   build-provenance attestation for the wheel, the sdist, each native archive,
   each metadata file, each SBOM, both installers, and the container tar — each
   checked for signer workflow, source digest (the exact candidate commit), and
   source ref (`refs/tags/vX`) with self-hosted runners denied; the metadata and
   SBOM identity contracts; and the container bytes by loading and running them.
7. **Promotion evidence.** After approval by a configured reviewer other than
   the triggering actor through the protected `release-production` Environment,
   promotion re-downloads every byte, compares them to the candidate identity,
   freshly revalidates the exact Release `tag_name`, remote tag-object SHA, and
   peeled candidate commit at each promotion boundary, binds the
   benchmark and qualification summaries to the reusable-workflow output
   digests, records the deployment identity, and attests the resulting
   `release-evidence.{json,md}` digests under the release workflow identity.

An external verifier can reproduce steps 4–6 with public data alone: the
`cosign verify-blob` command in [`docs/install.md`](../install.md), the
`SHA256SUMS` digest lines, and `gh attestation verify <asset> --repo
wenn-id/comicsol --signer-workflow wenn-id/comicsol/.github/workflows/release.yml`.

## OCI distribution decision

**OCI is an official distribution channel, delivered as the attested
`comic-sol-X-linux-x86_64.container.tar` release asset — not (yet) as a
registry image.** The release workflow builds the image once from the locked
source and the digest-pinned base (`DOCKER_BASE_DIGEST`), smokes it with
`comic-sol doctor`, exports it with `docker save`, and publishes the tar as a
payload: it is named by the signed manifest, covered by a build-provenance
attestation, and qualified by `docker load` plus `--version`/`doctor` runs
against the downloaded bytes. Publishing "by digest" is therefore the
`SHA256SUMS` entry plus the attestation, and the signing story is the Sigstore
bundle over that manifest.

A registry distribution (for example `ghcr.io`) would additionally require:
push-by-digest with `packages: write` scoped to the release job alone, a
registry-side SBOM and vulnerability scan as blocking gates, `cosign sign` and
`cosign attest` under the same workflow identity, qualification that verifies
the pushed digest against the manifest, and rollback/yank wording for registry
tags. Until those exist, no release may claim a registry image, and
`docs/install.md` documents the tar as the official OCI form. Revisiting this
decision is a deliberate change to this document plus the criteria, not a
workflow edit.

## Installer bootstrap verification

The installers are themselves release payloads, so no user should execute
installer code before it is externally verified. The complete pre-execution
procedure — download the installer from the release (not a branch), verify the
Sigstore bundle over `SHA256SUMS`, confirm the installer's digest appears in
the signed manifest, optionally verify its build-provenance attestation with
`gh attestation verify`, and only then run it — is documented for both
`install.sh` and `install.ps1` in [`docs/install.md`](../install.md#verify-installer-bytes-before-first-execution).

## Dependency-lock provenance

Every hash lock in `requirements/locks/` is generated from exactly one
canonical input file in `requirements/`, and each lock header records its input
and regeneration command. The per-platform regeneration procedure and the
required cross-platform diff review live in
[`requirements/README.md`](../../requirements/README.md).
