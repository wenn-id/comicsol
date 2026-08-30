"""Focused contracts for the Studio Generate and Review surfaces."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar

from fastapi.testclient import TestClient

from web.tests.support import valid_environment


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = WEB_ROOT / "comic_sol_web" / "static"
NEW_VIEW_ASSETS = {
    "static/views/generate.js",
    "static/views/review.js",
}


class StudioGenerateReviewPackagingTests(unittest.TestCase):
    def test_generate_and_review_assets_are_source_packaged_and_served(self) -> None:
        for asset in NEW_VIEW_ASSETS:
            self.assertTrue((WEB_ROOT / "comic_sol_web" / asset).is_file(), asset)

        project = tomllib.loads((WEB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = set(project["tool"]["setuptools"]["package-data"]["comic_sol_web"])
        self.assertTrue(NEW_VIEW_ASSETS <= package_data)

        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with TestClient(create_app(WebConfig.from_env(valid_environment()))) as client:
            statuses = {
                asset: client.get(f"/{asset}").status_code for asset in sorted(NEW_VIEW_ASSETS)
            }
        self.assertEqual({asset: 200 for asset in sorted(NEW_VIEW_ASSETS)}, statuses)


class StudioGenerateReviewContractTests(unittest.TestCase):
    index: ClassVar[str]
    app: ClassVar[str]
    api: ClassVar[str]
    state: ClassVar[str]
    generate: ClassVar[str]
    review: ClassVar[str]
    styles: ClassVar[str]
    scripts: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.api = (STATIC_ROOT / "api.js").read_text(encoding="utf-8")
        cls.state = (STATIC_ROOT / "state.js").read_text(encoding="utf-8")
        cls.generate = (STATIC_ROOT / "views" / "generate.js").read_text(encoding="utf-8")
        cls.review = (STATIC_ROOT / "views" / "review.js").read_text(encoding="utf-8")
        cls.styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.scripts = "\n".join((cls.app, cls.api, cls.state, cls.generate, cls.review))

    def test_shell_registers_four_guarded_steps_without_changing_start_or_plan(self) -> None:
        for index, view in enumerate(("start", "plan", "generate", "review"), start=1):
            self.assertRegex(
                self.index,
                rf'data-view=["\']{view}["\'][^>]*>{index}\. {view.title()}',
            )
        self.assertRegex(self.index, r'id=["\']generate-tab["\'][^>]+disabled')
        self.assertRegex(self.index, r'id=["\']review-tab["\'][^>]+disabled')
        self.assertIn('view === "generate"', self.app)
        self.assertIn('view === "review"', self.app)
        self.assertIn("disposeReviewView", self.app)
        self.assertRegex(self.app, r"canGenerate\s*=\s*Boolean\(state\.project\)")
        self.assertRegex(self.state, r'\["start",\s*"plan",\s*"generate",\s*"review"\]')
        self.assertNotRegex(self.state, r"localStorage|sessionStorage|indexedDB")

    def test_generate_discloses_selection_reasons_unknown_cost_and_confirmations(self) -> None:
        for label in (
            "Provider",
            "Model",
            "Authentication mode",
            "Capabilities",
            "Why this recommendation",
            "Estimated cost is unknown",
        ):
            self.assertIn(label, self.generate)
        self.assertIn("cost-confirmation", self.generate)
        self.assertRegex(self.generate, r"costConfirmation\.checked")
        self.assertIn("resetGenerationConfirmation", self.generate)
        self.assertIn("auth_modes", self.generate)
        for control in ("provider", "model", "authMode"):
            self.assertRegex(
                self.generate,
                rf'{control}\.addEventListener\("change",\s*resetGenerationConfirmation\)',
            )
        self.assertIn("syncProjectAndJobs", self.generate)
        self.assertIn("getProject", self.generate)
        self.assertIn("setTimeout", self.generate)
        self.assertIn("job.project_revision === project.revision", self.generate)
        for guard in (
            "revisionCurrent && RETRY_STATES.has(job.state)",
            "revisionCurrent && job.can_cancel === true",
            'revisionCurrent && job.state === "validating"',
            "revisionCurrent && SWITCH_STATES.has(job.state)",
        ):
            self.assertIn(guard, self.generate)
        self.assertIn("const displayState = JOB_STATES.has(job.state) ? job.state : \"unknown\";", self.generate)
        self.assertNotIn("PAUSE_STATES", self.generate)
        switch_start = self.generate.index("revisionCurrent && SWITCH_STATES.has(job.state)")
        self.assertLess(
            self.generate.index("const trigger = event.currentTarget;", switch_start),
            self.generate.index("await pauseForSwitch(", switch_start),
        )
        self.assertIn('createElement("dialog")', self.generate)
        self.assertIn("proposal_id", self.generate)
        self.assertIn("approveProposal", self.generate)
        self.assertIn("rejectProposal", self.generate)
        self.assertRegex(self.generate, r"approveProposal\(proposal\.proposal_id")
        self.assertRegex(self.generate, r"rejectProposal\(proposal\.proposal_id")
        self.assertNotRegex(
            self.generate,
            r"approveProposal\([^)]*(?:provider|model)|rejectProposal\([^)]*(?:provider|model)",
        )
        for state in (
            "queued",
            "running",
            "polling",
            "validating",
            "accepted",
            "awaiting_provider_confirmation",
            "paused",
            "failed",
            "cancelled",
        ):
            self.assertIn(state, self.generate)
        for action in ("Retry", "Cancel", "Promote", "Switch provider"):
            self.assertIn(action, self.generate)
        self.assertRegex(self.generate, r'aria-live["\'],\s*["\']polite')

    def test_generate_sync_publishes_project_and_jobs_atomically(self) -> None:
        sync = self.generate[
            self.generate.index("function syncProjectAndJobs") :
            self.generate.index("async function refresh") - 1
        ]
        self.assertIn("store.replaceProjectAndGenerationJobs", sync)
        self.assertNotIn("store.replaceProject(", sync)
        self.assertNotIn("store.setGenerationJobs(", sync)
        self.assertIn("replaceProjectAndGenerationJobs", self.state)

    def test_generate_requires_explicit_confirmation_before_promotion(self) -> None:
        self.assertIn("showPromotionDialog", self.generate)
        self.assertIn("Confirm promotion", self.generate)
        self.assertRegex(
            self.generate,
            r"showPromotionDialog\(job,\s*project,\s*refresh,\s*announce,\s*event\.currentTarget\)",
        )
        self.assertRegex(
            self.generate,
            r"submitStagedRaster\(job\.job_id,\s*project\.revision\)",
        )

    def test_review_distinguishes_artifacts_preserves_acceptance_and_bounds_operations(
        self,
    ) -> None:
        for text in (
            "Staged raster",
            "Accepted raster",
            "Last accepted raster",
            "Run QA",
            "Project revision",
            "Repair or rerender",
            "Export",
            "Confirm promotion",
            "Confirm overwrite",
        ):
            self.assertIn(text, self.review)
        self.assertIn("acceptedRasterUrl", self.review)
        self.assertIn("generation.accepted.artifact_job_id", self.review)
        self.assertIn("syncProjectAndJobs", self.review)
        self.assertIn("job.project_revision === project.revision", self.review)
        self.assertIn("generation.staged.project_revision === project.revision", self.review)
        self.assertIn("for (const job of actionableFailed)", self.review)
        self.assertIn("URL.revokeObjectURL", self.review)
        qa_handler = self.review[
            self.review.index("Run QA", self.review.index("QA findings"))
            : self.review.index("section.append(qaCard)")
        ]
        self.assertLess(
            qa_handler.index("const control = event.currentTarget;"),
            qa_handler.index("await runQa("),
        )
        self.assertNotIn("event.currentTarget.disabled = true", qa_handler)
        self.assertNotIn("event.currentTarget.disabled = false", qa_handler)
        self.assertEqual(2, qa_handler.count("control.disabled"))
        self.assertIn("disposeReviewView", self.review)
        self.assertIn("submitStagedRaster", self.review)
        self.assertIn("runQa", self.review)
        self.assertIn("exportProject", self.review)
        self.assertRegex(self.review, r"format[^\n]+(?:archive|pdf)")
        self.assertNotRegex(
            self.review,
            r"createElement\([\"']canvas|drawImage|getImageData|putImageData|toDataURL",
        )
        self.assertNotRegex(self.review, r"crop|fit|composition", re.IGNORECASE)
        self.assertIn('createElement("dialog")', self.review)
        self.assertIn("previousFocus.focus", self.review)

    def test_review_export_completion_is_guarded_and_transferred_across_refresh(
        self,
    ) -> None:
        awaited = self.review.index("await exportProject(")
        lifecycle_guard = self.review.index(
            "if (!reviewSessionIsCurrent(exportSessionEpoch))",
            awaited,
        )
        self.assertLess(awaited, lifecycle_guard)
        self.assertIn("const exportSessionEpoch = reviewSessionEpoch", self.review)
        self.assertIn("reviewSessionEpoch += 1", self.review)
        self.assertRegex(
            self.review,
            r"if \(!preservePendingExport\) \{\s*reviewSessionEpoch \+= 1;"
            r"\s*pendingReviewExport = null;\s*\}",
        )
        object_url = self.review.index("URL.createObjectURL(result.blob)")
        self.assertIn("pendingReviewExport = Object.freeze", self.review)
        transfer = self.review.index("pendingReviewExport = Object.freeze")
        atomic_publish = self.review.index("store.replaceProjectAndGenerationJobs", transfer)
        self.assertLess(transfer, atomic_publish)
        self.assertIn("consumePendingReviewExport(exportCard", self.review)
        self.assertIn("replaceProjectAndGenerationJobs", self.state)
        self.assertIn(
            'preservePendingExport: state.view === "review"',
            self.app,
        )
        self.assertRegex(
            self.review,
            r"function disposeReviewView\(\{ preservePendingExport = false \} = \{\}\)\s*\{"
            r"\s*reviewViewEpoch \+= 1;",
        )
        self.assertGreater(object_url, self.review.index("function attachExportDownload"))

    def test_api_calls_are_same_origin_revision_bound_and_never_accept_destinations_on_decision(
        self,
    ) -> None:
        for name in (
            "getGenerationOptions",
            "getGenerationRecommendations",
            "listGenerationJobs",
            "queueGeneration",
            "retryGeneration",
            "cancelGeneration",
            "pauseForSwitch",
            "approveProposal",
            "rejectProposal",
            "submitStagedRaster",
            "runQa",
            "exportProject",
            "acceptedRasterUrl",
        ):
            self.assertIn(f"function {name}", self.api)
        self.assertIn('const GENERATION_PATH = "/api/generation"', self.api)
        self.assertIn('const APPROVALS_PATH = "/api/approvals"', self.api)
        self.assertGreaterEqual(self.api.count('"X-CSRF-Token"'), 1)
        self.assertGreaterEqual(self.api.count('"Idempotency-Key"'), 1)
        self.assertGreaterEqual(self.api.count('"X-Expected-Revision"'), 1)
        self.assertIn('credentials: "same-origin"', self.api)
        self.assertIn("accepted_job", self.scripts)
        self.assertIn('headers.get("x-project-revision")', self.api)
        self.assertRegex(
            self.api,
            r"function approveProposal\(proposalId, expectedRevision\)",
        )
        self.assertRegex(
            self.api,
            r"function rejectProposal\(proposalId, expectedRevision\)",
        )
        self.assertNotRegex(
            self.api, r"function (?:approve|reject)Proposal\([^)]*(?:provider|model)"
        )
        self.assertIn("StaleRevisionError", self.api)

    def test_browser_never_handles_credentials_provider_requests_paths_or_telemetry(self) -> None:
        self.assertNotRegex(
            self.scripts,
            r"api[_-]?key|password|bearer|plaintext|credentialValue|secretValue",
            re.IGNORECASE,
        )
        self.assertNotRegex(
            self.scripts,
            r'fetch\s*\(\s*["\']https?://|XMLHttpRequest|WebSocket|sendBeacon|reportError',
        )
        self.assertNotRegex(self.scripts, r"console\.(?:log|warn|error)")
        self.assertNotRegex(self.scripts, r"filesystem|filePath|projectRoot|rasterPath")
        self.assertNotRegex(self.scripts, r"innerHTML\s*=|insertAdjacentHTML|document\.write")

    def test_state_preserves_last_accepted_raster_without_fabricated_reload_completion(
        self,
    ) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the Studio runtime contract test")
        assert node is not None
        state_uri = (STATIC_ROOT / "state.js").as_uri()
        script = f"""
