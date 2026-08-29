"""FastAPI routes for bounded page-owned raster handles."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from comic_sol_web.api.generation import _job_envelope, _reject, _require_csrf, _revision
from comic_sol_web.auth import AuthError, SessionPrincipal, require_principal


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
            handle = store.get(principal, asset_id)
            content = store.read_bytes(principal, asset_id)
        except AssetError as error:
            raise HTTPException(status_code=404, detail="asset unavailable") from error
        return Response(
            content,
            media_type=handle.media_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    if generation_source is not None:

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
                service = _resolve(generation_source, request)
                job = service.submit_agent_asset(
                    principal,
                    job_id,
                    asset_id,
                    _revision(body),
                )
                return _job_envelope(job)
            except HTTPException:
                raise
            except Exception as error:
                _reject(error)

    return router


create_router = create_assets_router
