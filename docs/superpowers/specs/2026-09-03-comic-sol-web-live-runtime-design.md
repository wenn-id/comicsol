# Comic Sol Web Live Local Runtime Design

Date: 2026-09-03

## Objective

Turn the existing Comic Sol Studio from an offline-qualified WebMCP surface into a genuinely usable local single-user application. A user must be able to enter a prompt, generate a provider-authored plan, review and edit that plan, select an independent image provider, observe every production stage, inspect generated panels, and download the final PDF.

This design extends the existing Web project gateway, durable generation queue, provider adapters, handoff contracts, approvals, receipts, deterministic engine, and Studio views. It does not create a parallel pipeline or change the provider-neutral deterministic engine into a network client.

## Product flow

1. The user starts Studio bound to `127.0.0.1` with one or both provider credentials in the server environment.
2. Studio creates a local session for one fixed local user while preserving the existing session-cookie and CSRF boundaries.
3. The user enters a title, prompt or pasted story, language, page count, planning provider, and planning model.
4. Studio creates the canonical project and queues a durable planning job.
5. The planning provider produces the complete canonical Plan envelope:
   - story plan;
   - character bible;
   - storyboard;
   - visual identity pack.
6. Comic Sol validates the Plan. One schema-repair request is allowed when provider output is invalid.
7. Studio stops at Review Plan. No image-generation cost is incurred before approval.
8. The user edits the Plan, requests a new draft from either OpenAI or Anthropic, selects the image provider/model, and approves the canonical Plan.
9. Studio prepares reference and panel jobs and runs them through the existing durable generation system.
10. Each staged raster becomes visible immediately. The planning provider performs visual QA. A passing raster is promoted automatically; a failing raster is retried within the existing bounded budget.
11. After all panels pass, Studio runs deterministic lettering and composition, asks the planning provider to perform composed-page visual QA, then runs deterministic report generation and PDF export.
12. The final PDF and composed page images are available through private, owner-bound downloads.

## Provider separation

Planning and image generation are independent capabilities.

### Planning providers

The first release supports:

- OpenAI;
- Anthropic.

A `PlanningProvider` accepts the bounded source, project settings, canonical artifact schemas, and optional prior validation failures. It returns a complete Plan envelope and sanitized usage metadata. It never writes project files directly.

The configured planning model performs plan generation plus panel and composed-page visual QA. This avoids introducing a third provider choice. The activity feed still identifies the provider and model used for each QA decision.

### Image providers

The first live release registers the existing OpenAI image adapter. The existing `ImageProvider` and `ProviderRegistry` contracts remain authoritative. Other implemented adapters stay unroutable until separately enabled and verified.

Changing the image provider does not regenerate the Plan. A switch applies only to unstarted jobs or explicit retries. Accepted rasters retain their original provider, model, attempt, checksum, and receipt provenance.

### Credentials

The local runtime resolves credentials only from the server environment:

- `OPENAI_API_KEY`;
- `ANTHROPIC_API_KEY`.

Credential values never enter browser state, project artifacts, activity events, receipts, exports, or logs. The browser receives only provider/model availability and sanitized failure categories.

Model identifiers are configuration values with documented defaults rather than new persisted engine constants. Provider-specific network code remains under the Web distribution, outside `scripts/` and `comic_sol_product/`.

## Local single-user session

Local mode is explicit and binds to loopback by default. On first browser use, Studio provisions one stable local user and issues the existing session and CSRF cookies through a loopback-only bootstrap route. Project routes continue to require a `SessionPrincipal`; write routes continue to enforce CSRF, revision, and idempotency checks.

Local mode must not silently become a hosted authentication mode. Non-loopback deployment requires the existing hosted security work to be completed instead of weakening the local bootstrap boundary.

## Durable orchestration

The Web database gains a minimal workflow layer rather than a second engine state machine.

### Planning jobs

A planning job records:

- opaque job and project IDs;
- project revision;
- provider and model;
- state;
- attempt count;
- sanitized usage;
- created, started, and completed timestamps;
- sanitized error category.

Provider payloads and raw responses are never retained. Successful output crosses the existing `EngineGateway.update_plan()` boundary and is validated before publication.

### Production workflow

A production workflow records its current phase and pointers to existing generation jobs. It coordinates existing operations in this order:

`plan-approved → references → panels → visual-qa → lettering → composition → page-qa → export`

The workflow owns orchestration only. Canonical project state, generation attempts, receipts, accepted rasters, and exported artifacts remain owned by their existing components.

Every transition is idempotent and revision-bound. Restart recovery leases unfinished work and resumes from durable state. Accepted rasters are never regenerated merely because the Web process restarted.

## Plan review and invalidation

The Plan is a revision-bound draft until the user approves it. Users may:

- edit all four canonical documents;
- promote their edit through the existing Plan validation boundary;
- request regeneration with OpenAI or Anthropic;
- select the image provider and model;
- approve and start production.

Regenerating or editing the Plan increments the project revision and invalidates stale downstream workflow bindings. Planning provenance is retained in Web application data without changing the canonical engine schema.

Once production starts, changing the Plan requires an explicit return to Review Plan and the existing downstream invalidation behavior. Studio never overlays a new Plan onto accepted rasters.

## Visual QA and automatic promotion

For panel QA, the planning provider receives the staged raster plus the canonical character identity and storyboard requirements. For composed-page QA, it receives the page raster plus the applicable layout, reading-order, lettering, and continuity requirements. It returns the bounded Comic Sol QA evidence required by the existing panel and page QA records. Only normalized evidence is retained.

Automatic promotion is allowed only when:

- all required checks pass;
- the staged raster is still bound to the current project revision and generation job;
- the provider response validates against the bounded QA schema;
- no pause or cancellation was requested before promotion.

