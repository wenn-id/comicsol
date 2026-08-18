# Install Comic Sol v2.0.0rc4

Comic Sol `v2.0.0rc4` is a prerelease distributed as native portable archives for Linux, macOS, and Windows, plus a Python wheel/source archive and an OCI image definition. Native archives bundle Python 3.11, Pillow, MCP, fonts, templates, the Skill, and references; no system Python is required after extraction.

## Recommended companion: Superpowers

For structured brainstorming, planning, debugging, and verification workflows,
we recommend installing [Superpowers](https://github.com/obra/superpowers)
alongside Comic Sol. Superpowers is optional, installed separately, and is not
bundled with or required by Comic Sol.

## Security status

This release is **unsigned**. It is not Authenticode-signed, notarized, or GPG-signed. Every release includes `SHA256SUMS`, per-platform metadata with `signature_status: unsigned`, and a CycloneDX SBOM. Download the archive and checksum manifest over HTTPS, verify the digest, then run the installer. Never pipe a remote installer directly into a shell.

## Linux and macOS

Download the matching ZIP and copy `installers/install.sh` from the same release or repository checkout. Read it before execution, then verify and install:

```bash
sha256sum comic-sol-2.0.0rc4-linux-x86_64.zip
# Compare the digest with SHA256SUMS.
sh installers/install.sh \
  --archive ./comic-sol-2.0.0rc4-linux-x86_64.zip \
  --sha256 <digest-from-SHA256SUMS>

$HOME/.local/share/comic-sol/bin/comic-sol --version
$HOME/.local/share/comic-sol/bin/comic-sol doctor
```

For macOS, use `comic-sol-2.0.0rc4-macos-x86_64.zip`. The default installation root is `$HOME/.local/share/comic-sol`. Override it with `--install-root PATH` or `COMIC_SOL_INSTALL_ROOT`.

The POSIX installer requires `perl`, `sha256sum`, `unzip`, and standard POSIX utilities. Perl is used for race-free no-follow install-root traversal. Native binaries are unsigned, so macOS Gatekeeper may require an explicit local approval for this prerelease.

## Windows PowerShell

Download `comic-sol-2.0.0rc4-windows-x86_64.zip` and copy `installers/install.ps1` from the same release or repository checkout, then run:

```powershell
(Get-FileHash .\comic-sol-2.0.0rc4-windows-x86_64.zip -Algorithm SHA256).Hash
# Compare the digest with SHA256SUMS.
.\installers\install.ps1 `
  -Archive .\comic-sol-2.0.0rc4-windows-x86_64.zip `
  -SHA256 <digest-from-SHA256SUMS>

& "$HOME\AppData\Local\ComicSol\bin\comic-sol.exe" --version
& "$HOME\AppData\Local\ComicSol\bin\comic-sol.exe" doctor
```

The default root is `$HOME\AppData\Local\ComicSol`. Override it with `-InstallRoot PATH`. The executable is unsigned, so Windows SmartScreen may warn during this prerelease.

## Release qualification

The release qualification workflow validates the *intended release artifact*: the published native archive, not a package rebuilt from the checkout. A maintainer dispatches `.github/workflows/release-qualification.yml` with an existing release tag. The workflow downloads the matching Linux, macOS, and Windows ZIP, `SHA256SUMS`, and installer directly from that GitHub Release, then runs on native runners:

- `comic-sol --version` and `comic-sol doctor` from the installed runtime;
- `init`, `status`, and `validate` on an offline fixture project;
- checksum verification, installer install, uninstall, and preservation of the fixture project, user projects, unrelated files, and client configuration;
- one separate summary artifact for Linux, macOS, Windows, and WSL2.

WSL2 uses the Linux x86_64 release archive and `install.sh`; it is a separate qualification from native Windows PowerShell. If WSL2 is unavailable on the runner, the workflow records an explicit `exception` summary instead of silently treating the target as passed. To reproduce WSL qualification locally, run the Linux commands above inside WSL2 and retain the generated platform summary.

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

For manual rollback, reinstall the previously verified archive and matching SHA-256 digest. Never edit `active-version` alone: the entire one-directory runtime must change together.

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

Build and run the non-root image from a checkout:

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
- platform metadata declaring the unsigned state
- a CycloneDX SBOM
- `SHA256SUMS`

The GitHub prerelease also provides a global `SHA256SUMS`. A mismatch means the artifact must not be executed.
