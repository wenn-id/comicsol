# Devpost Submission Kit — Comic Sol Studio (WebMCP Challenge)

Copy-paste ready. Verified against the Official Rules (2026-09-01).
Deadline: **2026-09-03 13:00 PT / 16:00 EDT**.

## Title (pick one)

- Comic Sol Studio — agent-native comic creation with WebMCP
- Comic Sol Studio: humans and agents, making comics together

## Tagline

A browser-resident model context plans, generates, and QA-checks a comic
project through 14 WebMCP tools — while the human stays in control of
every approval.

## Live URL

```
https://comic-sol-studio.vercel.app
```

Static Studio UI: `index.html`, `app.js`, `api.js`, `state.js`,
`styles.css`, `webmcp.js`, `views/`. 14 WebMCP tools register client-side:
5 read (`get_project_state`, `list_generation_options`,
`recommend_provider`, `list_generation_jobs`, `get_qa_summary`) + 9 write
(`create_project`, `import_project`, `update_project_plan`,
`queue_generation`, `submit_generated_asset`, `approve_provider_switch`,
`reject_provider_switch`, `run_qa`, `export_project`).

> **Honest note (keep in the submission):** the deployed surface is the
> Studio UI and the WebMCP tool registration surface. The FastAPI
> backend, SQLite store, and durable generation queue are not deployed;
> API routes return 404 by design. Full workflow execution is
> offline-qualified through deterministic contract tests
> (`web/tests/test_web_e2e.py`, FakeProvider) and documented in the repo.

## Public code repository

```
https://github.com/wenn-id/comicsol
```

- License: MIT — visible in About section (repo API `license.spdx_id=MIT`).
- WebMCP implementation: `web/comic_sol_web/static/webmcp.js` —
  `document.modelContext.registerTool({name, description, inputSchema,
  execute})` for all 14 tools (`registerWebMcp()`, line 817).
- Pre-existing project, meaningfully extended during the Submission
  Period with timestamped commits:
  - `8a137a9` feat(web): add safe ComfyUI execution routes
  - `0f33374` feat(web): expose verified WebMCP site tools
  - `fbef19c` docs(web): qualify and document WebMCP Studio
  - `f0410be` docs: add Web/Studio live evidence collection framework

## Text description (400–800 words; paste into the form)

**Why this is a strong fit for WebMCP.** Comics are collaboration: story
→ plan → page breakdown → panel generation → visual QA → export. Today
that loop is manual or spread across disconnected tools. WebMCP lets a
website hand an agent a precise, typed tool surface — no scraping the UI,
no fragile selectors. Comic Sol Studio defines exactly what an agent may
do: create a project, inspect state, queue generation, submit rasters,
approve or reject a provider switch, run QA, export. The agent acts like
a production assistant that knows the API without being shown the DOM.

**How it creates a better user experience.** The human keeps every
judgment call. Creating a project, updating the plan, approving a
provider switch, and exporting are explicit approvals; generation and QA
are delegated. A writer can say "set up a dark noir detective story, six
panels, translate the plan" and the agent drafts the Plan. The artist
then reviews, approves a provider, and the agent queues panels, checks
QA, and preps the export. Control stays with the human; the repetitive
coordination disappears.

**What people and agents can do together that was difficult before.**
An agent inside the browser can now walk the full comic production
pipeline against a live product: read the project state, list what
generation options exist, queue work against an approved provider,
submit the produced raster, run QA, and export a portable archive —
without a custom integration, an API key in the page, or scraping. The
same surface that serves the human teams also serves their agents, so
the product improves for both at once.

**How WebMCP was implemented.** `web/comic_sol_web/static/webmcp.js`
registers the exact tool list through
`document.modelContext.registerTool(...)`, each with a strict
`inputSchema` (project ID, revision bounds, idempotency keys, UUID /
hex patterns), `additionalProperties: false`, and an `execute` bound to
the Studio API layer (`api.js`). The tool list is contract-tested:
`web/tests/test_web_docs.py::WebMcpSurfaceContractTests` fails on any
drift between the published list and the merged module. Security model:
a provider credential is never exposed to the browser, written into a
project archive, recorded in a receipt, emitted to a log, or included in
this submission. Generation receipts carry only
`{provider, model, auth_mode, usage, checksum}`.

