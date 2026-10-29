"""Generate the CycloneDX SBOM for the Comic Sol OCI image runtime.

The image's Python environment is inventoried directly from the image
filesystem: ``docker cp`` copies the image's real ``site-packages`` over an
empty mirror virtual environment, and the pinned ``cyclonedx_py`` generator
records exactly the distributions installed in the image — including the
``comic-sol`` wheel the image itself installed. The finalized BOM records the
digest-pinned base image from the Dockerfile's single canonical ``ARG`` and
the container tar payload it describes, validates against the pinned
CycloneDX 1.6 schema, and is written with the release-asset name
``comic-sol-<version>-linux-x86_64.container.sbom.json`` so it travels through
``SHA256SUMS``, the Sigstore signature, and build-provenance attestation like
every other release payload.

Requires the release lock (``cyclonedx-bom``) in the running interpreter; the
image itself is never executed with any added tooling.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comic_sol_product.distribution import (  # noqa: E402
    ReleaseIdentity,
    validate_sbom_schema,
    write_sbom,
)
from scripts.build_portable import write_environment_sbom  # noqa: E402

BASE_IMAGE_PATTERN = re.compile(r'^ARG PYTHON_BASE="(?P<reference>[^"]+@[^\s"]+)"$', re.MULTILINE)
OS_PACKAGE_PURL_TYPES = frozenset({"apk", "deb", "rpm"})


def container_sbom_name(version: str) -> str:
    """Return the release-asset filename for the container image SBOM."""

    return f"comic-sol-{version}-linux-x86_64.container.sbom.json"


def container_tar_name(version: str) -> str:
    """Return the release-asset filename for the exported container tar."""

    return f"comic-sol-{version}-linux-x86_64.container.tar"


def parse_base_image(dockerfile: Path | None = None) -> str:
    """Return the single digest-pinned base reference from the Dockerfile."""

    if dockerfile is None:
        dockerfile = ROOT / "Dockerfile"
    matches = BASE_IMAGE_PATTERN.findall(dockerfile.read_text(encoding="utf-8"))
    if len(matches) != 1:
        raise RuntimeError(
            "Dockerfile must define exactly one digest-pinned ARG PYTHON_BASE; "
            f"found {len(matches)}"
        )
    return matches[0]


def merge_syft_os_packages(environment_sbom: Path, syft_sbom: Path) -> int:
    record = json.loads(environment_sbom.read_text(encoding="utf-8"))
    syft_record = json.loads(syft_sbom.read_text(encoding="utf-8"))
    if syft_record.get("bomFormat") != "CycloneDX":
        raise RuntimeError("Syft SBOM must use CycloneDX format")
    components = record.get("components")
    syft_components = syft_record.get("components")
    if not isinstance(components, list) or not isinstance(syft_components, list):
        raise RuntimeError("CycloneDX SBOM is missing a component list")

    reference_map: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        canonical = str(component.get("purl") or component.get("bom-ref") or "")
        if not canonical:
            continue
        for reference in (component.get("bom-ref"), component.get("purl")):
            if reference:
                reference_map[str(reference)] = canonical

    added: list[dict[str, object]] = []
    for component in syft_components:
        if not isinstance(component, dict):
            continue
        purl = str(component.get("purl") or "")
        purl_type = purl.partition(":")[2].partition("/")[0] if purl.startswith("pkg:") else ""
        if component.get("type") != "operating-system" and purl_type not in OS_PACKAGE_PURL_TYPES:
            continue
        source_ref = str(component.get("bom-ref") or purl)
        canonical = purl or source_ref
        if not source_ref or source_ref in reference_map or canonical in reference_map:
            continue
        component["bom-ref"] = source_ref
        components.append(component)
        added.append(component)
        reference_map[source_ref] = canonical
        reference_map[canonical] = canonical

    if not added:
        raise RuntimeError("Syft SBOM contains no operating-system package components")

    dependencies = record.get("dependencies")
    if not isinstance(dependencies, list):
        dependencies = []
        record["dependencies"] = dependencies
    added_refs = {
        reference_map[str(component.get("bom-ref") or component.get("purl") or "")]
        for component in added
    }
    dependency_refs = {
        str(dependency.get("ref"))
        for dependency in dependencies
        if isinstance(dependency, dict) and dependency.get("ref")
    }
    for dependency in syft_record.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        source_ref = str(dependency.get("ref") or "")
        canonical_ref = reference_map.get(source_ref)
        if not canonical_ref or canonical_ref not in added_refs or canonical_ref in dependency_refs:
            continue
        depends_on = [
            reference_map[reference]
            for reference in dependency.get("dependsOn", [])
            if isinstance(reference, str) and reference in reference_map
        ]
        dependencies.append({"ref": canonical_ref, "dependsOn": sorted(set(depends_on))})
        dependency_refs.add(canonical_ref)

    environment_sbom.write_text(json.dumps(record), encoding="utf-8")
    return len(added)


def _check(command: list[str], description: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise RuntimeError(f"{description} failed:\n{completed.stderr.decode('utf-8', 'replace')}")
    return completed


def image_site_packages(docker: str, image: str) -> str:
    """Ask the image itself where its site-packages directory lives."""

    completed = _check(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        "image site-packages discovery",
    )
    return completed.stdout.decode("utf-8").strip()


def mirror_image_environment(docker: str, image: str, mirror: Path, site_packages: str) -> Path:
    """Copy the image's real site-packages into the mirror environment."""

    venv.EnvBuilder(with_pip=False, clear=True).create(mirror)
    python_name = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    mirror_python = mirror / python_name
    mirror_purelib = subprocess.check_output(
        [str(mirror_python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        text=True,
    ).strip()
    container_id = subprocess.check_output([docker, "create", image], text=True).strip()
    try:
        _check(
            [docker, "cp", f"{container_id}:{site_packages}/.", mirror_purelib],
            "docker cp of the image site-packages",
        )
    finally:
        subprocess.run(
            [docker, "rm", "-f", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return Path(mirror_python)


def main() -> int:
    """Generate, finalize, and validate the container image SBOM."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="built image reference to inventory")
    parser.add_argument("--version", required=True, help="canonical release version")
    parser.add_argument("--output", required=True, type=Path, help="destination SBOM path")
    parser.add_argument("--syft-sbom", required=True, type=Path, help="Syft CycloneDX image SBOM")
    parser.add_argument("--docker", default="docker", help="docker CLI to invoke")
    args = parser.parse_args()

    base_image = parse_base_image()
    with tempfile.TemporaryDirectory(prefix="comic-sol-container-sbom-") as raw:
        temporary = Path(raw)
        site_packages = image_site_packages(args.docker, args.image)
        mirror_python = mirror_image_environment(
            args.docker, args.image, temporary / "mirror", site_packages
        )
        environment_sbom = temporary / "image-environment.sbom.json"
        write_environment_sbom(mirror_python, environment_sbom, temporary, Path(sys.executable))
        os_package_count = merge_syft_os_packages(environment_sbom, args.syft_sbom)
        record = json.loads(environment_sbom.read_text(encoding="utf-8"))
        metadata = record.setdefault("metadata", {})
        properties = metadata.setdefault("properties", [])
        properties.append({"name": "comic-sol:container:base-image", "value": base_image})
        properties.append({"name": "comic-sol:container:os-package-source", "value": "Syft"})
        properties.append(
            {"name": "comic-sol:container:os-package-count", "value": str(os_package_count)}
        )
        environment_sbom.write_text(json.dumps(record), encoding="utf-8")

        output = args.output.resolve()
        identity = ReleaseIdentity(args.version, "linux", "x86_64")
        sbom = write_sbom(
            output.parent,
            identity,
            environment_sbom,
            container_tar_name(args.version),
            destination_name=output.name,
        )
        validate_sbom_schema(sbom)
    print(sbom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
