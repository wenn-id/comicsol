"""FastAPI composition root for comic-sol-web.

`create_app` builds a provider-free application with a deterministic,
bounded `/healthz` endpoint, lazily resolved project routes, and a static mount
foundation. It imports no provider adapters or deterministic engine modules and
performs no network I/O, background task, migration, or filesystem side effect.
Project storage and engine modules are initialized only after an authenticated
project endpoint is invoked.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from comic_sol_web.api.projects import create_projects_router

if TYPE_CHECKING:
    from comic_sol_web.config import WebConfig

# The static surface is a "foundation" mount that later work packages (WP13+)
# populate with Studio UI assets. The serving package directory is created
# once when the application is built; it is a Python packaging resource, not
# application or database state.
STATIC_DIR = Path(__file__).resolve().parent / "static"


class FutureStaticFiles(StaticFiles):
    """Serve the future static surface without requiring WP13 assets.

    Starlette's `StaticFiles.__call__` triggers `check_config` on the first
    request, raising `RuntimeError` when the directory does not exist. This
    subclass short-circuits to a deterministic 404 before that check runs
    so the future surface can be mounted in WP1 without shipping assets.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not Path(str(self.directory)).is_dir():
            response = PlainTextResponse("Not Found", status_code=404)
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _project_service(request: Request):
    """Construct and cache the real project boundary on first endpoint use."""
    existing = getattr(request.app.state, "projects", None)
    if existing is not None:
        return existing

    from comic_sol_web.assets import _canonical_data_root, _make_plain_directory
    from comic_sol_web.database import Database
    from comic_sol_web.engine_gateway import EngineGateway, PROJECT_MIGRATIONS
    from comic_sol_web.migrations import apply_migrations
    from comic_sol_web.projects import ProjectService

    config = request.app.state.web_config
    configured_root = Path(config.data_root)
    if not configured_root.is_absolute():
        raise ValueError("project data root must be absolute")
    data_root = _canonical_data_root(configured_root)
    _make_plain_directory(data_root)
    database = Database(data_root / "application.sqlite3")
    apply_migrations(database, PROJECT_MIGRATIONS)
    service = ProjectService(EngineGateway(database, data_root))
    request.app.state.projects = service
    return service


def create_app(_config: "WebConfig") -> FastAPI:
    """Return a configured FastAPI application.

    `_config` is accepted for the composition-root contract; it is not used
    to create filesystem state. `/healthz` is deterministic, bounded, and
    provider-free.
    """
    app = FastAPI(title="Comic Sol Web", docs_url=None, redoc_url=None)
    app.state.web_config = _config
    app.include_router(create_projects_router(_project_service))

    @app.get("/healthz")
    def healthz() -> Response:
        return Response(content='{"status":"ok"}', media_type="application/json")

    app.mount(
        "/static",
        FutureStaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    return app
