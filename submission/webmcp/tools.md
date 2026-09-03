# WebMCP tool surface

Comic Sol Studio now exposes two complementary WebMCP layers when a browser
model context is available:

1. `web/comic_sol_web/static/webmcp.js` keeps the exact, contract-tested
   **14 low-level Studio tools** for project state, generation, QA, provider
   decisions, and export.
2. `web/comic_sol_web/static/app.js` registers **3 creator-first tools** that
   reuse the same authenticated Studio API but hide revision, idempotency,
   provider, and job mechanics from the creative instruction.

The page therefore registers **17 WebMCP tools in total**: the existing 14
production primitives plus the 3 creator-facing tools below.

## Creator-first tools (3)

- `get_comic_context` — explicitly read the active creator-owned Plan body:
  `storyPlan`, `characterBible`, `storyboard`, and `visualIdentityPack`.
  This is intentionally separate from `get_project_state`, whose privacy
  boundary remains unchanged. The purpose is to let a browser agent reason
  about coherent story and character revisions instead of merely learning
  that a Plan exists.
- `create_comic` — create a 1–4 page comic or manga from creator-facing
  inputs: title, concept, language, page count, and visual style. The facade
  builds the Studio prompt and handles the revision/idempotency mechanics
  internally.
- `revise_comic` — persist an agent-revised Plan after the agent has read the
  current comic context. The creator supplies natural-language direction;
  low-level project revision and idempotency values stay inside Comic Sol.

These three tools are covered by
`web/tests/test_webmcp_creator_flow.py`. They are deliberately additive: the
existing `webmcp.js` contract remains untouched so the proven generation,
QA, provider-switch, and export primitives continue to work as before.

## Core read tools (5)

- `get_project_state` — return the current project's `project_id`,
  `revision`, `status`, and two booleans (`plan_available`, `qa_available`).
  It still does **not** return the Plan itself; `get_comic_context` is the
  explicit creator-facing operation for that content.
- `list_generation_options` — return the routes, models, and credential
  modes available to the current user.
- `recommend_provider` — recommend a provider for a panel given the current
  Plan, queue, and credential inventory.
- `list_generation_jobs` — return the current generation queue, including
  pending, in-flight, finished, and failed jobs.
- `get_qa_summary` — return whether a QA report is available for the current
  project, plus `valid` and `issue_count` when available. On the current
  gateway path it is an availability summary, not a prior-report fetch API.

## Core write tools (9)

- `create_project` — create a new project from a prompt or pasted story.
- `import_project` — import a project from a portable
  `.comic-sol-handoff` archive.
- `update_project_plan` — update the Plan within the same revision.
- `queue_generation` — queue a generation request for a panel.
- `submit_generated_asset` — submit a raster as a staged asset for a panel.
- `approve_provider_switch` — approve a provider switch proposed for the
  current revision.
- `reject_provider_switch` — reject a provider switch proposed for the
  current revision.
- `run_qa` — run QA over the project's promoted panels.
- `export_project` — export the project as a private PDF or portable archive.

## Local MCP remains exactly 17 tools

The local MCP surface (the `comic-sol` MCP server registered in
`scripts/mcp_server.py`) remains exactly 17 `comic_*` tools. Its count is
independent of the browser WebMCP surface and is not changed by this creator
facade.
