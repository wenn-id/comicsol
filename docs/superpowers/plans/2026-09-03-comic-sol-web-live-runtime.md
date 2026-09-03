# Comic Sol Web Live Local Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local single-user Comic Sol Studio that turns a prompt into a reviewed canonical Plan, generates and visually reviews artwork through independently selected providers, resumes safely, and exports a verified PDF.

**Architecture:** Extend the existing FastAPI/SQLite Web distribution and `EngineGateway`; do not create a parallel Comic Sol pipeline. Planning and visual review use a new provider contract, image work continues through `GenerationService`, and a thin durable workflow coordinates the existing engine boundaries. The browser remains vanilla JavaScript and presents durable state through snapshots plus replayable Server-Sent Events.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, HTTPX, Pillow through the existing engine, vanilla JavaScript, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-03-comic-sol-web-live-runtime-design.md`

## Global Constraints

- Bind local mode to `127.0.0.1`; reject local-session bootstrap from a non-loopback client.
- Provider credentials come only from server environment variables and never enter browser state, artifacts, events, receipts, exports, or logs.
- Keep provider network code under `web/comic_sol_web/`; do not add provider SDK dependencies or network calls to `scripts/` or `comic_sol_product/`.
- Keep the canonical project schema and the 17-tool local MCP surface unchanged.
- Retain revision, ownership, CSRF, idempotency, path-containment, atomic-write, retry-budget, and append-only provenance invariants.
- Do not silently switch providers; planning and image provider selections are independent.
- Do not generate images before the user approves the Plan.
- Permit one Plan schema-repair call, at most two visual retries per panel, and at most eight extra image calls project-wide.
- Keep premium UI design and frontend framework migration out of scope.
- Keep tests offline and provider-free; a paid live smoke needs a user-supplied credential and explicit cost ceiling.
- Before merge, record the Article 9 maintainer waiver in both the tracking issue and pull request, naming the exact scope in the spec.

## File Structure

### New files

- `web/comic_sol_web/__main__.py` — local-only command entry point bound to loopback.
- `web/comic_sol_web/planning/types.py` — immutable Plan/visual-review provider values and protocols.
- `web/comic_sol_web/planning/providers.py` — bounded OpenAI and Anthropic HTTP adapters.
- `web/comic_sol_web/planning/service.py` — durable planning jobs and one-repair publication flow.
- `web/comic_sol_web/workflow.py` — durable workflow records, append-only events, and request-driven orchestration.
- `web/comic_sol_web/api/planning.py` — planning option, queue, and job endpoints.
- `web/comic_sol_web/api/workflows.py` — approval, snapshot, pause/resume, event replay, and SSE endpoints.
- `web/comic_sol_web/static/activity.js` — adjustable/collapsible activity drawer with EventSource fallback.
- `web/tests/test_local_runtime.py` — local configuration, bootstrap, loopback, and launcher coverage.
- `web/tests/test_planning_providers.py` — mocked provider transport and schema coverage.
- `web/tests/test_planning_service.py` — planning migration, queue, repair, and publication coverage.
- `web/tests/test_workflow.py` — orchestration, event, QA, retry, pause, and recovery coverage.
- `web/tests/test_live_golden_path.py` — fake-provider prompt-to-PDF integration test.

### Existing files to modify

- `web/comic_sol_web/config.py` — explicit local configuration and provider/model availability.
- `web/comic_sol_web/auth.py` — reusable local cookie policy without weakening hosted behavior.
- `web/comic_sol_web/api/auth.py` — loopback-only session bootstrap route.
- `web/comic_sol_web/app.py` — lazy composition of auth, planning, OpenAI image generation, and workflow services.
- `web/comic_sol_web/migrations.py` — contiguous versions 9 and 10 for planning/workflow state.
- `web/comic_sol_web/engine_gateway.py` — safe source/context reads and deterministic QA/finalization entry points.
- `web/comic_sol_web/projects.py` — owner-bound wrappers for the new gateway operations.
- `web/comic_sol_web/generation/catalog.py` — current configured OpenAI image model.
- `web/comic_sol_web/generation/providers/openai.py` — model injection from trusted configuration.
- `web/comic_sol_web/generation/service.py` — subject metadata and workflow-safe staged/accepted helpers.
- `web/comic_sol_web/api/generation.py` — expose bounded subject metadata needed for panel rendering.
- `web/comic_sol_web/static/api.js` — local bootstrap, planning, workflow, and event API functions.
- `web/comic_sol_web/static/state.js` — planning/workflow/activity state.
- `web/comic_sol_web/static/app.js` — bootstrap before restore and mount the activity drawer.
- `web/comic_sol_web/static/views/start.js` — planning selection and automatic planning job creation.
- `web/comic_sol_web/static/views/plan.js` — planning progress/regeneration, image selection, and Plan approval.
- `web/comic_sol_web/static/views/generate.js` — workflow-driven progress and panel previews.
- `web/comic_sol_web/static/views/review.js` — final QA state and ready PDF download.
- `web/comic_sol_web/static/index.html` — functional drawer container only.
- `web/comic_sol_web/static/styles.css` — minimal resize/collapse/layout rules only.
- `web/pyproject.toml` — package the new JavaScript asset and expose `comic-sol-web`.
- `web/tests/support.py` — local environment and fake provider helpers.
- `web/tests/test_app.py`, `web/tests/test_auth.py`, `web/tests/test_provider_openai.py`, `web/tests/test_studio_start_plan.py`, `web/tests/test_studio_generate_review.py`, `web/tests/test_web_docs.py` — update frozen contracts intentionally.
- `docs/web/index.md`, `docs/web/providers.md`, `docs/web/security.md`, `docs/web/deployment.md`, `docs/web/live-evidence.md` — document implemented versus live-verified status and local startup.

---

### Task 1: Local single-user runtime and bootstrap

**Files:**
- Create: `web/comic_sol_web/__main__.py`
- Create: `web/tests/test_local_runtime.py`
- Modify: `web/comic_sol_web/config.py`
- Modify: `web/comic_sol_web/auth.py`
- Modify: `web/comic_sol_web/api/auth.py`
- Modify: `web/comic_sol_web/app.py`
- Modify: `web/pyproject.toml`
- Test: `web/tests/test_app.py`
- Test: `web/tests/test_auth.py`

**Interfaces:**
- Produces: `WebConfig.local_from_env(environ: Mapping[str, str]) -> WebConfig`.
- Produces: `create_local_session_router(service_source: Any) -> APIRouter` with `POST /api/auth/local-session`.
- Produces: `main() -> int`, always running Uvicorn on `127.0.0.1`.
- Preserves: `WebConfig.from_env()` and hosted `AuthService` behavior.

- [ ] **Step 1: Write failing local configuration and bootstrap tests**

```python
class LocalRuntimeTests(unittest.TestCase):
    def test_local_config_needs_only_a_data_root(self):
        config = WebConfig.local_from_env({"COMIC_SOL_WEB_DATA_ROOT": str(self.root)})
        self.assertTrue(config.local_mode)
        self.assertEqual(self.root, config.data_root)
        self.assertNotIn(config.session_secret, repr(config))

    def test_bootstrap_issues_session_and_csrf_cookies_on_loopback(self):
        with TestClient(create_app(self.config), client=("127.0.0.1", 50000)) as client:
            response = client.post("/api/auth/local-session")
            self.assertEqual(200, response.status_code)
            self.assertEqual("local", response.json()["login"])
            self.assertIn("comic_sol_session", response.cookies)
            self.assertIn("comic_sol_csrf", response.cookies)

    def test_bootstrap_rejects_non_loopback_client(self):
        with TestClient(create_app(self.config), client=("192.0.2.10", 50000)) as client:
            self.assertEqual(403, client.post("/api/auth/local-session").status_code)
