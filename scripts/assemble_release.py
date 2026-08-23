"""Assemble and verify one platform's deterministic release bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comic_sol_product import __version__
from comic_sol_product.distribution import (
    ReleaseIdentity,
    artifact_name,
    validate_sbom_schema,
    verify_release_directory,
    write_checksums,
    write_release_metadata,
    write_sbom,
)
from comic_sol_product.portable import create_portable_archive


def main() -> int:
    """Assemble the portable release from repository artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=("linux", "macos", "windows"))
    parser.add_argument("--architecture", default="x86_64", choices=("x86_64", "arm64"))
    args = parser.parse_args()

    identity = ReleaseIdentity(__version__, args.platform, args.architecture)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / artifact_name(identity, "zip")
    create_portable_archive(args.runtime, archive, architecture=identity.architecture)
    sbom = write_sbom(output, identity, args.environment, archive.name)
    validate_sbom_schema(sbom)
    write_release_metadata(output, identity, [archive.name])
    write_checksums(output, [archive, sbom])
    verify_release_directory(output, identity)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
