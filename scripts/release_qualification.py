"""Qualify a published native release archive on a clean platform runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"linux", "macos", "windows", "wsl"}
ARTIFACT_PLATFORMS = {"linux": "linux", "macos": "macos", "windows": "windows", "wsl": "linux"}


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run a command and include complete diagnostics on failure."""
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


def executable_path(install_root: Path) -> Path:
    """Return the installed native executable for the current runner."""
    name = "comic-sol.exe" if os.name == "nt" else "comic-sol"
    return install_root / "bin" / name


def checksum_for(checksums: Path, archive: Path) -> str:
    """Read and independently verify one archive's SHA-256 manifest entry."""
    expected: str | None = None
    for line in checksums.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2 and Path(parts[1]).name == archive.name:
            expected = parts[0].strip().lower()
            break
    if expected is None:
        raise RuntimeError(f"SHA256SUMS has no entry for {archive.name}")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch for {archive.name}")
    return expected


def json_command(
    executable: Path,
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the installed CLI's machine-readable command envelope."""
    output = run([str(executable), "--json", *arguments], cwd=cwd, env=env)
    record = json.loads(output)
    if not isinstance(record, dict) or record.get("ok") is not True:
        raise RuntimeError(f"installed CLI returned an invalid result: {record!r}")
    return record


def install(
    *,
    platform_name: str,
    installer: Path,
    archive: Path,
    digest: str,
    install_root: Path,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Install using the platform's release installer, never a source checkout."""
    if platform_name in {"linux", "macos", "wsl"}:
        run(
            [
                "sh",
                str(installer),
                "--archive",
                str(archive),
                "--sha256",
                digest,
                "--install-root",
                str(install_root),
            ],
            cwd=cwd,
            env=env,
        )
        return
    if platform_name == "windows":
        run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(installer),
                "-Archive",
                str(archive),
                "-SHA256",
                digest,
                "-InstallRoot",
                str(install_root),
            ],
            cwd=cwd,
            env=env,
        )
        return
    raise ValueError(f"unsupported qualification platform: {platform_name}")