```

- [ ] **Step 2: Run the tests and verify the missing local runtime fails**

Run: `python -m unittest web.tests.test_local_runtime -v`

Expected: FAIL because `WebConfig.local_from_env` and the bootstrap route do not exist.

- [ ] **Step 3: Add explicit local configuration without persisted secrets**

Add `local_mode: bool` and trusted model fields to `WebConfig`. Implement `local_from_env()` by requiring an absolute `COMIC_SOL_WEB_DATA_ROOT`, generating process-local session/encryption secrets with `secrets.token_urlsafe(48)`, and recording only environment-variable names for available provider credentials. Keep `from_env()` strict and set `local_mode=False` there.

```python
@classmethod
def local_from_env(cls, environ: Mapping[str, str]) -> "WebConfig":
    raw_root = _require(environ, DATA_ROOT_VAR)
    data_root = Path(raw_root)
    if not data_root.is_absolute():
        raise WebConfigError(f"{DATA_ROOT_VAR} must be an absolute path")
    return cls(
        session_secret=secrets.token_urlsafe(48),
        encryption_secret=secrets.token_urlsafe(48),
        data_root=data_root,
        hosted_secret_references=MappingProxyType({
            provider: variable
            for provider, variable in {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
            }.items()
            if environ.get(variable)
        }),
        master_key_references=MappingProxyType({}),
        active_credential_key_id=None,
        local_mode=True,
    )
```

- [ ] **Step 4: Reuse `AuthService` for a fixed local principal**

Add a `secure_cookies: bool = True` constructor value and use it in every cookie setter/deleter. In local mode create `AuthService(..., github_oauth=None, secure_cookies=False)` and expose only `create_session`, authentication, CSRF, and revoke. `begin_oauth()` must fail closed when the OAuth client is absent.

The bootstrap route must require `ipaddress.ip_address(request.client.host).is_loopback`, create `SessionPrincipal("comic-sol-local-user", "local")`, call `create_session`, and issue cookies. Repeated calls may rotate the browser session but must keep the same owner ID.

- [ ] **Step 5: Add the loopback-only launcher and composition wiring**

```python
def main() -> int:
    config = WebConfig.local_from_env(os.environ)
    uvicorn.run(create_app(config), host="127.0.0.1", port=8765, log_level="info")
    return 0
```

Register `comic-sol-web = "comic_sol_web.__main__:main"` in `[project.scripts]`. Build/cache the auth service lazily from the same `EngineGateway` database as projects, set `app.state.auth`, and register `create_local_session_router` only when `config.local_mode` is true.

- [ ] **Step 6: Run focused auth and app tests**

Run: `python -m unittest web.tests.test_local_runtime web.tests.test_auth web.tests.test_app -v`

Expected: PASS, including hosted secure-cookie behavior and provider-free `/healthz` construction.

- [ ] **Step 7: Commit the local runtime**

```bash
git add web/comic_sol_web/__main__.py web/comic_sol_web/config.py web/comic_sol_web/auth.py web/comic_sol_web/api/auth.py web/comic_sol_web/app.py web/pyproject.toml web/tests/test_local_runtime.py web/tests/test_auth.py web/tests/test_app.py
git commit -m "feat(web): add loopback single-user runtime"
```

---

### Task 2: Register configurable OpenAI image generation

**Files:**
- Modify: `web/comic_sol_web/config.py`
- Modify: `web/comic_sol_web/app.py`
- Modify: `web/comic_sol_web/generation/catalog.py`
- Modify: `web/comic_sol_web/generation/providers/openai.py`
- Modify: `web/tests/test_provider_openai.py`
- Modify: `web/tests/test_credentials.py`
- Modify: `web/tests/test_app.py`
- Modify: `web/tests/test_web_docs.py`

**Interfaces:**
- Produces: `WebConfig.openai_image_model: str`, default `gpt-image-2`.
- Produces: `OpenAIProvider(model: str, transport: AsyncBaseTransport | None = None)`.
- Preserves: `ProviderRegistry`, `CredentialBroker`, `GenerationRequest`, and receipt shapes.

- [ ] **Step 1: Write failing registration and model-injection tests**

```python
def test_local_app_exposes_openai_only_when_key_exists(self):
    environment = local_environment(self.root)
    environment["OPENAI_API_KEY"] = "test-openai-key"
    app = create_app(WebConfig.local_from_env(environment))
    with authenticated_local_client(app) as client:
        options = client.get("/api/generation/options").json()["options"]
    self.assertEqual(["openai"], [item["provider"] for item in options])
    self.assertEqual("gpt-image-2", options[0]["model"])

    def test_adapter_uses_injected_model(self):
        provider = OpenAIProvider(model="gpt-image-2", transport=transport)
        result = asyncio.run(provider.generate(request, "gpt-image-2", "secret"))
        self.assertEqual("accepted", result.state.value)

    def test_adapter_normalizes_native_output_to_handoff_dimensions(self):
        request = generation_request(width=736, height=1136)
        result = asyncio.run(provider.generate(request, "gpt-image-2", "secret"))
        self.assertEqual((736, 1136), png_size(result.raster_bytes))
        self.assertEqual("1024x1536", captured_request_json["size"])
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `python -m unittest web.tests.test_provider_openai web.tests.test_credentials web.tests.test_app web.tests.test_web_docs -v`