**Honest scope note.** The deployed URL serves the Studio UI and the
WebMCP tool registration surface; the FastAPI backend, SQLite store, and
durable generation queue are not deployed on the free Vercel tier, so
API routes return 404. Workflow execution is offline-qualified: the E2E
suite (`web/tests/test_web_e2e.py`) drives the full HTTP flow through a
deterministic FakeProvider with zero network calls, and all 2,446 tests
(root 1,937 + web 509) pass at the exact head `f0410be`.

## Demonstration video script (≤ 3 minutes)

Record with OBS or the OS screen recorder. Must have audio. Upload to
YouTube, set Public, paste the link on the submission form.

| Time | Visual | Narration |
|---|---|---|
| 0:00–0:20 | Open `https://comic-sol-studio.vercel.app` in Chrome (149+, `chrome://flags/#enable-webmcp-testing` enabled), or ChatGPT desktop in-app browser. | "This is Comic Sol Studio, built for the WebMCP Challenge. It's a web app where a human and an agent make a comic together — the website exposes structured tools to the browser's model context." |
| 0:20–0:50 | DevTools / agent panel showing `document.modelContext.registerTool` definitions; scroll the 14 tools. | "The site registers 14 tools with the agent: create a project, inspect its state, list generation options, queue generation, submit a raster, approve or reject a provider switch, run QA, and export. Each tool has a strict input schema, so the agent can't guess — it gets a precise interface." |
| 0:50–1:40 | Run the offline E2E flow locally (commands below); show `create_project` → plan available → `queue_generation` → job listed → `run_qa` → `export_project`. | "Behind the scenes, the same flow is exercised end-to-end by a deterministic test harness with a fake provider — no network calls. The agent drive is the WebMCP surface; the engine stays provider-neutral and never sees a credential." |
| 1:40–2:20 | Show `web/comic_sol_web/static/webmcp.js` in the editor; point at `registerWebMcp()` and one tool's `inputSchema`. Show repo page with MIT license. | "Here's the implementation — `registerWebMcp` registers every tool against `document.modelContext`. The published tool list is contract-tested: if it drifts from the merged module, CI fails. The repo is open source, MIT licensed." |
| 2:20–2:55 | Recap: 14 tools, human approvals, honest scope note. | "What's new: an agent inside the browser can now coordinate a full comic production pipeline — plan, generate, QA, export — while the human keeps every approval. That's the agent-native web: one surface for people and their agents." |

### Local offline flow (for the video, step 0:50–1:40)

```bash
cd /home/acer/comicsol-wp17
source ~/.venvs/comicsol-wp16/bin/activate
PYTHONPATH=. python -m unittest web.tests.test_web_e2e -v
```

Expected tail: `OK` — the E2E suite drives the HTTP endpoints through a
fake-auth fixture and the deterministic `FakeProvider` (`fake-raster-v1`),
asserting `create_project` → plan → `queue_generation` → job →
submission → (1x1 raster rejected by WP3 validation, documented) →
`run_qa` → `export_project`.

> Do not claim any live paid provider call, a local ComfyUI run, or a
> `document.modelContext` session that was not actually recorded. The
> script above is the honest, reproducible core.

## Submission checklist

- [ ] Join hackathon (`https://webmcp.devpost.com/register`)
- [ ] Live URL: `https://comic-sol-studio.vercel.app`
- [ ] Video: record ≤ 3 min with audio, YouTube Public, no third-party
      music/trademarks
- [ ] Repo: `https://github.com/wenn-id/comicsol` (MIT visible)
- [ ] Description: paste the block above
- [ ] No credentials anywhere; no login credentials needed (static URL)
- [ ] Submit before **2026-09-03 13:00 PT / 16:00 EDT**