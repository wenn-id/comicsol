"""Validate and canonicalize Actions artifact identities for release jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED_ARTIFACTS = frozenset(
    {
        "benchmark-results",
        "comic-sol-container",
        "comic-sol-linux-x86_64",
        "comic-sol-macos-arm64",
        "comic-sol-source",
        "comic-sol-windows-x86_64",
        "live-visual-evidence",
    }
)
_ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _response_artifacts(response: object) -> list[Any]:
    if not isinstance(response, dict):
        raise ValueError("current-run Actions artifact inventory is invalid")
    artifacts = response.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("current-run Actions artifact inventory is invalid")
    return artifacts


def _artifact_row(value: object) -> tuple[dict[Any, Any], str]:
    if not isinstance(value, dict):
        raise ValueError("current-run Actions artifact record is invalid")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("current-run Actions artifact name is invalid")
    return value, name


def _artifact_id(artifact: dict[Any, Any], name: str) -> int:
    value = artifact.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Actions artifact ID is invalid: {name}")
    return value


def _artifact_digest(artifact: dict[Any, Any], name: str) -> str:
    value = artifact.get("digest")
    if not isinstance(value, str) or _ARTIFACT_DIGEST.fullmatch(value) is None:
        raise ValueError(f"Actions artifact digest is invalid: {name}")
    return value


def _artifact_url(artifact: dict[Any, Any], field: str, name: str) -> str:
    value = artifact.get(field)
    label = "URL" if field == "url" else "archive URL"
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError(f"Actions artifact {label} is invalid: {name}")
    return value


def _normalized_identity(artifact: dict[Any, Any], name: str) -> tuple[dict[str, object], int]:
    artifact_id = _artifact_id(artifact, name)
    if artifact.get("expired") is not False:
        raise ValueError(f"Actions artifact is expired or has unknown state: {name}")
    identity = {
        "id": artifact_id,
        "name": name,
        "digest": _artifact_digest(artifact, name),
        "url": _artifact_url(artifact, "url", name),
        "archive_download_url": _artifact_url(artifact, "archive_download_url", name),
    }
    return identity, artifact_id


def _inventory_digest(inventory: list[dict[str, object]]) -> str:
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_inventory(response: object) -> tuple[list[dict[str, object]], str]:
    by_name: dict[str, dict[str, object]] = {}
    seen_ids: set[int] = set()
    for value in _response_artifacts(response):
        artifact, name = _artifact_row(value)
        if name not in EXPECTED_ARTIFACTS:
            continue
        if name in by_name:
            raise ValueError(f"duplicate Actions artifact for current run: {name}")
        identity, artifact_id = _normalized_identity(artifact, name)
        if artifact_id in seen_ids:
            raise ValueError("Actions artifact IDs are not unique")
        by_name[name] = identity
        seen_ids.add(artifact_id)

    if set(by_name) != EXPECTED_ARTIFACTS:
        missing = sorted(EXPECTED_ARTIFACTS - set(by_name))
        raise ValueError(f"current-run Actions artifact mismatch: {missing}")

    inventory = [by_name[name] for name in sorted(by_name)]
    return inventory, _inventory_digest(inventory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    arguments = parser.parse_args(argv)

    try:
        response = json.loads(arguments.input.read_text(encoding="utf-8"))
        inventory, digest = compute_inventory(response)
        if arguments.expected_sha256 is not None:
            if _SHA256.fullmatch(arguments.expected_sha256) is None:
                raise ValueError("expected Actions artifact inventory digest is invalid")
            if digest != arguments.expected_sha256:
                raise ValueError("Actions artifact identities changed after read-only validation")

        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(inventory, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with Path(github_output).open("a", encoding="utf-8") as output:
                output.write(f"artifact_inventory_sha256={digest}\n")
    except (OSError, ValueError) as error:
        print(f"Actions artifact inventory rejected: {error}", file=sys.stderr)
        return 1

    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
