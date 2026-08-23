# Manual and advanced installation

Use the pinned release mode in [`docs/install.md`](install.md) for normal native
CLI installation. This page covers bootstrap verification, explicit local
archives, source/wheel installs, direct extraction, metadata, and OCI use.

## Verify installer bytes before first execution

`install.sh` and `install.ps1` are release payloads. The installer can verify the
archive only after it starts, so close that bootstrap gap first:

1. Download the installer, `SHA256SUMS`, and
   `SHA256SUMS.sigstore.json` from the release page for the exact tag.
2. Set the exact tag, then verify the signed manifest against that tag:

   ```bash
   RELEASE=v2.0.0rc6
   cosign verify-blob \
     --bundle SHA256SUMS.sigstore.json \
     --certificate-identity "https://github.com/wenn-id/comicsol/.github/workflows/release.yml@refs/tags/${RELEASE}" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     SHA256SUMS
   ```

3. Verify the installer digest. Linux/macOS:

   ```bash
   grep -E '  install\.sh$' SHA256SUMS | sha256sum -c -
   ```

   On macOS, use the digest from the `install.sh` record with
   `shasum -a 256 ./install.sh`. Windows:

   ```powershell
   (Get-FileHash .\install.ps1 -Algorithm SHA256).Hash.ToLower()
   ```

4. Optionally verify build provenance using the downloaded release-asset name,
   not the `installers/` paths from a repository checkout:

   ```bash
   gh attestation verify ./install.sh \
     --repo wenn-id/comicsol \
     --signer-workflow wenn-id/comicsol/.github/workflows/release.yml
   ```

   Use `.\install.ps1` for the Windows installer.

A digest, signature, identity, or attestation mismatch means the installer must
not be executed. Never pipe a remote installer into a shell.

## Manual local-archive installation

This mode preserves the original explicit interface for offline use and
independent testing. Download the archive, `SHA256SUMS`, and
`SHA256SUMS.sigstore.json` for one exact release and determine the archive's
signed digest.

Linux, macOS, or WSL2:

```bash
sh ./install.sh \
  --archive ./comic-sol-<version>-<platform>-<architecture>.zip \
  --sha256 <digest-from-SHA256SUMS> \
  --checksums ./SHA256SUMS \
  --signature ./SHA256SUMS.sigstore.json
```

A fixed HTTPS archive URL may replace `--archive`:

```bash
sh ./install.sh \
  --url https://example.invalid/comic-sol.zip \
  --sha256 <digest-from-SHA256SUMS> \
  --checksums ./SHA256SUMS \
  --signature ./SHA256SUMS.sigstore.json
```

Windows PowerShell:

```powershell
.\install.ps1 `
  -Archive .\comic-sol-<version>-windows-x86_64.zip `
  -SHA256 <digest-from-SHA256SUMS> `
  -Checksums .\SHA256SUMS `
  -Signature .\SHA256SUMS.sigstore.json
```

Manual mode applies the same checksum, safe-archive, staged version,
staged `doctor`, transaction, rollback, locking, and uninstall rules as pinned
release mode. It also verifies `SHA256SUMS` with a release-tag identity from the
Comic Sol release workflow, but without `-Release`/`--release` it cannot bind that
identity to one caller-selected exact tag. The pre-execution verification above
provides that exact-tag binding. Manual mode accepts neither unsigned archives
nor a missing caller-supplied digest.

## Direct archive extraction

After independently verifying a native ZIP, extract it into a dedicated
directory and keep the executable beside `_internal`:

```bash
./comic-sol/comic-sol --version
./comic-sol/comic-sol doctor --output-root "$HOME/Comic Sol"
```

On Windows use `.\comic-sol\comic-sol.exe`.

## Source and wheel installation

Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+,
including platforms without a native archive. From a trusted checkout:

```bash
PYTHON=python3
"$PYTHON" -m pip install --require-hashes -r requirements/locks/base-linux-x86_64.txt
"$PYTHON" -m pip install .
comic-sol doctor
```

Use the matching macOS or Windows lock documented in
[`CONTRIBUTING.md`](../CONTRIBUTING.md). A published wheel can likewise be
installed into a Python 3.11+ environment after its digest and provenance are
verified.

## Verify release metadata

Each native target publishes its ZIP, metadata JSON, CycloneDX SBOM,
`SHA256SUMS`, and `SHA256SUMS.sigstore.json`. The metadata must identify
`signature_status: sigstore`; the archive, installer, metadata, and SBOM must each
have exactly one record in the signed manifest. The complete subject contract is
[`docs/releases/release-trust-chain.md`](releases/release-trust-chain.md).

## OCI image

The official OCI distribution is the attested
`comic-sol-<version>-linux-x86_64.container.tar` release asset. Verify it against
the signed manifest, then load it:

```bash
docker load --input comic-sol-<version>-linux-x86_64.container.tar
docker run --rm --entrypoint comic-sol comic-sol:<version> doctor --output-root /tmp/comic-sol-doctor
```

A checkout build is development-only:

```bash
docker build -t comic-sol:local .
docker compose up
```

The image runs as `comic-sol`, uses `/data` for persistent projects, and exposes
the MCP server over stdio. There is no official registry image yet.
