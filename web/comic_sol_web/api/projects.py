"""FastAPI routes for opaque, owner-bound canonical project services."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile, status

from comic_sol_web.auth import AuthError, SessionPrincipal, require_principal

if TYPE_CHECKING:
    from comic_sol_web.projects import ProjectService

_UPLOAD_CHUNK_BYTES = 64 * 1024


def _envelope(snapshot: Any) -> dict[str, object]:
    return {
        "project_id": snapshot.project_id,
        "revision": snapshot.revision,
        "status": snapshot.status,
        "summary": dict(snapshot.summary),
    }


def _reject(error: Exception) -> NoReturn:
    # Import the engine-owning boundary only after a project endpoint is called.
    from comic_sol_web.engine_gateway import (
        GatewayError,
        ProjectUnavailableError,
        StaleProjectRevisionError,
    )

    if isinstance(error, ProjectUnavailableError):
        raise HTTPException(status_code=404, detail="project unavailable") from error
    if isinstance(error, StaleProjectRevisionError):
        raise HTTPException(status_code=409, detail="project revision is stale") from error
    if isinstance(error, (GatewayError, ValueError, OSError)):
        raise HTTPException(status_code=400, detail="project request rejected") from error
    raise error


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


def _resolve_service(source: Any, request: Request) -> ProjectService:
    return source(request) if callable(source) else source


async def _stage_archive(upload: UploadFile, destination: Path) -> None:
    from comic_sol_product.cli import _load_engine_module

    archive_module = _load_engine_module("handoff_archive")
    max_bytes = archive_module.MAX_TOTAL_COMPRESSED_BYTES
    total = 0
    with destination.open("xb") as output:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status_code=413, detail="project archive is too large")
            output.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="project archive is empty")


def create_projects_router(service_source: Any) -> APIRouter:
    """Create project routes whose service source may resolve lazily per request."""
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.post("", status_code=status.HTTP_201_CREATED)
    async def create_project(
        request: Request,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        try:
            service = _resolve_service(service_source, request)
            return _envelope(service.create_project(principal, body))
        except Exception as error:
            _reject(error)

    @router.post("/import", status_code=status.HTTP_201_CREATED)
    async def import_project(
        request: Request,
        archive: Annotated[UploadFile, File(...)],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        from comic_sol_product.cli import _load_engine_module

        archive_suffix = _load_engine_module("handoff_archive").ARCHIVE_SUFFIX
        filename = archive.filename or ""
        if not filename.endswith(archive_suffix):
            raise HTTPException(status_code=400, detail="unsupported project archive format")
        try:
            with tempfile.TemporaryDirectory(prefix="comic-sol-web-import-") as temporary:
                staged = Path(temporary) / f"upload{archive_suffix}"
                await _stage_archive(archive, staged)
                service = _resolve_service(service_source, request)
                return _envelope(service.import_project(principal, staged))
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)
        finally:
            await archive.close()

    @router.get("/{project_id}")
    async def get_project(
        request: Request,
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        try:
            service = _resolve_service(service_source, request)
            return _envelope(service.snapshot(principal, project_id))
        except Exception as error:
            _reject(error)

    return router


create_router = create_projects_router
