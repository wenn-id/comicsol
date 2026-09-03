"""Offline prompt-to-PDF golden path through the live Web composition."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from comic_sol_web.app import create_app
from comic_sol_web.assets import AssetStore
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.config import WebConfig
from comic_sol_web.engine_gateway import EngineGateway
from comic_sol_web.generation.approvals import ProviderSwitchApprovals
from comic_sol_web.generation.providers.base import ProviderRegistry
from comic_sol_web.generation.service import GenerationService
from comic_sol_web.generation.types import GenerationResult, JobState
from comic_sol_web.planning.service import PlanningService
from comic_sol_web.planning.types import PlanResult, VisualReviewResult
from comic_sol_web.projects import ProjectService
from comic_sol_web.workflow import WorkflowService
from tests.test_finalization import valid_page_reviewer_checks
from web.tests.fixtures.wp16_fixture import FakeAuth, bounded_png
from web.tests.test_projects import first_plan_payload


class FakePlanningAndVisualReviewProvider:
    provider_id = "openai"
    model = "gpt-5.4-mini"

    def __init__(self, projects: ProjectService, principal: SessionPrincipal) -> None:
        self.projects = projects
        self.principal = principal

    async def generate_plan(self, request, credential):
        return PlanResult(first_plan_payload(), {"input_tokens": 1})

    async def review_visual(self, request, credential):
        if request.kind == "page":
            project_id = request.context["project_id"]
            root = self.projects.snapshot(self.principal, project_id).root
            return VisualReviewResult(tuple(valid_page_reviewer_checks(root, 1)), (), {})
        checks = tuple(
            {
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": f"Offline reviewer inspected visible {check_id} evidence across the panel.",
                "method": "bounded-visual-review",
                "reviewer": "offline-fixture",
                "regions": [],
            }
            for check_id in request.check_ids
        )
        assessments = tuple(
            {
                "character_id": character["character_id"],
                "trait": trait["trait"],
                "result": "pass",
                "severity": "error",
                "evidence": f"Visible {trait['trait']} matches the approved character identity.",
            }
            for character in request.context.get("characters", ())
            for trait in character["traits"]
        )
        return VisualReviewResult(checks, assessments, {})


class FakeImageProvider:
    provider_id = "fake"

    async def generate(self, request, model, credential):
        return GenerationResult(
            None,
            JobState.ACCEPTED,
            bounded_png(request.width, request.height),
            "image/png",
            {"width": request.width, "height": request.height},
            {"images": 1},
        )

    async def poll(self, external_job_id, credential):
        raise AssertionError("synchronous fake provider must not poll")

    async def cancel(self, external_job_id, credential):
        return None


class FakeCredentials:
    @asynccontextmanager
    async def resolve(self, owner_id, provider, auth_mode):
        yield "offline-fixture-credential"


class LiveGoldenPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.principal = SessionPrincipal("comic-sol-local-user", "local")
        self.client: TestClient | None = None
        self.restart_app_with_same_data_root()

    def tearDown(self) -> None:
        if self.client is not None:
            self.client.close()

    def restart_app_with_same_data_root(self) -> None:
        if self.client is not None:
            self.client.close()
        app = create_app(WebConfig.local_from_env({"COMIC_SOL_WEB_DATA_ROOT": str(self.data_root)}))
        gateway = EngineGateway.open(self.data_root)
        projects = ProjectService(gateway)
        assets = AssetStore(gateway.database, self.data_root)
        credentials = FakeCredentials()
        planning = PlanningService(
            gateway.database,
            projects,
            (FakePlanningAndVisualReviewProvider(projects, self.principal),),
            credentials,
        )
        generation = GenerationService(
            gateway.database,
            projects,
            ProviderRegistry((FakeImageProvider(),)),
            gateway.staging_root,
            credentials=credentials,
            assets=assets,
        )
        app.state.projects = projects
        app.state.assets = assets
        app.state.planning = planning
        app.state.generation = generation
        app.state.generation_credentials = credentials
        app.state.approvals = ProviderSwitchApprovals(gateway.database)
        app.state.workflow = WorkflowService(
            gateway.database, projects, planning, generation
        )
        app.state.auth = FakeAuth(self.principal)
        app.dependency_overrides[require_principal] = lambda: self.principal
        self.app = app
        self.client = TestClient(app, client=("127.0.0.1", 50000))

    def create_prompt_project(self):
        response = self.client.post(
            "/api/projects",
            json={
                "title": "Sunlight Courier",
                "prompt": "A courier carries the last light through a quiet city.",
                "language": "en",
                "page_count": 1,
            },
            headers={"Idempotency-Key": str(uuid4()), "X-Expected-Revision": "0"},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def queue_fake_plan(self, project):
        response = self.client.post(
            "/api/planning/jobs",
            json={
                "project_id": project["project_id"],
                "expected_revision": project["revision"],
                "provider": "openai",
                "model": "gpt-5.4-mini",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def approve_with_fake_image_provider(self, reviewed):
        response = self.client.post(
            "/api/workflows",
            json={
                "project_id": reviewed["project_id"],
                "expected_revision": reviewed["revision"],
                "planning_job_id": self.planning_job_id,
                "image_provider": "fake",
                "image_model": "fake-raster-v1",
                "image_auth_mode": "agent",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def drive_until(self, value, target):
        if "job_id" in value:
            self.planning_job_id = value["job_id"]
            for _ in range(10):
                response = self.client.get(f"/api/planning/jobs/{value['job_id']}")
                self.assertEqual(200, response.status_code, response.text)
                value = response.json()
                if value["state"] == target:
                    return value
        else:
            for _ in range(80):
                response = self.client.get(f"/api/workflows/{value['project_id']}")
                self.assertEqual(200, response.status_code, response.text)
                value = response.json()
                if value["phase"] == target or value["state"] == target:
                    return value
                self.assertEqual("running", value["state"], value)
        self.fail(f"did not reach {target}: {value}")

    def first_accepted_raster_url(self, completed):
        response = self.client.get(
            f"/api/generation/jobs?project_id={completed['project_id']}"
            f"&expected_revision={completed['revision']}"
        )
        self.assertEqual(200, response.status_code, response.text)
        job = next(
            item for item in response.json()["jobs"]
            if item["subject_kind"] == "panel" and item.get("artifact_job_id")
        )
        return (
            f"/api/projects/{completed['project_id']}/accepted-raster/{job['artifact_job_id']}"
            f"?expected_revision={completed['revision']}"
        )

    def download_pdf(self, completed):
        return self.client.post(
            f"/api/projects/{completed['project_id']}/export",
            json={"format": "pdf", "overwrite_confirmed": True},
            headers={
                "Idempotency-Key": str(uuid4()),
                "X-Expected-Revision": str(completed["revision"]),
            },
        )

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


if __name__ == "__main__":
    unittest.main()
