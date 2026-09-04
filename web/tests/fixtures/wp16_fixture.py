"""Shared WP16 test fixture: wired Web app with offline deterministic providers.

This module mirrors how `comic_sol_web.app.create_app` composes the four
owner-bound routers over lazily initialized real storage, but registers the
deterministic `FakeProvider` so generation completes offline. It is used by
`test_web_e2e.py` and `test_web_security.py` (WP16, issue #267).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401

from comic_sol_web.api.approvals import create_approvals_router
from comic_sol_web.api.assets import create_assets_router
from comic_sol_web.api.generation import create_generation_router
from comic_sol_web.api.projects import create_projects_router
from comic_sol_web.assets import AssetStore
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import PROJECT_MIGRATIONS, EngineGateway
from comic_sol_web.generation.approvals import ProviderSwitchApprovals
from comic_sol_web.generation.providers.agent import AgentProvider
from comic_sol_web.generation.providers.base import ProviderRegistry
from comic_sol_web.generation.providers.fake import FakeProvider
from comic_sol_web.generation.service import GenerationService
from comic_sol_web.generation.store import GenerationJob
from comic_sol_web.generation.types import AuthMode
from comic_sol_web.migrations import apply_migrations
from comic_sol_web.projects import ProjectService

from scripts import comic_sol
from scripts.handoff_archive import export_handoff_archive


class NullCredentialResolver:
    """Deterministic resolver that never discloses a credential value."""

    def resolve(self, user_id: str, provider: str, auth_mode: object):
        return self._resolved(user_id, provider, auth_mode)

    @asynccontextmanager
    async def _resolved(
        self,
        user_id: str,
        provider: str,
        auth_mode: object,
    ) -> AsyncIterator[None]:
        del user_id, provider, auth_mode
        yield None


class FakeAuth:
    """CSRF stub that can deny a request or report a changed identity."""

    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal
        self.csrf_checks = 0
        self.deny = False
        self.impersonate: SessionPrincipal | None = None

    def require_csrf(self, _request) -> SessionPrincipal:
        from comic_sol_web.auth import AuthError

        self.csrf_checks += 1
        if self.deny:
            raise AuthError("csrf validation failed")
        return self.impersonate or self.principal


def bounded_png(width: int = 8, height: int = 8) -> bytes:
    """Valid PNG bytes for raster tests, generated rather than hand-encoded."""
    import io

    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (width, height), "#334455").save(stream, format="PNG")
    return stream.getvalue()


def headers(revision: int = 0, *, key: str | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": key or str(uuid4()),
        "X-Expected-Revision": str(revision),
    }


class DeterministicFixtureGenerationService(GenerationService):
    """Return jobs in the same tie-break order used by the durable worker queue."""

    def queue(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        *,
        provider: str,
        model: str,
        auth_mode: AuthMode | str,
        max_retries: int = 2,
    ) -> tuple[GenerationJob, ...]:
        jobs = super().queue(
            principal,
            project_id,
            expected_revision,
            provider=provider,
            model=model,
            auth_mode=auth_mode,
            max_retries=max_retries,
        )
        return tuple(sorted(jobs, key=lambda job: job.job_id))


def pump(generation: GenerationService, count: int = 16) -> None:
    """Run the offline worker loop the deterministic number of times."""
    for _ in range(count):
        asyncio.run(generation.run_once("wp16-test-worker"))


class WiredAppFixture(unittest.TestCase):
    """Compose the real routers over real storage with a fake provider."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "web-data"
        self.database = Database(self.data_root / "application.sqlite3")
        apply_migrations(self.database, PROJECT_MIGRATIONS)
        self.gateway = EngineGateway(self.database, self.data_root)
        self.projects = ProjectService(self.gateway)
        self.assets = AssetStore(self.database, self.data_root)
        self.clock_value = 1_000
        self.generation = DeterministicFixtureGenerationService(
            self.database,
            self.projects,
            ProviderRegistry(
                (
                    FakeProvider(),
                    AgentProvider(frozenset({"text_to_image", "custom_dimensions"})),
                )
            ),
            self.gateway.staging_root,
            credentials=NullCredentialResolver(),
            assets=self.assets,
            clock=lambda: self.clock_value,
        )
        self.approvals = ProviderSwitchApprovals(
            self.database,
            clock=lambda: self.clock_value,
        )
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")

    def app_for(
        self,
        principal: SessionPrincipal,
    ) -> tuple[FastAPI, FakeAuth]:
        app = FastAPI()
        auth = FakeAuth(principal)
        app.state.auth = auth
        app.include_router(create_projects_router(self.projects))
        app.include_router(
            create_generation_router(
                self.generation,
                self.approvals,
                NullCredentialResolver(),
            )
        )
        app.include_router(create_approvals_router(self.approvals, self.generation))
        app.include_router(create_assets_router(self.assets, self.generation))
        app.dependency_overrides[require_principal] = lambda: principal
        return app, auth

    def client(self, principal: SessionPrincipal | None = None) -> tuple[TestClient, FakeAuth]:
        app, auth = self.app_for(principal or self.alice)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client, auth

    def portable_archive(self) -> Path:
        """Export a real golden handoff archive via the public engine surface."""
        project = self.planner_project()
        comic_sol.prepare_handoff(project)
        archive_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, archive_root, True)
        archive = archive_root / "golden.comic-sol-handoff"
        export_handoff_archive(project, archive)
        return archive

    def planner_project(self) -> Path:
        """Build a canonical planner project without depending on a private API."""
        from tests.test_handoff_lifecycle import HandoffLifecycleGoldenTests

        _root, project = HandoffLifecycleGoldenTests._planner_project(self)
        return project
