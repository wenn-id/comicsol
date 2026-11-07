"""Build a native portable runtime from the validated wheel artifact."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path) -> None:
    """Run a command and fail when it returns a nonzero status."""
    subprocess.run(command, cwd=cwd, check=True)


def run_output(command: list[str], cwd: Path) -> str:
    """Run a command and return its captured standard output."""
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


PYTHON_LICENSE_RELATIVE = Path("_internal/licenses/python/LICENSE.txt")


def find_python_license(stdlib: Path, prefix: Path) -> Path:
    """Locate the license supplied with the build interpreter."""
    for candidate in (stdlib / "LICENSE.txt", prefix / "LICENSE.txt"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Python runtime LICENSE.txt is missing")


def copy_python_license(source: Path, runtime: Path) -> Path:
    """Copy the interpreter license into the frozen runtime's licenses tree."""
    source = source.resolve(strict=True)
    runtime_root = runtime.resolve(strict=True)
    parent = runtime_root / PYTHON_LICENSE_RELATIVE.parent
    resolved_parent = parent.resolve(strict=False)
    if resolved_parent != runtime_root and runtime_root not in resolved_parent.parents:
        raise ValueError("Python license destination escapes the portable runtime")
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if resolved_parent != runtime_root and runtime_root not in resolved_parent.parents:
        raise ValueError("Python license destination escapes the portable runtime")
    destination = resolved_parent / PYTHON_LICENSE_RELATIVE.name
    if destination.is_symlink() or destination.exists():
        raise FileExistsError(f"Python license destination already exists: {destination}")
    shutil.copy2(source, destination)
    return destination


def install_locked(python: Path, lock: Path, cwd: Path) -> None:
    """Install locked dependencies into the target environment."""
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "-r",
            str(lock),
        ],
        cwd,
    )


def write_environment_sbom(
    python: Path, destination: Path, temporary: Path, generator_python: Path
) -> None:
    """Write an inventory of installed release dependencies."""
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
    """Build the portable Comic Sol distribution."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve(strict=True)
    output = arguments.output.resolve()
    lock = arguments.lock.resolve(strict=True)
    python_license = find_python_license(Path(sysconfig.get_path("stdlib")), Path(sys.prefix))
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
        run(
            [
                str(python),
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(temporary / spec.name),
            ],
            temporary,
        )
        built = temporary / "dist/comic-sol"
        target = output / "comic-sol"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(built, target)
        copy_python_license(python_license, target)
        generator = temporary / "sbom-venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(generator)
        generator_python = generator / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
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
