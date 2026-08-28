"""FastAPI routes for bounded page-owned raster handles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from comic_sol_web.assets import AssetError, AssetStore
from comic_sol_web.auth import AuthError, SessionPrincipal, require_principal


def create_assets_router(store: AssetStore) -> APIRouter:
    router = APIRouter(prefix="/api/assets", tags=["assets"])

    @router.post("")
    async def upload(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, int | str]:
        auth = getattr(request.app.state, "auth", None)
        try:
            if auth is None:
                raise AuthError("authentication unavailable")
            auth.require_csrf(request)
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
        asset_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> Response:
        try:
            handle = store.get(principal, asset_id)
            content = store.read_bytes(principal, asset_id)
        except AssetError as error:
            raise HTTPException(status_code=404, detail="asset unavailable") from error
        return Response(
            content,
            media_type=handle.media_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    return router


create_router = create_assets_router
