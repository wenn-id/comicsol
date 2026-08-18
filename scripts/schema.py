"""Project schema compatibility and non-destructive migration hooks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core_primitives import canonical_artifact_bytes
from .project_io import ProjectTransaction, open_path_nofollow

CURRENT_PROJECT_SCHEMA_VERSION = "1.0"
MIN_READER_PROJECT_SCHEMA_VERSION = "1.0"
SUPPORTED_PROJECT_SCHEMA_VERSIONS = frozenset({CURRENT_PROJECT_SCHEMA_VERSION})

Manifest = dict[str, object]
Migration = Callable[[Manifest], Manifest]
# Migrations are deliberately explicit and keyed by source version. A future
# schema change must add a hook here, update the version constants, and add a
# fixture/test before it can be accepted.
PROJECT_MIGRATIONS: dict[tuple[str, str], Migration] = {}


class UnsupportedSchemaVersionError(ValueError):
    """Raised when a project cannot be safely read or migrated."""

    def __init__(self, version: object, *, reason: str | None = None) -> None:
        self.version = version
        self.reason = reason or "no migration path is registered"
        super().__init__(f"project schema {version} is unsupported: {self.reason}")


def _read_manifest(path: Path) -> Manifest:
    with open_path_nofollow(Path(path)) as stream:
        value: Any = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("project.json must contain a JSON object")
    return value


def schema_version_error(version: object) -> UnsupportedSchemaVersionError | None:
    """Return the explicit compatibility error for a manifest version."""
    if isinstance(version, str) and version in SUPPORTED_PROJECT_SCHEMA_VERSIONS:
        return None
    if isinstance(version, str) and version > CURRENT_PROJECT_SCHEMA_VERSION:
        return UnsupportedSchemaVersionError(
            version,
            reason=(
                f"reader supports up to {CURRENT_PROJECT_SCHEMA_VERSION}; "
                "upgrade Comic Sol before opening this project"
            ),
        )
    return UnsupportedSchemaVersionError(
        version,
        reason=(
            f"reader requires at least {MIN_READER_PROJECT_SCHEMA_VERSION}; "
            "no migration path is registered"
        ),
    )


def ensure_supported_project_schema(version: object) -> None:
    """Reject a project version unless the current reader explicitly supports it."""
    error = schema_version_error(version)
    if error is not None:
        raise error


def read_project_manifest(
    path: Path,
    *,
    normalize_legacy: bool = True,
) -> Manifest:
    """Read a project manifest after applying the non-mutating legacy gate."""
    manifest = _read_manifest(Path(path).absolute())
    # Pre-schema manifests are the legacy 1.0 representation. Normalize only
    # in memory so every downstream validator/consumer sees one contract.
    had_schema_version = "schema_version" in manifest
    version = manifest.get("schema_version", CURRENT_PROJECT_SCHEMA_VERSION)
    ensure_supported_project_schema(version)
    manifest.setdefault("schema_version", CURRENT_PROJECT_SCHEMA_VERSION)
    if not normalize_legacy and not had_schema_version:
        manifest.pop("schema_version", None)
    return manifest


def migrate_project_manifest(project_dir: Path) -> Manifest:
    """Run a registered migration transactionally, or fail without mutation.

    A migration receives an in-memory manifest and must return a complete
    manifest for ``CURRENT_PROJECT_SCHEMA_VERSION``. The project file is only
    staged after the hook returns successfully, then published through the
    existing journal-backed transaction.
    """
    project_dir = Path(project_dir)
    manifest_path = project_dir / "project.json"
    with ProjectTransaction(project_dir, "schema-migration") as transaction:
        manifest = _read_manifest(manifest_path.absolute())
        source_version = manifest.get("schema_version", CURRENT_PROJECT_SCHEMA_VERSION)
        if source_version == CURRENT_PROJECT_SCHEMA_VERSION:
            manifest.setdefault("schema_version", CURRENT_PROJECT_SCHEMA_VERSION)
            return manifest
        migration = PROJECT_MIGRATIONS.get((str(source_version), CURRENT_PROJECT_SCHEMA_VERSION))
        if migration is None:
            ensure_supported_project_schema(source_version)
            raise UnsupportedSchemaVersionError(
                source_version, reason="no migration path is registered"
            )
        migrated = migration(dict(manifest))
        if not isinstance(migrated, dict):
            raise ValueError("project migration must return a JSON object")
        migrated["schema_version"] = CURRENT_PROJECT_SCHEMA_VERSION
        transaction.stage_bytes("project.json", canonical_artifact_bytes(migrated))
        return migrated
