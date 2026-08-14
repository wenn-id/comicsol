# Image provider setup by platform

Comic Sol is provider-agnostic — it never embeds credentials or vendor client
libraries. The agent session must expose a compatible text-to-image tool; Comic Sol
detects it automatically (see [capability-detection.md](capability-detection.md)).

This platform-specific document lists concrete setup steps for the most common
MCP-capable agent platforms.

---

## Codex (OpenAI)

**Built-in.** Codex includes GPT-5.6 Sol / GPT-5.5 Image generation as a native tool.
No extra setup needed. Comic Sol's capability detection finds the tool and records it.

If agent cannot find the tool, confirm your Codex model supports image generation.
GPT-5.6 Sol and newer GPT-5.x models include it.

---

## Claude Desktop / Claude Code

Neither Claude Desktop nor Claude Code ships a native image-generation tool.
You must provide one via a separate MCP server.

### Option A — FAL MCP (recommended, free tier up to ~400 credits/month)

1. Create an account at [fal.ai](https://fal.ai) and get an API key.
2. Install `uv` (universal Python package installer):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. Add the FAL MCP server to `claude_desktop_config.json`:

   **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

   ```json
   {
     "mcpServers": {
       "image-gen": {
         "command": "uvx",
         "args": ["mcp-fal"]
       }
     }
   }
   ```

4. Set the `FAL_KEY` environment variable or configure it in the same JSON:
   ```json
   "env": {
     "FAL_KEY": "your-fal-api-key"
   }
   ```

5. Restart Claude Desktop. The agent session gains image-generation tools exposed
   by the configured MCP server.
6. Comic Sol detects and uses them at the planning step.

### Option B — Direct API via bash (no MCP server needed)

The agent can call an image API directly. Simplify the skill prompt to include:

> You have access to bash. Use curl to call OpenAI / Stability / Replicate
> image API and save the result as a PNG file. Your API key is set as
> `$OPENAI_API_KEY`/`$STABILITY_KEY`.

This pattern works but relies on the agent writing correct API calls; prefer
the MCP approach for reliability.

### Option C — Any image MCP server from the market

- [mcp-server-to-images](https://github.com/nicholasgriffintn/mcp-server-to-images)
  — wraps multiple providers
- [mcp-fal](https://github.com/fal-ai/mcp-fal) — FAL AI, fast SD/Flux models
- [sequential-thinking](https://github.com/modelcontextprotocol/servers) + Replicate

---

## Cline / Continue / Roo Code (IDE plugins)

These IDE agents load MCP servers from their own config file (typically
`.vscode/mcp.json` or Continue's `config.json`). Add the same image-gen
server there:

```json
{
  "mcpServers": {
    "image-gen": {
      "command": "uvx",
      "args": ["mcp-fal"]
    }
  }
}
```

The agent exposes tools from all configured MCP servers. Comic Sol's
capability detection scans them.

---

## Hermes Agent (Nous Research)

Hermes has a built-in `image_generate` tool. No extra MCP server is needed
for image generation if a provider is configured under `image_gen` in
`config.yaml` (OpenAI, FAL, xAI, or 9Router).

If you also want the Comic Sol MCP deterministic pipeline, register the
MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  comic-sol:
    command: "/path/to/python3.11"
    args:
      - "/path/to/comic-sol/scripts/mcp_server.py"
      - "--root"
      - "/path/to/output-root"
    sampling:
      enabled: false
```

---

## Summary table

| Platform | Native image gen | Extra setup | MCP deterministic server |
|---|---|---|---|
| Codex | ✅ built-in | None | Optional (for MCP tools) |
| Claude Desktop | ❌ | FAL/MCP or direct API | Optional |
| Claude Code | ❌ | FAL/MCP or direct API | Optional |
| Cline / Continue | ❌ | MCP server config | Optional |
| Hermes Agent | ✅ built-in | None | Optional |

> The `comic_*` deterministic MCP server is separate from image generation.
> You can run Comic Sol as a pure Skill (no MCP server, image via native tool)
> or as a Skill + MCP server (image via native tool, pipeline via MCP tools).
