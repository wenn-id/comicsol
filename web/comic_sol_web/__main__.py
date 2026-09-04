"""Loopback launcher for the single-user Comic Sol Studio."""

from __future__ import annotations

import os

import uvicorn

from comic_sol_web.app import create_app
from comic_sol_web.config import WebConfig


def main() -> int:
    config = WebConfig.local_from_env(os.environ)
    uvicorn.run(create_app(config), host=config.host, port=8765, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
