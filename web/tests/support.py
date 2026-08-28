"""Shared helpers for the isolated comic-sol-web test suite.

Discovery runs as `python -m unittest discover -s web/tests -p "test_app.py"`,
which puts `web/tests` on `sys.path`. The Web distribution is normally
installed (CI installs it with `--no-deps`), so `comic_sol_web` imports from
the installed distribution. The fallback below keeps the documented command
working in a plain checkout without an editable install.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = WEB_ROOT.parent
REQUIRE_SYMLINK_TESTS = os.environ.get("COMIC_SOL_REQUIRE_SYMLINK_TESTS") == "1"

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


def make_symlink(test_case, link: Path, target: Path, *, directory: bool = False) -> None:
    """Create a link at ``link`` pointing to ``target``, or skip the test.

    This mirrors `tests/support.py` in the repository root. Creating a symlink
    on Windows needs Developer Mode or elevation, so a path-containment test
    would otherwise vanish silently on a normal Windows machine. Directory links
    fall back to a junction, which needs no privilege. Set
    COMIC_SOL_REQUIRE_SYMLINK_TESTS=1 (CI does) to turn a skip into a failure so
    the lost coverage cannot go unnoticed.
    """
    try:
        link.symlink_to(target, target_is_directory=directory)
        if link.is_symlink():
            return
        link.unlink(missing_ok=True)
        first_error: OSError = OSError("created path is not recognized as a symlink")
    except OSError as error:
        first_error = error

    if directory and os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        first_error = OSError(
            f"symlink failed ({first_error}); junction failed "
            f"({completed.stderr.strip() or completed.stdout.strip()})"
        )

    message = f"symlink unavailable: {first_error}"
    if REQUIRE_SYMLINK_TESTS:
        test_case.fail(
            f"{message}. COMIC_SOL_REQUIRE_SYMLINK_TESTS=1 forbids skipping this "
            f"path-containment test; enable Developer Mode or run elevated."
        )
    test_case.skipTest(message)
