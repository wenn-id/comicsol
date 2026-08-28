"""Shared helpers for the isolated comic-sol-web test suite.

Discovery runs as `python -m unittest discover -s web/tests -p "test_app.py"`,
which puts `web/tests` on `sys.path`. The Web distribution is normally
installed (CI installs it with `--no-deps`), so `comic_sol_web` imports from
the installed distribution. The fallback below keeps the documented command
working in a plain checkout without an editable install.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WEB_ROOT.parent

if importlib.util.find_spec("comic_sol_web") is None:  # pragma: no cover - checkout fallback
    sys.path.insert(0, str(WEB_ROOT))

# Test-only values. They are syntactically valid secrets and carry no meaning
# outside this suite; the Web distribution never invents a development secret.
SESSION_SECRET = "test-session-secret-000000000000000000000000"
ENCRYPTION_SECRET = "test-encryption-secret-00000000000000000000"


def valid_environment(data_root: Path | None = None) -> dict[str, str]:
    """Return a complete, valid Web environment mapping.

    The returned data root is never created; configuration parsing must not
    create application or database state.
    """
    root = (
        data_root if data_root is not None else Path(tempfile.gettempdir()) / "comic-sol-web-tests"
    ).resolve()
    return {
        "COMIC_SOL_WEB_SESSION_SECRET": SESSION_SECRET,
        "COMIC_SOL_WEB_ENCRYPTION_SECRET": ENCRYPTION_SECRET,
        "COMIC_SOL_WEB_DATA_ROOT": str(root),
    }