const {{ createStore }} = await import({json.dumps(state_uri)});
function check(condition, message) {{ if (!condition) throw new Error(message); }}
const plan = Object.freeze({{
  storyPlan: "story", characterBible: "characters",
  storyboard: "boards", visualIdentityPack: "identity",
}});
const project = Object.freeze({{
  project_id: "project_0123456789abcdef01234567", revision: 7,
  status: "STORYBOARDED", summary: Object.freeze({{ plan }}),
}});
const accepted = Object.freeze({{
  job_id: "a".repeat(64), artifact_job_id: "d".repeat(64),
  project_id: project.project_id,
  project_revision: 6, accepted_project_revision: 7,
  state: "accepted", provider: "fake", model: "fake-raster-v1",
  auth_mode: "agent", artifact_state: "accepted",
}});
const failed = Object.freeze({{
  ...accepted, job_id: "b".repeat(64), project_revision: 7,
  accepted_project_revision: undefined, state: "failed", artifact_state: undefined,
}});
const staged = Object.freeze({{
  ...failed, job_id: "c".repeat(64), state: "validating", artifact_state: "staged",
}});
const fresh = createStore();
check(fresh.getState().generation.jobs.length === 0, "reload fabricated jobs");
check(fresh.getState().generation.accepted === null, "reload fabricated acceptance");
fresh.setProject(project);
fresh.setGenerationJobs([accepted]);
check(fresh.getState().generation.accepted.job_id === accepted.job_id, "accepted job missing");
fresh.setGenerationJobs([failed]);
check(fresh.getState().generation.accepted.job_id === accepted.job_id, "failure hid acceptance");
fresh.setGenerationJobs([failed], accepted);
check(
  fresh.getState().generation.accepted.artifact_job_id === accepted.artifact_job_id,
  "authoritative accepted binding missing",
);
fresh.setGenerationJobs([staged, failed], accepted);
check(fresh.getState().generation.staged.job_id === staged.job_id, "staged job missing");
check(fresh.getState().generation.accepted.job_id === accepted.job_id, "staging hid acceptance");
fresh.setQa(Object.freeze({{
  ...project,
  summary: Object.freeze({{
    ...project.summary,
    qa: Object.freeze({{ valid: true, issues: Object.freeze([]) }}),
  }}),
}}));
check(fresh.getState().generation.qa?.valid === true, "QA result was not stored");
let publications = 0;
const unsubscribe = fresh.subscribe(() => {{ publications += 1; }});
const nextProject = Object.freeze({{ ...project, revision: 8 }});
fresh.replaceProjectAndGenerationJobs(nextProject, [failed], accepted);
unsubscribe();
check(publications === 1, "project and generation refresh was not atomic");
check(fresh.getState().project.revision === 8, "atomic refresh lost project revision");
check(fresh.getState().generation.loadedRevision === 8, "atomic refresh lost job revision");
check(fresh.getState().generation.accepted.job_id === accepted.job_id, "atomic refresh lost acceptance");
check(fresh.getState().generation.qa === null, "stale QA survived revision change");
"""
        completed = subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_review_export_survives_same_view_rerender_but_not_departure(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the Review lifecycle test")
        assert node is not None
        review_uri = (STATIC_ROOT / "views" / "review.js").as_uri()
        script = """
