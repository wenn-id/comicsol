"""Owner-bound application services for canonical Comic Sol projects."""

from __future__ import annotations

from pathlib import Path
from typing import ContextManager, Mapping

from comic_sol_web.planning.types import PlanRequest, VisualReviewRequest, VisualReviewResult

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.engine_gateway import AcceptedRaster, EngineGateway, ProjectSnapshot
from comic_sol_web.generation.types import GenerationRequest


class ProjectService:
    """Authorize opaque project IDs before crossing the engine gateway."""

    def __init__(self, gateway: EngineGateway) -> None:
        self.gateway = gateway

    def _authorize(self, principal: SessionPrincipal, project_id: str) -> None:
        self.gateway.require_owner(project_id, principal.user_id)

    def create_project(
        self,
        principal: SessionPrincipal,
        request: Mapping[str, object],
        idempotency_key: str,
    ) -> ProjectSnapshot:
        return self.gateway.create_project(principal.user_id, request, idempotency_key)

    def import_project(
        self,
        principal: SessionPrincipal,
        archive: Path,
        idempotency_key: str,
    ) -> ProjectSnapshot:
        return self.gateway.import_project(principal.user_id, archive, idempotency_key)

    def current_project(self, principal: SessionPrincipal) -> ProjectSnapshot | None:
        return self.gateway.current_project(principal.user_id)

    def snapshot(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int | None = None,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.snapshot(project_id, expected_revision)

    def read_plan(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int | None = None,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.read_plan(project_id, expected_revision)

    def update_plan(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        plan: Mapping[str, object],
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.update_plan(project_id, expected_revision, plan)

    def planning_input(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> PlanRequest:
        self._authorize(principal, project_id)
        return self.gateway.planning_input(project_id, expected_revision)

    def planning_publication(
        self, principal: SessionPrincipal, project_id: str
    ) -> ContextManager[None]:
        self._authorize(principal, project_id)
        return self.gateway.planning_publication(project_id)

    def prepare_generation(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> tuple[GenerationRequest, ...]:
        self._authorize(principal, project_id)
        return self.gateway.prepare_generation(project_id, expected_revision)

    def submit_raster(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        job_id: str,
        raster: Path,
        media_type: str,
        capabilities_used: Mapping[str, object],
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.submit_raster(
            project_id,
            expected_revision,
            job_id,
            raster,
            media_type,
            capabilities_used,
        )

    def accepted_raster(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        job_id: str,
    ) -> AcceptedRaster:
        self._authorize(principal, project_id)
        return self.gateway.accepted_raster(
            project_id,
            expected_revision,
            job_id,
        )

    def run_qa(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.run_qa(project_id, expected_revision)

    def panel_review_input(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int, panel_id: str
    ) -> VisualReviewRequest:
        self._authorize(principal, project_id)
        return self.gateway.panel_review_input(project_id, expected_revision, panel_id)

    def publish_panel_review(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        panel_id: str,
        review: VisualReviewResult,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.publish_panel_review(project_id, expected_revision, panel_id, review)

    def prepare_pages(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.prepare_pages(project_id, expected_revision)

    def page_review_input(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int, page_number: int
    ) -> VisualReviewRequest:
        self._authorize(principal, project_id)
        return self.gateway.page_review_input(project_id, expected_revision, page_number)

    def publish_page_review(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        page_number: int,
        review: VisualReviewResult,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.publish_page_review(project_id, expected_revision, page_number, review)

    def finalize(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> tuple[ProjectSnapshot, Path]:
        self._authorize(principal, project_id)
        return self.gateway.finalize(project_id, expected_revision)

    def export(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        formats: tuple[str, ...],
    ) -> Mapping[str, Path]:
        self._authorize(principal, project_id)
        return self.gateway.export(project_id, expected_revision, formats)
