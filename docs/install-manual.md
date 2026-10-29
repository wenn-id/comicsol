# Manual and advanced installation

Use the pinned release mode in [`docs/install.md`](install.md) for normal native
CLI installation. This page covers bootstrap verification, explicit local
archives, source/wheel installs, direct extraction, metadata, and OCI use.

## Historical prerelease status

The published `v2.0.0rc1`–`rc4` releases are unsigned and not marked immutable.
Their `SHA256SUMS` files have no Sigstore bundle or build-provenance attestations;
a matching digest does not establish publisher authenticity. The immutable-release
setting was enabled later and does not apply retroactively. The current native
installer refuses these releases because the signature bundle is absent. Do not
bypass that check or execute their installers.

## Verify installer bytes before first execution

`install.sh` and `install.ps1` are release payloads. The installer can verify the
archive only after it starts, so close that bootstrap gap first:

1. Download the installer, `SHA256SUMS`, and
   `SHA256SUMS.sigstore.json` from the release page for the exact tag.
2. Set the exact tag, then verify the signed manifest with the exact certificate
   identity of the protected reusable release workflow:

   ```bash
   RELEASE=v2.0.0rc6
   cosign verify-blob \
     --bundle SHA256SUMS.sigstore.json \
     --certificate-identity "https://github.com/wenn-id/comicsol/.github/workflows/release.yml@refs/heads/main" \
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
     --cert-identity "https://github.com/wenn-id/comicsol/.github/workflows/release.yml@refs/heads/main" \
     --cert-oidc-issuer https://token.actions.githubusercontent.com \
     --source-ref "refs/tags/${RELEASE}"
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
release mode. It verifies `SHA256SUMS` with the exact certificate identity of the
protected reusable release workflow, `release.yml@refs/heads/main`; that identifies
the trusted workflow, not one specific tag. Manual mode cannot bind that identity
to a caller-selected tag by itself. The optional provenance check in step 4 ties
the downloaded installer bytes to the selected `refs/tags/<tag>`. Manual mode
accepts neither unsigned archives nor a missing caller-supplied digest.

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

Each native target in a release that passes the current signed pipeline publishes
its ZIP, metadata JSON, CycloneDX SBOM, `SHA256SUMS`, and
`SHA256SUMS.sigstore.json`. The metadata must identify
`signature_status: sigstore`; the archive, installer, metadata, and SBOM must each
have exactly one record in the signed manifest. Historical `rc1`–`rc4` releases
do not meet this contract. The complete subject contract is
[`docs/releases/release-trust-chain.md`](releases/release-trust-chain.md).

## OCI image

The official OCI distribution consists of the attested
`comic-sol-<version>-linux-x86_64.container.tar` release asset and its CycloneDX
SBOM, `comic-sol-<version>-linux-x86_64.container.sbom.json`. Verify both against
the signed manifest, then load and run the image under the supported hardening
contract:

```bash
docker load --input comic-sol-<version>-linux-x86_64.container.tar
docker run --rm --init --read-only --network none --cap-drop ALL --pids-limit 64 \
  --security-opt no-new-privileges --tmpfs /tmp \
  --entrypoint comic-sol comic-sol:<version> doctor --output-root /data/doctor
```

A checkout build is development-only:

```bash
docker build -t comic-sol:local .
docker compose up
```

The image runs as fixed numeric identity `10001:10001`, uses `/data` for
persistent projects, allows only `/data` and the `/tmp` tmpfs to be writable,
and exposes the MCP server over stdio. The supported runtime requires the
container engine's default seccomp profile; `seccomp=unconfined` is unsupported.
See the complete verification and runtime-audit contract in
[`docs/install.md`](install.md#oci-image). There is no official registry image
yet.
