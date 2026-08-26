# Image provider setup by platform

Comic Sol is provider-agnostic — it never embeds credentials or vendor client
libraries, and the deterministic engine never calls a network service. The
agent session must expose a compatible text-to-image capability; Comic Sol
detects it automatically (see [capability-detection.md](capability-detection.md)).

This platform-specific document describes the capability Comic Sol needs and
how to wire one into the most common agent platforms, without prescribing a
vendor, endpoint, or credential mechanism.

## The capability, not a vendor

Whatever the host, Comic Sol needs exactly one thing from the agent session:

- a tool that can **create a raster image from text alone** and return or save
  it as a **local file** (PNG) that the session can place on disk;

and, to get the most from it:

- acceptance of one or more **reference images** for character consistency;
- acceptance of **exact output dimensions** matching the panel format;
- the ability to render **authored SFX text** into the artwork.

An editing-only image tool (crop, resize, retouch) is not sufficient — the first
panel of a new comic must be drawable from the prompt alone. If no compatible
capability is exposed, Comic Sol preserves the project, marks it `BLOCKED`, and
reports exactly what is missing; it never fabricates a placeholder image.

## Credential safety

Comic Sol stores no provider credentials, and none belong in its files:

- Keep API keys in the **agent or MCP client's own configuration** (typically an
  environment block or secret store managed by that client), never in prompts,
  story text, `SKILL.md`, project JSON, or generated logs.
- Never paste an API key into a prompt to "give the agent access". Logs and QA
  records keep sanitized paths, hashes, and categories precisely so a credential
  that never entered the pipeline can never leak back out of it.
- If a credential may have been exposed, revoke it before filing any report, and
  use the private reporting route in [`SUPPORT.md`](../SUPPORT.md) rather than a
  public issue.

## Wiring a capability by platform

### Codex

Codex may expose a native image-generation capability. When the active session
has one, Comic Sol detects and records it with no extra setup. If the tool
cannot be found, confirm the session's configured model exposes image
generation; Comic Sol only sees the tools the session actually offers.

### Claude Desktop / Claude Code

Neither ships a native image-generation tool. Provide one through a separate
stdio MCP server of your choosing, registered in the client's MCP
configuration (for Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "image-gen": {
      "command": "<launcher for your chosen MCP image server>",
      "args": ["<its arguments>"],
      "env": {
        "<ITS_CREDENTIAL_VARIABLE>": "<key stored by the client, not by Comic Sol>"
      }
    }
  }
}
```

Restart the client; the session gains the server's image tools and Comic Sol's
capability detection finds them at the planning step.

### Cline / Continue / Roo Code (IDE agents)

These agents load MCP servers from their own config file (typically
`.vscode/mcp.json` or the editor's `config.json`). Register the same
`mcpServers` entry there. Tools from every configured server are exposed to the
session and scanned by capability detection.

### Any agent with shell access (direct HTTP pattern)

An agent with a shell can call any image API directly and save the PNG itself.
Treat this as a fallback, not a recommendation: it relies on the agent writing
a correct API call each time, and it puts request-shaping in the prompt instead
of a reviewed tool. If you use it, keep credentials in environment variables
read by the command — never inline keys in the prompt — and prefer an MCP
server for reliability.

### Hermes Agent

Hermes has a built-in `image_generate` tool driven by whatever provider is
configured under `image_gen` in its `config.yaml`; no MCP server is needed for
image generation.

If you also want the Comic Sol deterministic pipeline over MCP, register the
`comic_*` server separately (see
[`docs/surfaces.md` → MCP server](../docs/surfaces.md#mcp-server)).

## Summary table

| Platform | Native image gen | Extra setup | MCP deterministic server |
|---|---|---|---|
| Codex | ✅ when the session's model exposes it | None | Optional (for MCP tools) |
| Claude Desktop | ❌ | any image MCP server, or direct API | Optional |
| Claude Code | ❌ | any image MCP server, or direct API | Optional |
| Cline / Continue / Roo Code | ❌ | image MCP server config | Optional |
| Hermes Agent | ✅ built-in `image_generate` | provider configured in `config.yaml` | Optional |

> The `comic_*` deterministic MCP server is separate from image generation.
> You can run Comic Sol as a pure Skill (no MCP server, image via native tool)
> or as a Skill + MCP server (image via native tool, pipeline via MCP tools).

## ComfyUI local reference executor (experimental)

Comic Sol includes an agent-managed ComfyUI adapter as a reference implementation of the
existing `external-tool` handoff contract. It remains outside `scripts/`,
`comic_sol_product/`, the deterministic wheel, and the MCP surface. The active agent—not
the deterministic engine—launches it for one prepared generation job:

```text
python integrations/comfyui-local/comfyui_executor.py run --job JOB --workflow WORKFLOW --profile PROFILE --output FILE [--endpoint URL] [--allow-non-loopback]
```

The user supplies an already-running ComfyUI installation, a workflow exported in API
format, and a versioned profile. Comic Sol does not start or configure ComfyUI and does not
download models, custom nodes, or workflows. The profile maps the positive prompt,
optional negative prompt and seed, optional width and height, and optional ordered
references to exact node IDs and input names. Unmapped features are false; the adapter
never infers capability from ComfyUI, model, workflow, or node names.

The default endpoint is `http://127.0.0.1:8188` and accepts loopback IPv4/IPv6 only. The
adapter rejects credentials, fragments, non-loopback resolution, rebinding, and redirects.
`--allow-non-loopback` is an explicit unsafe override that prints a warning: local ComfyUI
has no Comic Sol authentication boundary. Workflow/profile/upload/history/raster sizes and
connection/queue/execution/download times are bounded.

The bounded route uploads declared references through `/upload/image`, submits through
`POST /prompt`, polls `/history/{prompt_id}`, and retrieves the declared output through
`/view`. Only the local raster and sanitized result metadata survive. The invoking agent
then submits it through normal handoff result intake as `executor_kind=external-tool` and
`executor_id=comfyui-local`; raster validation, retry accounting, receipts, retention,
visual QA, and promotion remain authoritative.

See the [integration README](https://github.com/wenn-id/comicsol/blob/main/integrations/comfyui-local/README.md) for the profile,
command, threat boundary, and troubleshooting details. This route is
**reference/experimental**, not verified: fake-loopback tests prove mechanics only. It must
not be described as verified until issue #244 records the required manual local ComfyUI
smoke result.

## Non-normative vendor pointers

The following pointers are **not normative** and are retained only as dated
convenience leads. Comic Sol endorses none of them; availability, pricing,
free-tier terms, and model behavior change without notice, so verify each
against its own current documentation before relying on it:

- MCP server directories and registries published by the MCP project and by
  each agent client (search for "image generation" MCP servers).
- `fal.ai` publishes `mcp-fal`, an MCP server wrapping its image models
  (checked 2026-08; see its repository for current terms).
- Community MCP servers that wrap multiple image providers exist on GitHub;
  review any server's code and network behavior before granting it a credential.

Nothing in Comic Sol depends on these services, and a report about one of them
belongs with that vendor, not in this repository's issue tracker.
