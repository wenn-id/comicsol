"""Lazy owner-bound endpoints for one-shot provider-switch decisions."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request

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


def _write_headers(request: Request) -> tuple[str, int]:
    key = request.headers.get("Idempotency-Key", "")
    revision = request.headers.get("X-Expected-Revision", "")
    try:
        parsed_key = UUID(key)
        parsed_revision = int(revision)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="provider switch request rejected") from error
    if str(parsed_key) != key.lower() or parsed_revision < 1:
        raise HTTPException(status_code=400, detail="provider switch request rejected")
    return key.lower(), parsed_revision


def _reject(error: Exception) -> NoReturn:
    from comic_sol_web.generation.approvals import (
        ApprovalConflictError,
        ApprovalRequestError,
        ApprovalUnavailableError,
    )

    if isinstance(error, ApprovalUnavailableError):
        raise HTTPException(
            status_code=404, detail="provider switch proposal unavailable"
        ) from error
    if isinstance(error, ApprovalConflictError):
        raise HTTPException(status_code=409, detail="provider switch state conflict") from error
    if isinstance(error, (ApprovalRequestError, TypeError, ValueError)):
        raise HTTPException(status_code=400, detail="provider switch request rejected") from error
    raise error


def _envelope(proposal: Any, decision: str) -> dict[str, object]:
    return {
        "proposal_id": proposal.proposal_id,
        "project_id": proposal.project_id,
        "project_revision": proposal.project_revision,
        "job_ids": list(proposal.job_ids),
        "decision": decision,
    }


def create_approvals_router(
    service_source: Any,
    generation_source: Any | None = None,
) -> APIRouter:
    """Register decision routes without constructing proposal storage."""
    router = APIRouter(prefix="/api/approvals", tags=["approvals"])

    @router.post("/{proposal_id}/approve")
    async def approve_provider_switch(
        request: Request,
        background_tasks: BackgroundTasks,
        proposal_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        body: Annotated[dict[str, object] | None, Body()] = None,
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        if body:
            raise HTTPException(status_code=400, detail="provider switch request rejected")
        key, revision = _write_headers(request)
        try:
            service = _resolve_service(service_source, request)
            proposal = service.approve(
                principal,
                proposal_id,
                expected_revision=revision,
                idempotency_key=key,
            )
            if generation_source is not None:
                from comic_sol_web.api.generation import _consume_queue

                generation = _resolve_service(generation_source, request)
                background_tasks.add_task(_consume_queue, generation)
            return _envelope(proposal, "approved")
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.post("/{proposal_id}/reject")
    async def reject_provider_switch(
        request: Request,
        proposal_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
        body: Annotated[dict[str, object] | None, Body()] = None,
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        if body:
            raise HTTPException(status_code=400, detail="provider switch request rejected")
        key, revision = _write_headers(request)
        try:
            service = _resolve_service(service_source, request)
            proposal = service.reject(
                principal,
                proposal_id,
                expected_revision=revision,
                idempotency_key=key,
            )
            return _envelope(proposal, "rejected")
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    return router


create_router = create_approvals_router
