"""Deterministic, offline contracts for native release artifacts."""

from __future__ import annotations

import hashlib
import json
import uuid
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import __version__

_VERSION = __version__
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


def metadata_name(identity: ReleaseIdentity) -> str:
    """Return the canonical metadata filename for a native release identity."""
    return f"{identity.stem}.metadata.json"


def sbom_name(identity: ReleaseIdentity) -> str:
    """Return the canonical SBOM filename for a native release identity."""
    return f"{identity.stem}.sbom.json"


def native_payload_names(identity: ReleaseIdentity) -> tuple[str, str, str]:
    """Return the complete native archive, metadata, and SBOM filename contract."""
    return artifact_name(identity, "zip"), metadata_name(identity), sbom_name(identity)


def write_release_metadata(
    release_dir: Path, identity: ReleaseIdentity, artifacts: Iterable[str]
) -> Path:
    release_dir.mkdir(parents=True, exist_ok=True)
    names = sorted({_safe_member(name) for name in artifacts})
    if not names:
        raise ValueError("release metadata requires at least one artifact")
    destination = release_dir / metadata_name(identity)
    destination.write_text(
        _canonical_json(
            {
                "architecture": identity.architecture,
                "artifacts": names,
                "platform": identity.platform,
                "product": "comic-sol",
                "signature_file": "SHA256SUMS.sigstore.json",
                "signature_status": "sigstore",
                "tag": identity.tag,
                "version": identity.version,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def _component_name(component: object) -> str:
    return str(component.get("name", "")).casefold() if isinstance(component, dict) else ""


def _set_sbom_property(properties: list[dict[str, str]], name: str, value: str) -> None:
    for item in properties:
        if item.get("name") == name:
            item["value"] = value
            return
    properties.append({"name": name, "value": value})


def write_sbom(
    release_dir: Path,
    identity: ReleaseIdentity,
    environment_sbom: Path,
    artifact: str,
    *,
    destination_name: str | None = None,
) -> Path:
    """Finalize the CycloneDX BOM produced from the frozen build environment.

    ``destination_name`` overrides the native-bundle SBOM filename for payload
    BOMs that travel under a different release-asset name (for example the
    container image SBOM); the serial number is then derived from that name so
    the two BOMs never share an identity.
    """

    source = environment_sbom.resolve(strict=True)
    record = json.loads(source.read_text(encoding="utf-8"))
    components = record.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("build environment SBOM has no components")

    application = next(
        (component for component in components if _component_name(component) == "comic-sol"),
        None,
    )
    if not isinstance(application, dict):
        raise ValueError("build environment SBOM is missing comic-sol")
    application["version"] = identity.version
    application["type"] = "application"
    application["purl"] = f"pkg:pypi/comic-sol@{identity.version}"
    for item in components:
        if not isinstance(item, dict):
            continue
        references = item.get("externalReferences")
        if isinstance(references, list):
            item["externalReferences"] = [
                reference
                for reference in references
                if not isinstance(reference, dict)
                or not str(reference.get("url", "")).startswith("file:")
            ]
            if not item["externalReferences"]:
                item.pop("externalReferences")
    references = {
        item.get("bom-ref"): item.get("purl") or item.get("bom-ref")
        for item in components
        if isinstance(item, dict) and item.get("bom-ref")
    }
    for item in components:
        if isinstance(item, dict) and item.get("bom-ref") in references:
            item["bom-ref"] = references[item["bom-ref"]]
    for dependency in record.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        dependency["ref"] = references.get(dependency.get("ref"), dependency.get("ref"))
        dependency["dependsOn"] = [
            references.get(ref, ref) for ref in dependency.get("dependsOn", [])
        ]
    components = [item for item in components if item is not application]
    record["components"] = components
    metadata = record.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("build environment SBOM has invalid metadata")
    metadata["component"] = dict(application)
    properties = metadata.setdefault("properties", [])
    if not isinstance(properties, list):
        raise ValueError("build environment SBOM has invalid metadata properties")
    _set_sbom_property(properties, "comic-sol:release:artifact", _safe_member(artifact))
    _set_sbom_property(properties, "comic-sol:release:platform", identity.platform)
    _set_sbom_property(properties, "comic-sol:release:architecture", identity.architecture)

    if not any(_component_name(component) == "python" for component in components):
        raise ValueError("build environment SBOM is missing Python")
    if not isinstance(record.get("dependencies"), list) or not record["dependencies"]:
        raise ValueError("build environment SBOM has no dependency graph")

    record["bomFormat"] = "CycloneDX"
    record["specVersion"] = "1.6"
    record["version"] = 1
    serial_stem = destination_name if destination_name is not None else identity.stem
    record["serialNumber"] = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, serial_stem)}"
    destination = release_dir / _safe_member(destination_name or sbom_name(identity))
    release_dir.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(record), encoding="utf-8", newline="\n")
    return destination


