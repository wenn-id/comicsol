# Comic Sol Studio — live evidence collection framework

This document defines how the Web distribution of Comic Sol records,
retains, and validates **live and manual deployment evidence** for the
routes and execution paths the project may describe as live-verified. It
implements the evidence-tracking contract in
[issue #321](https://github.com/wenn-id/comicsol/issues/321) and is the
exact-candidate companion to the honest state recorded in
[`providers.md`](providers.md), [`deployment.md`](deployment.md), and
[`submission/webmcp/provider-evidence.md`](../../submission/webmcp/provider-evidence.md).

## Current status

**One external evidence bundle is retained: a static-only deployment.** The
complete local workflow is separately **offline-qualified** by
`web/tests/test_live_golden_path.py`; that deterministic test uses fake
planning, image, and visual-review adapters, survives an application restart,
and validates accepted PNG and exported PDF magic bytes. It is test evidence,
not a live-evidence row and not a provider-quality claim.

As of this work package:

- An external Studio deployment has been exercised and published — **static
  UI only**. See "Retained evidence" below.
- No active-agent WebMCP demonstration through a real
  `document.modelContext` surface has been retained.
- No local ComfyUI smoke through the agent-native handoff has been
  retained.
- No paid/live provider route has exact-candidate smoke evidence.
- No workflow video has been retained. Two static UI screenshots of the
  deployment are retained.
- No native portable release asset has been published and smoke-qualified
  as a release asset.

### Retained evidence

| Field | Value |
| --- | --- |
| Bundle | `evidence/web-live/948d853/` |
| Candidate | `948d853665c7f4a2368bea64a010e44230664224` |
| Host | Vercel Hobby, team `wenn-projects` |
| Cost | `USD 0.00` |
| Authorization | issue #321 comment `5479494077`, recorded before execution |
| Rows | `deploy-static-01` (`GET /`), `deploy-static-02` (`GET /webmcp.js`), `deploy-static-03` (mobile capture) — all `pass` |
| Gate | `python -m scripts.live_web_evidence evidence/web-live/948d853 --candidate 948d853665c7f4a2368bea64a010e44230664224` |

**What the deployment does not include:** the FastAPI application, SQLite
state at `COMIC_SOL_WEB_DATA_ROOT`, the `DurableGenerationQueue` poller, and
every API route. Only the files under `web/comic_sol_web/static/` are served.
The deployment therefore proves asset delivery and client-side WebMCP tool
registration surface, not workflow execution. Rows 2–6 of the gap table
remain `Not run`.

### Reproduction config (retainer-only, not committed to `static/`)

The canonical static-surface contract
(`test_exact_static_surface_exists_and_uses_vanilla_modules`) defines the
exact set of files that may live under `web/comic_sol_web/static/`. The
deploy-time Vercel config must therefore NOT be added to `static/`; it is
reproduced at deploy time from this section only. The deploy that produced
the retained evidence was driven by `vercel deploy ./web/comic_sol_web/static
--prod --yes --archive=tgz --name comic-sol-studio --scope wenn-projects
--token $VERCEL_TOKEN`, where `$VERCEL_TOKEN` is a single-use maintainer
token supplied via shell env (never committed, never pasted in chat, and
revoked once the deployment is verified). The `web/comic_sol_web/static`
directory is used as-is; the canonical `index.html` / `app.js` / `webmcp.js`
/ etc. surface requires no transformation or build step. To disable
Vercel-wide team SSO so the URL is anonymously reachable (so the evidence
is actually verifiable), the maintainer runs the same `Authorization`
workflow plus a project-level
`PATCH /v9/projects/comic-sol-studio?teamId=<team>` with
`{"ssoProtection": null}`. Both inputs are out of scope for this framework
and require an explicit maintainer action.

The sections below define the exact procedure and validation gate so that
any future evidence is **retained, sanitized, and candidate-bound** — never
fabricated and never inflated. What is not run remains explicitly
unverified, matching the offline-qualified state already documented in
`submission/webmcp/`.

## Four distinct evidence states

Every claim in the provider and evidence matrices must map to exactly one
of these states, and no state may silently imply a stronger one:

| State | Meaning |
| --- | --- |
| `implemented` | The code path exists in the merged composition root. |
| `offline-qualified` | Deterministic, contract-tested, provider-free (the WP17 base). |
| `manually exercised` | Executed against a real local target (a local deployment, a local ComfyUI), retained, and validated by this gate. |
| `live-verified` | Executed against a paid/live provider or an external deployment, retained, cost-bounded, and validated by this gate. |

A row is `manually exercised` or `live-verified` only when its `Evidence`
column links a retained, gate-validated bundle. Until then the row stays
`offline-qualified` or `implemented`, and the claim in prose stays
unverified.

## Authorization boundaries

Recording this framework does **not** authorize:

- paid provider calls or spending;
- an external deployment or purchase of hosting;
- publication of a deployment URL;
- use or disclosure of credentials, cookies, tokens, private endpoints,
  local paths, private stories, raw provider payloads, or user comic
  content;
- new features, providers, WebMCP tools, migrations, dependencies, or
  unrelated production changes;
- adoption or creator-success claims.

**Before any paid call or external deployment, record an explicit
maintainer authorization** containing the provider/host, maximum cost,
candidate SHA, and evidence-retention destination. That authorization is a
prerequisite, not a formality; no evidence gate replaces it.

## Recording the exact candidate

Acceptance criterion #1 requires that *the exact candidate and
evidence-retention location are recorded before execution*. Each evidence
bundle therefore begins with a candidate record:

```json
{
  "schema": "web-live-evidence/1",
  "candidate": {
    "sha": "0123456789abcdef0123456789abcdef01234567",
    "engine_version": "2.x",
    "recorded_before_execution": true
  },
  "authorization": {
    "provider_or_host": "provider-name-or-host",
    "max_cost": "USD 0.00",
    "maintainer": "wenn-id"
  },
  "retention": {
    "location": "relative/path/under/evidence-bundle",
    "created_at": "2026-09-01T00:00:00Z"
  },
  "rows": []
}
```

The `candidate.sha` must be a 40-hex commit SHA, `recorded_before_execution`
must be `true`, and `authorization` must be present (it may record
"none" for offline-only manual evidence). Missing one of these fails the
gate; the gate never fabricates a candidate.

## Evidence rows

Each retained observation is one row. Every row carries an immutable
candidate, a date, an environment class, the route/provider/model/credential
mode **without revealing the credential value**, a sanitized step or command
summary, a success/failure result and bounded cost where applicable, an
artifact/checksum or redacted-receipt reference, and a known-limitations
field.

```json
{
  "id": "deploy-smoke-01",
  "kind": "deployment",
  "date": "2026-09-01",
  "environment": "external"
}
```

Supported `kind` values: `deployment`, `agent-webmcp`, `comfyui`,
`provider-smoke`, `media`, `release-asset-smoke`.

## Validation gate

[`scripts/live_web_evidence.py`](../../scripts/live_web_evidence.py) is the
fail-closed publication gate. It reads a local evidence bundle only. It
never calls a provider, never reads credentials, and never pans a remote
session. It validates:

- every record has **exactly** the allowed fields (unknown/missing → fail);
- `candidate.sha` is a 40-hex SHA with `recorded_before_execution: true`;
- evidence file paths are **contained** POSIX-relative paths (no absolute
  path, no `..`, no symlink traversal) and name regular files;
- each referenced file's SHA-256 matches the manifest binding;
- no string field contains a secret-shaped value, a newline, a `|`, or a
  backtick (no Markdown/control injection);
- every referenced media artifact decodes as a PNG/JPEG when declared as an
  image, or falls in a bounded size when declared as narration/evidence.

The gate emits a machine-readable summary and a Markdown summary only when
every requirement passes. On any failure it raises and exits non-zero; it
never down-samples or truncates evidence to make a bundle pass.

## Evidence retention layout

A retained bundle is committed under a private working directory and only
the reviewed subset is published. The gate reads from that published subset:

```text
web-live-evidence/
├── manifest.json
├── deployment/deployment.json
├── agent-webmcp/agent-webmcp.json
├── comfyui/comfyui.json
├── provider-smoke/<route>.json
├── media/<artifact>.json        # references hashes recorded in manifest
└── release-asset-smoke/release-asset-smoke.json
```

A bundle is never committed to this repository: it binds an immutable
candidate SHA, and a committed copy would go stale on the next commit and
invite exactly the fabrication this framework exists to prevent. The
tooling is instead proved by
[`tests/test_live_web_evidence.py`](../../tests/test_live_web_evidence.py),
which constructs bundles in a temporary directory and asserts both the
accept path and every rejection path.

## Acceptance-criteria mapping

| # | Criterion | Where it is addressed |
| --- | --- | --- |
| 1 | Exact candidate + retention location recorded before execution | `manifest.json` `candidate` + `retention`, validated by the gate |
| 2 | External deployment startup, health, restart persistence, backup/restore, rollback, secret rotation exercised & recorded | `deployment/deployment.json` rows + `docs/web/deployment.md` operator contract |
| 3 | Active-agent WebMCP demonstrated through real capability surface | `agent-webmcp/agent-webmcp.json` |
| 4 | Local ComfyUI demonstrated through agent-native handoff | `comfyui/comfyui.json` |
| 5 | Every live-verified route has one cost-bounded exact-candidate smoke | `provider-smoke/*.json` + `submission/webmcp/provider-evidence.md` |
| 6 | Screenshots/video/narration produced only from executed flows and sanitized | `media/*.json` |
| 7 | Claimed native portable assets published and smoke-qualified | `release-asset-smoke/*.json` + `scripts/portable_release_smoke.py` |
| 8 | Current-head CI + release qualification pass for the immutable candidate | this PR's CI + the repo release gates |
| 9 | Docs, submission, and matrices match retained evidence | `submission/webmcp/provider-evidence.md` evidence links |
| 10 | No credential/token/path/story committed | the gate's secret + containment validation |

## Related documents

- [Provider matrix](providers.md)
- [Security and privacy](security.md)
- [Deployment](deployment.md)
- [Rollback and recovery](rollback.md)