Expected: FAIL because the merged app still registers only `AgentProvider` and the catalog is fixed to `gpt-image-1`.

- [ ] **Step 3: Inject the trusted OpenAI image model**

Validate `COMIC_SOL_WEB_OPENAI_IMAGE_MODEL` with the existing provider/model identifier bounds, default it to `gpt-image-2`, and construct the adapter with that value. Remove the module-level catalog lookup; the adapter returns one `ProviderModel` containing its configured model and the existing OpenAI capabilities.

- [ ] **Step 4: Normalize OpenAI native sizes to exact handoff dimensions**

Map a requested aspect ratio to `1024x1024`, `1024x1536`, or `1536x1024`, then use Pillow in memory to center-crop and resize the returned PNG to `request.width × request.height` with LANCZOS. Re-encode a deterministic RGB PNG and validate it before returning `GenerationResult`. Record both `provider_size` and `requested_size` in `effective_parameters`; keep the canonical receipt free of raster bytes and prompts.

- [ ] **Step 5: Register OpenAI only when the server credential is available**

In `_generation_service`, build the provider tuple from `AgentProvider` plus `OpenAIProvider(config.openai_image_model)` only when `openai` exists in `config.hosted_secret_references`. Continue resolving the actual key through `CredentialBroker` with `AuthMode.HOSTED`; never pass it into app state or the response envelope.

- [ ] **Step 6: Update the frozen provider assertions intentionally**

Replace the old assertion that no paid adapter can be wired with these assertions:

```python
self.assertNotIn("OpenAIProvider", providers_without_key)
self.assertIn("OpenAIProvider", providers_with_key)
self.assertNotIn("credential", generation_options_json)
self.assertNotIn("test-openai-key", generation_options_text)
```

- [ ] **Step 7: Run focused provider/security tests**

Run: `python -m unittest web.tests.test_provider_openai web.tests.test_credentials web.tests.test_web_docs web.tests.test_web_security -v`

Expected: PASS with mocked HTTP only.

- [ ] **Step 8: Commit OpenAI image routing**

```bash
git add web/comic_sol_web/config.py web/comic_sol_web/app.py web/comic_sol_web/generation/catalog.py web/comic_sol_web/generation/providers/openai.py web/tests/test_provider_openai.py web/tests/test_credentials.py web/tests/test_app.py web/tests/test_web_docs.py
git commit -m "feat(web): route configured OpenAI image generation"
```

---

### Task 3: Planning and visual-review provider contracts

**Files:**
- Create: `web/comic_sol_web/planning/__init__.py`
- Create: `web/comic_sol_web/planning/types.py`
- Create: `web/comic_sol_web/planning/providers.py`
- Create: `web/tests/test_planning_providers.py`
- Modify: `web/comic_sol_web/config.py`

**Interfaces:**
- Produces: `PlanRequest`, `PlanResult`, `VisualReviewRequest`, `VisualReviewResult`, `PlanningModel`, and `PlanningProvider`.
- Produces: `OpenAIPlanningProvider` and `AnthropicPlanningProvider`.
- Consumes: existing `BoundedHTTPClient`, `TransportPolicy`, `ProviderError`, and `ErrorCategory`.

- [ ] **Step 1: Write failing immutable-contract tests**

```python
class PlanningProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_returns_complete_four_document_plan(self):
        provider = OpenAIPlanningProvider(model="gpt-5.4-mini", transport=self.transport)
        result = await provider.generate_plan(self.request, "secret")
        self.assertEqual(
            {"storyPlan", "characterBible", "storyboard", "visualIdentityPack"},
            set(result.plan),
        )
        self.assertNotIn("secret", repr(result))

    async def test_anthropic_rejects_text_without_required_tool_result(self):
        provider = AnthropicPlanningProvider(model="claude-sonnet-4-6", transport=self.transport)
        with self.assertRaisesRegex(ProviderError, "output is invalid"):
            await provider.generate_plan(self.request, "secret")

    async def test_visual_review_is_bounded_to_requested_check_ids(self):
        result = await provider.review_visual(self.visual_request, "secret")
        self.assertEqual(self.visual_request.check_ids, tuple(c["id"] for c in result.checks))
```

- [ ] **Step 2: Run provider tests and verify imports fail**

Run: `python -m unittest web.tests.test_planning_providers -v`

Expected: FAIL because the planning package does not exist.

- [ ] **Step 3: Define the immutable provider values**

```python
@dataclass(frozen=True)
class PlanRequest:
    title: str
    source: str = field(repr=False)
    language: str
    page_count: int
    validation_errors: tuple[str, ...] = ()

@dataclass(frozen=True)
class PlanResult:
    plan: Mapping[str, str] = field(repr=False)
    usage: Mapping[str, int | float | str] = field(repr=False)

@dataclass(frozen=True)
class VisualReviewRequest:
    kind: Literal["panel", "page"]
    subject_id: str
    raster: bytes = field(repr=False)
    context: Mapping[str, object] = field(repr=False)
    check_ids: tuple[str, ...]

@dataclass(frozen=True)
class VisualReviewResult:
    checks: tuple[Mapping[str, object], ...]
    character_assessments: tuple[Mapping[str, object], ...]
    usage: Mapping[str, int | float | str] = field(repr=False)

@dataclass(frozen=True)
class PlanningModel:
    provider: str
    model: str
    enabled: bool
    required_environment_variable: str | None

class PlanningProvider(Protocol):
    provider_id: str
    model: str
    async def generate_plan(self, request: PlanRequest, credential: str) -> PlanResult: ...
    async def review_visual(self, request: VisualReviewRequest, credential: str) -> VisualReviewResult: ...
```

