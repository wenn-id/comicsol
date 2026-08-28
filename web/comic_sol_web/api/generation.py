"""Lazy FastAPI routes for owner-bound durable generation jobs."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from comic_sol_web.auth import AuthError, SessionPrincipal, require_principal


def _resolve_service(source: Any, request: Request) -> Any:
    return source(request) if callable(source) else source


def _require_csrf(request: Request, principal: SessionPrincipal) -> None:
    auth = getattr(request.app.state, "auth", None)
    try:
        if auth is None:
            raise AuthError("authentication unavailable")
        csrf_principal = auth.require_csrf(request)
        if csrf_principal != principal:
            raise AuthError("authenticated identity changed")
    except AuthError as error:
        raise HTTPException(status_code=403, detail="CSRF validation failed") from error


def _reject(error: Exception) -> NoReturn:
    # Generation and engine modules stay outside application construction and
    # /healthz; imports occur only after a generation endpoint is invoked.
    from comic_sol_web.engine_gateway import (
        GatewayError,
        ProjectUnavailableError,
        StaleProjectRevisionError,
    )
    from comic_sol_web.generation.service import (
        GenerationConflictError,
        GenerationUnavailableError,
    )

    if isinstance(error, (GenerationUnavailableError, ProjectUnavailableError)):
        raise HTTPException(status_code=404, detail="generation job unavailable") from error
    if isinstance(error, (GenerationConflictError, StaleProjectRevisionError)):
        raise HTTPException(status_code=409, detail="generation state conflict") from error
    if isinstance(error, (GatewayError, KeyError, TypeError, ValueError)):
        raise HTTPException(status_code=400, detail="generation request rejected") from error
    raise error


def _job_envelope(job: Any) -> dict[str, object]:
    value: dict[str, object] = {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "project_revision": job.project_revision,
        "state": job.state.value,
        "provider": job.provider,
        "model": job.model,
        "auth_mode": job.auth_mode.value,
        "attempt": job.attempt_number,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
    }
    if job.external_job_id is not None:
        value["external_job_id"] = job.external_job_id
    if job.accepted_project_revision is not None:
        value["accepted_project_revision"] = job.accepted_project_revision
    return value


def _revision(body: dict[str, object]) -> int:
    value = body.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=400, detail="generation request rejected")
    return value


def create_generation_router(service_source: Any) -> APIRouter:
    """Register routes without constructing storage, providers, or workers."""
    router = APIRouter(prefix="/api/generation", tags=["generation"])

    @router.post("/queue", status_code=status.HTTP_201_CREATED)
    async def queue_generation(
        request: Request,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        project_id = body.get("project_id")
        provider = body.get("provider")
        model = body.get("model")
        auth_mode = body.get("auth_mode")
        max_retries = body.get("max_retries", 2)
        if not all(isinstance(item, str) for item in (project_id, provider, model, auth_mode)):
            raise HTTPException(status_code=400, detail="generation request rejected")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise HTTPException(status_code=400, detail="generation request rejected")
        try:
            service = _resolve_service(service_source, request)
            jobs = service.queue(
                principal,
                project_id,
                _revision(body),
                provider=provider,
                model=model,
                auth_mode=auth_mode,
                max_retries=max_retries,
            )
            return {"jobs": [_job_envelope(job) for job in jobs]}
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.get("/{job_id}")
    async def get_generation(
        request: Request,
        job_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        try:
            service = _resolve_service(service_source, request)
            return _job_envelope(service.get(principal, job_id))
        except Exception as error:
            _reject(error)

    @router.post("/{job_id}/retry")
    async def retry_generation(
        request: Request,
        job_id: str,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        try:
            service = _resolve_service(service_source, request)
            return _job_envelope(service.retry_same_provider(principal, job_id, _revision(body)))
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.post("/{job_id}/pause-for-switch")
    async def pause_generation_for_switch(
        request: Request,
        job_id: str,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        try:
            service = _resolve_service(service_source, request)
            return _job_envelope(service.pause_for_switch(principal, job_id, _revision(body)))
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.post("/{job_id}/submit-staged")
    async def submit_staged_generation(
        request: Request,
        job_id: str,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        try:
            service = _resolve_service(service_source, request)
            return _job_envelope(service.submit_staged_raster(principal, job_id, _revision(body)))
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    return router


create_router = create_generation_router
