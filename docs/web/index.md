# Comic Sol Studio — user guide

Comic Sol Studio is the Web distribution of Comic Sol: a browser client that
plans, generates, and QAs a comic project, then exports a private PDF or a
portable archive. This guide covers the end-to-end user workflow and the
surface contracts the Web distribution guarantees.

> **Scope truth**
>
> This documentation describes the implementation merged into `wenn-id/comicsol`
> at the pinned baseline and the offline work package that qualifies it. Passing
> offline contract tests are **not** live verification. Anything labelled
> *offline-qualified* is deterministic and contract-tested but has **not** been
> run against a live provider in this work package.

## Workflow at a glance

The ten-step user workflow is, in order:

1. **Sign in.**
2. **Create from a prompt, a pasted story, or a portable archive import.**
3. **Review the Plan.**
4. **Choose a generation route.**
5. **Confirm the generation cost.**
6. **Inspect the queue.**
7. **Confirm every provider switch.**
8. **Explicitly promote staged rasters.**
9. **Run QA.**
10. **Export a private PDF or portable archive.**

Each step is detailed in the sections below.

## Sign in

**Studio does not currently ship an OAuth sign-in route.** The merged
`web/comic_sol_web/app.py::create_app` composition root includes the
`projects`, `generation`, `approvals`, and `assets` routers; an
authentication router is **not yet included**, so no `/api/auth/login`
is served by the merged application. The `WebConfig` also has no
GitHub-client configuration. `/healthz` is unauthenticated and
deterministic; every project and generation route currently requires a
principal, but the route that issues one is not present in the merged
build.

**The intended end state** — the path the build will require once the
auth router is wired — is:

- the user opens the Studio in a browser and authenticates through
  GitHub OAuth (`web/comic_sol_web/auth.py` contains the construction
  logic, but the router is not registered by `create_app` in the merged
  build);
- Studio stores only a keyed digest of the OAuth state; a replay is
  rejected on consume;
- after the callback succeeds, the browser holds an `HttpOnly` session
  cookie, every state-changing request carries CSRF protection, and the
  server never streams a provider credential to the browser.

Signing in does not configure a provider. The user chooses a generation
route (and, for BYOK routes, supplies or authorizes their own
credentials) only after the project is planned. The sign-in wiring is
tracked as a release blocker; the **steps after sign-in below are the
documented user path that the build will require once the auth router
is added — not a flow that is exercised by the merged code today.**

## Entry modes

A project begins one of three ways:

- **Create from a short prompt** — type a one-line idea; Studio drafts a plan.
- **Create from a pasted story** — paste a longer narrative; Studio drafts a
  plan from it.
- **Import a portable archive** — load a prior project from a
  `.comic-sol-handoff` portable archive produced on any surface that supports
  the handoff format. Importing does not pull arbitrary files or URLs.

All physical files you work with after import are referenced by **page-owned
handles**: an asset or archive is addressed by an identifier that belongs to
the project page, **never** by a filesystem path and **never** by an arbitrary
URL the page could redirect elsewhere.

## Review the Plan

Before any generation cost is incurred, Studio shows you the **Plan**: the
panel breakdown, the panel count, the page ownership map, and the queue it
would create. Review it and confirm it before generation.

## Generation routes

In the merged build, only the **`agent`** route is registered, and it
is **selectable only when capabilities are passed at startup**.
`create_app` accepts an `active_agent_image_capabilities` parameter
that defaults to the empty set; the agent provider exposes
`AgentProvider` as a route option only when that set intersects
`{text_to_image}`. With the documented bare
`create_app(WebConfig.from_env(os.environ))` start command, the set
is empty, the curated `available_options()` list is empty, and every
generation request is rejected as not currently executable. To make
the agent route selectable in this build, the operator must supply
`text_to_image` (or a wider set) through the start invocation;
otherwise the process is up but no route is executable.

The other three routes (`hosted`, `session BYOK`, and `encrypted
persisted BYOK`) exist as offline adapter-level tests but are not
wired into `create_app`; selecting them at runtime is not possible
until the adapters are registered. The descriptions below remain for
reference and for future builds that wire them in. Choose exactly one
of the routes below that is currently implemented in the build you
are running.

- **Agent** — a local agent session drives generation and hands finished
  rasters back through agent-native handoff. Credentials, if any, live and are
  used in the agent session; Studio never sees them.
- **Hosted** — the Studio server calls a provider using credentials the
  operator configured as hosted secrets. You do not supply credentials, and you
  are billed/rated against the operator's hosted account.
- **Session BYOK** — you paste a provider credential for a single session.
  It is used in memory, never persisted, and expires when the session does.
