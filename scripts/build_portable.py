"""Build a native portable runtime from the validated wheel artifact."""

from __future__ import annotations

import argparse
import json
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


def run_output(command: list[str], cwd: Path) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def install_locked(python: Path, lock: Path, cwd: Path) -> None:
    run(
        [
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--require-hashes", "-r", str(lock),
        ],
        cwd,
    )


def write_environment_sbom(
    python: Path, destination: Path, temporary: Path, generator_python: Path
) -> None:
    run(
        [
            str(generator_python),
            "-m",
            "cyclonedx_py",
            "environment",
            str(python),
            "--sv",
            "1.6",
            "--output-reproducible",
            "--validate",
            "--of",
            "JSON",
            "-o",
            str(destination),
        ],
        temporary,
    )
    record = json.loads(destination.read_text(encoding="utf-8"))
    components = record.setdefault("components", [])
    python_version = run_output(
        [str(python), "-c", "import platform; print(platform.python_version())"],
        temporary,
    )
    python_ref = f"pkg:generic/python@{python_version}"
    if not any(str(item.get("name", "")).casefold() == "python" for item in components):
        components.append(
            {
                "bom-ref": python_ref,
                "name": "Python",
                "purl": python_ref,
                "type": "framework",
                "version": python_version,
            }
        )
    application = next(
        (item for item in components if str(item.get("name", "")).casefold() == "comic-sol"),
        None,
    )
    if application is None:
        raise RuntimeError("CycloneDX environment SBOM is missing comic-sol")
    dependencies = record.setdefault("dependencies", [])
    root = next(
        (item for item in dependencies if item.get("ref") == application.get("bom-ref")),
        None,
    )
    if root is None:
        root = {"dependsOn": [], "ref": application["bom-ref"]}
        dependencies.append(root)
    if python_ref not in root.setdefault("dependsOn", []):
        root["dependsOn"].append(python_ref)
    destination.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve(strict=True)
    output = arguments.output.resolve()
    lock = arguments.lock.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="comic-sol-freeze-") as raw:
        temporary = Path(raw)
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        install_locked(python, lock, temporary)
        run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], temporary)
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
        generator = temporary / "sbom-venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(generator)
        generator_python = generator / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        install_locked(generator_python, lock, temporary)
        write_environment_sbom(
            python,
            output / "build-environment.sbom.json",
            temporary,
            generator_python,
        )
    executable = target / ("comic-sol.exe" if platform.system() == "Windows" else "comic-sol")
    if not executable.is_file():
        raise RuntimeError("portable executable was not produced")
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