Freeze nested values in `__post_init__`, enforce source/raster/check bounds, and suppress narrative/raster/credential data from `repr`.

- [ ] **Step 4: Implement one shared canonical prompt/schema builder**

Keep provider-specific envelopes separate but share these exact output boundaries:

- Plan result: exactly four object-valued keys, serialized with sorted keys to the string envelope expected by `EngineGateway.update_plan()`.
- Panel result: exactly the seven ordered panel check IDs plus character trait assessments when characters are present.
- Page result: only the subjective page check IDs; deterministic page checks remain engine-derived.
- Evidence: non-empty, non-generic, bounded strings; regions are bounded arrays; usage contains only numeric/token fields.

Use a strict top-level JSON schema with `additionalProperties: false`; canonical engine validation remains the final authority for nested Plan semantics.

- [ ] **Step 5: Implement bounded OpenAI and Anthropic adapters**

OpenAI uses `POST https://api.openai.com/v1/responses`, bearer authorization, structured JSON output, and `data:image/png;base64,...` only inside the transient request. Anthropic uses `POST https://api.anthropic.com/v1/messages`, `x-api-key`, `anthropic-version`, one forced tool with `input_schema`, and a base64 image block only inside the transient request. Both use `BoundedHTTPClient`, reject redirects/oversized bodies, normalize HTTP failures, and never retain raw responses.

- [ ] **Step 6: Add configurable model defaults**

Add and validate:

```text
COMIC_SOL_WEB_OPENAI_PLANNING_MODEL=gpt-5.4-mini
COMIC_SOL_WEB_ANTHROPIC_PLANNING_MODEL=claude-sonnet-4-6
COMIC_SOL_WEB_OPENAI_IMAGE_MODEL=gpt-image-2
```

Expose only models whose matching API-key environment variable is present.

- [ ] **Step 7: Run provider tests**

Run: `python -m unittest web.tests.test_planning_providers web.tests.test_app -v`

Expected: PASS; mocked request bodies contain the prompt/image, while returned values and exception text do not leak credentials or raw responses.

- [ ] **Step 8: Commit provider contracts**

```bash
git add web/comic_sol_web/planning web/comic_sol_web/config.py web/tests/test_planning_providers.py web/tests/test_app.py
git commit -m "feat(web): add planning and visual review providers"
```

---

### Task 4: Durable planning jobs and automatic Plan publication

**Files:**
- Create: `web/comic_sol_web/planning/service.py`
- Create: `web/comic_sol_web/api/planning.py`
- Create: `web/tests/test_planning_service.py`
- Modify: `web/comic_sol_web/migrations.py`
- Modify: `web/comic_sol_web/engine_gateway.py`
- Modify: `web/comic_sol_web/projects.py`
- Modify: `web/comic_sol_web/app.py`

**Interfaces:**
- Produces: `PlanningJob`, `PlanningService.queue()`, `PlanningService.run_once()`, `PlanningService.get()`, and `PlanningService.options()`.
- Produces: `ProjectService.planning_input(principal, project_id, expected_revision) -> PlanRequest`.
- Produces endpoints: `GET /api/planning/options`, `POST /api/planning/jobs`, `GET /api/planning/jobs/{job_id}`.
- Consumes: Task 3 `PlanningProvider` and existing `ProjectService.update_plan()`.

- [ ] **Step 1: Write failing migration, queue, repair, and API tests**

```python
def test_invalid_first_plan_is_repaired_once_and_published_atomically(self):
    provider.results = [invalid_plan(), valid_plan()]
    job = self.service.queue(self.alice, self.project_id, 1, "openai", "gpt-5.4-mini", self.key)
    completed = asyncio.run(self.service.run_once("planning-test-worker"))
    self.assertEqual("ready_for_review", completed.state)
    self.assertEqual(2, completed.attempt_count)
    snapshot = self.projects.read_plan(self.alice, self.project_id)
    self.assertTrue(all(snapshot.summary["plan"].values()))

def test_second_invalid_plan_fails_without_partial_plan(self):
    provider.results = [invalid_plan(), invalid_plan()]
    asyncio.run(self.service.run_once("planning-test-worker"))
    self.assertEqual("failed", self.service.get(self.alice, self.job_id).state)
    self.assertTrue(all(value == "" for value in self.projects.read_plan(self.alice, self.project_id).summary["plan"].values()))
```

Also assert owner isolation, UUID idempotency, stale revision rejection, lease reclaim after expiry, sanitized errors, and no raw result column.

- [ ] **Step 2: Run the tests and verify migration/service failures**

Run: `python -m unittest web.tests.test_planning_service -v`

Expected: FAIL because migration 9 and `PlanningService` do not exist.

- [ ] **Step 3: Add migration 9**

Create `planning_jobs` with bounded columns: `job_id`, `idempotency_key`, `owner_id`, `project_id`, `project_revision`, `provider`, `model`, `state`, `attempt_count`, `usage_json`, `error_category`, lease fields, `published_revision`, and timestamps. Allowed states are `queued`, `running`, `repairing`, `ready_for_review`, `failed`, and `cancelled`. Add owner/project and lease indexes. Do not add provider request/response or Plan JSON columns.

Publish `PLANNING_MIGRATIONS = (*APPROVAL_MIGRATIONS, PLANNING_MIGRATION)` and keep versions contiguous from 1 through 9.

- [ ] **Step 4: Add the owner-bound planning input read**

`EngineGateway.planning_input(project_id, expected_revision)` reads only canonical title, bounded UTF-8 `source/input.txt`, `source/request.json`, and page count under `ProjectLock(read_only=True)`. Return `PlanRequest`; do not expose the project filesystem path. Wrap it in `ProjectService.planning_input()` after `_authorize()`.

- [ ] **Step 5: Implement lease-based planning execution**

`queue()` validates owner/project/revision/provider/model and canonical UUID idempotency. `run_once()` must:

