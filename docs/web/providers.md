# Comic Sol Studio — provider matrix

This page lists every image-generation route the Web distribution can use, the
evidence each has, the authentication modes it supports, and its known
limitations.

> **Honesty rule**
>
> **Passing offline contract tests is not live verification.** A route is
> *live-verified* only when BOTH an offline and a live smoke passed against a
> real provider and a retained evidence link exists. In this work package **no
> paid provider call or external deployment is authorized**, so no paid route
> below is live-verified. Every non-`n/a` route is at most *offline-qualified*
> via a deterministic FakeProvider or AgentProvider flow, or *experimental*.

## Provider verification matrix

The columns are:

- **Implemented** — the code path exists and is exercised by merged tests.
- **Offline-qualified** — a deterministic, zero-cost, contract-tested flow
  exercises the route with no live call.
- **Live smoke** — an offline AND a live call both passed against the real
  provider, with retained evidence. `Yes` implies a working evidence link.
- **Authentication** — the supported credential modes for that route.
- **Evidence** — a link only when evidence actually exists; otherwise `None`.

| Provider | Implemented | Offline-qualified | Live smoke | Authentication | Evidence | Surface tier |
| --- | --- | --- | --- | --- | --- | --- |
| OpenAI | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| Google | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| BFL (direct) | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| xAI | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| Stability | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| Replicate | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| fal.ai | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| Cloudflare | Yes | Yes | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Standard |
| ComfyUI (remote) | Yes | Yes | Not run | BYOK endpoint URL | None | Standard |
| ComfyUI (local, agent handoff) | No | Not run | Not run | Agent-native (no Studio credential) | None | Experimental |
| Active-agent image generation | No | Not run | Not run | Agent-native | None | Experimental |

*> `Not run` in the live-smoke column means the route was NOT exercised against a
> live provider. Passing offline contract tests did not change that.*

## Open regions

No overview image model is enabled for these studio regions in the merged
Web distribution. Select roles are listed in the catalog and may be quoted in
the engine elsewhere; the Web connection surfaces are bounded by the matrix
above.

## Remote ComfyUI

A remote ComfyUI route points Studio at a **public HTTPS** ComfyUI endpoint the
operator provides. The origin must be `https` (a cleartext `http` origin is
refused unless it resolves to a loopback address). The endpoint is dialed
directly; Studio enforces transport timeouts and does **not** follow redirects.
A `loopback` origin is refused for the hosted-route case because the hosted
server must never open a connection to a user's `localhost`.

Authentication is BYOK at the endpoint URL. Known limitations:

- the remote endpoint must be reachable over public HTTPS from the server;
- the endpoint must not be on a loopback/private/link-local/metadata range for
  hosted usage;
- Studio does not proxy or tunnel; it dials the endpoint you configure.

## Local ComfyUI

Local ComfyUI runs on **user hardware** and reaches Studio only through
**agent-native handoff**. The hosted server never opens a connection to a
user's localhost; there is no browser- or server-driven tunnel back to the
user's machine. An agent session on the user's side generates locally and hands
rasters back through page-owned handles, never by Studio dialing a local
address.

The local route is **experimental**: it depends on the user's ComfyUI workflow
being exact-candidate-matching, on the user's GPU and **model licenses**, and
on agent-native integration that is not a qualified release surface in this
work package. No local ComfyUI instance was available in this work package, so
no local ComfyUI evidence was recorded.

## Model identifiers

The published model identifiers below match the merged generation catalog
exactly (`web/comic_sol_web/generation/catalog.py`):

- **OpenAI** — `gpt-image-1`
- **Google** — `gemini-2.5-flash-image`
- **BFL (direct)** — `flux-1.1-pro`
- **xAI** — `grok-imagine-image-2.0`
- **Stability** — `sd3.5-large`
- **Replicate** — `black-forest-labs/flux-1.1-pro`
- **fal.ai** — `fal-ai/flux-pro/v1.1`
- **Cloudflare** — `@cf/black-forest-labs/flux-1-schnell`
- **ComfyUI** — `sdxl-base`

## Agent and active-agent image generation

The agent route and active-agent image generation are handled in the agent
session, not by Studio. Credentials, if any, live in the agent session and are
never uploaded to Studio. Rasters return through agent-native handoff as
page-owned handles. These routes are **experimental** and were not live-run in
this work package.

## Related documents

- [User guide](index.md)
- [Security and privacy](security.md)
- [Deployment](deployment.md)
- [Rollback and recovery](rollback.md)
- [Provider evidence table](../../submission/webmcp/provider-evidence.md)