const { disposeReviewView, renderReviewView } = await import(__REVIEW_MODULE__);
function check(condition, message) { if (!condition) throw new Error(message); }
class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attributes = new Map(); this.listeners = new Map(); this.isConnected = false;
    this.textContent = ""; this.value = ""; this.checked = false; this.disabled = false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  append(...nodes) {
    for (const node of nodes) {
      node.parentNode = this; this.children.push(node);
      if (this.isConnected) node.connect();
      if (this.tagName === "SELECT" && !this.value && node.value) this.value = node.value;
    }
  }
  prepend(node) { node.parentNode = this; this.children.unshift(node); if (this.isConnected) node.connect(); }
  addEventListener(name, listener) {
    const values = this.listeners.get(name) || []; values.push(listener); this.listeners.set(name, values);
  }
  async dispatch(name) {
    for (const listener of this.listeners.get(name) || []) {
      await listener({ currentTarget: this, preventDefault() {} });
    }
  }
  connect() { this.isConnected = true; for (const child of this.children) child.connect(); }
  disconnect() { this.isConnected = false; for (const child of this.children) child.disconnect(); }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((item) => item !== this);
    this.parentNode = null; this.disconnect();
  }
  focus() {}
  showModal() {}
  close() { void this.dispatch("close"); }
  querySelector(selector) {
    return find(this, (node) => selector === "a[download]" && node.tagName === "A" && node.attributes.has("download"));
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (selector === ".card" && (node.attributes.get("class") || "").split(" ").includes("card")) return node;
      node = node.parentNode;
    }
    return null;
  }
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const match = find(child, predicate); if (match) return match; }
  return null;
}
function findAll(root, predicate, values = []) {
  if (predicate(root)) values.push(root);
  for (const child of root.children) findAll(child, predicate, values);
  return values;
}
const body = new FakeNode("body"); body.connect();
globalThis.document = {
  cookie: "comic_sol_csrf=csrf-token",
  body,
  createElement(tag) { return new FakeNode(tag); },
  getElementById(id) { return find(body, (node) => node.attributes.get("id") === id); },
};
const created = []; const revoked = [];
URL.createObjectURL = (blob) => { const value = `blob:${created.length + 1}`; created.push([value, blob]); return value; };
URL.revokeObjectURL = (value) => revoked.push(value);
const pending = [];
globalThis.fetch = () => new Promise((resolve) => pending.push(() => resolve(new Response(
  new Blob(["private-export"]),
  { status: 200, headers: { "content-type": "application/octet-stream", "x-project-revision": "7" } },
))));
const project = Object.freeze({
  project_id: "project_0123456789abcdef01234567", revision: 7,
  status: "STORYBOARDED", summary: Object.freeze({ qa: null }),
});
const generation = Object.freeze({ jobs: Object.freeze([]), accepted: null, staged: null, qa: null, loadedRevision: 7 });
const store = { getState() { return { project, generation }; } };
const announcements = [];
const context = { store, announce(message, tone) { announcements.push([message, tone]); }, navigate() {} };
function exportControls() {
  const button = find(body, (node) => node.tagName === "BUTTON" && node.textContent === "Export selected format");
  const checkbox = find(body, (node) => node.tagName === "INPUT");
  const format = find(body, (node) => node.tagName === "SELECT");
  check(button && checkbox && format, "export controls missing");
  checkbox.checked = true; format.value = "archive";
  return button;
}
async function beginExport() {
  await exportControls().dispatch("click");
  const confirm = find(body, (node) => node.tagName === "BUTTON" && node.textContent === "Create export");
  check(confirm, "export confirmation missing");
  const completion = confirm.dispatch("click");
  await Promise.resolve();
  check(pending.length === 1, "export request was not deferred");
  return { completion };
}
const first = renderReviewView(context); body.append(first);
const { completion: firstCompletion } = await beginExport();
disposeReviewView({ preservePendingExport: true }); first.remove();
const second = renderReviewView(context); body.append(second);
pending.shift()(); await firstCompletion;
check(created.length === 1, "same-Review rerender dropped or duplicated export URL");
check(findAll(body, (node) => node.tagName === "A" && node.attributes.has("download")).length === 1, "current Review lacks one download");
const { completion: secondCompletion } = await beginExport();
disposeReviewView(); second.remove();
pending.shift()(); await secondCompletion;
check(created.length === 1, "departure allowed stale export URL creation");
check(revoked.length === 1 && revoked[0] === "blob:1", "departure did not revoke current export URL");
check(findAll(body, (node) => node.tagName === "A" && node.attributes.has("download") && node.isConnected).length === 0, "departure retained a live download");
""".replace("__REVIEW_MODULE__", json.dumps(review_uri))
        completed = subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_accessibility_and_reduced_motion_contracts_cover_dynamic_surfaces(self) -> None:
        self.assertRegex(self.generate, r"aria-label|setAttribute\([\"']aria-")
        self.assertRegex(self.review, r"aria-label|setAttribute\([\"']aria-")
        self.assertIn("aria-describedby", self.generate)
        self.assertIn("aria-describedby", self.review)
        self.assertNotRegex(self.scripts, r'tabindex["\']?,\s*["\']?[1-9]')
        self.assertIn("prefers-reduced-motion: reduce", self.styles)
        self.assertRegex(self.styles, r":focus-visible")


if __name__ == "__main__":
    unittest.main()
