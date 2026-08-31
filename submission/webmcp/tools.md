# WebMCP tool surface

The WebMCP client (`web/comic_sol_web/static/webmcp.js`) registers the
exact tool list below. This list is generated from the merged module and
is verified by `web/tests/test_web_docs.py::WebMcpSurfaceContractTests`.

## Read tools (5)

- `get_project_state` — return the current Plan and the current revision.
- `list_generation_options` — return the routes, models, and credential
  modes available to the current user.
- `recommend_provider` — recommend a provider for a panel given the
  current Plan, the queue, and the credential inventory.
- `list_generation_jobs` — return the current generation queue, including
  pending, in-flight, finished, and failed jobs.
- `get_qa_summary` — return the most recent QA report for the project.

## Write tools (9)

- `create_project` — create a new project from a prompt or a pasted story.
- `import_project` — import a project from a portable
  `.comic-sol-handoff` archive.
- `update_project_plan` — update the Plan within the same revision.
- `queue_generation` — queue a generation request for a panel.
- `submit_generated_asset` — submit a raster as a staged asset for a
  panel.
- `approve_provider_switch` — approve a provider switch proposed for the
  current revision.
- `reject_provider_switch` — reject a provider switch proposed for the
  current revision.
- `run_qa` — run QA over the project's promoted panels.
- `export_project` — export the project as a private PDF or a portable
  archive.

## Local MCP remains exactly 17 tools

The local MCP surface (the `comic-sol` MCP server registered in
`scripts/mcp_server.py`) remains exactly 17 `comic_*` tools and is not
modified by this work package. WP17 is documentation-only and does not
touch the local MCP surface.
