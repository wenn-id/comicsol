"""Shared helpers for tests that need real filesystem links."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REQUIRE_SYMLINK_TESTS = os.environ.get("COMIC_SOL_REQUIRE_SYMLINK_TESTS") == "1"


def make_symlink(test_case, link: Path, target: Path, *, directory: bool = False) -> None:
    """Create a link at ``link`` pointing to ``target``, or skip the test.

    Creating a symlink on Windows needs Developer Mode or elevation, so these
    path-containment tests would otherwise vanish silently on a normal Windows
    machine. Directory links fall back to a junction, which needs no privilege.
    Set COMIC_SOL_REQUIRE_SYMLINK_TESTS=1 (CI does) to turn a skip into a
    failure so the lost coverage cannot go unnoticed.
    """
    try:
        link.symlink_to(target, target_is_directory=directory)
        return
    except OSError as error:
        first_error = error

    if directory and os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
            capture_output=True,
            text=True,
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