A failed panel check triggers the existing repair strategy and at most two regenerations for that panel, subject to eight extra calls project-wide. A failed composed-page check reruns only the earliest stale deterministic stage needed to repair the page; it never regenerates an accepted panel unless the normalized evidence explicitly identifies panel content as the cause and retry budget remains. When retry budget is exhausted, the workflow stops in a resumable blocked state and exposes the failed checks.

The user may pause production. Already accepted content remains accepted and visible.

## Activity timeline

The right-side process drawer is a future presentation of durable backend state, not a browser console.

Existing sources remain authoritative:

- project status and revision;
- generation jobs;
- append-only attempts;
- receipts;
- provider-switch proposals and decisions.

A small append-only `workflow_events` table fills only cross-stage gaps such as planning, validation, visual QA, finalization, and recovery. Each event contains:

- monotonically increasing event ID;
- project ID and revision;
- event type;
- phase and status;
- provider/model when relevant;
- attempt and bounded progress metadata;
- sanitized human summary;
- timestamp.

No prompt, story, API key, raw provider payload, filesystem path, or internal exception is allowed in an event.

Studio exposes replayable Server-Sent Events with an event ID cursor. A refreshed browser first reconstructs current state from durable records and then follows new events. Polling remains an allowed fallback; WebSocket infrastructure is unnecessary.

Representative events include:

- `planning.started`;
- `planning.repairing`;
- `plan.validated`;
- `workflow.waiting_for_plan_approval`;
- `generation.reference_started`;
- `generation.panel_completed`;
- `qa.panel_failed`;
- `qa.panel_passed`;
- `generation.panel_retrying`;
- `composition.completed`;
- `qa.page_passed`;
- `export.ready`;
- `workflow.blocked`;
- `workflow.resumed`.

## API surface

The implementation adds only the endpoints required for the local golden path:

- local session bootstrap;
- planning-provider/model availability;
- create or regenerate a planning job;
- read planning-job state;
- approve the current Plan and start production;
- pause or resume the production workflow;
- read the workflow snapshot;
- follow replayable workflow events.

Existing project, Plan update, generation option, generation job, provider-switch, raster, QA, and export endpoints remain in use. New responses use opaque IDs, safe envelopes, fixed bounds, revision checks, and idempotency keys.

The existing UI receives only minimal functional wiring: provider selectors, planning progress, Plan approval, panel previews, production controls, workflow state, and PDF download. Premium visual design is explicitly deferred.

## Failure and recovery behavior

- Missing credentials disable the affected provider option and explain the required environment variable without exposing its value.
- Planning timeout or provider failure leaves the canonical project at its prior valid revision and makes the planning job retryable.
- Invalid Plan output gets one bounded repair attempt; a second invalid result fails visibly without publishing partial artifacts.
- Image moderation, quota exhaustion, invalid raster, or provider failure uses the existing normalized failure taxonomy.
- Provider switching is never silent. Existing approval semantics remain authoritative.
- Visual QA failure retries only the failed panel and retains every attempt.
- Retry exhaustion, safety refusal, or an unavailable required capability leaves a resumable blocked workflow.
- Finalization failure retains accepted and lettered panels and resumes from the earliest stale deterministic stage.
- Server restart reclaims expired work leases and emits a recovery event; it does not claim completion until canonical validation succeeds.

## Testing and acceptance

Behavior changes follow RED-to-GREEN tests.

### Offline tests

- local-session bootstrap, loopback restriction, cookies, CSRF, ownership, revision, and idempotency;
- OpenAI and Anthropic planning adapters with mocked transports;
- strict Plan schema output, repair-once behavior, and atomic publication;
- independent planning/image selections and immutable provenance;
- OpenAI image adapter registration and sanitized credential resolution;
- panel and composed-page visual-QA pass, failure, retry, pause race, and automatic promotion;
- durable restart recovery without duplicate accepted work;
- append-only bounded workflow events and SSE replay;
- full prompt-to-PDF flow using fake providers and real deterministic finalization;
- existing Web, engine, packaging, security, and release regressions.

### Live acceptance

One cost-bounded local smoke must exercise:

1. prompt entry;
2. Plan generation with one configured planning provider;
3. human Plan review and approval;
4. OpenAI reference and panel generation;
5. provider-backed panel and composed-page visual QA;
6. automatic promotion and bounded retry behavior;
7. deterministic lettering and composition;
8. verified PDF download;
9. restart/resume from one non-terminal stage.

The result remains `offline-qualified` until that live smoke is explicitly authorized and retained. A live call requires a user-supplied credential and explicit cost ceiling.

## Governance and compatibility

This work preserves the deterministic engine's provider-neutral boundary and the existing 17-tool local MCP contract. It does not add provider SDK dependencies; the Web adapters use bounded HTTP transports already established by the repository.

Activating paid provider execution and adding Anthropic planning are material execution-surface changes. Before implementation is merged, the maintainer must record the Article 9 waiver in the tracking issue and pull request, or supply qualifying adoption evidence. The waiver must name this local single-user scope, OpenAI image execution, OpenAI/Anthropic planning, provider-backed visual QA, automatic post-QA promotion, and the cost-bounded live smoke boundary.

Documentation must continue to distinguish implemented, offline-qualified, manually exercised, and live-verified behavior.

## Out of scope

- premium UI design or a frontend framework migration;
- hosted multi-user authentication;
- browser-side provider calls or browser-visible credentials;
- billing, subscriptions, analytics, or telemetry;
- automatic unapproved provider fallback;
- enabling image providers other than OpenAI;
- planning providers other than OpenAI and Anthropic;
- changing the deterministic project schema solely for Web metadata;
- changing the 17-tool local MCP surface;
- publishing or deploying a hosted Studio instance.
