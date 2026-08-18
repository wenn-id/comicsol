"""Qualify a published native release archive on a clean platform runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"linux", "macos", "windows", "wsl"}
ARTIFACT_PLATFORMS = {"linux": "linux", "macos": "macos", "windows": "windows", "wsl": "linux"}
DEFAULT_REQUIRED_PLATFORMS = ("linux", "macos", "windows", "wsl")


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
    candidates: set[str] = set()
    for line in checksums.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2 and Path(parts[1]).name == archive.name:
            candidates.add(parts[0].strip().lower())
    if not candidates:
        raise RuntimeError(f"SHA256SUMS has no entry for {archive.name}")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual not in candidates:
        raise RuntimeError(f"SHA256 mismatch for {archive.name}")
    return actual


def verify_payload_checksums(manifest: Path, payloads: list[Path]) -> int:
    """Verify payloads against a global manifest that may repeat basenames."""
    records: dict[str, set[str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise RuntimeError("invalid SHA256SUMS entry")
        digest, name = parts[0].strip().lower(), Path(parts[1]).name
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeError("invalid SHA256SUMS entry")
        records.setdefault(name, set()).add(digest)
    required_names = {payload.name for payload in payloads}
    if not required_names <= records.keys():
        missing = ", ".join(sorted(required_names - records.keys()))
        raise RuntimeError(f"SHA256SUMS payload coverage mismatch: missing {missing}")
    for payload in payloads:
        actual = hashlib.sha256(payload.read_bytes()).hexdigest()
        if actual not in records[payload.name]:
            raise RuntimeError(f"SHA256 mismatch for {payload.name}")
    return len(payloads)


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


def aggregate_summaries(
    root: Path,
    output: Path,
    *,
    required_platforms: tuple[str, ...] = DEFAULT_REQUIRED_PLATFORMS,
    markdown: Path | None = None,
) -> dict[str, Any]:
    """Aggregate qualification summaries with a fail-closed release decision."""
    root = root.resolve()
    required = tuple(dict.fromkeys(required_platforms))
    records: dict[str, dict[str, Any]] = {}
    exceptions: list[str] = []
    for path in sorted(root.glob("summary-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            exceptions.append(f"{path.name}: {type(error).__name__}: {error}")
            continue
        platform = record.get("platform") if isinstance(record, dict) else None
        if not isinstance(platform, str) or not platform:
            exceptions.append(f"{path.name}: summary has no platform")
            continue
        if platform in records:
            exceptions.append(f"duplicate summary for platform: {platform}")
            continue
        records[platform] = record

    missing = [platform for platform in required if platform not in records]
    failed = [
        platform
        for platform in required
        if platform in records
        and (
            records[platform].get("status") != "passed" or bool(records[platform].get("exceptions"))
        )
    ]
    ready = not missing and not failed and not exceptions
    result: dict[str, Any] = {
        "decision": "RELEASE READY" if ready else "RELEASE BLOCKED",
        "status": "passed" if ready else "failed",
        "platform_count": len(records),
        "required_platforms": list(required),
        "missing_platforms": missing,
        "failed_platforms": failed,
        "exceptions": exceptions,
        "summaries": {
            platform: {
                "status": records[platform].get("status"),
                "checks": records[platform].get("checks", []),
                "exceptions": records[platform].get("exceptions", []),
            }
            for platform in sorted(records)
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = markdown or output.with_suffix(".md")
    lines = [f"# {result['decision']}", "", f"Status: `{result['status']}`", ""]
    for platform in sorted(set(required) | set(records)):
        record = records.get(platform)
        status = record.get("status", "missing") if record else "missing"
        lines.append(f"- `{platform}`: **{status}**")
    if missing:
        lines.extend(["", "Missing summaries:", *[f"- `{item}`" for item in missing]])
    if failed:
        lines.extend(["", "Failed summaries:", *[f"- `{item}`" for item in failed]])
    if exceptions:
        lines.extend(["", "Aggregator errors:", *[f"- {item}" for item in exceptions]])
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return result


def validate_published_metadata(
    metadata: Path, sbom: Path, *, artifact: str, platform: str, version: str
) -> None:
    """Validate published metadata and the full CycloneDX runtime contract."""
    metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
    expected_metadata = {
        "product": "comic-sol",
        "platform": platform,
        "signature_status": "unsigned",
    }
    for key, expected in expected_metadata.items():
        if metadata_record.get(key) != expected:
            raise RuntimeError(f"published metadata mismatch: {key}")
    artifacts = metadata_record.get("artifacts")
    if not isinstance(artifacts, list) or artifact not in artifacts:
        raise RuntimeError("published metadata does not list the qualified archive")

    sbom_record = json.loads(sbom.read_text(encoding="utf-8"))
    components = sbom_record.get("components")
    dependencies = sbom_record.get("dependencies")
    sbom_metadata = sbom_record.get("metadata")
    root_component = sbom_metadata.get("component") if isinstance(sbom_metadata, dict) else None
    serial = sbom_record.get("serialNumber")
    try:
        if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
            raise ValueError("missing UUID URN")
        uuid.UUID(serial.removeprefix("urn:uuid:"))
    except (AttributeError, ValueError):
        raise RuntimeError("published SBOM serialNumber is not a UUID URN") from None
    if (
        sbom_record.get("bomFormat") != "CycloneDX"
        or sbom_record.get("specVersion") != "1.6"
        or not isinstance(components, list)
        or not isinstance(dependencies, list)
        or not isinstance(root_component, dict)
        or root_component.get("name") != "comic-sol"
        or root_component.get("version") != version
        or not root_component.get("bom-ref")
    ):
        raise RuntimeError("published SBOM identity or collection types are invalid")
    properties = {
        item.get("name"): item.get("value")
        for item in sbom_metadata.get("properties", [])
        if isinstance(item, dict)
    }
    if (
        properties.get("comic-sol:release:artifact") != artifact
        or properties.get("comic-sol:release:platform") != platform
        or properties.get("comic-sol:release:architecture") != "x86_64"
    ):
        raise RuntimeError("published SBOM release properties are invalid")

    expected_components = {"pillow", "mcp", "pyinstaller", "python"}
    component_names = {
        item.get("name", "").casefold() for item in components if isinstance(item, dict)
    }
    if not expected_components <= component_names:
        missing = ", ".join(sorted(expected_components - component_names))
        raise RuntimeError(f"published SBOM missing runtime components: {missing}")
    if any(
        not isinstance(item, dict) or not item.get("purl") or not item.get("bom-ref")
        for item in components
    ):
        raise RuntimeError("published SBOM component identity is incomplete")

    references = {item["bom-ref"] for item in components}
    root_ref = root_component["bom-ref"]
    references.add(root_ref)
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not dependency.get("ref"):
            raise RuntimeError("published SBOM dependency graph has a missing reference")
        if dependency["ref"] not in references:
            raise RuntimeError("published SBOM dependency graph has an unknown reference")
        depends_on = dependency.get("dependsOn", [])
        if not isinstance(depends_on, list) or not all(item in references for item in depends_on):
            raise RuntimeError("published SBOM dependency graph has an unknown dependency")
    root_dependency = next((item for item in dependencies if item.get("ref") == root_ref), None)
    if not root_dependency or not root_dependency.get("dependsOn"):
        raise RuntimeError("published SBOM root dependency graph is incomplete")


def write_plan_fixture(project: Path) -> None:
    """Add the smallest canonical plan accepted by the installed validator."""
    plan = project / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    characters = {
        "schema_version": "1.0",
        "characters": [
            {
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
            }
        ],
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
        "scenes": [
            {
                "id": "delivery-hall",
                "purpose": "launch the delivery",
                "location": "dispatch hall",
                "time": "artificial dusk",
                "characters": ["mira"],
                "continuity_anchor": "brass walls and amber strips",
            },
            {
                "id": "generator-shaft",
                "purpose": "resolve delivery",
                "location": "generator shaft",
                "time": "artificial dusk",
                "characters": ["mira"],
                "continuity_anchor": "brass walls and amber strips",
            },
        ],
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
    for key, name in (
        ("character_bible", "character-bible.json"),
        ("story_plan", "story-plan.json"),
    ):
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
    version: str,
    metadata: Path | None = None,
    sbom: Path | None = None,
) -> dict[str, Any]:
    """Run version, doctor, lifecycle, installer, and preservation checks."""
    if platform_name not in PLATFORMS:
        raise ValueError(f"unsupported qualification platform: {platform_name}")
    archive = archive.resolve(strict=True)
    installer = installer.resolve(strict=True)
    checksums = checksums.resolve(strict=True)
    if metadata is not None or sbom is not None:
        if metadata is None or sbom is None:
            raise RuntimeError("published metadata and SBOM must be supplied together")
        metadata = metadata.resolve(strict=True)
        sbom = sbom.resolve(strict=True)
        verify_payload_checksums(checksums, [archive, installer, metadata, sbom])
        digest = checksum_for(checksums, archive)
    else:
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
    if metadata is not None or sbom is not None:
        if metadata is None or sbom is None:
            raise RuntimeError("published metadata and SBOM must be supplied together")
        validate_published_metadata(
            metadata.resolve(strict=True),
            sbom.resolve(strict=True),
            artifact=archive.name,
            platform=artifact_platform,
            version=version,
        )
        record["checks"].append("metadata-sbom")
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
            raise RuntimeError(
                f"installed fixture validation reported issues: {validation['data']!r}"
            )
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
    summary.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return record


def main(argv: list[str] | None = None) -> int:
    """Run release qualification or aggregate summaries and always write evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--sha256", type=Path)
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--sbom", type=Path)
    parser.add_argument("--aggregate-root", type=Path)
    parser.add_argument("--aggregate-output", type=Path)
    parser.add_argument("--require-source", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.aggregate_root is not None or arguments.aggregate_output is not None:
        if arguments.aggregate_root is None or arguments.aggregate_output is None:
            parser.error("--aggregate-root and --aggregate-output must be supplied together")
        required = DEFAULT_REQUIRED_PLATFORMS + (("source",) if arguments.require_source else ())
        result = aggregate_summaries(
            arguments.aggregate_root,
            arguments.aggregate_output,
            required_platforms=required,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    required = (
        arguments.archive,
        arguments.installer,
        arguments.sha256,
        arguments.platform,
        arguments.summary,
        arguments.version,
    )
    if any(value is None for value in required):
        parser.error(
            "artifact qualification requires --archive, --installer, --sha256, --platform, --summary, and --version"
        )
    try:
        record = qualify(
            platform_name=arguments.platform,
            archive=arguments.archive,
            installer=arguments.installer,
            checksums=arguments.sha256,
            summary=arguments.summary,
            version=arguments.version,
            metadata=arguments.metadata,
            sbom=arguments.sbom,
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
