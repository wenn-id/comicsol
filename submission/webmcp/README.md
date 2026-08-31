# Comic Sol Studio — WebMCP challenge submission

This submission packages the WebMCP-capable Comic Sol Studio for the
challenge. It is honest: it documents what is implemented, what is
offline-qualified, and what is **not** yet verified by an external
deployment, a live paid provider call, a local ComfyUI instance, or a
recorded video.

## Submission overview

Comic Sol Studio is the Web distribution of Comic Sol (`wenn-id/comicsol`).
It exposes a WebMCP tool surface (five read tools, nine write tools) whose
declared purpose is to let a browser-resident model context plan, generate,
and QA a comic project.

**That workflow is not callable against the shipped Web distribution
today.** The merged `create_app` installs no authentication router and no
`app.state.auth` service, so every API request these WebMCP tools make
fails `require_principal` with `401`. The workflow is currently executable
only through the fake-auth test harness (`web/tests/test_web_e2e.py`), or
after authentication wiring is added. See
[`docs/web/index.md` "Sign in"](../../docs/web/index.md#sign-in) and
[`docs/web/security.md` "Authentication and CSRF"](../../docs/web/security.md#authentication-and-csrf)
for the same boundary. The submission ships:

- this overview and the exact tool list;
- an architecture summary grounded in the merged source;
- a security and privacy summary grounded in the merged code;
- a demo script and a sanitized demo fixture;
- a narration demo (no video, no screenshots) backed by the runnable
  offline E2E flow;
- a provider evidence table;
- verification commands and recorded outcomes.

The WebMCP client is the only model-facing surface in this submission. The
local MCP surface remains exactly 17 `comic_*` tools and was not modified
by this work package; no production code, dependency, or migration was
touched.

## Source repository

- [https://github.com/wenn-id/comicsol](https://github.com/wenn-id/comicsol)

The work package is documented in `docs/web/` and submitted under
`submission/webmcp/`. Every claim below is backed by a path under that
repository at the pinned baseline.

## Architecture summary

Studio is a FastAPI application composed of:

- a provider-free composition root in `web/comic_sol_web/app.py` that
  exposes a single, deterministic `/healthz` endpoint and a static mount
  for the future UI;
- a GitHub OAuth authentication service in
  `web/comic_sol_web/auth.py` that records keyed digests of OAuth state
  and CSRF tokens and rejects replays;
- a generation service in
  `web/comic_sol_web/generation/service.py` and a credential broker in
  `web/comic_sol_web/generation/credentials.py` that supports the four
  credential modes and rotation;
- a transport policy in
  `web/comic_sol_web/generation/providers/http.py` that enforces the
  `https`/loopback origin policy, disables redirect following, and bounds
  connect/read/total timeouts;
- a WebMCP client in `web/comic_sol_web/static/webmcp.js` that registers
  the exact five read and nine write tools;
- a provider registry whose default surface is the deterministic
  `AgentProvider`; every other provider is offline-qualified through
  the same contract-tested harness.

The deterministic engine under `scripts/` and `comic_sol_product/` is
unchanged. It remains provider-neutral and never imports a provider SDK
or reads a provider credential.

## WebMCP tool surface

The exact WebMCP tool list is in [tools.md](tools.md). The published list
is read directly from `web/comic_sol_web/static/webmcp.js`; any drift
between the published list and the merged module fails
`web/tests/test_web_docs.py`.

## Security and privacy summary

The full security model is in [`docs/web/security.md`](../../docs/web/security.md).
The one-sentence summary is the rule the entire document supports:

> A provider credential is never exposed to the browser, written into a
> project archive, recorded in a receipt, emitted to a log, or included
> in this submission.

The other boundaries Studio enforces:

- GitHub OAuth with keyed digests of state and CSRF tokens;
- opaque IDs and owner-scoped lookups, with `not found` rather than
  `forbidden` for unrelated IDs;
- revision-bound, expiring provider-switch approvals;
- a transport policy that refuses non-loopback cleartext, private, and
  link-local/metadata origins, does not follow redirects, and bounds
  timeouts and response bytes;
- a credential broker with the four documented modes and a documented
  rotation path for persisted BYOK;
- **generation receipts** whose authorized field set is exactly
  `{provider, model, auth_mode, usage, checksum}`
  (`generation/receipts.py::AUTHORIZED_RECEIPT_FIELDS`), appended only
  when an accepted raster is recorded. Receipts carry no action, status,
  or error field; failed attempts live in the attempt history and
  provider-switch decisions live in the proposal and decision tables.
  Raw provider payloads never appear in a receipt, and any would-be
  credential value is replaced with `[REDACTED]`.

## Demo instructions

The demo is an **intended narrated WebMCP flow**, not an executed
WebMCP run. No WebMCP tool call was recorded; the flow is documented in
[demo.md](demo.md) and is bounded by the offline HTTP-only E2E test
(`web/tests/test_web_e2e.py`), which drives HTTP endpoints through a
fake-auth fixture rather than the WebMCP client. No screenshots or
video were produced in this work package.

## Deployment and recording status

- **Deployment:** not deployed. No production instance of Comic Sol
  Studio exists; no external deployment URL is claimed.
- **Recording:** no video was recorded. The submission ships a
  narration/demo script instead.
- **Live provider smoke:** none was run. No paid provider call was
  authorized or made in this work package.
- **Local ComfyUI:** none was available in this work package; no local
  ComfyUI evidence was recorded.
- **Active-agent WebMCP:** no browser environment in this work package
  exposed `document.modelContext`; the manual WebMCP demonstration was
  not run.

The submission is honest about every unavailable evidence class. None
are presented as passing or verified.

## Limitations

Every class of unavailable evidence is itemized in
[limitations.md](limitations.md). The summary at the top of that document
is the canonical list, and the list is also referenced from the PR
checkpoint on issue #268.

## Provider evidence

The provider verification table is in
[provider-evidence.md](provider-evidence.md). It mirrors the matrix in
[`docs/web/providers.md`](../../docs/web/providers.md) and adds a column
that points at retained evidence where it exists and `None` where it
does not. In this work package **no paid provider call was authorized**,
so every paid row shows `Not run` for live smoke and `None` for
evidence.

## Verification commands and results

Every gate run for this work package, with its real outcome, is recorded
in [verification.md](verification.md). Incomplete, timed-out, skipped, or
unavailable gates are recorded as such; nothing is described as passing
without an explicit recorded result.
