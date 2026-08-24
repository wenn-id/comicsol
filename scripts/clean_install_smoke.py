"""Build-artifact smoke in a brand-new virtual environment."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
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


def read_codex_entry(config: Path, output_root: Path, executable: Path) -> dict[str, object]:
    """Read and validate the exact MCP entry produced for Codex."""
    record = tomllib.loads(config.read_text(encoding="utf-8"))
    try:
        entry = record["mcp_servers"]["comic-sol"]
        command = entry["command"]
        arguments = entry["args"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("installed setup did not persist the Codex MCP entry") from error
    if not isinstance(command, str) or not Path(command).is_absolute():
        raise RuntimeError("installed setup persisted a non-absolute MCP command")
    if Path(command).resolve(strict=True) != executable.resolve(strict=True):
        raise RuntimeError("installed setup persisted an unexpected MCP command")
    expected = ["mcp", "--root", str(output_root.resolve())]
    if arguments != expected:
        raise RuntimeError("installed setup persisted unexpected MCP arguments")
    return {"command": command, "args": arguments}


def minimal_environment(environment: dict[str, str]) -> dict[str, str]:
    """Keep platform essentials while excluding installation search paths."""
    minimal = {
        key: environment[key]
        for key in (
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "TMP",
            "TEMP",
            "TMPDIR",
            "LANG",
        )
        if key in environment
    }
    if os.name == "nt":
        system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
        if system_root is None:
            raise RuntimeError("Windows system root is unavailable")
        minimal["PATH"] = str(Path(system_root) / "System32")
    else:
        minimal["PATH"] = "/usr/bin:/bin"
    return minimal


def prepare_client_configs(home: Path) -> tuple[list[str], Path]:
    """Create the existing native Claude fixture needed by the macOS smoke."""
    clients = ["codex"]
    claude = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        claude.parent.mkdir(parents=True)
        claude.write_text(
            '{"mcpServers":{"other":{"command":"other"}}}\n',
            encoding="utf-8",
        )
        clients.append("claude-desktop")
    return clients, claude


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
        environment_root = root / "venv with spaces"
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
                    str(executable),
                    "--json",
                    "init",
                    "--output-root",
                    str(output_root),
                    "--title",
                    "Clean Install",
                    "--source",
                    str(source),
                    "--request-json",
                    str(request),
                    "--page-count",
                    "3",
                ],
                cwd=root,
            )
        )
        project = output_root / initialized["data"]["project_id"]
        status = json.loads(run([str(executable), "--json", "status", str(project)], cwd=root))
        if status["data"]["status"] != "INIT":
            raise RuntimeError("installed project status is not INIT")
        if status["data"]["settings"]["page_count"] != 3:
            raise RuntimeError("installed project did not preserve its page scope")

        home = root / "home"
        codex = home / ".codex" / "config.toml"
        codex.parent.mkdir(parents=True)
        codex.write_text('model = "test"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HOME"] = str(home)
        if os.name == "nt":
            env["USERPROFILE"] = str(home)
        clients, claude = prepare_client_configs(home)
        setup_command = [
            str(executable),
            "--json",
            "setup",
            "--output-root",
            str(output_root),
        ]
        for client in clients:
            setup_command.extend(["--client", client])
        setup = json.loads(run(setup_command, cwd=root, env=env))
        setup_results = {result["client"]: result for result in setup["data"]}
        if any(setup_results[client]["status"] != "configured" for client in clients):
            raise RuntimeError("installed client setup did not configure selected clients")
        entry = read_codex_entry(codex, output_root, executable)
        if sys.platform == "darwin":
            claude_record = json.loads(claude.read_text(encoding="utf-8"))
            if claude_record["mcpServers"].get("other") != {"command": "other"}:
                raise RuntimeError("Claude setup did not preserve the existing entry")
            if claude_record["mcpServers"].get("comic-sol") != entry:
                raise RuntimeError("Claude and Codex MCP entries differ")
        if arguments.mcp:
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    f"command = {json.dumps(entry['command'])}",
                    'command = "stale-comic-sol"',
                ),
                encoding="utf-8",
            )
            if sys.platform == "darwin":
                claude_record["mcpServers"]["comic-sol"]["command"] = "stale-comic-sol"
                claude.write_text(json.dumps(claude_record), encoding="utf-8")
            repair_command = [
                str(executable),
                "--json",
                "repair",
                "--output-root",
                str(output_root),
            ]
            for client in clients:
                repair_command.extend(["--client", client])
            preview = json.loads(run([*repair_command, "--dry-run"], cwd=root, env=env))
            preview_results = {result["client"]: result for result in preview["data"]}
            if any(preview_results[client]["status"] != "planned" for client in clients):
                raise RuntimeError("installed repair preview did not plan selected clients")
            first_repair = json.loads(run(repair_command, cwd=root, env=env))
            first_results = {result["client"]: result for result in first_repair["data"]}
            if any(
                (first_results[client]["state"], first_results[client]["status"])
                != ("success", "configured")
                for client in clients
            ):
                raise RuntimeError("installed repair did not repair selected clients")
            second_repair = json.loads(run(repair_command, cwd=root, env=env))
            second_results = {result["client"]: result for result in second_repair["data"]}
            if any(
                (second_results[client]["state"], second_results[client]["status"])
                != ("no-op", "unchanged")
                for client in clients
            ):
                raise RuntimeError("installed repair is not idempotent")
            entry = read_codex_entry(codex, output_root, executable)
        uninstall_command = [
            str(executable),
            "--json",
            "uninstall",
            "--output-root",
            str(output_root),
        ]
        for client in clients:
            uninstall_command.extend(["--client", client])
        uninstall = json.loads(run(uninstall_command, cwd=root, env=env))
        uninstall_results = {result["client"]: result for result in uninstall["data"]}
        if (
            any(uninstall_results[client]["status"] != "removed" for client in clients)
            or not project.is_dir()
        ):
            raise RuntimeError("uninstall removed project data or failed to remove integration")

        if arguments.mcp:
            run(
                [
                    str(python),
                    str(repository / "scripts" / "installed_mcp_smoke.py"),
                    "--command",
                    entry["command"],
                    "--args-json",
                    json.dumps(entry["args"]),
                ],
                cwd=root,
                env=minimal_environment(env),
            )

        shutil.rmtree(project)
        print(f"clean-install-ok: mcp={arguments.mcp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
