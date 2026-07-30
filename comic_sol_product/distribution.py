"""Deterministic, offline contracts for native release artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_VERSION = "2.0.0rc2"
_PLATFORMS = {"linux", "macos", "windows"}
_ARCHITECTURES = {"x86_64", "arm64"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _canonical_json(record: object) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _safe_member(name: str) -> str:
    if not _SAFE_NAME.fullmatch(name) or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("unsafe release artifact name")
    return name


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    version: str
    platform: str
    architecture: str

    def __post_init__(self) -> None:
        if self.version != _VERSION:
            raise ValueError(f"release version must be {_VERSION}")
        if self.platform not in _PLATFORMS:
            raise ValueError("unsupported release platform")
        if self.architecture not in _ARCHITECTURES:
            raise ValueError("unsupported release architecture")

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def stem(self) -> str:
        return f"comic-sol-{self.version}-{self.platform}-{self.architecture}"


def artifact_name(identity: ReleaseIdentity, extension: str) -> str:
    if extension not in {"zip", "tar.gz"}:
        raise ValueError("unsupported portable archive extension")
    return f"{identity.stem}.{extension}"


def _metadata_name(identity: ReleaseIdentity) -> str:
    return f"{identity.stem}.metadata.json"


def _sbom_name(identity: ReleaseIdentity) -> str:
    return f"{identity.stem}.sbom.json"


def write_release_metadata(
    release_dir: Path, identity: ReleaseIdentity, artifacts: Iterable[str]
) -> Path:
    release_dir.mkdir(parents=True, exist_ok=True)
    names = sorted({_safe_member(name) for name in artifacts})
    if not names:
        raise ValueError("release metadata requires at least one artifact")
    destination = release_dir / _metadata_name(identity)
    destination.write_text(
        _canonical_json(
            {
                "architecture": identity.architecture,
                "artifacts": names,
                "platform": identity.platform,
                "product": "comic-sol",
                "signature_status": "unsigned",
                "tag": identity.tag,
                "version": identity.version,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def write_sbom(release_dir: Path, identity: ReleaseIdentity) -> Path:
    release_dir.mkdir(parents=True, exist_ok=True)
    destination = release_dir / _sbom_name(identity)
    destination.write_text(
        _canonical_json(
            {
                "bomFormat": "CycloneDX",
                "components": [
                    {"name": "Pillow", "type": "library", "version": "12.3.0"},
                    {"name": "mcp", "type": "library", "version": "1.28.1"},
                ],
                "metadata": {
                    "component": {
                        "name": "comic-sol",
                        "type": "application",
                        "version": identity.version,
                    }
                },
                "serialNumber": f"urn:uuid:comic-sol-{identity.version}-{identity.platform}-{identity.architecture}",
                "specVersion": "1.6",
                "version": 1,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def write_checksums(release_dir: Path, artifacts: Iterable[Path]) -> Path:
    release_dir.mkdir(parents=True, exist_ok=True)
    records: list[str] = []
    for artifact in sorted((Path(item) for item in artifacts), key=lambda item: item.name):
        _safe_member(artifact.name)
        if artifact.parent.resolve() != release_dir.resolve() or not artifact.is_file():
            raise ValueError(f"missing artifact: {artifact.name}")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        records.append(f"{digest}  {artifact.name}")
    if not records:
        raise ValueError("checksum manifest requires at least one artifact")
    destination = release_dir / "SHA256SUMS"
    destination.write_text("\n".join(sorted(records)) + "\n", encoding="utf-8", newline="\n")
    return destination


def verify_release_directory(release_dir: Path, identity: ReleaseIdentity) -> None:
    release_dir = release_dir.resolve(strict=True)
    metadata_path = release_dir / _metadata_name(identity)
    sbom_path = release_dir / _sbom_name(identity)
    checksums_path = release_dir / "SHA256SUMS"
    for required in (metadata_path, sbom_path, checksums_path):
        if not required.is_file():
            raise ValueError(f"missing artifact: {required.name}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_metadata = {
        "architecture": identity.architecture,
        "platform": identity.platform,
        "product": "comic-sol",
        "signature_status": "unsigned",
        "tag": identity.tag,
        "version": identity.version,
    }
    for key, value in expected_metadata.items():
        if metadata.get(key) != value:
            raise ValueError(f"release metadata mismatch: {key}")
    artifact_names = metadata.get("artifacts")
    if not isinstance(artifact_names, list) or not artifact_names:
        raise ValueError("release metadata has no artifacts")

    checksum_records: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError("invalid checksum manifest")
        name = _safe_member(parts[1])
        if name in checksum_records:
            raise ValueError("duplicate checksum entry")
        checksum_records[name] = parts[0]

    required_names = {_safe_member(name) for name in artifact_names} | {sbom_path.name}
    for name in sorted(required_names):
        path = release_dir / name
        if not path.is_file():
            raise ValueError(f"missing artifact: {name}")
        expected = checksum_records.get(name)
        if expected is None:
            raise ValueError(f"missing checksum: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"checksum mismatch: {name}")

    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    component = sbom.get("metadata", {}).get("component", {})
    if sbom.get("bomFormat") != "CycloneDX" or component.get("version") != identity.version:
        raise ValueError("SBOM metadata mismatch")
