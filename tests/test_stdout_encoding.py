"""Regression tests for JSON output written to a piped legacy-code-page stdout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JAPANESE_TITLE = "日本の配達人"
CONSOLE_BOOTSTRAP = (
    "import sys; from comic_sol_product.cli import main; raise SystemExit(main(sys.argv[1:]))"
)


def _piped_cp1252_environment() -> dict[str, str]:
    """Mimic an agent host on Windows: stdout is a pipe decoded as cp1252."""
    environment = dict(os.environ)
    environment.pop("PYTHONUTF8", None)
    environment["PYTHONIOENCODING"] = "cp1252"
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), existing]))
    return environment


class PipedLegacyCodePageJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output_root = Path(temporary.name) / "out"

    def _run(
        self, arguments: list[str], *, script: str | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        command = (
            [sys.executable, "-c", CONSOLE_BOOTSTRAP, *arguments]
            if script is None
            else [sys.executable, str(REPO_ROOT / script), *arguments]
        )
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=_piped_cp1252_environment(),
            capture_output=True,
            timeout=180,
            check=False,
        )

    def _create_japanese_project(self) -> Path:
        completed = self._run(
            [
                "--json",
                "init",
                "--title",
                JAPANESE_TITLE,
                "--output-root",
                str(self.output_root),
                "--starter",
                "minimal-one-page",
            ]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(payload["ok"])
        return self.output_root / payload["data"]["project_dir"]

    def test_console_json_status_with_japanese_title_survives_piped_cp1252(self) -> None:
        project_dir = self._create_japanese_project()

        completed = self._run(["--json", "status", str(project_dir)])

        self.assertEqual(0, completed.returncode, completed.stdout)
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["error"])

    def test_scripts_entry_json_status_with_japanese_title_survives_piped_cp1252(self) -> None:
        project_dir = self._create_japanese_project()

        completed = self._run(
            ["status", str(project_dir), "--json"],
            script="scripts/comic_sol.py",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn(b"UnicodeEncodeError", completed.stderr)
        self.assertNotIn(b"UnicodeEncodeError", completed.stdout)
        json.loads(completed.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
