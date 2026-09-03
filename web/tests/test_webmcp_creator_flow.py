"""Creator-first WebMCP contract for ComicSol Studio."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC = WEB_ROOT / "comic_sol_web" / "static"
APP = STATIC / "app.js"
INDEX = STATIC / "index.html"


class WebMcpCreatorFlowTests(unittest.TestCase):
    app: ClassVar[str]
    bootstrap: ClassVar[str]
    index: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP.read_text(encoding="utf-8")
        cls.index = INDEX.read_text(encoding="utf-8")
        match = re.search(
            r'<script[^>]+id=["\']creator-webmcp-bootstrap["\'][^>]*>(?P<body>.*?)</script>',
            cls.index,
            re.DOTALL,
        )
        cls.bootstrap = match.group("body") if match else ""

    def test_app_registers_three_creator_first_tools(self) -> None:
        for name in ("get_comic_context", "create_comic", "revise_comic"):
            self.assertIn(f'name: "{name}"', self.app)
        self.assertIn("registerCreatorWebMcp", self.app)

    def test_creator_bootstrap_waits_for_core_surface_before_registration(self) -> None:
        self.assertTrue(self.bootstrap, "creator WebMCP bootstrap must exist in index.html")
        self.assertIn('from "./app.js"', self.bootstrap)
        self.assertIn("registerCreatorWebMcp", self.bootstrap)
        self.assertIn("getTools", self.bootstrap)
        self.assertIn("CORE_TOOL_COUNT = 14", self.bootstrap)
        self.assertRegex(
            self.bootstrap,
            r"tools\.length\s*>=\s*CORE_TOOL_COUNT[\s\S]+return registerCreatorWebMcp\(\)",
        )
        self.assertLess(
            self.index.index('src="./app.js"'),
            self.index.index('id="creator-webmcp-bootstrap"'),
        )

    def test_creator_layer_reuses_existing_project_api(self) -> None:
        self.assertRegex(
            self.app,
            r'import\s*\{[^}]*createProject[^}]*getCurrentProject[^}]*updatePlan[^}]*\}\s*from\s*"\.\/api\.js"',
        )
        self.assertIn("createProject(", self.app)
        self.assertIn("getCurrentProject(", self.app)
        self.assertIn("updatePlan(", self.app)

    def test_creator_inputs_hide_low_level_revision_mechanics(self) -> None:
        create_block = re.search(r'name: "create_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        revise_block = re.search(r'name: "revise_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        self.assertIsNotNone(create_block)
        self.assertIsNotNone(revise_block)
        assert create_block is not None and revise_block is not None
        for forbidden in ("expected_revision", "idempotency_key", "provider", "job_id"):
            self.assertNotIn(forbidden, create_block.group("body"))
            self.assertNotIn(forbidden, revise_block.group("body"))

    def test_creator_flow_has_ephemeral_hosted_browser_fallback(self) -> None:
        self.assertIn("let browserLocalCreatorProject = null;", self.app)
        self.assertNotIn("localStorage", self.app)
        self.assertNotIn("sessionStorage", self.app)
        self.assertIn('"browser-local"', self.app)
        self.assertIn("isBrowserLocalProject(project)", self.app)
        self.assertIn("const CREATOR_PLAN_SCHEMA = creatorSchema(", self.app)
        create_block = re.search(r'name: "create_comic"(?P<body>.*?)execute:', self.app, re.DOTALL)
        self.assertIsNotNone(create_block)
        assert create_block is not None
        self.assertIn("plan: CREATOR_PLAN_SCHEMA", create_block.group("body"))

    def test_creator_runtime_preserves_plans_until_confirmation(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the WebMCP creator runtime contract")
        assert node is not None
        state_uri = (STATIC / "state.js").as_uri()
        script = r"""
import { readFileSync } from "node:fs";

function moduleUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
}
function check(condition, message) {
  if (!condition) throw new Error(message);
}
function same(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

globalThis.apiCalls = [];
globalThis.backendProject = null;
const apiUrl = moduleUrl(`
  export async function createProject(request, idempotencyKey) {
    globalThis.apiCalls.push({ kind: "create", request, idempotencyKey });
    globalThis.backendProject = {
      project_id: "project_0123456789abcdef01234567", revision: 1, status: "INIT",
      summary: { title: request.title, plan: {
        storyPlan: "backend", characterBible: "", storyboard: "", visualIdentityPack: ""
      } }
    };
    return structuredClone(globalThis.backendProject);
  }
  export async function getCurrentProject() {
    return globalThis.backendProject ? structuredClone(globalThis.backendProject) : null;
  }
  export async function updatePlan(projectId, plan, expectedRevision, idempotencyKey) {
    globalThis.apiCalls.push({
      kind: "update", projectId, plan, expectedRevision, idempotencyKey
    });
    globalThis.backendProject = {
      ...globalThis.backendProject,
      revision: expectedRevision + 1,
      status: "STORYBOARDED",
      summary: { ...globalThis.backendProject.summary, plan }
    };
    return structuredClone(globalThis.backendProject);
  }
`);
const webmcpUrl = moduleUrl(`export async function registerWebMcp() { return false; }`);
const viewsUrl = moduleUrl(`
  let proposalHandler = null;
  export function renderStartView() { return {}; }
  export function renderGenerateView() { return {}; }
  export function renderReviewView() { return {}; }
  export function disposeReviewView() {}
  export function renderPlanView({ store, persistPlan }) {
    if (proposalHandler) document.removeEventListener("comic-sol:plan-proposal", proposalHandler);
    proposalHandler = (event) => {
      const state = store.getState();
      if (state.draft || state.project.revision !== event.detail.expectedRevision) {
        event.preventDefault();
        return;
      }
      store.createDraft(event.detail.changes, "agent");
    };
    document.addEventListener("comic-sol:plan-proposal", proposalHandler);
    globalThis.confirmPlan = async () => {
      const state = store.getState();
      const draft = state.draft;
      const project = await persistPlan(
        state.project.project_id, draft.changes, draft.expectedRevision
      );
      if (!store.promoteDraft(project)) throw new Error("confirmed draft was not promoted");
    };
    globalThis.creatorStore = store;
    return {};
  }
`);

const listeners = new Map();
const tools = [];
const outlet = { replaceChildren() {}, append() {} };
const main = { focus() {} };
const status = { textContent: "", dataset: {} };
globalThis.CustomEvent = class {
  constructor(type, init = {}) {
    this.type = type;
    this.detail = init.detail;
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
};
globalThis.document = {
  cookie: "comic_sol_csrf=csrf-token",
  modelContext: {
    registerTool(definition) { tools.push(definition); return Promise.resolve(); }
  },
  getElementById(id) {
    return { "studio-view": outlet, "studio-main": main, "studio-status": status }[id] || null;
  },
  querySelectorAll() { return []; },
  addEventListener(type, listener) {
    const values = listeners.get(type) || [];
    values.push(listener);
    listeners.set(type, values);
  },
  removeEventListener(type, listener) {
    listeners.set(type, (listeners.get(type) || []).filter((value) => value !== listener));
  },
  dispatchEvent(event) {
    for (const listener of [...(listeners.get(event.type) || [])]) listener(event);
    return !event.defaultPrevented;
  }
};

const original = readFileSync(new URL(__APP_MODULE__), "utf8");
let source = original
  .replace('from "./api.js"', `from ${JSON.stringify(apiUrl)}`)
  .replace('from "./webmcp.js"', `from ${JSON.stringify(webmcpUrl)}`)
  .replace('from "./state.js"', `from ${JSON.stringify(__STATE_MODULE__)}`)
  .replaceAll('from "./views/start.js"', `from ${JSON.stringify(viewsUrl)}`)
  .replaceAll('from "./views/plan.js"', `from ${JSON.stringify(viewsUrl)}`)
  .replaceAll('from "./views/generate.js"', `from ${JSON.stringify(viewsUrl)}`)
  .replaceAll('from "./views/review.js"', `from ${JSON.stringify(viewsUrl)}`);
check(source !== original && !source.includes('from "./'), "app imports were not isolated");
await import(moduleUrl(source));
for (let index = 0; index < 5; index += 1) await Promise.resolve();
const findTool = (name) => tools.find((tool) => tool.name === name);
const createComic = findTool("create_comic");
const reviseComic = findTool("revise_comic");
const getContext = findTool("get_comic_context");
check(createComic && reviseComic && getContext, "creator tools did not register");

const originalPlan = {
  storyPlan: "agent story", characterBible: "agent characters",
  storyboard: "agent storyboard", visualIdentityPack: "agent identity"
};
const revisedPlan = {
  storyPlan: "revised story", characterBible: "revised characters",
  storyboard: "revised storyboard", visualIdentityPack: "revised identity"
};
const created = await createComic.execute({
  title: "Creator comic", concept: "A careful story", language: "en", page_count: 2,
  visual_style: "ink wash", plan: originalPlan
});
check(created.ok && created.data.plan_available, "authenticated creation lost Plan availability");
const createCall = apiCalls.find((call) => call.kind === "create");
check(createCall.request.title === "Creator comic", "creation request lost the title");
check(createCall.request.page_count === 2, "creation request lost the page count");
check(createCall.request.prompt.includes("Visual direction: ink wash"), "creation request lost visual direction");
check(!("plan" in createCall.request), "client-only Plan leaked into the strict backend envelope");
let context = await getContext.execute({});
check(context.ok && same(context.data.plan, originalPlan), "returned context replaced the agent Plan");

const proposed = await reviseComic.execute({ instruction: "Revise it", plan: revisedPlan });
check(proposed.ok && proposed.data.review_required, "revision was not returned as pending review");
check(proposed.data.revision === 1, "proposal advanced the authenticated revision");
check(apiCalls.filter((call) => call.kind === "update").length === 0, "proposal wrote before confirmation");
let state = creatorStore.getState();
check(same(state.workingPlan, originalPlan), "proposal changed the authenticated working Plan");
check(state.draft?.origin === "agent" && same(state.draft.changes, revisedPlan), "proposal draft was not staged");
context = await getContext.execute({});
check(context.data.revision === 1 && same(context.data.plan, originalPlan), "pending context exposed unconfirmed changes");
await confirmPlan();
state = creatorStore.getState();
check(apiCalls.filter((call) => call.kind === "update").length === 1, "confirmation did not persist once");
check(state.project.revision === 2 && same(state.workingPlan, revisedPlan), "confirmed Studio Plan was not promoted");

document.cookie = "";
globalThis.backendProject = null;
const localPlan = { ...originalPlan, storyPlan: "local story" };
const localRevision = { ...revisedPlan, storyPlan: "local revision" };
const localCreated = await createComic.execute({
  title: "Local comic", concept: "Offline", language: "en", page_count: 1,
  visual_style: "pencil", plan: localPlan
});
check(localCreated.ok && localCreated.data.mode === "browser-local", "local creation failed");
const writesBeforeLocalProposal = apiCalls.filter((call) => call.kind === "update").length;
const localProposed = await reviseComic.execute({ instruction: "Revise locally", plan: localRevision });
check(localProposed.ok && localProposed.data.revision === 1, "local proposal advanced the revision");
state = creatorStore.getState();
check(same(state.workingPlan, localPlan), "local proposal changed the working Plan");
check(
  apiCalls.filter((call) => call.kind === "update").length === writesBeforeLocalProposal,
  "local proposal called the backend"
);
await confirmPlan();
state = creatorStore.getState();
check(state.project.revision === 2 && same(state.workingPlan, localRevision), "confirmed local Plan was not promoted");
context = await getContext.execute({});
check(context.data.revision === 2 && same(context.data.plan, localRevision), "local confirmed context was stale");
""".replace("__APP_MODULE__", json.dumps(APP.as_uri())).replace(
            "__STATE_MODULE__", json.dumps(state_uri)
        )
        completed = subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"Node creator runtime contract failed:\n{completed.stdout}\n{completed.stderr}",
        )

    def test_app_remains_valid_javascript(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the WebMCP creator contract")
        assert node is not None
        completed = subprocess.run(
            [node, "--check", str(APP)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
