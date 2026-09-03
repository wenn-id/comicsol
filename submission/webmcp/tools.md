# WebMCP tool surface

The core WebMCP client (`web/comic_sol_web/static/webmcp.js`) registers the
exact tool list below. This list is generated from the merged module and is
verified by `web/tests/test_web_docs.py::WebMcpSurfaceContractTests`.

The Studio page also registers a small creator-facing facade from `app.js`.
That additive facade is intentionally **not** part of the exact core list below:
it provides the named operations `get_comic_context`, `create_comic`, and
`revise_comic` so a browser agent can create, inspect, and revise a Comic Sol
Plan without exposing revision or idempotency mechanics in the creative
request. The existing 14-tool production contract remains unchanged.

## Read tools (5)

- `get_project_state` — return the current project's `project_id`,
  `revision`, `status`, and two booleans (`plan_available`,
  `qa_available`). It does **not** return the Plan itself. The additive
  creator facade has a separate, explicit `get_comic_context` operation for
  creator-owned Plan content.
- `list_generation_options` — return the routes, models, and credential
  modes available to the current user.
- `recommend_provider` — recommend a provider for a panel given the current
  Plan, the queue, and the credential inventory.
- `list_generation_jobs` — return the current generation queue, including
  pending, in-flight, finished, and failed jobs.
- `get_qa_summary` — return whether a QA report is available for the current
  project, plus `valid` and `issue_count` when it is. It **cannot retrieve a
  prior QA result** on the current gateway path; treat it as an availability
  summary rather than a prior-report fetch API.

## Write tools (9)

- `create_project` — create a new project from a prompt or a pasted story.
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
- `export_project` — export the project as a private PDF or a portable
  archive.

## Creator facade

The page-level creator facade is additive to the core list above. It is
contract-tested separately by `web/tests/test_webmcp_creator_flow.py` and
reuses the authenticated Studio project API when available. On the hosted
static Studio, those creator operations can fall back to an ephemeral,
in-memory project so create/read/revise can be demonstrated without claiming a
deployed generation backend. Private Plan content is not persisted in browser
storage and disappears on refresh.

## Local MCP remains exactly 17 tools

The local MCP surface (the `comic-sol` MCP server registered in
`scripts/mcp_server.py`) remains exactly 17 `comic_*` tools and is independent
of the browser WebMCP surface.