def validate_sbom_schema(sbom_path: Path) -> None:
    """Validate a finalized BOM with the pinned CycloneDX JSON schema."""

    try:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonValidator
    except ImportError as error:  # pragma: no cover - release-only dependency
        raise RuntimeError("install cyclonedx-bom==7.3.1 to validate release SBOMs") from error
    validation_error = JsonValidator(SchemaVersion.V1_6).validate_str(
        sbom_path.read_text(encoding="utf-8")
    )
    if validation_error is not None:
        raise ValueError(f"CycloneDX 1.6 schema validation failed: {validation_error}")


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
    metadata_path = release_dir / metadata_name(identity)
    sbom_path = release_dir / sbom_name(identity)
    checksums_path = release_dir / "SHA256SUMS"
    for required in (metadata_path, sbom_path, checksums_path):
        if not required.is_file():
            raise ValueError(f"missing artifact: {required.name}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_metadata = {
        "architecture": identity.architecture,
        "platform": identity.platform,
        "product": "comic-sol",
        "signature_file": "SHA256SUMS.sigstore.json",
        "signature_status": "sigstore",
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
    serial = sbom.get("serialNumber", "")
    try:
        if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
            raise ValueError("missing urn:uuid: prefix")
        uuid.UUID(serial.removeprefix("urn:uuid:"))
    except (AttributeError, ValueError):
        raise ValueError("SBOM serialNumber is not a UUID URN") from None
    if (
        sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.6"
        or component.get("name") != "comic-sol"
        or component.get("version") != identity.version
        or not isinstance(sbom.get("components"), list)
        or not isinstance(sbom.get("dependencies"), list)
    ):
        raise ValueError("SBOM metadata mismatch")
    properties = {
        item.get("name"): item.get("value")
        for item in sbom.get("metadata", {}).get("properties", [])
        if isinstance(item, dict)
    }
    artifact_names = [name for name in artifact_names if name != sbom_path.name]
    if properties.get("comic-sol:release:artifact") not in artifact_names:
        raise ValueError("SBOM artifact metadata mismatch")
    expected_components = {"pillow", "mcp", "pyinstaller", "python"}
    component_names = {_component_name(item) for item in sbom["components"]}
    if not expected_components <= component_names:
        missing = ", ".join(sorted(expected_components - component_names))
        raise ValueError(f"SBOM missing runtime components: {missing}")
    if any(not isinstance(item, dict) or not item.get("purl") for item in sbom["components"]):
        raise ValueError("SBOM component is missing a stable Package URL")
    refs = {
        item.get("bom-ref")
        for item in sbom["components"]
        if isinstance(item, dict) and item.get("bom-ref")
    }
    root_ref = component.get("bom-ref")
    if not root_ref:
        raise ValueError("SBOM metadata component is missing a bom-ref")
    refs.add(root_ref)
    for dependency in sbom["dependencies"]:
        if not isinstance(dependency, dict) or not dependency.get("ref"):
            raise ValueError("SBOM dependency graph has a missing reference")
        if dependency.get("ref") not in refs:
            raise ValueError("SBOM dependency graph has an unknown reference")
        if not all(item in refs for item in dependency.get("dependsOn", [])):
            raise ValueError("SBOM dependency graph has an unknown dependency")
    root_dependency = next(
        (item for item in sbom["dependencies"] if item.get("ref") == component.get("bom-ref")),
        None,
    )
    if not root_dependency or not root_dependency.get("dependsOn"):
        raise ValueError("SBOM root dependency graph is incomplete")
