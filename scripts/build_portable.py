"""Build a native portable runtime from the validated wheel artifact."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve(strict=True)
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="comic-sol-freeze-") as raw:
        temporary = Path(raw)
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "pyinstaller==6.15.0", f"{wheel}[mcp]"], temporary)
        spec = ROOT / "packaging/comic-sol.spec"
        entrypoint = ROOT / "packaging/entrypoint.py"
        shutil.copy2(spec, temporary / spec.name)
        shutil.copy2(entrypoint, temporary / entrypoint.name)
        run([str(python), "-m", "PyInstaller", "--clean", "--noconfirm", str(temporary / spec.name)], temporary)
        built = temporary / "dist/comic-sol"
        target = output / "comic-sol"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(built, target)
    executable = target / ("comic-sol.exe" if platform.system() == "Windows" else "comic-sol")
    if not executable.is_file():
        raise RuntimeError("portable executable was not produced")
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
