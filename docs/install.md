# Install Comic Sol v2.0.0rc4

> `v2.0.0rc6` is prepared in the repository but **not yet published**. These instructions name
> `v2.0.0rc4`, the latest published prerelease, because those are the archives that exist.
> They are updated to name `v2.0.0rc6` when that tag is published.

Comic Sol `v2.0.0rc4` is a prerelease distributed as native portable archives for Linux, macOS, and Windows, plus a Python wheel/source archive and an OCI image definition. Its published macOS filename is historically mislabeled, as explained below. The current native archive matrix is Linux x86_64, macOS arm64, and Windows x86_64. WSL2 uses the Linux x86_64 archive; it has no separate native archive. Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+. Intel macOS is source-install-only; it has no native archive. Native archives bundle Python 3.11, Pillow, MCP, fonts, templates, the Skill, and references; no system Python is required after extraction.

> **First time installing Comic Sol?** Start with
> [`docs/onboarding.md`](onboarding.md) instead. It is one short path from install
> to a first comic. This page is the complete distribution reference: archives,
> checksum verification, upgrade, rollback, uninstall, and containers.

## Recommended companion: Superpowers

For structured brainstorming, planning, debugging, and verification workflows,
we recommend installing [Superpowers](https://github.com/obra/superpowers)
alongside Comic Sol. Superpowers is optional, installed separately, and is not
bundled with or required by Comic Sol.

## Security status

This release uses a keyless Sigstore signature for `SHA256SUMS`; it is not Authenticode-signed, Apple-notarized, or GPG-signed. Every release includes `SHA256SUMS`, `SHA256SUMS.sigstore.json`, per-platform metadata with `signature_status: sigstore`, and a CycloneDX SBOM. Installers require `cosign` and verify the bundle against the GitHub Actions release workflow identity before checking the archive digest. Never pipe a remote installer directly into a shell. The complete list of release subjects and how each is bound — payload manifest entries, build-provenance attestations, the signed manifest, and the candidate identity — is defined in [`docs/releases/release-trust-chain.md`](releases/release-trust-chain.md).

## Verify installer bytes before first execution

`install.sh` and `install.ps1` are release payloads exactly like the archives: each one is named by the signed `SHA256SUMS`, carries a GitHub build-provenance attestation, and is verified during release qualification. The installers verify the archive, the signed manifest, and the Sigstore bundle before installing anything — but that code runs only after you execute the installer. Close the bootstrap gap by verifying the installer itself from outside it first:

1. Download, from the release page of the exact version you are installing (never from a branch), the installer for your platform plus `SHA256SUMS` and `SHA256SUMS.sigstore.json`.
2. Verify the Sigstore signature over the manifest (requires `cosign`):

   ```bash
   cosign verify-blob \
     --bundle SHA256SUMS.sigstore.json \
     --certificate-identity-regexp '^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/tags/v' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     SHA256SUMS
   ```

3. Confirm the installer's own digest appears in the now-trusted manifest. On Linux/macOS, from the directory containing the downloaded files:

   ```bash
   grep -E '  install\.sh$' SHA256SUMS | sha256sum -c -
   ```

   On Windows, compare the hash to the `install.ps1` line of `SHA256SUMS`:

   ```powershell
   (Get-FileHash .\install.ps1 -Algorithm SHA256).Hash.ToLower()
   ```

4. Optionally verify the installer's build provenance with the GitHub CLI (`gh auth login` once):

   ```bash
   gh attestation verify installers/install.sh \
     --repo wenn-id/comicsol \
     --signer-workflow wenn-id/comicsol/.github/workflows/release.yml
   ```

   The same command with `installers/install.ps1` verifies the PowerShell installer.

5. Read the installer, then run it with the commands below.

A digest or signature mismatch at any step means the installer must not be executed. Copying an installer from a repository checkout instead of the release is acceptable only when you trust that checkout, because branch copies have no release attestation.

## Linux and macOS

Download the matching ZIP and copy `installers/install.sh` from the same release or repository checkout. Read it before execution, then verify and install:

```bash
sha256sum comic-sol-2.0.0rc4-linux-x86_64.zip
# Compare the digest with SHA256SUMS.
sh installers/install.sh \
  --archive ./comic-sol-2.0.0rc4-linux-x86_64.zip \
  --sha256 <digest-from-SHA256SUMS> \
  --checksums ./SHA256SUMS \
  --signature ./SHA256SUMS.sigstore.json

$HOME/.local/share/comic-sol/bin/comic-sol --version
$HOME/.local/share/comic-sol/bin/comic-sol doctor
```

For macOS, use `comic-sol-2.0.0rc4-macos-x86_64.zip`. Despite that name, the archive contains
arm64 binaries and will not run natively on an Intel Mac — it was built on an arm64 runner while
the release still labelled every artifact `x86_64`. From `2.0.0rc6` onward the macOS archive is
named `comic-sol-<version>-macos-arm64.zip` so the name matches its contents, and Apple silicon
is the only macOS native-archive target. Intel macOS remains supported through source
installation on Python 3.11+, but it has no native archive. The default installation root is `$HOME/.local/share/comic-sol`.
Override it with `--install-root PATH` or `COMIC_SOL_INSTALL_ROOT`.

The POSIX installer requires `cosign`, `perl`, `sha256sum`, `unzip`, and standard POSIX utilities. Perl is used for race-free no-follow install-root traversal. The native binaries are not notarized, so macOS Gatekeeper may require an explicit local approval for this prerelease.

## Windows PowerShell

Download `comic-sol-2.0.0rc4-windows-x86_64.zip` and copy `installers/install.ps1` from the same release or repository checkout, then run:

```powershell
(Get-FileHash .\comic-sol-2.0.0rc4-windows-x86_64.zip -Algorithm SHA256).Hash
# Compare the digest with SHA256SUMS.
.\installers\install.ps1 `
  -Archive .\comic-sol-2.0.0rc4-windows-x86_64.zip `
  -SHA256 <digest-from-SHA256SUMS> `
  -Checksums .\SHA256SUMS `
  -Signature .\SHA256SUMS.sigstore.json

& "$HOME\AppData\Local\ComicSol\bin\comic-sol.exe" --version
& "$HOME\AppData\Local\ComicSol\bin\comic-sol.exe" doctor
```

The default root is `$HOME\AppData\Local\ComicSol`. Override it with `-InstallRoot PATH`. The executable is not Authenticode-signed, so Windows SmartScreen may warn during this prerelease.

## Release qualification

The release qualification workflow validates the *intended release artifact*: the published native archive, not a package rebuilt from the checkout. A maintainer dispatches `.github/workflows/release-qualification.yml` with an existing release tag. The workflow downloads the matching Linux, macOS, and Windows ZIP, `SHA256SUMS`, its Sigstore bundle, and installer directly from that GitHub Release, then runs on native runners:

- `comic-sol --version` and `comic-sol doctor` from the installed runtime;
- `init`, `status`, and `validate` on an offline fixture project;
- reinstalling the same published archive over the installed runtime, plus a forced mid-swap installer failure that must restore every managed byte before uninstall;
- checksum verification, installer install, uninstall, and preservation of the fixture project, user projects, unrelated files, and client configuration;
- one separate summary artifact for Linux, macOS, Windows, and WSL2.

WSL2 uses the Linux x86_64 release archive and `install.sh`; it is a separate qualification from native Windows PowerShell. Every supplied release tag is full-matched against the strict `vX.Y.Z[rcN]` pattern before anything is dispatched into WSL, and the WSL leg runs a static dispatch script whose arguments cross the boundary as direct argv plus the `WSLENV` environment handoff — no value is ever interpolated into a bash command string. If WSL2 is unavailable on the runner, the workflow records an explicit `exception` summary instead of silently treating the target as passed. To reproduce WSL qualification locally, run the Linux commands above inside WSL2 and retain the generated platform summary.

## Structured doctor diagnostics

`comic-sol --json doctor --output-root PATH` returns the stable CLI envelope and an authoritative readiness report. `data.ready` and legacy `data.healthy` agree; `data.messages` remains available for older consumers; and `data.checks` contains stable `id`, `status`, `message`, and `remediation` fields. Required runtime, Pillow, fonts, templates, references, and output-root checks fail closed. Optional MCP and image-generation capability checks return actionable warnings when unavailable rather than pretending those capabilities are installed.


Extract the archive into a dedicated directory. Keep the executable beside its `_internal` directory; this is a PyInstaller one-directory runtime.

```bash
./comic-sol/comic-sol --version
./comic-sol/comic-sol doctor --output-root "$HOME/Comic Sol"
```

On Windows use `.\comic-sol\comic-sol.exe`.

## Upgrade and rollback

Running the installer again with a verified newer archive performs an upgrade. Runtime versions live beneath `versions/`, the stable runtime is exposed at `bin/`, and `active-version` records the active release. The new runtime runs `doctor` before activation. Transactional lifecycle code restores the previous `bin/` runtime and `active-version` if verification fails.

For manual rollback, reinstall the previously verified archive and matching SHA-256 digest. Never edit `active-version` alone: the entire one-directory runtime must change together. Repository-side withdrawal and production rollback procedures — which preserve the immutable release evidence instead of replacing bytes — are in [`docs/releases/rollback-runbook.md`](releases/rollback-runbook.md).

## Uninstall

Linux/macOS:

```bash
sh installers/install.sh --uninstall
```

Windows:

```powershell
.\installers\install.ps1 -Uninstall
```

Uninstall validates the installation sentinel and active version, rejects filesystem roots, home/current directories, repositories, and Comic Sol project roots, then removes only installer-managed children. Unknown files left in the installation directory are preserved. Installations created before the sentinel was introduced fail closed; reinstall or upgrade the same installation root once before uninstalling it. Comic projects are preserved because output roots live outside the installation directory. MCP client integration is managed separately with `comic-sol setup`, `comic-sol repair`, and `comic-sol uninstall`.

## OCI image

OCI is an official distribution channel delivered as the attested release asset `comic-sol-<version>-linux-x86_64.container.tar` — not as a registry image. Every release builds the image once from locked source and a digest-pinned base, smokes it, and publishes the tar inside the signed `SHA256SUMS` with a build-provenance attestation; qualification loads and runs the downloaded bytes. There is no `ghcr.io` image yet, so do not trust one claiming to be Comic Sol. The full decision record and what a registry distribution would additionally require are in [`docs/releases/release-trust-chain.md`](releases/release-trust-chain.md#oci-distribution-decision).

To run the official image, download the container tar from the release, verify it against `SHA256SUMS` (see above), and load it:

```bash
docker load --input comic-sol-2.0.0rc4-linux-x86_64.container.tar
docker run --rm --entrypoint comic-sol comic-sol:2.0.0rc4 doctor --output-root /tmp/comic-sol-doctor
```

Alternatively, build and run the non-root image from a checkout:

```bash
docker build -t comic-sol:2.0.0rc4 .
docker run --rm --entrypoint comic-sol comic-sol:2.0.0rc4 doctor --output-root /tmp/comic-sol-doctor
docker compose up
```

The image runs as `comic-sol`, uses `/data` for persistent projects, and exposes the MCP server over stdio by default. `compose.yaml` mounts a named `/data` volume, uses a read-only root filesystem, disables network access, applies CPU/memory limits, and enables `no-new-privileges`.

### MCP trust boundary

MCP stdio has no authentication. Any local process able to launch the configured MCP command can invoke all tools inside its `--root`. Use only trusted clients and a dedicated absolute root containing Comic Sol projects; never point MCP at `$HOME`, a repository root, or a shared multi-user directory. Path containment and symlink rejection protect the configured root, but do not authenticate clients. CLI commands that accept `project_dir` should likewise use paths beneath the chosen output root.

## Verify release metadata

Each platform bundle contains:

- `comic-sol-2.0.0rc4-<platform>-x86_64.zip`
- platform metadata declaring `signature_status: sigstore`
- a CycloneDX SBOM
- `SHA256SUMS`
- `SHA256SUMS.sigstore.json`

The GitHub prerelease also provides a global `SHA256SUMS` and its Sigstore bundle. To verify manually:

```bash
cosign verify-blob \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/wenn-id/comicsol/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
```

The installer additionally checks that the archive digest matches the signed manifest. Any signature or digest mismatch means the artifact must not be executed.
