# Support matrix

This page is the published statement of which platform, install mode,
architecture, and runtime combinations Comic Sol supports. It is the
user-facing summary; the release-side gate lives in
[`docs/releases/v2.0-stable-criteria.md`](releases/v2.0-stable-criteria.md)
and the qualification workflow that proves each published archive.

Two contracts hold across every mode:

- Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+.
- Intel macOS is source-install-only; it has no native archive.

The native archive matrix is Linux x86_64, macOS arm64, and Windows x86_64.
WSL2 uses the Linux x86_64 archive; it has no separate native archive.

## Platform × install mode × architecture × runtime

| Install mode | Linux x86_64 | Linux other arch | macOS arm64 | macOS Intel (x86_64) | Windows x86_64 | Windows arm64 | WSL2 |
|---|---|---|---|---|---|---|---|
| Codex Skill / Plugin | ✅ system Python 3.11+ | ✅ system Python 3.11+ | ✅ system Python 3.11+ | ✅ system Python 3.11+ | ✅ system Python 3.11+ | ✅ system Python 3.11+ | ✅ system Python 3.11+ |
| Source / development | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ |
| Installed CLI wheel (`pip`) | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ | ✅ Python 3.11+ |
| Native portable archive | ✅ bundled Python 3.11 | ❌ no archive | ✅ bundled Python 3.11 | ❌ source only | ✅ bundled Python 3.11 | ❌ no archive | ✅ uses the Linux x86_64 archive |
| MCP server (`mcp` extra) | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK | ✅ Python 3.11+ and MCP SDK |
| OCI image | ✅ bundled runtime, `linux/amd64` | ❌ not built | ❌ not built | ❌ not built | ❌ not built | ❌ not built | run the Linux image from WSL2 Docker if available |

Notes on each mode:

- **Codex Skill / Plugin** — platform-independent. The agent session needs no
  specific OS; the deterministic scripts need one Python 3.11+ launcher and
  `Pillow==12.3.0` (installed from the hash-locked `requirements/locks/base-*`
  files). Image generation is supplied by the agent session, not the OS.
- **Source / development** — any host that runs Python 3.11+. The offline test
  suite is provider-neutral and runs on all four platforms.
- **Installed CLI wheel** — the wheel is pure-Python (`py3-none-any`), so any
  Python 3.11+ interpreter can install it.
- **Native portable archive** — bundles its own Python 3.11, Pillow, MCP,
  fonts, templates, the Skill, and references; no system Python is required
  after extraction. The published `v2.0.0rc4` macOS archive was historically
  mislabelled `x86_64` while containing arm64 binaries; from `2.0.0rc6` onward
  the name matches the contents and Apple silicon is the only macOS
  native-archive target.
- **MCP server** — the optional `stdio` server is an install extra
  (`.[mcp]`, MCP SDK pinned in the `requirements/locks/runtime-*` files, which
  are per-platform but architecture-agnostic). `--root` is always an explicit
  absolute path; see the trust boundary in
  [`README.md` → MCP Server](../README.md#mcp-server-optional).
- **OCI image** — a `linux/amd64` image distributed as the attested release
  assets `comic-sol-<version>-linux-x86_64.container.tar` and its CycloneDX
  SBOM `comic-sol-<version>-linux-x86_64.container.sbom.json`, not a registry
  image. The supported runtime contract is: fixed numeric user `10001:10001`
  (never root), read-only root filesystem, no network, all Linux capabilities
  dropped, `no-new-privileges`, an init process, a 64-process limit, and the
  engine's default seccomp profile (no custom profile; `seccomp=unconfined`
  is unsupported). The only writable paths are the `/data` volume and a
  `/tmp` tmpfs. Only Docker Engine-compatible runtimes that provide the
  default seccomp profile are supported; the image is not published for other
  architectures and there is no registry image. See
  [`docs/install.md` → OCI image](install.md#oci-image) for the verification
  commands and the hardening audit each release runs.

## Runtime extras

| Extra | Needed for | Dependency |
|---|---|---|
| none | full deterministic CLI lifecycle | `Pillow==12.3.0` |
| `mcp` | `comic-sol mcp` stdio server, 17 `comic_*` tools | `mcp==2.0.0` |

The MCP extra changes no deterministic behavior; without it, MCP tests skip
gracefully and every other command works unchanged.
