"""Exercise an unpacked native runtime without relying on source checkout imports."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    executable = runtime / ("comic-sol.exe" if sys.platform == "win32" else "comic-sol")
    if not executable.is_file():
        raise RuntimeError("portable executable is missing")
    with tempfile.TemporaryDirectory(prefix="comic-sol-native-smoke-") as raw:
        temporary = Path(raw)
        run([str(executable), "--version"], temporary)
        run([str(executable), "doctor", "--output-root", str(temporary / "output")], temporary)
        helper = Path(__file__).resolve().parent / "installed_mcp_smoke.py"
        run(
            [sys.executable, str(helper), "--executable", str(executable), "--output-root", str(temporary / "mcp")],
            temporary,
        )
    print("portable-release-smoke-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
