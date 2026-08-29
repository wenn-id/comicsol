"""FastAPI routes for bounded page-owned raster handles."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from comic_sol_web.api.generation import _job_envelope, _reject, _require_csrf, _revision
from comic_sol_web.auth import AuthError, SessionPrincipal, require_principal

_ASSET_HANDLE = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _opaque_asset_handle(value: str) -> str:
    """Reject path syntax before request data reaches the asset store boundary."""
    if _ASSET_HANDLE.fullmatch(value) is None:
        raise ValueError("asset handle is invalid")
    return value


def _agent_binding(project_id: str, job_id: str, expected_revision: int) -> tuple[str, str, int]:
    if (
        _PROJECT_ID.fullmatch(project_id) is None
        or _JOB_ID.fullmatch(job_id) is None
        or isinstance(expected_revision, bool)
        or expected_revision < 1
    ):
        raise ValueError("agent handoff binding is invalid")
    return project_id, job_id, expected_revision


def _resolve(source: Any, request: Request) -> Any:
    return source(request) if callable(source) else source


def create_assets_router(store_source: Any, generation_source: Any | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/assets", tags=["assets"])

    @router.post("")
    async def upload(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, int | str]:
        from comic_sol_web.assets import AssetError

        auth = getattr(request.app.state, "auth", None)
        try:
            if auth is None:
                raise AuthError("authentication unavailable")
            csrf_principal = auth.require_csrf(request)
            if csrf_principal != principal:
                raise AuthError("authenticated identity changed")
            store = _resolve(store_source, request)
            handle = store.create_upload(
                principal,
                file.file,
                file.content_type or "application/octet-stream",
            )
        except AuthError as error:
            raise HTTPException(status_code=403, detail="CSRF validation failed") from error
        except AssetError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "asset_id": handle.asset_id,
            "media_type": handle.media_type,
            "byte_size": handle.byte_size,
            "width": handle.width,
            "height": handle.height,
        }

    @router.get("/{asset_id}")
    async def download(
        request: Request,
        asset_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> Response:
        from comic_sol_web.assets import AssetError

        try:
            store = _resolve(store_source, request)
            canonical_asset_id = _opaque_asset_handle(asset_id)
            handle = store.get(principal, canonical_asset_id)
            # Read only the owner-authorized identifier returned by durable
            # metadata, not the raw request path segment.
            content = store.read_bytes(principal, handle.asset_id)
        except (AssetError, ValueError) as error:
            raise HTTPException(status_code=404, detail="asset unavailable") from error
        return Response(
            content,
            media_type=handle.media_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    if generation_source is not None:

        @router.get("/agent-handoff/{job_id}")
        async def get_agent_package(
            request: Request,
            job_id: str,
            project_id: Annotated[str, Query()],
            expected_revision: Annotated[int, Query()],
            principal: Annotated[SessionPrincipal, Depends(require_principal)],
        ) -> JSONResponse:
            try:
                bound_project, bound_job, bound_revision = _agent_binding(
                    project_id,
                    job_id,
                    expected_revision,
                )
                service = _resolve(generation_source, request)
                package = service.agent_package(
                    principal,
                    bound_project,
                    bound_job,
                    bound_revision,
                )
                return JSONResponse(
                    dict(package),
                    headers={
                        "Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            except Exception as error:
                _reject(error)

        @router.post("/{asset_id}/submit-agent")
        async def submit_agent_asset(
            request: Request,
            asset_id: str,
            body: Annotated[dict[str, object], Body()],
            principal: Annotated[SessionPrincipal, Depends(require_principal)],
        ) -> dict[str, object]:
            _require_csrf(request, principal)
            job_id = body.get("job_id")
            if not isinstance(job_id, str):
                raise HTTPException(status_code=400, detail="agent submission rejected")
            try:
                canonical_asset_id = _opaque_asset_handle(asset_id)
                service = _resolve(generation_source, request)
                job = service.submit_agent_asset(
                    principal,
                    job_id,
                    canonical_asset_id,
                    _revision(body),
                )
                return _job_envelope(job)
            except HTTPException:
                raise
            except Exception as error:
                _reject(error)

    return router


create_router = create_assets_router
