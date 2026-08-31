# WebMCP submission — live and deployment evidence (issue #321)

This page is the submission-side companion to
[`docs/web/live-evidence.md`](../../docs/web/live-evidence.md). It records
whether each piece of the Devpost WebMCP submission that would require a
live or deployed execution has been retained, and references the
gate-validated evidence bundle when (and only when) one exists.

## Current status: not run

None of the following has been executed or retained in this submission:

- **Active-agent WebMCP demonstration.** No real `document.modelContext`
  session has been run and retained.
- **ComfyUI smoke.** No local ComfyUI has been exercised through the
  agent-native handoff.
- **Paid/live provider smoke.** No paid or live provider route has been
  called.
- **Screenshot / video demonstration.** No workflow video has been recorded;
  the submission ships a narration script only. One static UI screenshot of
  the live deployment is retained (see below).
- **Native portable release asset.** No native portable artifact has been
  published as a release asset.

## Live deployment URL (retained, static-only)

A static-only deployment of the Studio UI is live and retained:

- **URL:** `https://comic-sol-studio.vercel.app`
- **Host:** Vercel Hobby (team `wenn-projects`); cost `USD 0.00`.
- **Candidate:** `948d853`.
- **Authorization:** recorded before execution — issue #321 comment
  [#issuecomment-5479494077](https://github.com/wenn-id/comicsol/issues/321#issuecomment-5479494077).
- **Retained bundle:** `evidence/web-live/948d853/`, validated by
  `scripts/live_web_evidence.py` (3 rows: `deploy-static-01`..`03`, all
  `pass`).

**Scope honesty:** this is the *Studio UI* only — `index.html`, `app.js`,
`api.js`, `state.js`, `styles.css`, `webmcp.js`, `views/`. The FastAPI
backend, SQLite database, durable generation queue, and every API route are
**not** deployed. The WebMCP `comic_*` tools register client-side in a real
browser session, matching what `submission/webmcp/README.md` already
discloses ("workflow is not callable through a deployed backend URL"). This
fulfills the **Working live URL** evidence row at the static-UI level; it is
not a claim that any backend workflow runs.

This submission therefore relies on the **offline-qualified** evidence the
work package produced: deterministic contract tests, the demo narration
(`demo.md`), and the provider-evidence table. Those are honest checks, not
live verification, and the submission does not claim otherwise.

## The one thing that is live-verifyable today

`/healthz` returns `{"status":"ok"}` from any merged `create_app` build
before any authenticated request. It is the only endpoint that requires no
principal. A judge, agent, or maintainer can verify a built instance is
running by requesting this endpoint; it proves process liveness only and is
deliberately provider-free (`docs/web/deployment.md` documents this).

## Evidence gate

When live evidence is authorized, retained, and validated, it is recorded
under the `web-live-evidence` bundle and validated by
[`scripts/live_web_evidence.py`](../../scripts/live_web_evidence.py).
`submission/webmcp/provider-evidence.md` is then updated so every
`live-verified` or `manually exercised` row links its retained bundle;
until then every row stays `Not run` / `offline-qualified`.

## Authorization reminder

Creating a live or deployed submission element requires an explicit
maintainer authorization (provider/host, maximum cost, candidate SHA,
evidence-retention destination). No evidence gate, tool, or this document
replaces that authorization.
