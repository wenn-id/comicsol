"""Owner-bound application services for canonical Comic Sol projects."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.engine_gateway import EngineGateway, ProjectSnapshot
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
    ) -> ProjectSnapshot:
        return self.gateway.create_project(principal.user_id, request)

    def import_project(
        self,
        principal: SessionPrincipal,
        archive: Path,
    ) -> ProjectSnapshot:
        return self.gateway.import_project(principal.user_id, archive)

    def snapshot(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int | None = None,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.snapshot(project_id, expected_revision)

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
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.submit_raster(
            project_id,
            expected_revision,
            job_id,
            raster,
            media_type,
        )

    def run_qa(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> ProjectSnapshot:
        self._authorize(principal, project_id)
        return self.gateway.run_qa(project_id, expected_revision)

    def export(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        formats: tuple[str, ...],
    ) -> Mapping[str, Path]:
        self._authorize(principal, project_id)
        return self.gateway.export(project_id, expected_revision, formats)
