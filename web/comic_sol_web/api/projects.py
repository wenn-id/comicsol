"""FastAPI routes for opaque, owner-bound canonical project services."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

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


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


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
    if isinstance(error, (GatewayError, ValueError)):
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


def _write_headers(request: Request, *, creation: bool) -> tuple[str, int]:
    idempotency_key = request.headers.get("Idempotency-Key", "")
    expected_value = request.headers.get("X-Expected-Revision", "")
    try:
        parsed_key = UUID(idempotency_key)
        expected_revision = int(expected_value)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail="project write headers are invalid") from error
    valid_revision = expected_revision == 0 if creation else expected_revision >= 1
    if str(parsed_key) != idempotency_key.lower() or not valid_revision:
        raise HTTPException(status_code=400, detail="project write headers are invalid")
    return idempotency_key.lower(), expected_revision


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
        response: Response,
        body: Annotated[dict[str, object], Body()],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        _private_response(response)
        try:
            service = _resolve_service(service_source, request)
            if "project_id" in body or "plan" in body:
                if set(body) != {"project_id", "plan"}:
                    raise ValueError("Plan update envelope is invalid")
                project_id = body["project_id"]
                plan = body["plan"]
                if not isinstance(project_id, str) or not isinstance(plan, dict):
                    raise ValueError("Plan update envelope is invalid")
                _idempotency_key, expected_revision = _write_headers(
                    request,
                    creation=False,
                )
                snapshot = service.update_plan(
                    principal,
                    project_id,
                    expected_revision,
                    plan,
                )
                response.status_code = status.HTTP_200_OK
                return _envelope(snapshot)
            idempotency_key, _expected_revision = _write_headers(request, creation=True)
            snapshot = service.create_project(principal, body, idempotency_key)
            return _envelope(service.read_plan(principal, snapshot.project_id, snapshot.revision))
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)

    @router.post("/import", status_code=status.HTTP_201_CREATED)
    async def import_project(
        request: Request,
        response: Response,
        archive: Annotated[UploadFile, File(...)],
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _require_csrf(request, principal)
        _private_response(response)
        idempotency_key, _expected_revision = _write_headers(request, creation=True)
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
                snapshot = service.import_project(principal, staged, idempotency_key)
                return _envelope(
                    service.read_plan(principal, snapshot.project_id, snapshot.revision)
                )
        except HTTPException:
            raise
        except Exception as error:
            _reject(error)
        finally:
            await archive.close()

    @router.get("/current", response_model=None)
    async def get_current_project(
        request: Request,
        response: Response,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object] | Response:
        _private_response(response)
        try:
            service = _resolve_service(service_source, request)
            snapshot = service.current_project(principal)
            if snapshot is None:
                response.status_code = status.HTTP_204_NO_CONTENT
                return response
            return _envelope(snapshot)
        except Exception as error:
            _reject(error)

    @router.get("/{project_id}")
    async def get_project(
        request: Request,
        response: Response,
        project_id: str,
        principal: Annotated[SessionPrincipal, Depends(require_principal)],
    ) -> dict[str, object]:
        _private_response(response)
        try:
            service = _resolve_service(service_source, request)
            return _envelope(service.read_plan(principal, project_id))
        except Exception as error:
            _reject(error)

    return router


create_router = create_projects_router
