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

from comic_sol_web.api.approvals import create_approvals_router
from comic_sol_web.api.assets import create_assets_router
from comic_sol_web.api.auth import create_local_session_router
from comic_sol_web.api.generation import create_generation_router
from comic_sol_web.api.projects import create_projects_router

if TYPE_CHECKING:
    from comic_sol_web.assets import AssetStore
    from comic_sol_web.config import WebConfig
    from comic_sol_web.projects import ProjectService

# The static surface is a "foundation" mount that later work packages (WP13+)
# populate with Studio UI assets. The serving package directory is created
# once when the application is built; it is a Python packaging resource, not
# application or database state.
STATIC_DIR = Path(__file__).resolve().parent / "static"
_AGENT_IMAGE_CAPABILITIES = frozenset(
    {
        "custom_dimensions",
        "negative_prompt",
        "text_to_image",
    }
)


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


def _project_service(request: Request) -> "ProjectService":
    """Construct and cache the real project boundary on first endpoint use."""
    existing = getattr(request.app.state, "projects", None)
    if existing is not None:
        return existing

    from comic_sol_web.engine_gateway import EngineGateway
    from comic_sol_web.projects import ProjectService

    config = request.app.state.web_config
    service = ProjectService(EngineGateway.open(Path(config.data_root)))
    request.app.state.projects = service
    return service


def _auth_service(request: Request) -> object:
    """Construct local session storage from the same database as projects."""
    existing = getattr(request.app.state, "auth", None)
    if existing is not None:
        return existing

    from comic_sol_web.auth import AuthService

    config = request.app.state.web_config
    if not config.local_mode:
        raise RuntimeError("local authentication is unavailable")
    service = AuthService(
        _project_service(request).gateway.database,
        session_secret=config.session_secret,
        github_oauth=None,
        secure_cookies=False,
    )
    request.app.state.auth = service
    return service


def _asset_store(request: Request) -> "AssetStore":
    """Construct and cache bounded page-owned asset storage on demand."""
    existing = getattr(request.app.state, "assets", None)
    if existing is not None:
        return existing

    from comic_sol_web.assets import AssetStore

    projects = _project_service(request)
    service = AssetStore(projects.gateway.database, request.app.state.web_config.data_root)
    request.app.state.assets = service
    return service


def _generation_service(request: Request) -> object:
    """Construct and cache provider-neutral queue storage on demand."""
    existing = getattr(request.app.state, "generation", None)
    if existing is not None:
        return existing

    # Keep provider, credential, queue, migration, and engine imports outside
    # application construction so /healthz remains a pure in-memory response.
    import os

    from comic_sol_web.generation.credentials import CredentialBroker
    from comic_sol_web.generation.providers.agent import AgentProvider
    from comic_sol_web.generation.providers.base import ProviderRegistry
    from comic_sol_web.generation.service import GenerationService

    projects = _project_service(request)
    gateway = projects.gateway
    config = request.app.state.web_config
    credentials = CredentialBroker(
        gateway.database,
        deployment_environment=os.environ,
        hosted_secret_references=config.hosted_secret_references,
        master_key_references=config.master_key_references,
        active_key_id=config.active_credential_key_id,
    )
    active_agent_capabilities = request.app.state.agent_image_capabilities
    service = GenerationService(
        gateway.database,
        projects,
        ProviderRegistry((AgentProvider(active_agent_capabilities),)),
        gateway.staging_root,
        credentials=credentials,
        assets=_asset_store(request),
    )
    request.app.state.generation_credentials = credentials
    request.app.state.generation = service
    return service


def _generation_credentials(request: Request) -> object:
    """Return the lazily constructed broker without exposing credential values."""
    _generation_service(request)
    return request.app.state.generation_credentials


def _approval_service(request: Request) -> object:
    """Construct and cache proposal storage only after an authenticated request."""
    existing = getattr(request.app.state, "approvals", None)
    if existing is not None:
        return existing

    from comic_sol_web.generation.approvals import ProviderSwitchApprovals

    projects = _project_service(request)
    service = ProviderSwitchApprovals(projects.gateway.database)
    request.app.state.approvals = service
    return service


def create_app(
    _config: "WebConfig",
    *,
    active_agent_image_capabilities: frozenset[str] = frozenset(),
) -> FastAPI:
    """Return a configured FastAPI application.

    Active-agent capabilities come only from this trusted construction call;
    request data can neither assert nor expand them. Startup creates no
    application filesystem state. The first authenticated project request
    lazily creates the data root, SQLite database, and project directories.
    `/healthz` remains deterministic, bounded, and provider-free.
    """
    if (
        not isinstance(active_agent_image_capabilities, frozenset)
        or not active_agent_image_capabilities <= _AGENT_IMAGE_CAPABILITIES
    ):
        raise ValueError("active agent image capabilities are invalid")
    app = FastAPI(title="Comic Sol Web", docs_url=None, redoc_url=None)
    app.state.web_config = _config
    app.state.agent_image_capabilities = active_agent_image_capabilities
    app.include_router(create_projects_router(_project_service))
    app.include_router(
        create_generation_router(
            _generation_service,
            _approval_service,
            _generation_credentials,
        )
    )
    app.include_router(create_approvals_router(_approval_service, _generation_service))
    app.include_router(create_assets_router(_asset_store, _generation_service))
    if _config.local_mode:
        app.include_router(create_local_session_router(_auth_service))

    @app.get("/healthz")
    def healthz() -> Response:
        return Response(content='{"status":"ok"}', media_type="application/json")

    app.mount(
        "/static",
        FutureStaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    return app
