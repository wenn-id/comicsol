# Provider evidence table

This table mirrors the matrix in
[`docs/web/providers.md`](../../docs/web/providers.md) and is referenced
from [`README.md`](README.md). Every row's `Live smoke` and `Evidence`
columns are honest about what was actually done in this work package.

| Provider | Implemented | Offline-qualified | Live smoke | Evidence |
| --- | --- | --- | --- | --- |
| OpenAI | Yes | Yes | Not run | None |
| Google | Yes | Yes | Not run | None |
| BFL (direct) | Yes | Yes | Not run | None |
| xAI | Yes | Yes | Not run | None |
| Stability | Yes | Yes | Not run | None |
| Replicate | Yes | Yes | Not run | None |
| fal.ai | Yes | Yes | Not run | None |
| Cloudflare | Yes | Yes | Not run | None |
| ComfyUI (remote) | Yes | Yes | Not run | None |
| ComfyUI (local, agent handoff) | No | Not run | Not run | None |
| Active-agent image generation | Partial | Yes | Not run | None |

## What this table does not claim

- No live provider call was authorized or made in this work package.
- No local ComfyUI instance was available; no local ComfyUI evidence
  was recorded.
- No browser environment in this work package exposed
  `document.modelContext`; no active-agent WebMCP demonstration was run.
- The web distribution does not include a hosted deployment; no external
  deployment URL is claimed.

A future work package that exercises a live smoke, a local ComfyUI
instance, or an active-agent WebMCP demonstration must record the
retained evidence link in the `Evidence` column above and update
[`docs/web/providers.md`](../../docs/web/providers.md) to match.
