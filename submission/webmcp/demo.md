# Demo script

This document is a **demo script** (a narration, not a video). The demo
was not recorded as a video, and **no screenshots were produced** in this
work package: the environment exposed no `document.modelContext`, so the
WebMCP client could not be driven to a rendered screen, and WP17
authorizes no fabricated demonstration artifacts. This narration is the
honest record of the offline flow a reviewer can run to reproduce the
demo.

## Environment

The demo reproduces against a local checkout of the pinned baseline on
WSL. The Web distribution is invoked from a deterministic FakeProvider
harness; no provider network call is made. The only provider exercised is
the bundled `fake-raster-v1` model.

> **Honest note**
>
> No screenshot in this submission shows a live paid provider result.
> No screenshot of any kind is included; the demo is narrated and the
> underlying offline flow is runnable from the commands below.

## Steps

1. **Start the Web distribution with FakeProvider in offline mode.**
   The process is started with a complete, valid environment and a
   `FakeProvider` registered as the only provider. No live call is made
   at any point. The bundled E2E test
   (`web/tests/test_web_e2e.py::WebE2ETests::test_full_imported_archive_e2e`)
   drives exactly this offline flow.
2. **Create a project from a short prompt.** The demo project's
   `story.txt` is loaded as the prompt; the WebMCP `create_project`
   tool returns a Plan and a revision.
3. **Inspect the Plan.** The WebMCP `get_project_state` tool returns
   the Plan; the demo's Plan is recorded in `demo-project/plan.json`.
4. **Queue a generation.** The WebMCP `queue_generation` tool queues a
   panel against `fake-raster-v1`. The queue is read with
   `list_generation_jobs` to confirm the panel is in flight; the queue
   state is recorded in `demo-project/queue.json`.
5. **Submit a staged raster.** The WebMCP `submit_generated_asset`
   tool submits the deterministic FakeProvider raster as a staged
   asset. WP3 validation rejects the 1x1 FakeProvider raster, which the
   E2E test documents; the submission retains that honest boundary.
6. **Promote the staged raster.** Promotion is explicit; the
   staging-then-promotion boundary is enforced by the WebMCP write
   tools and documented in [docs/web/index.md](../../docs/web/index.md).
7. **Run QA.** The WebMCP `run_qa` tool runs deterministic QA over
   the project's promoted panels. QA is local and does not call a
   provider.
8. **Export the project.** The WebMCP `export_project` tool exports
   the project as a portable archive.

## Running the offline flow

The authoritative offline run is the merged E2E test, executed against
the same FakeProvider harness the demo describes:

```bash
python -m unittest web.tests.test_web_e2e
```

That test drives `create_project`, `queue_generation`,
`submit_generated_asset`, approval, and export entirely offline. It is
the deterministic, reproducible form of this narration.

## Demo fixture

A sanitized, offline-only demo fixture is at
`submission/webmcp/demo-project/`. It contains:

- `story.txt` — a short story used as the prompt for `create_project`;
- `plan.json` — the Plan produced by the deterministic WebMCP flow;
- `queue.json` — the queue state at the time of the offline run;
- `README.md` — a short note that the fixture is offline-only and
  contains no credentials.

The fixture does not contain a credential, an API key, a token, a
password, a session secret, a Bearer header, or any other value that
would identify a real provider account.