1. lease the oldest queued or expired running job;
2. resolve the job owner's hosted credential;
3. call `generate_plan()`;
4. call `ProjectService.update_plan()` only with the complete four-document envelope;
5. on canonical validation failure, call the same provider once with `validation_errors=(safe_error,)`;
6. publish only the valid result and record `published_revision` plus sanitized usage;
7. mark the job failed after the second invalid result or a normalized provider failure.

No successful provider response is written to SQLite; the canonical Plan files are the only retained result.

- [ ] **Step 6: Add planning endpoints and bounded request pumping**

`POST /api/planning/jobs` requires CSRF, `Idempotency-Key`, `project_id`, `expected_revision`, `provider`, and `model`; it queues the job and adds one bounded `_consume_planning_queue` background task. `GET` endpoints require the owner session, return private/no-store responses, and expose only IDs, state, attempt count, provider/model, published revision, usage, and sanitized error category.

- [ ] **Step 7: Wire the service lazily in `create_app`**

Construct the provider registry from configured OpenAI/Anthropic adapters, reuse the existing `CredentialBroker`, and cache `app.state.planning`. Registering the router must not initialize storage or call a provider during `create_app()` or `/healthz`.

- [ ] **Step 8: Run planning, project, and security tests**

Run: `python -m unittest web.tests.test_planning_service web.tests.test_projects web.tests.test_web_security web.tests.test_database -v`

Expected: PASS, including crash/re-lease and atomic Plan publication.

- [ ] **Step 9: Commit durable planning**

```bash
git add web/comic_sol_web/planning/service.py web/comic_sol_web/api/planning.py web/comic_sol_web/migrations.py web/comic_sol_web/engine_gateway.py web/comic_sol_web/projects.py web/comic_sol_web/app.py web/tests/test_planning_service.py web/tests/test_projects.py web/tests/test_web_security.py web/tests/test_database.py
git commit -m "feat(web): add durable provider-authored planning"
```

---

### Task 5: Canonical panel/page QA and deterministic finalization gateway

**Files:**
- Modify: `web/comic_sol_web/engine_gateway.py`
- Modify: `web/comic_sol_web/projects.py`
- Create: `web/tests/test_workflow.py`
- Test: `tests/test_character_quality.py`
- Test: `tests/test_page_quality.py`
- Test: `tests/test_finalize.py`

**Interfaces:**
- Produces: `ProjectService.panel_review_input(principal, project_id, expected_revision, panel_id) -> VisualReviewRequest`.
- Produces: `ProjectService.publish_panel_review(principal, project_id, expected_revision, panel_id, review) -> ProjectSnapshot`.
- Produces: `ProjectService.prepare_pages(principal, project_id, expected_revision) -> ProjectSnapshot`.
- Produces: `ProjectService.page_review_input(principal, project_id, expected_revision, page_number) -> VisualReviewRequest`.
- Produces: `ProjectService.publish_page_review(principal, project_id, expected_revision, page_number, review) -> ProjectSnapshot`.
- Produces: `ProjectService.finalize(principal, project_id, expected_revision) -> tuple[ProjectSnapshot, Path]`.
- Consumes: Task 3 `VisualReviewResult` and existing engine normalization, character-quality, page-quality, repair, and finalization functions.

- [ ] **Step 1: Write failing gateway tests for real canonical artifacts**

Create a one-panel canonical fixture, accept a fake raster through the existing handoff, then assert:

```python
request = projects.panel_review_input(alice, project_id, revision, "p01-01")
self.assertEqual("panel", request.kind)
self.assertEqual(PANEL_CHECK_IDS, request.check_ids)

snapshot = projects.publish_panel_review(alice, project_id, revision, "p01-01", passing_review)
record = read_json(snapshot.root / "qa/panels/p01-01.json")
self.assertEqual("accept", record["decision"])
self.assertTrue((snapshot.root / "panels/p01-01/clean.png").is_file())

composed = projects.prepare_pages(alice, project_id, snapshot.revision)
page_request = projects.page_review_input(alice, project_id, composed.revision, 1)
self.assertEqual("page", page_request.kind)
page_reviewed = projects.publish_page_review(
    alice, project_id, composed.revision, 1, passing_page_review
)
snapshot, pdf = projects.finalize(alice, project_id, page_reviewed.revision)
self.assertTrue(pdf.is_file())
self.assertEqual("COMPLETE", snapshot.status)
```

Add negative assertions for stale revision, wrong panel/page ID, missing character trait assessment, generic evidence, stale page hash, and failed page QA.

- [ ] **Step 2: Run focused gateway tests and verify methods are missing**

Run: `python -m unittest web.tests.test_workflow -v`

Expected: FAIL because the QA/finalization gateway methods do not exist.

- [ ] **Step 3: Build panel review input from canonical state**

Under the existing read lock, build the provider context from the current raw raster, storyboard panel data, character consistency context, generated-SFX requirements, adjacent-panel anchors, and requested dimensions. This method is read-only: return bytes and bounded JSON values, never paths outside canonical relative identifiers.

- [ ] **Step 4: Publish panel QA through existing validators**

Normalize `panels/raw/{panel_id}.png` through `normalize_panel(..., mode="exact")` when the current clean binding is absent or stale. Construct schema-2.0 bindings from the current raw, clean, and normalization hashes/dimensions. Use `build_character_identity_check()` for trait provenance, combine it with the other six ordered checks, derive `accept`, `accept-warning`, or `regenerate`, validate through `validate_panel_record()` and `validate_panel_provenance()`, and publish with `ProjectTransaction`.

When all panel records accept the current rasters, transition through `PANELS_READY` to `QA_READY` and record the generation stage. On `regenerate`, call `plan_and_write_repair_plan()` and invalidate from the registered `generation` stage before preparing the next visual-retry handoff; do not advance status. Every engine mutation reconciles the Web revision exactly once.

- [ ] **Step 5: Prepare deterministic pages without fabricating page QA**

`prepare_pages()` runs existing `letter_project()`, records/transitions lettering, runs `compose_project()`, records/transitions composition, and stops at `COMPOSED`. It must not call `finalize_project()` because page QA is still absent.

- [ ] **Step 6: Build and publish composed-page QA**

