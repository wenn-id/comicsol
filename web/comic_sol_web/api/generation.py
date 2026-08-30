"""Lazy FastAPI routes for owner-bound durable generation jobs."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)

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
    from comic_sol_web.generation.approvals import (
        ApprovalConflictError,
        ApprovalRequestError,
        ApprovalUnavailableError,
    )
    from comic_sol_web.generation.service import (
        GenerationConflictError,
        GenerationUnavailableError,
    )

    if isinstance(
        error,
        (GenerationUnavailableError, ProjectUnavailableError, ApprovalUnavailableError),
    ):
        raise HTTPException(status_code=404, detail="generation job unavailable") from error
    if isinstance(
        error,
        (GenerationConflictError, StaleProjectRevisionError, ApprovalConflictError),
    ):
        raise HTTPException(status_code=409, detail="generation state conflict") from error
    if isinstance(
        error,
        (GatewayError, ApprovalRequestError, KeyError, TypeError, ValueError),
    ):
        raise HTTPException(status_code=400, detail="generation request rejected") from error
    raise error


def _job_envelope(job: Any, expected_revision: int | None = None) -> dict[str, object]:
    revision_current = expected_revision is not None and job.project_revision == expected_revision
    can_cancel = revision_current and (
        job.state.value
        in {
            "queued",
            "polling",
            "awaiting_provider_confirmation",
            "paused",
        }
        or (job.state.value == "running" and job.external_job_id is not None)
    )
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
        "can_cancel": can_cancel,
    }
    if job.accepted_project_revision is not None:
        value["accepted_project_revision"] = job.accepted_project_revision
        artifact_job_id = getattr(getattr(job, "request", None), "job_id", None)
        if isinstance(artifact_job_id, str):
            value["artifact_job_id"] = artifact_job_id
    if job.state.value == "validating":
        value["artifact_state"] = "staged"
    elif job.state.value == "accepted":
        value["artifact_state"] = "accepted"
    return value


def _recommendation_envelope(recommendation: Any) -> dict[str, object]:
    cost = recommendation.estimated_cost
    return {
        "provider": recommendation.provider,
        "model": recommendation.model,
        "auth_mode": recommendation.auth_mode.value,
        "reasons": list(recommendation.reasons),
        "estimated_cost": None if cost is None else dict(cost),
    }


def _options_envelope(models: Any) -> list[dict[str, object]]:
    return [
        {
            "provider": entry.provider,
            "model": entry.model,
            "capabilities": sorted(entry.capabilities),
            "auth_modes": ["agent"] if entry.provider in {"agent", "fake"} else ["hosted", "byok"],
        }
        for entry in models
    ]


def _revision(body: dict[str, object]) -> int:
    value = body.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=400, detail="generation request rejected")
    return value


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="generation request rejected") from error
    canonical = str(parsed)
    if value.lower() != canonical:
        raise HTTPException(status_code=400, detail="generation request rejected")
    return canonical


def _proposal_envelope(proposal: Any) -> dict[str, object]:
    return {
        "proposal_id": proposal.proposal_id,
        "job_ids": list(proposal.job_ids),
        "project_id": proposal.project_id,
        "project_revision": proposal.project_revision,
        "from_provider": proposal.from_provider,
        "to_provider": proposal.to_provider,
        "to_model": proposal.to_model,
        "reason": proposal.reason.value,
        "expires_at": proposal.expires_at,
    }


def _switch_reason(service: Any, job_id: str) -> Any:
    from comic_sol_web.generation.types import ErrorCategory

    for attempt in reversed(service.attempts(job_id)):
        value = attempt.get("error_category")
        try:
            if value is not None:
                return ErrorCategory(value)
        except (TypeError, ValueError):
            continue
    raise HTTPException(status_code=409, detail="generation state conflict")


async def _available_routing_credentials(
    source: Any,
    service: Any,
    request: Request,
    principal: SessionPrincipal,
) -> dict[str, tuple[object, ...]]:
    from comic_sol_web.generation.credentials import CredentialBrokerError
    from comic_sol_web.generation.types import AuthMode

    resolver = _resolve_service(source, request)
    executable = await service.available_options()
    providers = sorted(
        {entry.provider for entry in executable if entry.provider not in {"agent", "fake"}}
    )
    available: dict[str, tuple[object, ...]] = {}
    for provider in providers:
        modes: list[AuthMode] = []
        for mode in (AuthMode.HOSTED, AuthMode.BYOK):
            try:
                async with resolver.resolve(principal.user_id, provider, mode):
                    modes.append(mode)
            except CredentialBrokerError:
                continue
        if modes:
            available[provider] = tuple(modes)
    return available


_POLL_DELAY_SECONDS = 0.05
_MAX_POLLING_RESULTS_PER_REQUEST = 4
_MAX_RUN_ATTEMPTS_PER_REQUEST = 64


async def _consume_queue(service: Any) -> None:
    """Drain eligible work within one bounded request-background budget."""
    from comic_sol_web.engine_gateway import StaleProjectRevisionError
    from comic_sol_web.generation.service import GenerationConflictError

    polling_results = 0
    for _attempt in range(_MAX_RUN_ATTEMPTS_PER_REQUEST):
        try:
            completed = await service.run_once("web-request-worker")
        except (GenerationConflictError, StaleProjectRevisionError):
            continue
        if completed is None:
            return
        if completed.state.value == "polling":
            polling_results += 1
            if polling_results >= _MAX_POLLING_RESULTS_PER_REQUEST:
                return
            await asyncio.sleep(_POLL_DELAY_SECONDS)


def create_generation_router(
    service_source: Any,
    approval_source: Any | None = None,
    credential_source: Any | None = None,
) -> APIRouter:
    """Register routes without constructing storage, providers, or workers."""
    router = APIRouter(prefix="/api/generation", tags=["generation"])

    @router.get("/options")
    async def generation_options(
        request: Request,
        response: Response,
        _principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            service = _resolve_service(service_source, request)
            return {"options": _options_envelope(await service.available_options())}
        except Exception as error:
            _reject(error)

    @router.get("/recommendations")
    async def generation_recommendations(
        request: Request,
        response: Response,
        project_id: str,
        expected_revision: int,
        job_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            service = _resolve_service(service_source, request)
            jobs = service.list_jobs(
                principal,
                project_id,
                expected_revision,
                limit=50,
            )
            job = next((item for item in jobs if item.job_id == job_id), None)
            if job is None:
                raise HTTPException(status_code=404, detail="generation job unavailable")
            from comic_sol_web.generation.router import recommend
            from comic_sol_web.generation.types import ErrorCategory

            history: dict[tuple[str, str], dict[str, object]] = {}
            for attempt in reversed(service.attempts(job.job_id)):
                error = attempt.get("error_category")
                try:
                    if error is not None:
                        history[(job.provider, job.model)] = {"last_error": ErrorCategory(error)}
                        break
                except (TypeError, ValueError):
                    continue
            recommendations = recommend(
                job.request,
                {(job.provider, job.model): (job.auth_mode,)},
                history,
            )
            return {"recommendations": [_recommendation_envelope(item) for item in recommendations]}
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.get("/jobs")
    async def list_generation_jobs(
        request: Request,
        response: Response,
        project_id: str,
        expected_revision: int,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        limit: int = 50,
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            service = _resolve_service(service_source, request)
            jobs = service.list_jobs(
                principal,
                project_id,
                expected_revision,
                limit=limit,
            )
            accepted = service.current_accepted(
                principal,
                project_id,
                expected_revision,
            )
            return {
                "jobs": [_job_envelope(job, expected_revision) for job in jobs],
                "accepted_job": (
                    None if accepted is None else _job_envelope(accepted, expected_revision)
                ),
            }
        except Exception as error:
            _reject(error)

    @router.post("/queue", status_code=status.HTTP_201_CREATED)
    async def queue_generation(
        request: Request,
        background_tasks: BackgroundTasks,
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
            revision = _revision(body)
            jobs = service.queue(
                principal,
                project_id,
                revision,
                provider=provider,
                model=model,
                auth_mode=auth_mode,
                max_retries=max_retries,
            )
            background_tasks.add_task(_consume_queue, service)
            return {"jobs": [_job_envelope(job, revision) for job in jobs]}
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
        background_tasks: BackgroundTasks,
        job_id: str,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        try:
            service = _resolve_service(service_source, request)
            revision = _revision(body)
            job = service.retry_same_provider(principal, job_id, revision)
            background_tasks.add_task(_consume_queue, service)
            return _job_envelope(job, revision)
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.post("/{job_id}/cancel")
    async def cancel_generation(
        request: Request,
        job_id: str,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        if set(body) != {"expected_revision"}:
            raise HTTPException(status_code=400, detail="generation request rejected")
        try:
            service = _resolve_service(service_source, request)
            revision = _revision(body)
            return _job_envelope(
                await service.cancel(principal, job_id, revision),
                revision,
            )
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
        if set(body) != {"expected_revision"}:
            raise HTTPException(status_code=400, detail="generation request rejected")
        if approval_source is None or credential_source is None:
            raise HTTPException(status_code=409, detail="generation state conflict")
        try:
            revision = _revision(body)
            service = _resolve_service(service_source, request)
            job = service.get(principal, job_id)
            if job.project_revision != revision:
                raise HTTPException(status_code=409, detail="generation state conflict")
            reason = _switch_reason(service, job_id)
            credentials = await _available_routing_credentials(
                credential_source,
                service,
                request,
                principal,
            )
            from comic_sol_web.generation.router import recommend

            recommendations = recommend(
                job.request,
                credentials,
                {(job.provider, job.model): {"last_error": reason}},
            )
            recommendation = next(
                (item for item in recommendations if item.provider != job.provider),
                None,
            )
            if recommendation is None:
                raise HTTPException(status_code=409, detail="generation state conflict")
            approvals = _resolve_service(approval_source, request)
            proposal = approvals.propose_switch(
                principal,
                job.project_id,
                revision,
                (job.job_id,),
                recommendation,
                reason,
                idempotency_key=_idempotency_key(request),
            )
            return _proposal_envelope(proposal)
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
            revision = _revision(body)
            return _job_envelope(
                service.submit_staged_raster(principal, job_id, revision),
                revision,
            )
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    return router


create_router = create_generation_router
