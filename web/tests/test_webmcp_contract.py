"""Contract tests for the page-owned WebMCP Studio surface."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path
from typing import Any

from web.tests.support import valid_environment


WEB_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = WEB_ROOT / "comic_sol_web" / "static"
WEBMCP = STATIC_ROOT / "webmcp.js"

READ_TOOLS = (
    "get_project_state",
    "list_generation_options",
    "recommend_provider",
    "list_generation_jobs",
    "get_qa_summary",
)
WRITE_TOOLS = (
    "create_project",
    "import_project",
    "update_project_plan",
    "queue_generation",
    "submit_generated_asset",
    "approve_provider_switch",
    "reject_provider_switch",
    "run_qa",
    "export_project",
)
TOOL_NAMES = READ_TOOLS + WRITE_TOOLS

OPAQUE_PROJECT = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
}
REVISION = {"type": "integer", "minimum": 1}
ZERO_REVISION = {"type": "integer", "const": 0}
IDEMPOTENCY = {
    "type": "string",
    "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
JOB_ID = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
ASSET_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{32,64}$"}
PROPOSAL_ID = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{32,128}$"}


def object_schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


EMPTY = object_schema({}, [])
PROJECT_REVISION = object_schema(
    {"project_id": OPAQUE_PROJECT, "expected_revision": REVISION},
    ["project_id", "expected_revision"],
)
PLAN = object_schema(
    {
        "storyPlan": {"type": "string", "maxLength": 1048576},
        "characterBible": {"type": "string", "maxLength": 1048576},
        "storyboard": {"type": "string", "maxLength": 1048576},
        "visualIdentityPack": {"type": "string", "maxLength": 1048576},
    },
    ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"],
)

EXPECTED_SCHEMAS: dict[str, Any] = {
    "get_project_state": EMPTY,
    "list_generation_options": EMPTY,
    "recommend_provider": object_schema(
        {"project_id": OPAQUE_PROJECT, "expected_revision": REVISION, "job_id": JOB_ID},
        ["project_id", "expected_revision", "job_id"],
    ),
    "list_generation_jobs": PROJECT_REVISION,
    "get_qa_summary": EMPTY,
    "create_project": object_schema(
        {
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 204800},
            "language": {"type": "string", "minLength": 1, "maxLength": 16},
            "mode": {"type": "string", "enum": ["short_prompt", "pasted_story"]},
            "page_count": {"type": "integer", "minimum": 1, "maximum": 4},
            "expected_revision": ZERO_REVISION,
            "idempotency_key": IDEMPOTENCY,
        },
        [
            "title",
            "prompt",
            "language",
            "mode",
            "page_count",
            "expected_revision",
            "idempotency_key",
        ],
    ),
    "import_project": object_schema(
        {
            "archive_handle": {"type": "string", "enum": ["selected"]},
            "expected_revision": ZERO_REVISION,
            "idempotency_key": IDEMPOTENCY,
        },
        ["archive_handle", "expected_revision", "idempotency_key"],
    ),
    "update_project_plan": object_schema(
        {
            "project_id": OPAQUE_PROJECT,
            "expected_revision": REVISION,
            "plan": PLAN,
            "confirm_plan": {"type": "boolean", "const": True},
            "idempotency_key": IDEMPOTENCY,
        },
        ["project_id", "expected_revision", "plan", "confirm_plan", "idempotency_key"],
    ),
    "queue_generation": object_schema(
        {
            "project_id": OPAQUE_PROJECT,
            "expected_revision": REVISION,
            "provider": {"type": "string", "minLength": 1, "maxLength": 64},
            "model": {"type": "string", "minLength": 1, "maxLength": 128},
            "auth_mode": {"type": "string", "enum": ["agent", "hosted", "byok"]},
            "confirm_cost": {"type": "boolean", "const": True},
            "idempotency_key": IDEMPOTENCY,
        },
        [
            "project_id",
            "expected_revision",
            "provider",
            "model",
            "auth_mode",
            "confirm_cost",
            "idempotency_key",
        ],
    ),
    "submit_generated_asset": object_schema(
        {
            "project_id": OPAQUE_PROJECT,
            "job_id": JOB_ID,
            "asset_id": ASSET_ID,
            "expected_revision": REVISION,
            "confirm_promotion": {"type": "boolean", "const": True},
            "idempotency_key": IDEMPOTENCY,
        },
        [
            "project_id",
            "job_id",
            "asset_id",
            "expected_revision",
            "confirm_promotion",
            "idempotency_key",
        ],
    ),
}
for _decision in ("approve_provider_switch", "reject_provider_switch"):
    EXPECTED_SCHEMAS[_decision] = object_schema(
        {
            "proposal_id": PROPOSAL_ID,
            "project_id": OPAQUE_PROJECT,
            "expected_revision": REVISION,
            "confirm_switch": {"type": "boolean", "const": True},
            "idempotency_key": IDEMPOTENCY,
        },
        ["proposal_id", "project_id", "expected_revision", "confirm_switch", "idempotency_key"],
    )
EXPECTED_SCHEMAS["run_qa"] = object_schema(
    {"project_id": OPAQUE_PROJECT, "expected_revision": REVISION, "idempotency_key": IDEMPOTENCY},
    ["project_id", "expected_revision", "idempotency_key"],
)
EXPECTED_SCHEMAS["export_project"] = object_schema(
    {
        "project_id": OPAQUE_PROJECT,
        "expected_revision": REVISION,
        "format": {"type": "string", "enum": ["archive", "pdf"]},
        "overwrite_confirmed": {"type": "boolean", "const": True},
        "idempotency_key": IDEMPOTENCY,
    },
    ["project_id", "expected_revision", "format", "overwrite_confirmed", "idempotency_key"],
)


class WebMcpContractTests(unittest.TestCase):
    def run_node(self, script: str) -> Any:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the WebMCP runtime contract")
        assert node is not None
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
            f"Node WebMCP contract failed:\n{completed.stdout}\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    def test_feature_detection_and_unavailable_page_behavior(self) -> None:
        self.assertTrue(WEBMCP.is_file(), "WebMCP registration module is missing")
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{}};
            const module = await import({json.dumps(module_url)});
            const registered = await module.registerWebMcp();
            console.log(JSON.stringify({{ registered }}));
            """
        )
        self.assertEqual({"registered": False}, result)

    def test_exactly_the_approved_fourteen_tools_register_without_extras(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const definitions = [];
            globalThis.document = {{
              modelContext: {{ registerTool(definition) {{ definitions.push(definition); return Promise.resolve(); }} }}
            }};
            const module = await import({json.dumps(module_url)});
            const registered = await module.registerWebMcp();
            console.log(JSON.stringify({{ registered, names: definitions.map((item) => item.name) }}));
            """
        )
        self.assertTrue(result["registered"])
        self.assertEqual(list(TOOL_NAMES), result["names"])

    def test_navigator_model_context_is_preferred_and_async_failures_are_safe(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const definitions = [];
            globalThis.document = {{ modelContext: {{ registerTool() {{ throw new Error("wrong context"); }} }} }};
            Object.defineProperty(globalThis, "navigator", {{
              configurable: true,
              value: {{
                modelContext: {{
                  registerTool(definition) {{
                    definitions.push(definition.name);
                    return Promise.resolve();
                  }}
                }}
              }}
            }});
            const module = await import({json.dumps(module_url)});
            const registered = await module.registerWebMcp();
            const failure = await (async () => {{
              globalThis.navigator.modelContext.registerTool = () => Promise.reject(new Error("raw failure"));
              return module.registerWebMcp();
            }})();
            console.log(JSON.stringify({{ registered, failure, definitions }}));
            """
        )
        self.assertTrue(result["registered"])
        self.assertFalse(result["failure"])
        self.assertEqual(list(TOOL_NAMES), result["definitions"])

    def test_exact_schemas_are_narrow_and_closed(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{ modelContext: {{ registerTool() {{}} }} }};
            const module = await import({json.dumps(module_url)});
            console.log(JSON.stringify(Object.fromEntries(
              module.TOOL_DEFINITIONS.map((item) => [item.name, item.inputSchema])
            )));
            """
        )
        self.assertEqual(EXPECTED_SCHEMAS, result)
        for schema in result.values():
            self.assertIsInstance(schema, dict)
            self.assertFalse(schema.get("additionalProperties", True))

    def test_read_write_annotations_and_revision_idempotency_requirements(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{ modelContext: {{ registerTool() {{}} }} }};
            const module = await import({json.dumps(module_url)});
            console.log(JSON.stringify(Object.fromEntries(
              module.TOOL_DEFINITIONS.map((item) => [item.name, item.annotations])
            )));
            """
        )
        for name in READ_TOOLS:
            self.assertEqual({"readOnlyHint": True}, result[name])
        for name in WRITE_TOOLS:
            self.assertEqual({"readOnlyHint": False}, result[name])
        for name in WRITE_TOOLS:
            self.assertIn("idempotency_key", EXPECTED_SCHEMAS[name]["required"])
        for name in WRITE_TOOLS:
            if name in {"create_project", "import_project"}:
                self.assertEqual(
                    0, EXPECTED_SCHEMAS[name]["properties"]["expected_revision"]["const"]
                )
            else:
                self.assertIn("expected_revision", EXPECTED_SCHEMAS[name]["required"])

    def test_safe_envelopes_do_not_return_private_content_or_raw_errors(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{
              cookie: "comic_sol_csrf=csrf-token",
              modelContext: {{ registerTool() {{}} }},
              getElementById() {{ return null; }}
            }};
            let calls = 0;
            globalThis.fetch = async () => {{
              calls += 1;
              return new Response(JSON.stringify({{
                project_id: "project_0123456789abcdef01234567",
                revision: 7,
                status: "STORYBOARDED",
                summary: {{
                  title: "private story title",
                  plan: {{ storyPlan: "private story text", characterBible: "secret", storyboard: "secret", visualIdentityPack: "secret" }},
                  qa: {{ valid: true, issues: [{{ path: "private/path", message: "private finding" }}] }}
                }}
              }}), {{ status: 200, headers: {{ "content-type": "application/json" }} }});
            }};
            const module = await import({json.dumps(module_url)});
            const state = await module.TOOL_DEFINITIONS.find((item) => item.name === "get_project_state").execute({{}});
            const qa = await module.TOOL_DEFINITIONS.find((item) => item.name === "get_qa_summary").execute({{}});
            console.log(JSON.stringify({{ state, qa, calls }}));
            """
        )
        rendered = json.dumps(result)
        for forbidden in (
            "private story text",
            "private story title",
            "private/path",
            "private finding",
            "secret",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertTrue(result["state"]["ok"])
        self.assertEqual(
            {
                "available": True,
                "project_id": "project_0123456789abcdef01234567",
                "revision": 7,
                "status": "STORYBOARDED",
                "plan_available": True,
                "qa_available": True,
            },
            result["state"]["data"],
        )
        self.assertTrue(result["qa"]["ok"])
        self.assertEqual(
            {
                "available": True,
                "project_id": "project_0123456789abcdef01234567",
                "revision": 7,
                "valid": True,
                "issue_count": 1,
            },
            result["qa"]["data"],
        )
        self.assertEqual(2, result["calls"])

    def test_page_owned_handles_and_proposal_ids_reject_paths_urls_and_destinations(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{ cookie: "comic_sol_csrf=csrf-token", getElementById() {{ return null; }} }};
            let calls = 0;
            globalThis.fetch = async () => {{ calls += 1; throw new Error("raw provider payload"); }};
            const module = await import({json.dumps(module_url)});
            const find = (name) => module.TOOL_DEFINITIONS.find((item) => item.name === name);
            const invalidImport = await find("import_project").execute({{ archive_handle: "C:\\\\private\\\\archive.comic-sol-handoff", expected_revision: 0, idempotency_key: "00000000-0000-4000-8000-000000000001" }});
            const invalidAsset = await find("submit_generated_asset").execute({{ project_id: "project_0123456789abcdef01234567", job_id: "a".repeat(64), asset_id: "https://attacker.example/asset", expected_revision: 1, confirm_promotion: true, idempotency_key: "00000000-0000-4000-8000-000000000002" }});
            const invalidDecision = await find("approve_provider_switch").execute({{ proposal_id: "https://attacker.example/proposal", expected_revision: 1, confirm_switch: true, idempotency_key: "00000000-0000-4000-8000-000000000003", provider: "attacker", model: "attacker" }});
            console.log(JSON.stringify({{ invalidImport, invalidAsset, invalidDecision, calls }}));
            """
        )
        self.assertEqual(0, result["calls"])
        for key in ("invalidImport", "invalidAsset", "invalidDecision"):
            self.assertFalse(result[key]["ok"])
            self.assertIn("error", result[key])
            self.assertNotIn("attacker", json.dumps(result[key]))

    def test_page_owned_selection_is_the_only_import_and_asset_source(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const archive = Object.assign(new Blob(["archive"]), {{ name: "project.comic-sol-handoff" }});
            let selectedFile = archive;
            globalThis.CustomEvent = class {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
            globalThis.document = {{
              cookie: "comic_sol_csrf=csrf-token",
              getElementById() {{ return {{ files: [selectedFile] }}; }},
              dispatchEvent(event) {{ event.detail.accepted = true; return true; }},
            }};
            const project = {{
              project_id: "project_0123456789abcdef01234567", revision: 1,
              status: "INIT", summary: {{ plan: {{
                storyPlan: "", characterBible: "", storyboard: "", visualIdentityPack: ""
              }} }}
            }};
            let calls = [];
            globalThis.fetch = async (url, init = {{}}) => {{
              calls.push({{ url, method: init.method || "GET", body: init.body }});
              if (url === "/api/projects/import" || url === "/api/projects/current") {{
                return new Response(JSON.stringify(project), {{ status: 200 }});
              }}
              return new Response(JSON.stringify({{
                job_id: "a".repeat(64), project_id: project.project_id,
                project_revision: 1, state: "accepted", provider: "agent",
                model: "agent-image-generation", auth_mode: "agent",
                attempt: 1, retry_count: 0, max_retries: 2, can_cancel: false,
              }}), {{ status: 200 }});
            }};
            const module = await import({json.dumps(module_url)});
            const importTool = module.TOOL_DEFINITIONS.find((item) => item.name === "import_project");
            const assetTool = module.TOOL_DEFINITIONS.find((item) => item.name === "submit_generated_asset");
            const imported = await importTool.execute({{ archive_handle: "selected", expected_revision: 0, idempotency_key: "00000000-0000-4000-8000-000000000020" }});
            selectedFile = {{ name: "oversized.comic-sol-handoff", size: 1024 * 1024 * 1024 + 1 }};
            const oversizedImport = await importTool.execute({{ archive_handle: "selected", expected_revision: 0, idempotency_key: "00000000-0000-4000-8000-000000000022" }});
            const submitted = await assetTool.execute({{
              project_id: project.project_id, job_id: "a".repeat(64), asset_id: "A".repeat(32),
              expected_revision: 1, confirm_promotion: true,
              idempotency_key: "00000000-0000-4000-8000-000000000021"
            }});
            console.log(JSON.stringify({{
              imported, oversizedImport, submitted, calls: calls.map((item) => ({{ url: item.url, method: item.method, form: item.body instanceof FormData }}))
            }}));
            """
        )
        self.assertTrue(result["imported"]["ok"], result)
        self.assertFalse(result["oversizedImport"]["ok"], result)
        self.assertTrue(result["submitted"]["ok"], result)
        self.assertEqual(3, len(result["calls"]))
        self.assertEqual("/api/projects/import", result["calls"][0]["url"])
        self.assertEqual("POST", result["calls"][0]["method"])
        self.assertTrue(result["calls"][0]["form"])
        self.assertIn("/api/assets/", result["calls"][2]["url"])
        self.assertNotIn("path", json.dumps(result))

    def test_plan_updates_enter_the_page_review_boundary_before_persistence(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const project = {{
              project_id: "project_0123456789abcdef01234567", revision: 3,
              status: "PLANNING", summary: {{ plan: {{
                storyPlan: "current", characterBible: "", storyboard: "", visualIdentityPack: ""
              }} }}
            }};
            let calls = 0;
            let proposal = null;
            globalThis.CustomEvent = class {{
              constructor(type, init) {{ this.type = type; this.detail = init.detail; }}
            }};
            globalThis.document = {{
              getElementById(id) {{ return id === "plan-editor" || id === "draft-diff" ? {{}} : null; }},
              dispatchEvent(event) {{ proposal = event; return true; }}
            }};
            globalThis.fetch = async () => {{
              calls += 1;
              return new Response(JSON.stringify(project), {{ status: 200 }});
            }};
            const module = await import({json.dumps(module_url)});
            const update = await module.TOOL_DEFINITIONS.find((item) => item.name === "update_project_plan").execute({{
              project_id: project.project_id,
              expected_revision: project.revision,
              plan: {{ storyPlan: "proposed", characterBible: "", storyboard: "", visualIdentityPack: "" }},
              confirm_plan: true,
              idempotency_key: "00000000-0000-4000-8000-000000000030"
            }});
            console.log(JSON.stringify({{
              update,
              calls,
              proposal: proposal && {{
                type: proposal.type,
                expectedRevision: proposal.detail.expectedRevision,
                storyPlan: proposal.detail.changes.storyPlan
              }}
            }}));
            """
        )
        self.assertEqual(
            {
                "ok": True,
                "data": {
                    "project_id": "project_0123456789abcdef01234567",
                    "revision": 3,
                    "review_required": True,
                },
            },
            result["update"],
        )
        self.assertEqual(1, result["calls"])
        self.assertEqual(
            {
                "type": "comic-sol:plan-proposal",
                "expectedRevision": 3,
                "storyPlan": "proposed",
            },
            result["proposal"],
        )

    def test_rejected_plan_proposals_and_background_generation_fail_closed(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const project = {{
              project_id: "current-project", revision: 3, status: "PLANNING",
              summary: {{ plan: {{ storyPlan: "", characterBible: "", storyboard: "", visualIdentityPack: "" }} }}
            }};
            globalThis.CustomEvent = class {{
              constructor(type, init) {{ this.type = type; this.detail = init.detail; }}
            }};
            globalThis.document = {{
              getElementById(id) {{ return id === "plan-editor" || id === "draft-diff" ? {{}} : null; }},
              dispatchEvent() {{ return false; }}
            }};
            let calls = [];
            globalThis.fetch = async (url) => {{
              calls.push(url);
              if (url === "/api/projects/current") {{
                return new Response(JSON.stringify(project), {{ status: 200 }});
              }}
              if (url === "/api/generation/options") {{
                return new Response(JSON.stringify({{ options: [{{
                  provider: "agent", model: "agent-image-generation", auth_modes: ["agent"]
                }}] }}), {{ status: 200 }});
              }}
              throw new Error("write must not run");
            }};
            const module = await import({json.dumps(module_url)});
            const find = (name) => module.TOOL_DEFINITIONS.find((item) => item.name === name);
            const common = {{ expected_revision: 3, idempotency_key: "00000000-0000-4000-8000-000000000040" }};
            const update = await find("update_project_plan").execute({{
              ...common, project_id: project.project_id, confirm_plan: true,
              plan: {{ storyPlan: "proposal", characterBible: "", storyboard: "", visualIdentityPack: "" }}
            }});
            const queue = await find("queue_generation").execute({{
              ...common, project_id: "background-project", provider: "agent",
              model: "agent-image-generation", auth_mode: "agent", confirm_cost: true
            }});
            const decision = await find("approve_provider_switch").execute({{
              ...common, project_id: "background-project", proposal_id: "p".repeat(32),
              confirm_switch: true
            }});
            console.log(JSON.stringify({{ update, queue, decision, calls }}));
            """
        )
        self.assertFalse(result["update"]["ok"])
        self.assertFalse(result["queue"]["ok"])
        self.assertFalse(result["decision"]["ok"])
        self.assertNotIn("/api/generation/queue", result["calls"])
        self.assertFalse(any(url.startswith("/api/approvals/") for url in result["calls"]))

    def test_non_revision_conflicts_are_not_reported_as_stale(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{ cookie: "comic_sol_csrf=csrf-token" }};
            globalThis.fetch = async (url) => new Response(
              url === "/api/projects/current"
                ? JSON.stringify({{
                    project_id: "current-project", revision: 1, status: "PLANNING",
                    summary: {{ plan: {{ storyPlan: "", characterBible: "", storyboard: "", visualIdentityPack: "" }} }}
                  }})
                : "{{}}",
              {{ status: url === "/api/projects/current" ? 200 : 409 }}
            );
            const module = await import({json.dumps(module_url)});
            const approve = await module.TOOL_DEFINITIONS.find(
              (item) => item.name === "approve_provider_switch"
            ).execute({{
              proposal_id: "p".repeat(32), project_id: "current-project",
              expected_revision: 1, confirm_switch: true,
              idempotency_key: "00000000-0000-4000-8000-000000000041"
            }});
            console.log(JSON.stringify(approve));
            """
        )
        self.assertFalse(result["ok"])
        self.assertEqual("conflict", result["error"]["code"])

    def test_qa_result_is_returned_and_published_to_the_studio(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const project = {{
              project_id: "project_0123456789abcdef01234567", revision: 4,
              status: "STORYBOARDED", summary: {{
                plan: {{ storyPlan: "private", characterBible: "", storyboard: "", visualIdentityPack: "" }},
                qa: {{ valid: false, issues: [{{ path: "private/path", message: "private finding" }}] }}
              }}
            }};
            let published = null;
            globalThis.CustomEvent = class {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
            globalThis.document = {{
              cookie: "comic_sol_csrf=csrf-token",
              dispatchEvent(event) {{ published = event; event.detail.accepted = true; return true; }}
            }};
            globalThis.fetch = async () => new Response(JSON.stringify(project), {{ status: 200 }});
            const module = await import({json.dumps(module_url)});
            const qa = await module.TOOL_DEFINITIONS.find((item) => item.name === "run_qa").execute({{
              project_id: project.project_id, expected_revision: 4,
              idempotency_key: "00000000-0000-4000-8000-000000000050"
            }});
            console.log(JSON.stringify({{
              qa, eventType: published.type,
              publishedProjectId: published.detail.project.project_id
            }}));
            """
        )
        self.assertFalse(result["qa"]["data"]["valid"])
        self.assertEqual(1, result["qa"]["data"]["issue_count"])
        self.assertEqual("comic-sol:qa-completed", result["eventType"])
        self.assertEqual("project_0123456789abcdef01234567", result["publishedProjectId"])
        self.assertNotIn("private", json.dumps(result["qa"]))

    def test_export_hands_off_to_review_without_downloading_the_payload(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            const project = {{
              project_id: "project_0123456789abcdef01234567", revision: 4,
              status: "STORYBOARDED", summary: {{ plan: {{
                storyPlan: "", characterBible: "", storyboard: "", visualIdentityPack: ""
              }} }}
            }};
            const calls = [];
            let requestedFormat = null;
            globalThis.CustomEvent = class {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
            globalThis.document = {{
              dispatchEvent(event) {{ requestedFormat = event.detail.format; event.detail.accepted = true; return true; }}
            }};
            globalThis.fetch = async (url, init = {{}}) => {{
              calls.push({{ url, method: init.method || "GET" }});
              return new Response(JSON.stringify(project), {{ status: 200 }});
            }};
            const module = await import({json.dumps(module_url)});
            const exported = await module.TOOL_DEFINITIONS.find((item) => item.name === "export_project").execute({{
              project_id: project.project_id, expected_revision: 4, format: "pdf",
              overwrite_confirmed: true,
              idempotency_key: "00000000-0000-4000-8000-000000000051"
            }});
            console.log(JSON.stringify({{ exported, requestedFormat, calls }}));
            """
        )
        self.assertTrue(result["exported"]["ok"])
        self.assertTrue(result["exported"]["data"]["review_required"])
        self.assertEqual("pdf", result["requestedFormat"])
        self.assertEqual([{"url": "/api/projects/current", "method": "GET"}], result["calls"])

    def test_confirmation_guards_match_the_studio_boundaries(self) -> None:
        module_url = WEBMCP.as_uri()
        result = self.run_node(
            f"""
            globalThis.document = {{ cookie: "comic_sol_csrf=csrf-token", getElementById() {{ return null; }} }};
            let calls = 0;
            globalThis.fetch = async () => {{ calls += 1; return new Response("{{}}", {{ status: 200 }}); }};
            const module = await import({json.dumps(module_url)});
            const find = (name) => module.TOOL_DEFINITIONS.find((item) => item.name === name);
            const common = {{ project_id: "project_0123456789abcdef01234567", expected_revision: 1, idempotency_key: "00000000-0000-4000-8000-000000000010" }};
            const oversizedPrompt = await find("create_project").execute({{
              title: "A title", prompt: "😀".repeat(60000), language: "en", mode: "short_prompt", page_count: 1,
              expected_revision: 0, idempotency_key: "00000000-0000-4000-8000-000000000012"
            }});
            const queue = await find("queue_generation").execute({{ ...common, provider: "agent", model: "agent-image-generation", auth_mode: "agent", confirm_cost: false }});
            const update = await find("update_project_plan").execute({{
              ...common,
              plan: {{ storyPlan: "", characterBible: "", storyboard: "", visualIdentityPack: "" }},
              confirm_plan: false,
            }});
            const exportResult = await find("export_project").execute({{ ...common, format: "pdf", overwrite_confirmed: false }});
            const approve = await find("approve_provider_switch").execute({{ proposal_id: "p".repeat(32), expected_revision: 1, confirm_switch: false, idempotency_key: "00000000-0000-4000-8000-000000000011" }});
            console.log(JSON.stringify({{ oversizedPrompt, queue, update, exportResult, approve, calls }}));
            """
        )
        self.assertEqual(0, result["calls"])
        self.assertFalse(result["oversizedPrompt"]["ok"])
        self.assertFalse(result["queue"]["ok"])
        self.assertFalse(result["update"]["ok"])
        self.assertFalse(result["exportResult"]["ok"])
        self.assertFalse(result["approve"]["ok"])

    def test_local_mcp_surface_remains_exactly_seventeen_tools(self) -> None:
        source = (WEB_ROOT.parent / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        self.assertEqual(17, len(names), sorted(names))
        self.assertTrue(all(name.startswith("comic_") for name in names))

    def test_static_source_and_wheel_inventory_include_webmcp(self) -> None:
        self.assertTrue(WEBMCP.is_file())
        self.assertIn(
            'import { registerWebMcp } from "./webmcp.js";',
            (STATIC_ROOT / "app.js").read_text(encoding="utf-8"),
        )
        project = tomllib.loads((WEB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = set(project["tool"]["setuptools"]["package-data"]["comic_sol_web"])
        self.assertIn("static/webmcp.js", package_data)

        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with TestClient(create_app(WebConfig.from_env(valid_environment()))) as client:
            response = client.get("/static/webmcp.js")
        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