def uninstall(
    *,
    platform_name: str,
    installer: Path,
    install_root: Path,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Remove only installer-owned runtime files."""
    if platform_name in {"linux", "macos", "wsl"}:
        run(
            ["sh", str(installer), "--uninstall", "--install-root", str(install_root)],
            cwd=cwd,
            env=env,
        )
    elif platform_name == "windows":
        run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(installer),
                "-Uninstall",
                "-InstallRoot",
                str(install_root),
            ],
            cwd=cwd,
            env=env,
        )
    else:
        raise ValueError(f"unsupported qualification platform: {platform_name}")


def write_plan_fixture(project: Path) -> None:
    """Add the smallest canonical plan accepted by the installed validator."""
    plan = project / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    characters = {
        "schema_version": "1.0",
        "characters": [{
            "id": "mira",
            "name": "Mira",
            "role": "courier",
            "age_band": "young-adult",
            "pronouns": "she/her",
            "visual_fingerprint": {
                "silhouette": "short compact build",
                "face": "round face",
                "hair": "chin-length black bob",
                "wardrobe": "cream jacket",
                "palette": ["charcoal", "cream", "amber"],
                "signature_props": ["courier bag"],
                "invariants": ["amber scarf", "circular bag clasp"],
                "avoid": ["logos", "generated text"],
            },
            "personality": ["resourceful"],
            "motivation": "finish delivery",
            "speech": "short practical sentences",
            "reference_path": "references/characters/mira.png",
        }],
    }
    story = {
        "schema_version": "1.0",
        "title": "Release Qualification Fixture",
        "logline": "A courier delivers the last vial of sunlight.",
        "theme": "Hope is shared.",
        "tone": ["urgent", "tender"],
        "rating": "teen",
        "setting": "An underground city.",
        "beginning": "Mira receives the vial.",
        "turn": "A bridge collapses.",
        "climax": "Mira crosses the shaft.",
        "ending": "The city relights.",
        "scenes": [{
            "id": "delivery-hall",
            "purpose": "launch the delivery",
            "location": "dispatch hall",
            "time": "artificial dusk",
            "characters": ["mira"],
            "continuity_anchor": "brass walls and amber strips",
        }, {
            "id": "generator-shaft",
            "purpose": "resolve delivery",
            "location": "generator shaft",
            "time": "artificial dusk",
            "characters": ["mira"],
            "continuity_anchor": "brass walls and amber strips",
        }],
    }
    for name, value in (("character-bible.json", characters), ("story-plan.json", story)):
        (plan / name).write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    manifest_path = project / "project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    for key, name in (("character_bible", "character-bible.json"), ("story_plan", "story-plan.json")):
        artifact = plan / name
        artifacts[key] = {
            "path": f"plan/{name}",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    manifest["artifacts"] = artifacts
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def qualify(
    *,
    platform_name: str,
    archive: Path,
    installer: Path,
    checksums: Path,
    summary: Path,
) -> dict[str, Any]:
    """Run version, doctor, lifecycle, installer, and preservation checks."""
    if platform_name not in PLATFORMS:
        raise ValueError(f"unsupported qualification platform: {platform_name}")
    archive = archive.resolve(strict=True)
    installer = installer.resolve(strict=True)
    checksums = checksums.resolve(strict=True)
    digest = checksum_for(checksums, archive)
    artifact_platform = ARTIFACT_PLATFORMS[platform_name]
    record: dict[str, Any] = {
        "artifact": archive.name,
        "artifact_platform": artifact_platform,
        "platform": platform_name,
        "status": "failed",
        "checks": [],
        "exceptions": [],
    }
    with tempfile.TemporaryDirectory(prefix=f"comic-sol-qualification-{platform_name}-") as raw:
        root = Path(raw)
        install_root = root / "install root with spaces"
        output_root = root / "projects"
        home = root / "home"
        home.mkdir()
        config = home / "client-config.json"
        config.write_text('{"client":"preserved"}\n', encoding="utf-8")
        source = root / "fixture.md"
        request = root / "request.json"
        source.write_text("A courier carries the last light.", encoding="utf-8")
        request.write_text('{"language":"en","mode":"short_prompt"}\n', encoding="utf-8")
        env = dict(os.environ)
        env["HOME"] = str(home)
        if os.name == "nt":
            env["USERPROFILE"] = str(home)

        install(
            platform_name=platform_name,
            installer=installer,
            archive=archive,
            digest=digest,
            install_root=install_root,
            cwd=root,
            env=env,
        )
        executable = executable_path(install_root)
        if not executable.is_file():
            raise RuntimeError(f"installed executable is missing: {executable}")
        version = run([str(executable), "--version"], cwd=root, env=env)
        if not version.startswith("comic-sol "):
            raise RuntimeError(f"unexpected installed version output: {version!r}")
        record["version"] = version
        record["checks"].append("version")

        doctor = json_command(
            executable,
            ["doctor", "--output-root", str(output_root)],
            cwd=root,
            env=env,
        )
        if not doctor["data"].get("healthy"):
            raise RuntimeError("installed doctor reported an unhealthy runtime")
        record["checks"].append("doctor")

        initialized = json_command(
            executable,
            [
                "init",
                "--output-root",
                str(output_root),
                "--title",
                "Release Qualification Fixture",
                "--source",
                str(source),
                "--request-json",
                str(request),
            ],
            cwd=root,
            env=env,
        )
        project = output_root / str(initialized["data"]["project_id"])
        write_plan_fixture(project)
        status = json_command(executable, ["status", str(project)], cwd=root, env=env)
        if status["data"].get("status") != "INIT":
            raise RuntimeError("installed fixture did not remain in INIT state")
        validation = json_command(
            executable, ["validate", str(project), "--stage", "plan"], cwd=root, env=env
        )
        if validation["data"] != []:
            raise RuntimeError(f"installed fixture validation reported issues: {validation['data']!r}")
        record["checks"].append("lifecycle")

        user_file = install_root / "user-owned.txt"
        user_file.write_text("do not delete\n", encoding="utf-8")
        uninstall(
            platform_name=platform_name,
            installer=installer,
            install_root=install_root,
            cwd=root,
            env=env,
        )
        if not project.is_dir():
            raise RuntimeError("uninstall removed the user project")
        if config.read_text(encoding="utf-8") != '{"client":"preserved"}\n':
            raise RuntimeError("uninstall changed unrelated client configuration")
        if not user_file.is_file():
            raise RuntimeError("uninstall removed an unrelated user file")
        for managed in ("bin", "versions", ".comic-sol-install", "active-version"):
            if (install_root / managed).exists():
                raise RuntimeError(f"uninstall left managed path behind: {managed}")
        record["checks"].append("uninstall-preservation")

    record["status"] = "passed"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    """Run release qualification and always leave a machine-readable summary."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--sha256", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--summary", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        record = qualify(
            platform_name=arguments.platform,
            archive=arguments.archive,
            installer=arguments.installer,
            checksums=arguments.sha256,
            summary=arguments.summary,
        )
    except Exception as error:
        record = {
            "platform": arguments.platform,
            "status": "failed",
            "checks": [],
            "exceptions": [f"{type(error).__name__}: {error}"],
        }
        arguments.summary.parent.mkdir(parents=True, exist_ok=True)
        arguments.summary.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(record["exceptions"][0], file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
