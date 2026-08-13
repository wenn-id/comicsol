"""Build-artifact smoke in a brand-new virtual environment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run a smoke-test command and require success."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def venv_paths(root: Path) -> tuple[Path, Path]:
    """Return the platform-specific virtual-environment executable paths."""
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "comic-sol.exe"
    return root / "bin" / "python", root / "bin" / "comic-sol"


def main() -> int:
    """Verify a clean installation in a temporary environment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--mcp", action="store_true")
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve()
    repository = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory(prefix="comic-sol-clean-") as temporary:
        root = Path(temporary)
        environment_root = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_root)
        python, executable = venv_paths(environment_root)
        requirement = f"{wheel}[mcp]" if arguments.mcp else str(wheel)
        run([str(python), "-m", "pip", "install", requirement], cwd=root)

        output_root = root / "projects"
        doctor = json.loads(
            run([str(executable), "--json", "doctor", "--output-root", str(output_root)], cwd=root)
        )
        if not doctor["ok"] or not doctor["data"]["healthy"]:
            raise RuntimeError("installed doctor is unhealthy")

        source = root / "story.md"
        request = root / "request.json"
        source.write_text("A courier carries the last light.", encoding="utf-8")
        request.write_text('{"language":"en","mode":"short_prompt"}\n', encoding="utf-8")
        initialized = json.loads(
            run(
                [
                    str(executable), "--json", "init", "--output-root", str(output_root),
                    "--title", "Clean Install", "--source", str(source), "--request-json", str(request),
                ],
                cwd=root,
            )
        )
        project = output_root / initialized["data"]["project_id"]
        status = json.loads(run([str(executable), "--json", "status", str(project)], cwd=root))
        if status["data"]["status"] != "INIT":
            raise RuntimeError("installed project status is not INIT")

        home = root / "home"
        codex = home / ".codex" / "config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text('model = "test"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HOME"] = str(home)
        if os.name == "nt":
            env["USERPROFILE"] = str(home)
        setup = json.loads(
            run(
                [str(executable), "--json", "setup", "--output-root", str(output_root), "--client", "codex"],
                cwd=root,
                env=env,
            )
        )
        if setup["data"][0]["status"] != "configured":
            raise RuntimeError("installed client setup did not configure Codex")
        uninstall = json.loads(
            run(
                [str(executable), "--json", "uninstall", "--output-root", str(output_root), "--client", "codex"],
                cwd=root,
                env=env,
            )
        )
        if uninstall["data"][0]["status"] != "removed" or not project.is_dir():
            raise RuntimeError("uninstall removed project data or failed to remove integration")

        if arguments.mcp:
            run(
                [
                    str(python), str(repository / "scripts" / "installed_mcp_smoke.py"),
                    "--executable", str(executable), "--output-root", str(output_root),
                ],
                cwd=root,
            )

        shutil.rmtree(project)
        print(f"clean-install-ok: mcp={arguments.mcp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