After deterministic lettering/composition, return a page `VisualReviewRequest` containing the page raster and only subjective check IDs. Publish with `page_quality.publish_page_quality_record(...)`; that function adds deterministic checks and binds all hashes under one lock. A failed record leaves export blocked and retains normalized evidence.

- [ ] **Step 7: Add a single finalization gateway call**

Call existing `comic_sol.finalize_project(root)` only after every current page-QA record validates. Return the verified PDF under `exports/{engine_project_id}.pdf`, reconcile revision/state, and reuse the current private export endpoint for download. Never duplicate lettering, composition, report, or PDF logic in Web code.

- [ ] **Step 8: Run engine and gateway regression tests**

Run: `python -m unittest web.tests.test_workflow tests.test_character_quality tests.test_page_quality tests.test_finalize -v`

Expected: PASS with real normalization, provenance validation, composition, page QA, report, and PDF bytes.

- [ ] **Step 9: Commit QA/finalization boundaries**

```bash
git add web/comic_sol_web/engine_gateway.py web/comic_sol_web/projects.py web/tests/test_workflow.py
git commit -m "feat(web): expose canonical QA and finalization boundaries"
```

---

### Task 6: Durable production workflow and append-only events

**Files:**
- Create: `web/comic_sol_web/workflow.py`
- Create: `web/comic_sol_web/api/workflows.py`
- Modify: `web/comic_sol_web/migrations.py`
- Modify: `web/comic_sol_web/generation/service.py`
- Modify: `web/comic_sol_web/api/generation.py`
- Modify: `web/comic_sol_web/app.py`
- Modify: `web/tests/test_workflow.py`

**Interfaces:**
- Produces: `WorkflowService.approve_plan()`, `advance_once()`, `snapshot()`, `pause()`, `resume()`, `events_after()`.
- Produces endpoints: `POST /api/workflows`, `GET /api/workflows/{project_id}`, `POST /api/workflows/{project_id}/pause`, `POST /api/workflows/{project_id}/resume`, and `GET /api/workflows/{project_id}/events`.
- Consumes: planning job provenance, `GenerationService`, Task 5 gateway methods, and Task 3 visual review providers.

- [ ] **Step 1: Add failing workflow state-machine tests**

```python
def test_approval_starts_generation_only_after_review(self):
    self.assertEqual([], self.generation.list_jobs(self.alice, self.project_id, self.revision))
    workflow = self.workflows.approve_plan(
        self.alice, self.project_id, self.revision,
        planning_job_id=self.planning_job_id,
        image_provider="openai", image_model="gpt-image-2", image_auth_mode="hosted",
        idempotency_key=self.key,
    )
    self.assertEqual("references", workflow.phase)

def test_restart_reclaims_expired_work_without_duplicate_acceptance(self):
    first.advance_once("worker-a", lease_seconds=1)
    clock.advance(2)
    second = self.reopen_services()
    second.advance_once("worker-b")
    self.assertEqual(1, len(second.accepted_receipts_for(self.panel_id)))

def test_failed_panel_qa_retries_only_that_panel_with_existing_budget(self):
    self.reviewer.results[self.panel_id] = [failed_review(), passing_review()]
    self.drive_until_terminal()
    self.assertEqual(2, self.engine_attempt_count(self.panel_id))
    self.assertEqual(1, self.engine_attempt_count("p01-02"))
```

Also test pause-before-promotion, eight-call global exhaustion, immutable provider provenance, no silent fallback, page-QA block, and resume from `composition`.

- [ ] **Step 2: Run workflow tests and verify durable tables are absent**

Run: `python -m unittest web.tests.test_workflow -v`

Expected: FAIL because migration 10 and `WorkflowService` do not exist.

- [ ] **Step 3: Add migration 10**

Create:

- `production_workflows`: one row per project with owner, current revision, state (`running`, `paused`, `blocked`, `complete`), phase, planning provider/model, image provider/model/auth mode, lease fields, error category, timestamps, and unique approval idempotency key.
- `workflow_events`: autoincrement event ID, owner/project/revision, type, phase, status, provider/model, attempt, bounded `progress_json`, bounded human summary, and timestamp.

Add no-update/no-delete triggers to `workflow_events`. Publish `WORKFLOW_MIGRATIONS = (*PLANNING_MIGRATIONS, WORKFLOW_MIGRATION)`.

- [ ] **Step 4: Implement bounded event storage and snapshots**

Allow only the event types named in the spec plus `qa.page_failed`, `workflow.paused`, and `workflow.complete`. Validate progress keys against a fixed allowlist and cap serialized progress/summary sizes. `events_after(owner, project, cursor, limit=100)` orders by `event_id` and never returns another owner's records.

- [ ] **Step 5: Implement one idempotent workflow advancement**

`advance_once(worker_id)` leases one workflow and performs at most one external call or one deterministic stage before releasing it:

1. `references`/`panels`: call `prepare_generation()`, enqueue only missing ready requests with the approved image selection, run one generation job, and automatically promote a valid staged result through `submit_staged_raster()`.
2. `panel-qa`: fetch one current unreviewed panel, call the recorded planning provider, and publish its QA. A `regenerate` record causes the next `prepare_generation()` to issue the existing visual-retry job.
3. `lettering`/`composition`: call the Task 5 deterministic gateway boundary once and advance.
4. `page-qa`: call the planning provider for one current page and publish it. A stale deterministic binding reruns the earliest stale deterministic stage; a subjective error blocks with evidence instead of repeating identical work.
5. `export`: call `finalize()` and mark complete only after canonical final validation.

Catch normalized credential/provider/budget failures and set a resumable `blocked` state. Never catch a stale revision and bind old output to the new revision.

- [ ] **Step 6: Keep generation metadata sufficient for previews**

Add `subject_kind` and `subject_id` to `_job_envelope()` and immutable `GenerationJob` responses. Add an owner-bound staged-raster response only if needed for immediate preview; reuse `accepted-raster/{artifact_job_id}` for accepted panels. Do not expose a server filesystem path.

- [ ] **Step 7: Add workflow APIs and request-driven pumping**

