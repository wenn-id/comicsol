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

## OCI image

OCI is an official distribution channel delivered as the attested release asset
`comic-sol-<version>-linux-x86_64.container.tar`, not a registry image. Loading,
verification, and checkout-build commands are in
[`docs/install-manual.md`](install-manual.md#oci-image). There is no `ghcr.io` image
yet.

### MCP trust boundary

MCP stdio has no authentication. Any local process able to launch the configured
MCP command can invoke all tools inside its `--root`. Use trusted clients and a
dedicated absolute project root; never use `$HOME`, a repository root, or a
shared multi-user directory. Path containment does not authenticate callers.
