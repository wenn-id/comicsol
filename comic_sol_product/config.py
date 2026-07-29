"""Portable Comic Sol user configuration defaults."""

from __future__ import annotations

import sys
from pathlib import Path


def default_output_root(
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform-native default project root."""
    platform = platform or sys.platform
    home = Path.home() if home is None else Path(home)
    if platform == "win32" or platform == "darwin":
        return home / "Documents" / "Comic Sol"
    return home / "Comic Sol"