Approval requires CSRF, current revision, a successful planning job from the same owner/project, explicit image provider/model/auth mode, and an idempotency key. Snapshot GET schedules a bounded background `advance_once()` when running, so browser polling resumes work after a server restart without a daemon. Pause/resume are revision-bound writes. Return `409` for stale/racing state and `404` for owner mismatch.

- [ ] **Step 8: Add replayable SSE with polling fallback**

`GET /api/workflows/{project_id}/events` accepts `Last-Event-ID` or `after`, verifies ownership, sends each row as `id:` plus JSON `data:`, sends a keepalive comment during an idle interval, and closes after a bounded stream window so clients reconnect with the cursor. The ordinary snapshot/events JSON path remains usable when EventSource is unavailable.

- [ ] **Step 9: Run workflow, generation, and security tests**

Run: `python -m unittest web.tests.test_workflow web.tests.test_generation_queue web.tests.test_generation_contract web.tests.test_web_security -v`

Expected: PASS with no duplicate provider call after lease recovery and no event/provenance leakage.

- [ ] **Step 10: Commit orchestration**

```bash
git add web/comic_sol_web/workflow.py web/comic_sol_web/api/workflows.py web/comic_sol_web/migrations.py web/comic_sol_web/generation/service.py web/comic_sol_web/api/generation.py web/comic_sol_web/app.py web/tests/test_workflow.py web/tests/test_generation_queue.py web/tests/test_generation_contract.py web/tests/test_web_security.py
git commit -m "feat(web): orchestrate durable prompt-to-PDF production"
```

---

### Task 7: Functional Studio wiring and transparent activity drawer

**Files:**
- Create: `web/comic_sol_web/static/activity.js`
- Modify: `web/comic_sol_web/static/api.js`
- Modify: `web/comic_sol_web/static/state.js`
- Modify: `web/comic_sol_web/static/app.js`
- Modify: `web/comic_sol_web/static/views/start.js`
- Modify: `web/comic_sol_web/static/views/plan.js`
- Modify: `web/comic_sol_web/static/views/generate.js`
- Modify: `web/comic_sol_web/static/views/review.js`
- Modify: `web/comic_sol_web/static/index.html`
- Modify: `web/comic_sol_web/static/styles.css`
- Modify: `web/pyproject.toml`
- Modify: `web/tests/test_studio_start_plan.py`
- Modify: `web/tests/test_studio_generate_review.py`

**Interfaces:**
- Consumes: Tasks 4 and 6 HTTP/SSE envelopes.
- Produces: automatic Start → Plan Review flow, independent image selection, approve/pause/resume controls, visible panels, activity timeline, and PDF download.
- Preserves: existing store stale-response guards, accessible focus handling, explicit export confirmation, and WebMCP events.

- [ ] **Step 1: Write failing static-contract and DOM behavior tests**

Add assertions that:

```javascript
await bootstrapLocalSession();
const project = await createProject(projectInput);
const planning = await queuePlanning(project.project_id, project.revision, planningSelection);
store.setPlanningJob(planning);
```

and that Plan approval calls `approveWorkflow` with the current revision plus the chosen OpenAI image model. Simulated DOM tests must verify the drawer toggles, pointer/keyboard width adjustment stays bounded, EventSource reconnect preserves the last ID, polling fallback updates events, generated panel `<img>` elements remain present across workflow refreshes, and a completed workflow exposes the PDF action.

- [ ] **Step 2: Run Studio tests and verify the new functions/assets are absent**

Run: `python -m unittest web.tests.test_studio_start_plan web.tests.test_studio_generate_review -v`

Expected: FAIL on missing planning/workflow/activity symbols.

- [ ] **Step 3: Extend `api.js` and state with strict envelopes**

Add `bootstrapLocalSession`, `getPlanningOptions`, `queuePlanning`, `getPlanningJob`, `approveWorkflow`, `getWorkflow`, `pauseWorkflow`, `resumeWorkflow`, and `workflowEventsUrl`. Validate every response before storing it. Add immutable `planning` and `workflow` branches; discard any response whose project ID/revision or request epoch is stale.

- [ ] **Step 4: Make Start automatically queue planning**

Load planning options before enabling Create. The form collects title, prompt/story, language, pages, planning provider, and model. After project creation, queue the planning job automatically and navigate to Plan. Missing credentials leave the relevant option disabled with the required environment-variable name, never a key value.

- [ ] **Step 5: Keep Plan as the human review gate**

Plan polls the planning job until `ready_for_review`, then reloads the canonical Plan into the existing four editors. Keep current edit/draft validation. Add planning regeneration provider/model controls and a separate OpenAI image model selector. `Approve Plan and generate` is the only action that creates the production workflow.

- [ ] **Step 6: Drive Generate/Review from workflow snapshots**

Generate displays all reference/panel jobs grouped by `subject_kind`/`subject_id`, never replaces an accepted image with an empty placeholder, and provides pause/resume. Review presents normalized QA findings, composed-page images, workflow block reason, and the existing private PDF download when `state === "complete"`.

- [ ] **Step 7: Add the functional activity drawer**

`activity.js` mounts beside `#studio-main`, subscribes with EventSource, and falls back to `GET events?after=<lastId>`. Use a native `<input type="range">` or one pointer resize handle with keyboard support to clamp width from 320 to 720 pixels; persist only width/collapsed preference in `localStorage`. Render event type, phase, status, provider/model, attempt, summary, and timestamp with `textContent` only.

- [ ] **Step 8: Package the asset and run Studio/WebMCP tests**

Run: `python -m unittest web.tests.test_studio_start_plan web.tests.test_studio_generate_review web.tests.test_webmcp_contract -v`

Expected: PASS. WebMCP still exposes exactly 17 tools and can propose Plan edits without bypassing the review gate.

- [ ] **Step 9: Commit functional Studio wiring**

```bash
git add web/comic_sol_web/static web/pyproject.toml web/tests/test_studio_start_plan.py web/tests/test_studio_generate_review.py web/tests/test_webmcp_contract.py
git commit -m "feat(web): connect Studio to the live local workflow"
```

---

### Task 8: Provider-free golden-path integration and documentation