- **Encrypted persisted BYOK** — you authorize a credential that is encrypted
  at rest and kept for future sessions, under a key the operator rotates.

## Explicit confirmations

Studio never commits money or destructive work silently. Each of the following
requires an explicit confirmation from you:

- **Generation cost** — before the first generation and before any action that
  would spend additional provider budget, the expected cost is shown and you
  confirm it.
- **Provider switch** — changing the active provider mid-project is a
  revision-bound change you must approve; it cannot be replayed from an old
  revision.
- **Raster promotion** — staged rasters are not final until you explicitly
  promote them.
- **Overwrite** — replacing an existing page-owned asset or an existing archive
  requires explicit overwrite confirmation.

## Inspect the queue

Generation runs through a queue that is persisted to the SQLite
`GenerationStore` (`application.sqlite3` under the data root), not
held in process memory. You can inspect the queue at any time: which
panels are pending, which are in flight, which finished, and which
failed. Expired `running` leases are reclaimed on the next
`DurableGenerationQueue.lease_next()` call after the service
restarts — there is no startup or lifespan recovery handler in
`create_app`, so recovery happens when a queue consumer first polls.
Only the in-memory task/service objects on `app.state` are
process-local; the queue itself survives a restart.

## Confirm provider switches

If a panel fails on the current provider and Studio proposes a different
provider, you must approve the switch before it is made. The approval is tied
to the exact revision it was proposed against and expires, so a stale approval
cannot be replayed onto a later revision.

## Explicitly promote staged rasters

Panels are produced as **staged** rasters. Nothing about them is final until
you explicitly promote them into the project. A staged raster that was never
promoted is not part of the project and is not included in an export.

## Run QA

After promotion, run QA over the panels. QA validates geometry, containment,
and completeness against the Plan. QA is local and deterministic; it calls no
provider.

## Export a private PDF or portable archive

The final step exports either:

- a **private PDF** of the finished comic, written only to a destination you
  and the operator designate; or
- a **portable `.comic-sol-handoff` archive** that preserves the project for
  import on another surface.

Exports are private by default: nothing is published, uploaded, or made
public unless an operator explicitly configures a publishing destination.

## Billing and access

**A ChatGPT Plus or Pro subscription is not provider API credit.**

A ChatGPT Plus or ChatGPT Pro subscription is not provider API credit and
does not pay for Comic Sol Studio generation. Studio generation is billed
and rated against the provider API account the route uses (hosted account
for the hosted route, your account for BYOK routes). Subscription access
and provider API billing are separate; the presence or absence of a
ChatGPT subscription has no effect on Studio usage.

## Asset and archive handles

All physical resources in Studio are addressed by **page-owned handles**,
and a handle is **never** a path or a URL:

- an **asset handle** refers to a raster or panel that belongs to the project
  page;
- an **archive handle** refers to a portable archive owned by the page.

Handles are opaque identifiers. Studio does **not** expose a filesystem path
to the browser and does **not** accept an arbitrary URL as a resource.
Accepting a URL-shaped resource would let an attacker point Studio at an
unexpected network destination, so the handle model is the only supported
input source.

## Evidence tiers

Comic Sol Studio uses four evidence tiers, and the docs never conflate them:

- **Implemented** — the code path exists and is exercised by the merged test
  suite.
- **Offline-qualified** — a deterministic, contract-tested offline flow
  (FakeProvider or AgentProvider) exercises the route end to end with no live
  call.
- **Experimental** — the capability exists but is not a qualified release
  surface (for example, local ComfyUI through agent-native handoff).
- **Live-verified** — an offline and a live smoke both passed against a real
  provider, with a retained evidence link.

**Passing unit or contract tests never establishes live-verified.** A route
with only offline coverage is *offline-qualified*, never *live-verified*. In
this work package **no paid provider call or external deployment is
authorized**, so every provider route below is at most *offline-qualified* or
*experimental*.

## Tool surfaces

- The WebMCP client surface is exactly **five read tools and nine write
  tools** (see `submission/webmcp/tools.md`).
- The local MCP surface remains **exactly 17 `comic_*` tools**; WP17 does not
  change it.
- The hosted server never opens a connection to a user's `localhost`. Local
  assets reach Studio only through page-owned handles or agent-native handoff —
  never by Studio dialing back into your machine.

## Deployment and recording status

- **Deployment:** not deployed. No production instance exists and no external
  deployment URL is claimed in this work package.
- **Recording:** no video was recorded. The submission ships a narration/demo
  script instead.
- **Live provider smoke:** none was run. No paid provider call was authorized
  or made.

## Related documents

- [Provider matrix](providers.md)
- [Security and privacy](security.md)
- [Deployment](deployment.md)
- [Rollback and recovery](rollback.md)
