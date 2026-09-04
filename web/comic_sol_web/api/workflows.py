"""Authenticated request-driven production workflow and replayable events."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from comic_sol_web.api.projects import _require_csrf
from comic_sol_web.auth import SessionPrincipal, require_principal

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


def _resolve(source: Any, request: Request) -> Any:
    return source(request) if callable(source) else source


def _workflow_envelope(workflow: Any) -> dict[str, object]:
    return {
        "project_id": workflow.project_id,
        "revision": workflow.revision,
        "state": workflow.state,
        "phase": workflow.phase,
        "planning_job_id": workflow.planning_job_id,
        "planning_provider": workflow.planning_provider,
        "planning_model": workflow.planning_model,
        "image_provider": workflow.image_provider,
        "image_model": workflow.image_model,
        "image_auth_mode": workflow.image_auth_mode,
        "error_category": workflow.error_category,
        "extra_calls": workflow.extra_calls,
        "can_pause": workflow.state == "running",
        "can_resume": workflow.state in {"paused", "blocked"},
        "pdf_available": workflow.state == "complete",
    }


def _event_envelope(event: Any) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "project_id": event.project_id,
        "project_revision": event.project_revision,
        "type": event.type,
        "phase": event.phase,
        "status": event.status,
        "provider": event.provider,
        "model": event.model,
        "attempt": event.attempt,
        "progress": dict(event.progress),
        "summary": event.summary,
        "created_at": event.created_at,
    }


def _reject(error: Exception) -> NoReturn:
    from comic_sol_web.engine_gateway import (
        GatewayError,
        ProjectUnavailableError,
        StaleProjectRevisionError,
    )
    from comic_sol_web.generation.service import GenerationConflictError
    from comic_sol_web.workflow import WorkflowConflictError, WorkflowUnavailableError

    if isinstance(error, HTTPException):
        error.headers = {**(error.headers or {}), **_PRIVATE_HEADERS}
        raise error from None
    if isinstance(error, (WorkflowUnavailableError, ProjectUnavailableError)):
        raise HTTPException(404, "workflow unavailable", headers=_PRIVATE_HEADERS) from None
    if isinstance(
        error, (WorkflowConflictError, StaleProjectRevisionError, GenerationConflictError)
    ):
        raise HTTPException(409, "workflow state conflict", headers=_PRIVATE_HEADERS) from None
    if isinstance(error, (GatewayError, KeyError, TypeError, ValueError)):
        raise HTTPException(400, "workflow request rejected", headers=_PRIVATE_HEADERS) from None
    raise HTTPException(503, "workflow unavailable", headers=_PRIVATE_HEADERS) from None


def _revision(body: dict[str, object]) -> int:
    revision = body.get("expected_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("workflow revision is invalid")
    return revision


def _require_idempotency(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "")
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError):
        raise ValueError("workflow idempotency key is invalid") from None
    if value.lower() != canonical:
        raise ValueError("workflow idempotency key is invalid")
    return canonical


async def _pump(service: Any) -> None:
    try:
        await service.advance_once("web-workflow-worker")
    except Exception:
        # The durable workflow retains normalized provider failures. Races and
        # stale revisions are retried by a later owner-bound snapshot request.
        return


def _cursor(request: Request, after: int | None) -> int:
    value: object = request.headers.get("Last-Event-ID")
    if value is None:
        value = 0 if after is None else after
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError("workflow event cursor is invalid")
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        raise ValueError("workflow event cursor is invalid") from None
    if cursor < 0:
        raise ValueError("workflow event cursor is invalid")
    return cursor


def create_workflows_router(service_source: Any) -> APIRouter:
    router = APIRouter(prefix="/api/workflows", tags=["workflows"])

    @router.post("", status_code=201)
    async def approve(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers.update(_PRIVATE_HEADERS)
        try:
            _require_csrf(request, principal)
            if set(body) != {
                "project_id",
                "expected_revision",
                "planning_job_id",
                "image_provider",
                "image_model",
                "image_auth_mode",
            } or not all(
                isinstance(body.get(key), str)
                for key in (
                    "project_id",
                    "planning_job_id",
                    "image_provider",
                    "image_model",
                    "image_auth_mode",
                )
            ):
                raise ValueError("workflow approval envelope is invalid")
            service = _resolve(service_source, request)
            workflow = service.approve_plan(
                principal,
                body["project_id"],
                _revision(body),
                planning_job_id=body["planning_job_id"],
                image_provider=body["image_provider"],
                image_model=body["image_model"],
                image_auth_mode=body["image_auth_mode"],
                idempotency_key=_require_idempotency(request),
            )
            background_tasks.add_task(_pump, service)
            return _workflow_envelope(workflow)
        except Exception as error:
            _reject(error)

    @router.get("/{project_id}")
    async def snapshot(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers.update(_PRIVATE_HEADERS)
        try:
            service = _resolve(service_source, request)
            workflow = service.snapshot(principal, project_id)
            if workflow.state == "running":
                background_tasks.add_task(_pump, service)
            return _workflow_envelope(workflow)
        except Exception as error:
            _reject(error)

    async def change_state(
        target: str,
        request: Request,
        body: dict[str, object],
        project_id: str,
        principal: SessionPrincipal,
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        if set(body) != {"expected_revision"}:
            raise ValueError("workflow state envelope is invalid")
        _require_idempotency(request)
        service = _resolve(service_source, request)
        method = service.pause if target == "pause" else service.resume
        return _workflow_envelope(method(principal, project_id, _revision(body)))

    @router.post("/{project_id}/pause")
    async def pause(
        request: Request,
        response: Response,
        body: Annotated[dict[str, object], Body()],
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers.update(_PRIVATE_HEADERS)
        try:
            return await change_state("pause", request, body, project_id, principal)
        except Exception as error:
            _reject(error)

    @router.post("/{project_id}/resume")
    async def resume(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        body: Annotated[dict[str, object], Body()],
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        response.headers.update(_PRIVATE_HEADERS)
        try:
            service = _resolve(service_source, request)
            value = await change_state("resume", request, body, project_id, principal)
            background_tasks.add_task(_pump, service)
            return value
        except Exception as error:
            _reject(error)

    @router.get("/{project_id}/events")
    async def events(
        request: Request,
        response: Response,
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        after: int | None = None,
        limit: int = 100,
    ) -> object:
        response.headers.update(_PRIVATE_HEADERS)
        try:
            service = _resolve(service_source, request)
            service.snapshot(principal, project_id)
            cursor = _cursor(request, after)
            rows = service.events_after(principal, project_id, cursor, limit=limit)
            if "text/event-stream" not in request.headers.get("accept", ""):
                return {"events": [_event_envelope(event) for event in rows]}

            async def stream():
                if rows:
                    for event in rows:
                        payload = json.dumps(
                            _event_envelope(event), separators=(",", ":"), sort_keys=True
                        )
                        yield f"id: {event.event_id}\ndata: {payload}\n\n"
                    return
                await asyncio.sleep(0)
                yield ": keepalive\n\n"

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    **_PRIVATE_HEADERS,
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )
        except Exception as error:
            _reject(error)

    return router


create_router = create_workflows_router
