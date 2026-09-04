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

- **Adapter implemented** — a provider adapter module exists and is exercised
  by merged adapter tests behind `httpx.MockTransport`.
- **Routable in merged build** — the route is actually reachable through the
  merged `create_app` composition root. OpenAI is available only when the
  server environment declares `OPENAI_API_KEY`; the agent route is available
  only when the startup capability set exposes `text_to_image`.
  `web/comic_sol_web/app.py::_generation_service` registers an
  `AgentProvider(...)` and conditionally adds `OpenAIProvider(...)`; the
  credential stays inside `CredentialBroker`. `GenerationService._runtime_options()`
  excludes every catalog entry with no registered adapter, then emits the agent model only when
  `text_to_image` is in the intersection of the agent's declared
  capabilities and the startup-supplied `active_agent_image_capabilities`
  (empty by default). OpenAI's configured model defaults to `gpt-image-2`.
  A bare start with neither agent capability nor an OpenAI key exposes no executable route.
- **Offline-qualified (adapter-level)** — a deterministic, zero-cost,
  contract-tested flow exercises the adapter with a mocked transport. This is
  **not** an end-to-end route qualification.
- **Live smoke** — an offline AND a live call both passed against the real
  provider, with retained evidence. `Yes` implies a working evidence link.
- **Authentication** — the credential modes the adapter is written to accept.
- **Evidence** — a link only when evidence actually exists; otherwise `None`.

> **Wiring caveat, stated once and applied to every row**
>
> Adapter-level implementation and offline adapter tests do **not** make a
> route available to a user of the merged build. Only OpenAI can be selected
> when its declared server credential is present; all other paid rows remain
> `Routable in merged build: No`. Two end-to-end offline-qualified routes exist:
> the deterministic `FakeProvider` used by `web/tests/test_web_e2e.py` and
> `web/tests/test_live_golden_path.py`, which is a test fixture rather than a
> shippable provider. `test_live_golden_path.py` drives the entire
> `prompt → plan → human review → image → panel QA → page QA → composition →
> PDF` loop with fake planning, fake image, and fake visual-review adapters plus
> the real engine, and asserts the loop survives an app restart. Registering
> paid adapters in the composition root is production work outside this
> documentation work package.

| Provider | Adapter implemented | Routable in merged build | Offline-qualified (adapter-level) | Live smoke | Authentication | Evidence | Surface tier |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenAI | Yes | Conditional | Yes (mocked transport) | Not run | Hosted server credential only | None | Offline-qualified |
| Google | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| BFL (direct) | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| xAI | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| Stability | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| Replicate | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| fal.ai | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| Cloudflare | Yes | No | Yes (mocked transport) | Not run | Hosted, session BYOK, encrypted persisted BYOK | None | Not routable |
| ComfyUI (remote) | Yes | No | Yes (mocked transport) | Not run | BYOK endpoint URL | None | Not routable |
| ComfyUI (local, agent handoff) | No | No | Not run | Not run | Agent-native (no Studio credential) | None | Experimental |
| Active-agent image generation | Partial (`AgentProvider` is the one registered provider) | Conditional | Yes (agent-native handoff exercised offline) | Not run | Agent-native | None | Experimental |

*> `Not run` in the live-smoke column means the route was NOT exercised against a
> live provider. Passing offline adapter tests did not change that. `Conditional`
> means OpenAI appears only with a declared server credential; `No` means a user
> of the merged build cannot select the route at all. Approving a Plan is the
> boundary at which image spending starts; provider switching is never automatic
> and always requires an explicit confirmation.*

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

- **OpenAI** — `gpt-image-2` by default, configurable through
  `COMIC_SOL_WEB_OPENAI_IMAGE_MODEL`
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
