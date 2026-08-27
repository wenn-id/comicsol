"""FastAPI composition root for comic-sol-web.

`create_app` builds a provider-free application with a deterministic,
bounded `/healthz` endpoint and a static mount foundation. It imports no
provider adapters, the deterministic engine, project lifecycle code,
credentials, or database code, and performs no network I/O, background
task, migration, or startup side effect.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Response

if TYPE_CHECKING:
    from comic_sol_web.config import WebConfig

# The static surface is a "foundation" mount that later work packages (WP13+)
# populate with Studio UI assets. The serving package directory is created
# once when the application is built; it is a Python packaging resource, not
# application or database state.
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(_config: "WebConfig") -> FastAPI:
    """Return a configured FastAPI application.

    `_config` is accepted for the composition-root contract; it is not used
    to create filesystem state. `/healthz` is deterministic, bounded, and
    provider-free.
    """
    app = FastAPI(title="Comic Sol Web", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> Response:
        return Response(content='{"status":"ok"}', media_type="application/json")

    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="static",
    )

    return app