**Files:**
- Create: `web/tests/test_live_golden_path.py`
- Modify: `web/tests/support.py`
- Modify: `web/tests/test_web_e2e.py`
- Modify: `web/tests/test_web_docs.py`
- Modify: `docs/web/index.md`
- Modify: `docs/web/providers.md`
- Modify: `docs/web/security.md`
- Modify: `docs/web/deployment.md`
- Modify: `docs/web/live-evidence.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete local runtime.
- Produces: deterministic fake-provider evidence for the whole flow and accurate operator/user documentation.
- Preserves: the distinction between `implemented`, `offline-qualified`, `manually exercised`, and `live-verified`.

- [ ] **Step 1: Write the end-to-end test before changing docs**

```python
def test_prompt_to_review_to_visible_panels_to_pdf_survives_restart(self):
    project = self.create_prompt_project()
    planning = self.queue_fake_plan(project)
    self.drive_until(planning, "ready_for_review")
    reviewed = self.client.get(f"/api/projects/{project['project_id']}").json()
    self.assertTrue(all(reviewed["summary"]["plan"].values()))

    workflow = self.approve_with_fake_image_provider(reviewed)
    self.drive_until(workflow, "composition")
    self.restart_app_with_same_data_root()
    completed = self.drive_until(workflow, "complete")

    panel = self.client.get(self.first_accepted_raster_url(completed))
    self.assertEqual(b"\x89PNG\r\n\x1a\n", panel.content[:8])
    pdf = self.download_pdf(completed)
    self.assertEqual(b"%PDF", pdf.content[:4])
```

Use fake planning, fake image, and fake visual-review adapters; use the real engine for Plan validation, handoff, normalization, lettering, composition, page QA, report, and PDF export.

- [ ] **Step 2: Run the golden path and verify the first integration gaps fail**

Run: `python -m unittest web.tests.test_live_golden_path -v`

Expected: FAIL at the first unhandled cross-stage integration gap; fix only the production code responsible for that gap, rerunning until PASS.

- [ ] **Step 3: Add restart, failure, and cost-bound scenarios**

Cover restart during planning, raster validation, panel QA, composition, and export; planning repair exhaustion; image moderation/quota; visual retry exhaustion; pause race; stale revision; and missing provider credential. Assert provider call counts exactly, not only terminal state.

- [ ] **Step 4: Update user/operator documentation accurately**

Document:

```powershell
$env:COMIC_SOL_WEB_DATA_ROOT = "C:\absolute\path\to\comic-sol-data"
$env:OPENAI_API_KEY = "set-in-your-shell"
$env:ANTHROPIC_API_KEY = "set-in-your-shell"
comic-sol-web
```

Explain that Anthropic is optional when OpenAI handles planning/QA, a ChatGPT subscription is not API credit, Plan approval starts image spending, provider switching is never automatic, the drawer is sanitized durable activity rather than raw logs, and local mode must not be exposed beyond loopback.

Update provider tables from “not routable” to “routable when configured” while retaining `offline-qualified` until an authorized live smoke is recorded.

- [ ] **Step 5: Run all Web tests**

Run: `python -m unittest discover -s web/tests -v`

Expected: PASS with no live network calls.

- [ ] **Step 6: Commit golden-path coverage and docs**

```bash
git add web/tests/test_live_golden_path.py web/tests/support.py web/tests/test_web_e2e.py web/tests/test_web_docs.py docs/web README.md
git commit -m "test(web): prove the offline prompt-to-PDF golden path"
```

---

### Task 9: Full verification, governance gate, and authorized live smoke

**Files:**
- Modify only if evidence is authorized: `submission/webmcp/provider-evidence.md`
- Add only if evidence is authorized: path required by `docs/web/live-evidence.md`

**Interfaces:**
- Consumes: release candidate plus explicit maintainer/user authorizations.
- Produces: verified offline build; optionally produces cost-bounded live evidence.

- [ ] **Step 1: Run the repository's full deterministic verification**

Run each command with the same Python interpreter:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s web/tests -v
python scripts/comic_sol.py doctor --output-root .tmp-doctor
python -m build --no-isolation
python -m comic_sol_product.release dist/*.whl dist/*.tar.gz
python scripts/benchmark.py
```

Also run `python scripts/sync_plugin_bundle.py --check` if any canonical bundled source changed. Delete no user data; remove only explicitly created temporary/build outputs after verifying their absolute repository-contained paths.

- [ ] **Step 2: Run static and packaging checks from the Web environment**

```bash
python -m ruff check web/comic_sol_web web/tests
python -m mypy web/comic_sol_web
python -m build --no-isolation web
```

Expected: every applicable command exits zero. Report any unavailable tool or environment limitation instead of claiming it passed.

- [ ] **Step 3: Record the Article 9 human waiver before merge**

The named maintainer must post an explicit waiver in the tracking issue and the pull request. It must name: local single-user runtime, OpenAI image execution, OpenAI/Anthropic planning and visual QA, automatic post-QA promotion, bounded retries, and the cost-bounded live-smoke boundary. An agent-authored comment does not count.

- [ ] **Step 4: Stop for live-call authorization and cost ceiling**

Do not call OpenAI or Anthropic until the user supplies credentials in the environment and states a maximum spend. Record provider, model, maximum images/panels, retry cap, and maximum currency amount before starting.

- [ ] **Step 5: Run one authorized live smoke**

Use a one-page, one-panel harmless prompt. Exercise Plan generation, human review, OpenAI image generation, panel/page QA, automatic promotion, PDF download, and restart from one non-terminal stage. Abort rather than exceed the stated ceiling. Retain only the sanitized evidence allowed by `docs/web/live-evidence.md`; never retain prompt, artwork, credentials, raw responses, or local paths.

- [ ] **Step 6: Update evidence claims only to the proven tier**

If the smoke passes and the retained evidence validates, update the exact candidate/provider row to `live-verified`. If it is not authorized or fails, leave the claim `offline-qualified` and document the gap without weakening tests.

- [ ] **Step 7: Commit authorized evidence separately**

```bash
git add submission/webmcp/provider-evidence.md docs/web/live-evidence.md
git commit -m "docs(web): record authorized live provider evidence"
```

Skip this commit when no live evidence was authorized or retained.
