"""Authenticated, bounded request-driven planning endpoints."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response

from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.api.projects import _require_csrf

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


def _envelope(job: Any) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "project_revision": job.project_revision,
        "state": job.state,
        "attempt_count": job.attempt_count,
        "provider": job.provider,
        "model": job.model,
        "published_revision": job.published_revision,
        "usage": dict(job.usage),
        "error_category": job.error_category,
    }


def _reject(error: Exception) -> NoReturn:
    from comic_sol_web.engine_gateway import ProjectUnavailableError, StaleProjectRevisionError
    from comic_sol_web.planning.service import PlanningConflictError

    if isinstance(error, HTTPException):
        error.headers = {**(error.headers or {}), **_PRIVATE_HEADERS}
        raise error from None
    if isinstance(error, ProjectUnavailableError):
        raise HTTPException(404, "planning job unavailable", headers=_PRIVATE_HEADERS) from None
    if isinstance(error, (PlanningConflictError, StaleProjectRevisionError)):
        raise HTTPException(409, "planning state conflict", headers=_PRIVATE_HEADERS) from None
    if isinstance(error, ValueError):
        raise HTTPException(400, "planning request rejected", headers=_PRIVATE_HEADERS) from None
    raise HTTPException(503, "planning unavailable", headers=_PRIVATE_HEADERS) from None


async def _consume_planning_queue(service: Any) -> None:
    """One job and at most its one schema repair per triggering request."""
    from comic_sol_web.planning.service import PlanningConflictError

    try:
        await service.run_once("web-planning-worker")
    except PlanningConflictError:
        # Another worker owns the job; this request must not consume extra work.
        return


def create_planning_router(service_source: Any) -> APIRouter:
    router = APIRouter(prefix="/api/planning", tags=["planning"])

    def service(request: Request) -> Any:
        return service_source(request) if callable(service_source) else service_source

    @router.get("/options")
    async def options(
        request: Request,
        response: Response,
        _principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return {
                "options": [
                    {
                        "provider": option.provider,
                        "model": option.model,
                        "enabled": option.enabled,
                        "required_environment_variable": option.required_environment_variable,
                    }
                    for option in service(request).options()
                ]
            }
        except Exception as error:
            _reject(error)

    @router.post("/jobs", status_code=201)
    async def queue(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            _require_csrf(request, principal)
            if set(body) != {"project_id", "expected_revision", "provider", "model"}:
                raise HTTPException(400, "planning request rejected", headers=_PRIVATE_HEADERS)
            planner = service(request)
            job = planner.queue(
                principal,
                body["project_id"],
                body["expected_revision"],
                body["provider"],
                body["model"],
                request.headers.get("Idempotency-Key", ""),
            )
            background_tasks.add_task(_consume_planning_queue, planner)
            return _envelope(job)
        except Exception as error:
            _reject(error)

    @router.get("/jobs/{job_id}")
    async def get(
        request: Request,
        response: Response,
        job_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return _envelope(service(request).get(principal, job_id))
        except Exception as error:
            _reject(error)

    return router
