"""Installed stdio launcher for the deterministic Comic Sol MCP server."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .cli import _load_engine_module


def _load_server() -> Any:
    if importlib.util.find_spec("mcp") is None:
        raise RuntimeError("MCP support is not installed; run: pip install 'comic-sol[mcp]'")
    return _load_engine_module("mcp_server")


def run(root: Path) -> None:
    """Run the canonical FastMCP server over stdio within one absolute root."""
    server = _load_server()
    server._configure_root(Path(root))
    server.mcp.run(transport="stdio")
