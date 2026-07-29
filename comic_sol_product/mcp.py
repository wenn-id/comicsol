"""Installed stdio launcher for the deterministic Comic Sol MCP server."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _engine_directory() -> Path:
    package_root = Path(__file__).resolve().parent
    for candidate in (package_root / "engine", package_root.parent / "scripts"):
        if (candidate / "mcp_server.py").is_file():
            return candidate
    raise RuntimeError("Comic Sol engine files are missing; reinstall the package")


def _load_server() -> Any:
    if importlib.util.find_spec("mcp") is None:
        raise RuntimeError("MCP support is not installed; run: pip install 'comic-sol[mcp]'")
    engine_dir = _engine_directory()
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    import mcp_server  # type: ignore[import-not-found]

    return mcp_server


def run(root: Path) -> None:
    """Run the canonical FastMCP server over stdio within one absolute root."""
    server = _load_server()
    server._configure_root(Path(root))
    server.mcp.run(transport="stdio")
