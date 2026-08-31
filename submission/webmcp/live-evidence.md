# WebMCP submission — live and deployment evidence (issue #321)

This page is the submission-side companion to
[`docs/web/live-evidence.md`](../../docs/web/live-evidence.md). It records
whether each piece of the Devpost WebMCP submission that would require a
live or deployed execution has been retained, and references the
gate-validated evidence bundle when (and only when) one exists.

## Current status: not run

None of the following has been executed or retained in this submission:

- **Working live URL.** No external deployment URL is claimed.
- **Active-agent WebMCP demonstration.** No real `document.modelContext`
  session has been run and retained.
- **ComfyUI smoke.** No local ComfyUI has been exercised through the
  agent-native handoff.
- **Paid/live provider smoke.** No paid or live provider route has been
  called.
- **Screenshot / video demonstration.** No video has been recorded; the
  submission ships a narration script only.
- **Native portable release asset.** No native portable artifact has been
  published as a release asset.

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
