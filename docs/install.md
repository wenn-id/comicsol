# Install Comic Sol v2.0.0rc6

This guide is prepared for the `v2.0.0rc6` release candidate and pins every
recommended command to that exact immutable tag. The tag and its assets are not
published yet, so these commands become usable only when `v2.0.0rc6` appears on
the [GitHub Releases page](https://github.com/wenn-id/comicsol/releases). Until
then, do not substitute a branch download or an older installer that does not
support `--release`/`-Release`; use the published release's documented manual
path instead.

This is the recommended native CLI installation path. Native releases bundle
Python, Pillow, MCP, fonts, templates, the Skill, and references; no system
Python is required. Source installation supports Linux, macOS, Windows, and
WSL2 on Python 3.11+. Intel macOS is source-install-only; it has no native
archive. WSL2 uses the Linux x86_64 archive; it has no separate native archive.
Other architectures use the source-install instructions in
[`docs/install-manual.md`](install-manual.md).

## Before installation

Download `install.sh` (Linux, macOS, or WSL2) or `install.ps1` (native Windows)
from the release page for the **same exact tag** you will install. Never pipe a
remote installer into a shell. Verify the installer before its first execution
using the signed-manifest or build-provenance steps in
[`docs/install-manual.md`](install-manual.md#verify-installer-bytes-before-first-execution).

Install [`cosign`](https://docs.sigstore.dev/cosign/system_config/installation/)
separately and make it available on `PATH`. The installers do not install or
update it. The POSIX installer also requires `perl`, `curl`, `unzip`, and either
`sha256sum` or the stock macOS `shasum`.

## Recommended companion: Superpowers

[Superpowers](https://github.com/obra/superpowers) supports structured
development workflows. Superpowers is optional, installed separately, and not
bundled with or required by Comic Sol.

## Recommended installation

Each platform has one recommended invocation. The tag is mandatory and immutable;
the installers never select `latest`.

### Linux x86_64

```bash
sh ./install.sh --release v2.0.0rc6
```

### macOS arm64

```bash
sh ./install.sh --release v2.0.0rc6
```

The published rc1–rc4 macOS filename says `x86_64`, but those archives contain
arm64 binaries. The installer applies that release-specific filename correction.
New releases use `macos-arm64`.

### Windows x86_64 PowerShell

```powershell
.\install.ps1 -Release v2.0.0rc6
```

### WSL2 x86_64

Run the Linux installer inside WSL2:

```bash
sh ./install.sh --release v2.0.0rc6
```

The default roots are `$HOME/.local/share/comic-sol` on Linux, macOS, and WSL2,
and `$HOME\AppData\Local\ComicSol` on Windows. Override them with
`--install-root PATH` or `-InstallRoot PATH`.

## What the recommended mode verifies

Before changing an active installation, the installer:

1. strictly validates the pinned `vX.Y.Z` or `vX.Y.ZrcN` tag and supported host;
2. downloads the fixed archive, `SHA256SUMS`, and
   `SHA256SUMS.sigstore.json` for that exact GitHub release through bounded,
   HTTPS-only redirects into private staging;
3. requires `cosign` and binds the manifest signature to that exact release tag
   and the Comic Sol release workflow;
4. accepts exactly one strict signed-manifest record for the selected archive and
   verifies its SHA-256 digest;
5. rejects unsafe archive members, checks the staged runtime version, and runs
   staged `doctor`; and
6. transactionally publishes the runtime or restores the previous installation.

A download, signature, checksum, archive, version, or staged-doctor failure does
not modify the active runtime. On success, the final output line is one executable
absolute `doctor` command. Run that exact line next; no PATH change is required.

For independently downloaded archives, air-gapped use, source/wheel installation,
bootstrap verification details, direct extraction, and release metadata checks,
see [`docs/install-manual.md`](install-manual.md).

## Upgrade and rollback

Upgrade by rerunning the one recommended command with a newer exact tag. Runtime
versions live under `versions/`, `bin/` is the active runtime, and
`active-version` records the selected version. Verification completes before the
transactional swap.

Rollback by reinstalling a previously verified release tag, or use the explicit
local-archive procedure in [`docs/install-manual.md`](install-manual.md#manual-local-archive-installation).
Repository-side withdrawal and production rollback procedures are in
[`docs/releases/rollback-runbook.md`](releases/rollback-runbook.md).

## Uninstall

Linux, macOS, or WSL2:

```bash
sh ./install.sh --uninstall
```

Windows:

```powershell
.\install.ps1 -Uninstall
```

Uninstall validates the installation marker and removes only installer-managed
runtime files. User projects, unknown files in the install root, and unrelated
client configuration are preserved. MCP client integration remains separately
managed by `comic-sol setup`, `comic-sol repair`, and `comic-sol uninstall`.

## Release qualification

The release qualification workflow exercises the recommended pinned mode on
Linux x86_64, macOS arm64, Windows x86_64, and WSL2. It verifies the installed
version, `doctor`, an offline fixture lifecycle, same-release reinstall, and safe
uninstall. Its forced mid-swap rollback test deliberately uses explicit local
assets so failure injection remains deterministic and independent of the network.
An unavailable WSL2 runner is recorded as an exception and blocks release
readiness rather than counting as a pass.

## Structured doctor diagnostics

`comic-sol --json doctor --output-root PATH` returns the stable CLI envelope.
`data.ready` and legacy `data.healthy` agree; `data.messages` remains available;
and `data.checks` contains stable `id`, `status`, `message`, and `remediation`
fields. Required runtime, Pillow, fonts, templates, references, and output-root
checks fail closed. Optional MCP and image-generation checks report actionable
warnings.

Preview integration recovery before applying it:

```bash
comic-sol --json repair --dry-run --output-root /absolute/path/to/comic-sol-output
comic-sol --json repair --output-root /absolute/path/to/comic-sol-output
```

Repair handles only the product-owned `comic-sol` MCP entry in existing verified
client config files. It safely adds or replaces stale executable, MCP arguments, and
output-root values; it never creates missing third-party config, guesses unsupported
formats, invokes an installer, or repairs unrelated application settings. Each change
uses a verified private backup, atomic publication, persisted verification, and
verified rollback. Results expose `success`, `no-op`, or `failure`; their compatible
statuses are `planned` (preview), `configured` (applied), `unchanged` (no-op),
`skipped` (not selected or not detected), `unsupported` (unverified native
format/location), `rolled-back` (failed change restored), and `rollback-failed`
(restoration could not be verified). Selecting an unverified client returns a
failure result. Failures keep per-client evidence and direct operators to
`comic-sol doctor` or a named backup.

## OCI image

OCI is an official distribution channel delivered as the attested release asset
`comic-sol-<version>-linux-x86_64.container.tar` plus its CycloneDX SBOM
`comic-sol-<version>-linux-x86_64.container.sbom.json` — not as a registry image.
Every release builds the image once from locked source and the Dockerfile's single
digest-pinned base argument, audits the running container's hardening, scans the
image dependency set with `pip-audit`, and publishes the tar and its SBOM inside
the signed `SHA256SUMS` with build-provenance attestations; qualification loads
and runs the downloaded bytes under the same audit. There is no `ghcr.io` image
yet, so do not trust one claiming to be Comic Sol. The full decision record and
what a registry distribution would additionally require are in
[`docs/releases/release-trust-chain.md`](releases/release-trust-chain.md#oci-distribution-decision).
Manual and advanced OCI installation context is in
[`docs/install-manual.md`](install-manual.md#oci-image).

To run the official image, download the container tar and its SBOM from the
release, verify both against the signed `SHA256SUMS`, and load the image:

```bash
docker load --input comic-sol-2.0.0rc6-linux-x86_64.container.tar
docker run --rm --init --read-only --network none --cap-drop ALL --pids-limit 64 \
  --security-opt no-new-privileges --tmpfs /tmp \
  --entrypoint comic-sol comic-sol:2.0.0rc6 doctor --output-root /data/doctor
```

Alternatively, build and run the non-root image from a checkout:

```bash
docker build -t comic-sol:2.0.0rc6 .
docker run --rm --init --read-only --network none --cap-drop ALL --pids-limit 64 \
  --security-opt no-new-privileges --tmpfs /tmp \
  --entrypoint comic-sol comic-sol:2.0.0rc6 doctor --output-root /data/doctor
docker compose up
```

### Container runtime hardening

The image runs as the fixed numeric identity `10001:10001` (the `comic-sol`
account; never root), uses `/data` for persistent projects, and exposes the MCP
server over stdio by default. `compose.yaml` mounts a named `/data` volume, uses
a read-only root filesystem, disables network access, drops every Linux
capability, applies a 64-process limit, an init process, CPU/memory limits, and
`no-new-privileges`. The only writable paths at runtime are the `/data` volume
and a `/tmp` tmpfs.

The effective seccomp policy is the container engine's **default profile**:
neither the image nor `compose.yaml` installs a custom profile, and nothing may
run the image with `seccomp=unconfined`. Docker's default profile is maintained
by the engine vendor and already blocks the privileged syscall classes the
runtime never needs. Verify the effective policy on your host with:

```bash
docker info --format '{{.SecurityOptions}}'   # must report seccomp with profile=default
docker run --rm --cap-drop ALL --security-opt no-new-privileges --entrypoint python comic-sol:2.0.0rc6 \
  -c "print([line for line in open('/proc/self/status') if line.startswith(('CapEff','NoNewPrivs','Seccomp'))])"
# expect CapEff: 0000000000000000, NoNewPrivs: 1, Seccomp: 2
```

The release workflow asserts all of this fail-closed with
`scripts/container_runtime_audit.py` — engine seccomp profile, image user, CLI
version, runtime UID/GID, zero effective capabilities, seccomp filter mode, the
process limit, the read-only root filesystem, the absent network, and working
`doctor` and MCP handshake under the full hardening set — on the built image,
and qualification repeats the audit against the published bytes.

### MCP trust boundary

MCP stdio has no authentication. Any local process able to launch the configured
MCP command can invoke all tools inside its `--root`. Use trusted clients and a
dedicated absolute project root; never use `$HOME`, a repository root, or a
shared multi-user directory. Path containment does not authenticate callers.
