# Image provider setup by platform

Comic Sol is provider-agnostic. It never embeds credentials or vendor client
libraries. The current Codex session must expose a compatible text-to-image
capability that returns or writes a local raster.

## Codex

Codex may expose a native image-generation capability. Comic Sol detects only
the tools available in the active session. No API key belongs in the skill or
project files.

If no compatible capability is exposed, preserve the project and stop with the
exact blocked-state guidance in `capability-detection.md`.

## Other agents

Other agent hosts may expose image generation through their own built-in tool or
an MCP image provider. Configure that outside Comic Sol, then start a fresh
session and resume the project. Comic Sol does not prescribe a vendor, endpoint,
credential variable, or client configuration.

## Deterministic pipeline

The optional `comic_*` MCP/CLI surface in the canonical repository is separate
from image generation. A pure Codex Skill run does not require MCP.

Never paste API keys into prompts, `SKILL.md`, project JSON, or generated logs.
