"""The sole Comic Sol Web boundary to deterministic project engine state."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast
from uuid import UUID

from comic_sol_product.cli import _load_engine_module

from comic_sol_web.assets import (
    AssetError,
    _canonical_data_root,
    _make_plain_directory,
)
from comic_sol_web.database import Database
from comic_sol_web.generation.types import GenerationRequest
from comic_sol_web.migrations import APPLICATION_MIGRATIONS, Migration
from comic_sol_web.planning.types import PlanRequest, VisualReviewRequest, VisualReviewResult

comic_sol = _load_engine_module("comic_sol")
_character_identity = _load_engine_module("character_identity")
_character_quality = _load_engine_module("character_quality")
_compose_pages = _load_engine_module("compose_pages")
_core_primitives = _load_engine_module("core_primitives")
_export_pdf = _load_engine_module("export_pdf")
_handoff = _load_engine_module("handoff")
_handoff_archive = _load_engine_module("handoff_archive")
_input_limits = _load_engine_module("input_limits")
_letter_panels = _load_engine_module("letter_panels")
_normalize_panels = _load_engine_module("normalize_panels")
_page_quality = _load_engine_module("page_quality")
_project_io = _load_engine_module("project_io")
_raster_limits = _load_engine_module("raster_limits")
_quality_records = _load_engine_module("quality_records")
_repair_strategy = _load_engine_module("repair_strategy")
_schema = _load_engine_module("schema")
_validation = _load_engine_module("validate_project")

ARCHIVE_SUFFIX = _handoff_archive.ARCHIVE_SUFFIX
ProjectLock = _project_io.ProjectLock
ProjectTransaction = _project_io.ProjectTransaction
canonical_artifact_bytes = _core_primitives.canonical_artifact_bytes
cleanup_owned_directory = _project_io.cleanup_owned_directory
contained_project_path = _project_io.contained_project_path
export_handoff_archive = _handoff_archive.export_handoff_archive
guarded_export = _export_pdf.guarded_export
import_handoff_archive = _handoff_archive.import_handoff_archive
loads_bounded_json = _input_limits.loads_bounded_json
read_contained_bytes = _project_io.read_contained_bytes
read_contained_json = _project_io.read_contained_json
read_project_manifest = _schema.read_project_manifest
validate_character_bible = _validation.validate_character_bible
validate_identity_pack = _character_identity.validate_identity_pack
validate_story_plan = _validation.validate_story_plan
validate_storyboard = _validation.validate_storyboard
validate_project = _validation.validate_project

_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_HANDOFF_JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
_DEFAULT_GENERATION_DIMENSION = 1024
_PLAN_FIELDS = {
    "storyPlan": "plan/story-plan.json",
    "characterBible": "plan/character-bible.json",
    "storyboard": "plan/storyboard.json",
    "visualIdentityPack": "plan/character-identity-pack.json",
}

PROJECT_MIGRATION = Migration(
    5,
    (
        """
        CREATE TABLE web_projects (
            project_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            storage_name TEXT NOT NULL UNIQUE,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            engine_state TEXT NOT NULL
        )
        """,
        "CREATE INDEX web_projects_owner ON web_projects (owner_id, project_id)",
    ),
)
PROJECT_IDEMPOTENCY_MIGRATION = Migration(
    6,
    (
        """
        CREATE TABLE web_project_creations (
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES web_projects(project_id) ON DELETE CASCADE,
            PRIMARY KEY (owner_id, idempotency_key),
            UNIQUE (project_id)
        )
        """,
    ),
)
PROJECT_MIGRATIONS = (
    *APPLICATION_MIGRATIONS,
    PROJECT_MIGRATION,
    PROJECT_IDEMPOTENCY_MIGRATION,
)


class GatewayError(ValueError):
    """A Web project request failed at the application/engine boundary."""


class GatewayInputError(GatewayError):
    """Caller input is not part of the frozen gateway contract."""


class ProjectUnavailableError(GatewayError):
    """A project is absent or unavailable to the requesting application service."""


class StaleProjectRevisionError(GatewayError):
    """A mutation was bound to a superseded project revision."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__("project revision is stale")


@dataclass(frozen=True)
class AcceptedRaster:
    payload: bytes
    media_type: str
    sha256: str


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    revision: int
    root: Path
    status: str
    summary: Mapping[str, object]


class EngineGateway:
    """Resolve opaque Web IDs and call existing deterministic engine operations."""

    def __init__(self, database: Database, data_root: Path) -> None:
        self.database = database
        configured_root = Path(data_root).expanduser()
        if not configured_root.is_absolute():
            raise GatewayInputError("project data root must be absolute")
        self.data_root = _canonical_data_root(configured_root)
        try:
            _make_plain_directory(self.data_root)
        except AssetError as error:
            raise GatewayInputError("project data root must be a plain directory") from error
        self.projects_root = contained_project_path(self.data_root, "projects")
        self.projects_root.mkdir(exist_ok=True)
        self.staging_root = contained_project_path(self.data_root, "staging")
        self.staging_root.mkdir(exist_ok=True)
        self.exports_root = contained_project_path(self.data_root, "project-exports")
        self.exports_root.mkdir(exist_ok=True)
        self._recover_revisions()

    @classmethod
    def open(cls, data_root: Path) -> "EngineGateway":
        """Initialize storage, migrations, and the gateway from one canonical root."""
        configured_root = Path(data_root).expanduser()
        if not configured_root.is_absolute():
            raise GatewayInputError("project data root must be absolute")
        canonical_root = _canonical_data_root(configured_root)
        try:
            _make_plain_directory(canonical_root)
        except AssetError as error:
            raise GatewayInputError("project data root must be a plain directory") from error
        database = Database(canonical_root / "application.sqlite3")
        from comic_sol_web.migrations import apply_migrations

        apply_migrations(database, PROJECT_MIGRATIONS)
        return cls(database, canonical_root)

    def _recover_revisions(self) -> None:
        """Reconcile engine commits interrupted before Web revision persistence."""
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT project_id, owner_id, storage_name, revision, engine_state "
                "FROM web_projects ORDER BY project_id"
            ).fetchall()
        for row in rows:
            root = self._root_from_row(row)
            with ProjectLock(root):
                prior_state = self._engine_state(root)
                with self.database.transaction() as connection:
                    self._reconcile_row(connection, cast(str, row["project_id"]), prior_state)

    @staticmethod
    def _validate_owner(owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 64:
            raise GatewayInputError("project owner is invalid")

    @staticmethod
    def _canonical_idempotency_key(idempotency_key: str) -> str:
        if not isinstance(idempotency_key, str):
            raise GatewayInputError("project idempotency key is invalid")
        try:
            parsed = UUID(idempotency_key)
        except ValueError as error:
            raise GatewayInputError("project idempotency key is invalid") from error
        if str(parsed) != idempotency_key.lower():
            raise GatewayInputError("project idempotency key is invalid")
        return str(parsed)

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            raise ProjectUnavailableError("project unavailable")

    def _allocate_container(self) -> tuple[str, Path, tuple[int, int]]:
        for _attempt in range(16):
            project_id = secrets.token_urlsafe(24)
            container = contained_project_path(self.projects_root, project_id)
            try:
                container.mkdir(mode=0o700)
            except FileExistsError:
                continue
            metadata = container.stat(follow_symlinks=False)
            return project_id, container, (metadata.st_dev, metadata.st_ino)
        raise RuntimeError("could not allocate an opaque project ID")

    @staticmethod
    def _cleanup_container(container: Path, identity: tuple[int, int]) -> None:
        if not cleanup_owned_directory(container, identity):
            raise RuntimeError("project rollback refused a changed directory identity")

    @staticmethod
    def _create_parameters(
        request: Mapping[str, object],
    ) -> tuple[str, bytes, dict[str, object], int]:
        if not isinstance(request, Mapping):
            raise GatewayInputError("project request must be an object")
        allowed = {"title", "prompt", "language", "mode", "page_count"}
        if set(request) - allowed:
            raise GatewayInputError("project request contains unsupported fields")
        title = request.get("title")
        prompt = request.get("prompt")
        language = request.get("language", "en")
        mode = request.get("mode", "short_prompt")
        page_count = request.get("page_count", 2)
        if not isinstance(title, str) or not title.strip():
            raise GatewayInputError("project title is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise GatewayInputError("project prompt is required")
        if not isinstance(language, str):
            raise GatewayInputError("project language is invalid")
        if mode not in {"short_prompt", "pasted_story"}:
            raise GatewayInputError("project source mode is invalid")
        if (
            isinstance(page_count, bool)
            or not isinstance(page_count, int)
            or not 1 <= page_count <= 4
        ):
            raise GatewayInputError("project page count is invalid")
        return (
            title,
            prompt.encode("utf-8"),
            {"language": language, "mode": mode},
            page_count,
        )

    def _creation_project_id(self, owner_id: str, idempotency_key: str) -> str | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT project_id FROM web_project_creations "
                "WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        project_id = row["project_id"]
        if not isinstance(project_id, str):
            raise ProjectUnavailableError("project unavailable")
        return project_id

    def _record_project(
        self,
        project_id: str,
        owner_id: str,
        storage_name: str,
        revision: int,
        engine_state: str,
        idempotency_key: str,
    ) -> str:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT project_id FROM web_project_creations "
                "WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                winner = existing["project_id"]
                if not isinstance(winner, str):
                    raise ProjectUnavailableError("project unavailable")
                return winner
            connection.execute(
                "INSERT INTO web_projects "
                "(project_id, owner_id, storage_name, revision, engine_state) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, owner_id, storage_name, revision, engine_state),
            )
            connection.execute(
                "INSERT INTO web_project_creations "
                "(owner_id, idempotency_key, project_id) VALUES (?, ?, ?)",
                (owner_id, idempotency_key, project_id),
            )
        return project_id

    def _row(self, project_id: str) -> sqlite3.Row:
        self._validate_project_id(project_id)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT project_id, owner_id, storage_name, revision, engine_state "
                "FROM web_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectUnavailableError("project unavailable")
        return row

    def _root_from_row(self, row: sqlite3.Row) -> Path:
        storage_name = row["storage_name"]
        if not isinstance(storage_name, str):
            raise ProjectUnavailableError("project unavailable")
        try:
            root = contained_project_path(self.projects_root, storage_name, must_exist=True)
        except (OSError, ValueError) as error:
            raise ProjectUnavailableError("project unavailable") from error
        if not root.is_dir():
            raise ProjectUnavailableError("project unavailable")
        return root

    def require_owner(self, project_id: str, owner_id: str) -> None:
        self._validate_owner(owner_id)
        row = self._row(project_id)
        if row["owner_id"] != owner_id:
            raise ProjectUnavailableError("project unavailable")

    def current_project(self, owner_id: str) -> ProjectSnapshot | None:
        self._validate_owner(owner_id)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT project_id FROM web_projects WHERE owner_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (owner_id,),
            ).fetchone()
        if row is None:
            return None
        project_id = row["project_id"]
        if not isinstance(project_id, str):
            raise ProjectUnavailableError("project unavailable")
        return self.read_plan(project_id)

    @staticmethod
    def _check_revision(actual: int, expected: int | None) -> None:
        if expected is not None and expected != actual:
            raise StaleProjectRevisionError(expected, actual)

    @staticmethod
    def _snapshot_for(
        project_id: str,
        revision: int,
        root: Path,
        *,
        extra_summary: Mapping[str, object] | None = None,
    ) -> ProjectSnapshot:
        manifest = read_project_manifest(root / "project.json")
        engine_project_id = manifest.get("project_id")
        status = manifest.get("status")
        schema_version = manifest.get("schema_version")
        title = manifest.get("title")
        if engine_project_id != root.name or not isinstance(status, str):
            raise GatewayError("canonical project identity is invalid")
        summary: dict[str, object] = {
            "engine_project_id": engine_project_id,
            "schema_version": schema_version,
            "title": title,
        }
        settings = manifest.get("settings")
        if isinstance(settings, Mapping):
            summary["page_count"] = settings.get("page_count")
            summary["panel_count"] = settings.get("panel_count")
        if extra_summary:
            summary.update(extra_summary)
        return ProjectSnapshot(project_id, revision, root, status, summary)

    def create_project(
        self,
        owner_id: str,
        request: Mapping[str, object],
        idempotency_key: str,
    ) -> ProjectSnapshot:
        self._validate_owner(owner_id)
        idempotency_key = self._canonical_idempotency_key(idempotency_key)
        title, source, engine_request, page_count = self._create_parameters(request)
        existing = self._creation_project_id(owner_id, idempotency_key)
        if existing is not None:
            return self.read_plan(existing)
        project_id, container, identity = self._allocate_container()
        try:
            root = comic_sol.init_project(
                container,
                title,
                source,
                engine_request,
                page_count=page_count,
            )
            snapshot = self._snapshot_for(project_id, 1, root)
            storage_name = root.relative_to(self.projects_root).as_posix()
            winner = self._record_project(
                project_id,
                owner_id,
                storage_name,
                1,
                self._engine_state(root),
                idempotency_key,
            )
        except BaseException:
            self._cleanup_container(container, identity)
            raise
        if winner != project_id:
            self._cleanup_container(container, identity)
            return self.read_plan(winner)
        return snapshot

    def import_project(
        self,
        owner_id: str,
        archive: Path,
        idempotency_key: str,
    ) -> ProjectSnapshot:
        self._validate_owner(owner_id)
        idempotency_key = self._canonical_idempotency_key(idempotency_key)
        archive_path = Path(archive)
        if archive_path.suffix != ARCHIVE_SUFFIX:
            raise GatewayInputError("unsupported project archive format")
        existing = self._creation_project_id(owner_id, idempotency_key)
        if existing is not None:
            return self.read_plan(existing)
        project_id, container, identity = self._allocate_container()
        try:
            result = import_handoff_archive(archive_path, container)
            root_value = result.get("project_dir")
            if not isinstance(root_value, str):
                raise GatewayError("archive import did not return a project root")
            root = Path(root_value)
            snapshot = self._snapshot_for(project_id, 1, root)
            storage_name = root.relative_to(self.projects_root).as_posix()
            winner = self._record_project(
                project_id,
                owner_id,
                storage_name,
                1,
                self._engine_state(root),
                idempotency_key,
            )
        except BaseException:
            self._cleanup_container(container, identity)
            raise
        if winner != project_id:
            self._cleanup_container(container, identity)
            return self.read_plan(winner)
        return snapshot

    def snapshot(self, project_id: str, expected_revision: int | None = None) -> ProjectSnapshot:
        row = self._row(project_id)
        revision = cast(int, row["revision"])
        self._check_revision(revision, expected_revision)
        return self._snapshot_for(project_id, revision, self._root_from_row(row))

    @staticmethod
    def _plan_summary(root: Path) -> dict[str, str]:
        plan: dict[str, str] = {}
        for field, relative in _PLAN_FIELDS.items():
            candidate = contained_project_path(root, relative)
            plan[field] = (
                read_contained_bytes(root, relative).decode("utf-8") if candidate.is_file() else ""
            )
        return plan

    def read_plan(self, project_id: str, expected_revision: int | None = None) -> ProjectSnapshot:
        initial = self._row(project_id)
        root = self._root_from_row(initial)
        with ProjectLock(root):
            ProjectTransaction.recover(root)
            state = self._engine_state(root)
            with self.database.transaction() as connection:
                row = self._reconcile_row(connection, project_id, state)
                revision = cast(int, row["revision"])
                self._check_revision(revision, expected_revision)
            return self._snapshot_for(
                project_id,
                revision,
                root,
                extra_summary={"plan": self._plan_summary(root)},
            )

    def planning_input(self, project_id: str, expected_revision: int) -> PlanRequest:
        """Read only the canonical source and request, never exposing its path."""
        root = self._root_from_row(self._row(project_id))
        with ProjectLock(root, read_only=True):
            row = self._row(project_id)
            self._check_revision(cast(int, row["revision"]), expected_revision)
            manifest = read_project_manifest(root / "project.json")
            request = read_contained_json(root, "source/request.json")
            source = read_contained_bytes(
                root, "source/input.txt", max_bytes=_project_io.MAX_SOURCE_BYTES
            ).decode("utf-8")
            settings = manifest.get("settings")
            if not isinstance(settings, dict) or not isinstance(request, dict):
                raise GatewayInputError("planning input is invalid")
            title = manifest.get("title")
            language = request.get("language")
            page_count = settings.get("page_count")
            if (
                not isinstance(title, str)
                or not isinstance(language, str)
                or isinstance(page_count, bool)
                or not isinstance(page_count, int)
            ):
                raise GatewayInputError("planning input is invalid")
            return PlanRequest(
                title=title,
                source=source,
                language=language,
                page_count=page_count,
            )

    @contextmanager
    def planning_publication(self, project_id: str) -> Iterator[None]:
        """Serialize lease fencing/publication with other canonical Plan writers.

        The service keeps provider calls outside this short, reentrant lock.
        A claimant also takes it before replacing an expired lease.
        """
        root = self._root_from_row(self._row(project_id))
        with ProjectLock(root, read_only=True):
            yield

    @staticmethod
    def _plan_candidate(
        root: Path, plan: Mapping[str, object]
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        bool,
    ]:
        if not isinstance(plan, Mapping) or set(plan) != set(_PLAN_FIELDS):
            raise GatewayInputError("Plan request must contain the complete canonical envelope")
        canonical_paths = list(_PLAN_FIELDS.values())
        existing = [
            contained_project_path(root, relative).is_file() for relative in canonical_paths
        ]
        first_plan = not any(existing)
        if not first_plan and not all(existing):
            raise GatewayInputError("Plan update requires a complete canonical artifact set")
        documents: dict[str, dict[str, object]] = {}
        for field, relative in _PLAN_FIELDS.items():
            payload = plan[field]
            if not isinstance(payload, str):
                raise GatewayInputError("Plan document must be JSON text")
            try:
                document = loads_bounded_json(payload, source=relative)
            except (TypeError, ValueError) as error:
                raise GatewayInputError("Plan document is invalid") from error
            if not isinstance(document, dict):
                raise GatewayInputError("Plan document must contain a JSON object")
            documents[field] = cast(dict[str, object], document)
        return (
            documents["storyPlan"],
            documents["characterBible"],
            documents["storyboard"],
            documents["visualIdentityPack"],
            first_plan,
        )

    @staticmethod
    def _validate_plan_candidate(
        root: Path,
        story: dict[str, object],
        character_bible: dict[str, object],
        storyboard: dict[str, object],
        identity_pack: dict[str, object],
        *,
        first_plan: bool,
    ) -> list[str]:
        issues: list[object] = []
        issues.extend(validate_story_plan(story))
        issues.extend(validate_character_bible(character_bible))
        issues.extend(validate_storyboard(storyboard, story, character_bible))
        issues.extend(
            validate_identity_pack(
                identity_pack,
                character_bible=character_bible,
            )
        )

        character_items = character_bible.get("characters")
        if not isinstance(character_items, list):
            character_items = []
        known_characters = {
            item.get("id")
            for item in character_items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        scenes = story.get("scenes")
        if not isinstance(scenes, list):
            scenes = []
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            scene_characters = scene.get("characters")
            if isinstance(scene_characters, list) and any(
                character_id not in known_characters for character_id in scene_characters
            ):
                issues.append("story scene references an unknown character")

        manifest = read_project_manifest(root / "project.json")
        settings = manifest.get("settings")
        pages = storyboard.get("pages")
        manifest_panels = manifest.get("panels")
        panel_ids: list[str] = []
        if isinstance(settings, dict) and isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                panels = page.get("panels")
                if not isinstance(panels, list):
                    continue
                panel_ids.extend(
                    panel["id"]
                    for panel in panels
                    if isinstance(panel, dict) and isinstance(panel.get("id"), str)
                )
            if settings.get("page_count") != len(pages):
                issues.append("storyboard page count does not match project settings")
            if not first_plan and settings.get("panel_count") != len(panel_ids):
                issues.append("storyboard panel count does not match project settings")
            if not first_plan and manifest_panels != panel_ids:
                issues.append("storyboard panel order does not match the project manifest")
        else:
            issues.append("project settings or storyboard pages are invalid")
        if issues:
            raise GatewayInputError("Plan candidate failed canonical validation")
        return panel_ids

    @staticmethod
    def _initial_prompt_payloads(
        character_bible: Mapping[str, object],
        storyboard: Mapping[str, object],
    ) -> dict[str, bytes]:
        prompts: dict[str, bytes] = {}
        characters = character_bible.get("characters")
        if isinstance(characters, list):
            for character in characters:
                if not isinstance(character, dict) or not isinstance(character.get("id"), str):
                    continue
                character_id = character["id"]
                prompts[f"prompts/references/{character_id}.txt"] = (
                    "Create a clean turnaround reference for this approved character.\n"
                    + json.dumps(character, ensure_ascii=False, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
        pages = storyboard.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict) or not isinstance(page.get("panels"), list):
                    continue
                for panel in page["panels"]:
                    if not isinstance(panel, dict) or not isinstance(panel.get("id"), str):
                        continue
                    panel_id = panel["id"]
                    prompts[f"prompts/panels/{panel_id}.txt"] = (
                        "Create this approved comic panel without lettering.\n"
                        + json.dumps(panel, ensure_ascii=False, sort_keys=True)
                        + "\n"
                    ).encode("utf-8")
        return prompts

    @staticmethod
    def _initial_plan_manifest(
        root: Path,
        payloads: Mapping[str, bytes],
        panel_ids: list[str],
    ) -> bytes:
        manifest = read_project_manifest(root / "project.json", normalize_legacy=False)
        artifacts = manifest.get("artifacts")
        settings = manifest.get("settings")
        if manifest.get("status") != "INIT" or artifacts != {} or not isinstance(settings, dict):
            raise GatewayInputError("first Plan requires a newly initialized canonical project")
        settings["panel_count"] = len(panel_ids)
        manifest["panels"] = panel_ids
        artifact_payloads = {
            "story_plan": ("plan/story-plan.json", payloads["storyPlan"]),
            "character_bible": ("plan/character-bible.json", payloads["characterBible"]),
            "storyboard": ("plan/storyboard.json", payloads["storyboard"]),
        }
        manifest["artifacts"] = {
            name: {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
            for name, (relative, payload) in artifact_payloads.items()
        }
        # A validated first Plan contains both planning and storyboard outputs.
        # Publish their terminal canonical state in the same project transaction.
        manifest["status"] = "STORYBOARDED"
        manifest["updated_at"] = comic_sol._utc_now()
        return canonical_artifact_bytes(manifest)

    def update_plan(
        self,
        project_id: str,
        expected_revision: int,
        plan: Mapping[str, object],
    ) -> ProjectSnapshot:
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        revision = expected_revision
        changed = False
        with ProjectLock(root):
            with ProjectTransaction(root, "studio-plan-update") as transaction:
                prior_state = self._engine_state(root)
                with self.database.transaction() as connection:
                    row = self._reconcile_row(connection, project_id, prior_state)
                    self._check_revision(cast(int, row["revision"]), expected_revision)
                    revision = cast(int, row["revision"])
                story, character_bible, storyboard, identity_pack, first_plan = (
                    self._plan_candidate(root, plan)
                )
                panel_ids = self._validate_plan_candidate(
                    root,
                    story,
                    character_bible,
                    storyboard,
                    identity_pack,
                    first_plan=first_plan,
                )
                payloads = {
                    "storyPlan": canonical_artifact_bytes(story),
                    "characterBible": canonical_artifact_bytes(character_bible),
                    "storyboard": canonical_artifact_bytes(storyboard),
                    "visualIdentityPack": canonical_artifact_bytes(identity_pack),
                }
                if first_plan:
                    for field, relative in _PLAN_FIELDS.items():
                        transaction.stage_bytes(relative, payloads[field])
                    for relative, payload in self._initial_prompt_payloads(
                        character_bible, storyboard
                    ).items():
                        transaction.stage_bytes(relative, payload)
                    transaction.stage_bytes(
                        "project.json",
                        self._initial_plan_manifest(
                            root,
                            payloads,
                            panel_ids,
                        ),
                    )
                    changed = True
                else:
                    changed_fields = [
                        field
                        for field, relative in _PLAN_FIELDS.items()
                        if read_contained_bytes(root, relative) != payloads[field]
                    ]
                    if changed_fields:
                        comic_sol._invalidate_from_locked(root, "planning", transaction)
                        for field in changed_fields:
                            transaction.stage_bytes(_PLAN_FIELDS[field], payloads[field])
                        changed = True
            if changed:
                revision += 1
                self._ensure_revision(project_id, expected_revision, revision, root)
            return self._snapshot_for(
                project_id,
                revision,
                root,
                extra_summary={"plan": self._plan_summary(root)},
            )

    @staticmethod
    def _request_from_job(
        project_id: str,
        revision: int,
        root: Path,
        job: Mapping[str, object],
    ) -> GenerationRequest:
        job_id = job.get("job_id")
        subject_kind = job.get("subject_kind")
        subject_id = job.get("subject_id")
        prompt_path = job.get("prompt_path")
        if not all(
            isinstance(value, str) for value in (job_id, subject_kind, subject_id, prompt_path)
        ):
            raise GatewayError("generation job identity is invalid")
        prompt = read_contained_bytes(root, cast(str, prompt_path)).decode("utf-8")
        references_value = job.get("references")
        if not isinstance(references_value, list):
            raise GatewayError("generation job references are invalid")
        references: list[Path] = []
        for item in references_value:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                raise GatewayError("generation job reference is invalid")
            references.append(
                contained_project_path(root, cast(str, item["path"]), must_exist=True)
            )
        dimensions = job.get("requested_dimensions")
        if dimensions is None:
            width = height = _DEFAULT_GENERATION_DIMENSION
        elif isinstance(dimensions, Mapping):
            candidate_width = dimensions.get("width")
            candidate_height = dimensions.get("height")
            if (
                isinstance(candidate_width, bool)
                or not isinstance(candidate_width, int)
                or isinstance(candidate_height, bool)
                or not isinstance(candidate_height, int)
            ):
                raise GatewayError("generation job dimensions are invalid")
            width = candidate_width
            height = candidate_height
        else:
            raise GatewayError("generation job dimensions are invalid")
        capabilities = {"text_to_image"}
        if references:
            capabilities.add("reference_images")
        if dimensions is not None:
            capabilities.add("custom_dimensions")
        return GenerationRequest(
            job_id=cast(str, job_id),
            project_id=project_id,
            project_revision=revision,
            subject_kind=cast(str, subject_kind),
            subject_id=cast(str, subject_id),
            prompt=prompt,
            negative_prompt=None,
            references=tuple(references),
            width=width,
            height=height,
            required_capabilities=frozenset(capabilities),
        )

    def _checked_row(
        self, connection: sqlite3.Connection, project_id: str, expected: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT project_id, owner_id, storage_name, revision, engine_state "
            "FROM web_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectUnavailableError("project unavailable")
        self._check_revision(cast(int, row["revision"]), expected)
        return row

    @staticmethod
    def _engine_state(root: Path) -> str:
        """Return a bounded durable token for canonical project state."""
        digest = hashlib.sha256()
        for relative, metadata in _handoff_archive._iter_project_entries(root):
            if _handoff_archive._is_excluded_project_path(relative):
                continue
            if stat.S_ISREG(metadata.st_mode):
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(read_contained_bytes(root, relative))
                digest.update(b"\0")
        return digest.hexdigest()

    def _reconcile_row(
        self, connection: sqlite3.Connection, project_id: str, state: str
    ) -> sqlite3.Row:
        """Advance Web revision after a process died following an engine commit."""
        row = connection.execute(
            "SELECT project_id, owner_id, storage_name, revision, engine_state "
            "FROM web_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectUnavailableError("project unavailable")
        if row["engine_state"] != state:
            self._set_revision(connection, project_id, cast(int, row["revision"]) + 1)
            self._set_engine_state(connection, project_id, state)
            row = connection.execute(
                "SELECT project_id, owner_id, storage_name, revision, engine_state "
                "FROM web_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return cast(sqlite3.Row, row)

    @staticmethod
    def _set_revision(connection: sqlite3.Connection, project_id: str, revision: int) -> None:
        connection.execute(
            "UPDATE web_projects SET revision = ? WHERE project_id = ?",
            (revision, project_id),
        )

    @staticmethod
    def _set_engine_state(connection: sqlite3.Connection, project_id: str, state: str) -> None:
        connection.execute(
            "UPDATE web_projects SET engine_state = ? WHERE project_id = ?",
            (state, project_id),
        )

    def _ensure_revision(self, project_id: str, previous: int, revision: int, root: Path) -> None:
        """Finish Web bookkeeping after a canonical engine commit.

        Canonical engine operations publish atomically before SQLite can record
        their resulting Web revision. If that first record attempt fails, make
        one independent durable repair while the project lock is still held so
        callers can never continue using the superseded revision.
        """
        state = self._engine_state(root)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM web_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectUnavailableError("project unavailable")
            actual = cast(int, row["revision"])
            if actual == revision:
                self._set_engine_state(connection, project_id, state)
                return
            if actual != previous:
                raise StaleProjectRevisionError(previous, actual)
            self._set_revision(connection, project_id, revision)
            self._set_engine_state(connection, project_id, state)

    def prepare_generation(
        self, project_id: str, expected_revision: int
    ) -> tuple[GenerationRequest, ...]:
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        engine_changed = False
        revision = expected_revision
        with ProjectLock(root):
            try:
                prior_state = self._engine_state(root)
                with self.database.transaction() as connection:
                    row = self._reconcile_row(connection, project_id, prior_state)
                    self._check_revision(cast(int, row["revision"]), expected_revision)
                    revision = cast(int, row["revision"])
                manifest = read_project_manifest(root / "project.json")
                if manifest.get("status") in {"INIT", "PLANNED", "SCRIPTED"}:
                    raise GatewayInputError(
                        "generation requires canonical planning and storyboard stages to be rebuilt"
                    )
                prepared = comic_sol.prepare_handoff(root)
                if prepared.get("changed") is True:
                    engine_changed = True
                    revision += 1
                    self._ensure_revision(project_id, expected_revision, revision, root)
                inspection = comic_sol.inspect_handoff(root)
                requests: list[GenerationRequest] = []
                for descriptor in cast(list[dict[str, object]], inspection["jobs"]):
                    if descriptor.get("status") != "ready":
                        continue
                    relative = descriptor.get("path")
                    if not isinstance(relative, str):
                        raise GatewayError("generation job path is invalid")
                    job = read_contained_json(root, relative)
                    if not isinstance(job, Mapping):
                        raise GatewayError("generation job is invalid")
                    requests.append(self._request_from_job(project_id, revision, root, job))
                return tuple(requests)
            except BaseException:
                if engine_changed:
                    self._ensure_revision(project_id, expected_revision, revision, root)
                raise

    def _staged_raster(self, raster: Path) -> Path:
        candidate = Path(raster)
        if not candidate.is_absolute():
            raise GatewayInputError("raster staging path must be absolute")
        try:
            relative = candidate.relative_to(self.staging_root)
            staged = contained_project_path(self.staging_root, relative, must_exist=True)
        except (OSError, ValueError) as error:
            raise GatewayInputError("raster is outside the contained staging root") from error
        if not staged.is_file():
            raise GatewayInputError("raster staging path is not a file")
        return staged

    def submit_raster(
        self,
        project_id: str,
        expected_revision: int,
        job_id: str,
        raster: Path,
        media_type: str,
        capabilities_used: Mapping[str, object],
    ) -> ProjectSnapshot:
        if media_type != "image/png":
            raise GatewayInputError("handoff raster media type must be image/png")
        staged = self._staged_raster(raster)
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        capability_keys = {"dimensions", "localized_edit", "reference_images"}
        if set(capabilities_used) != capability_keys or not all(
            isinstance(capabilities_used[key], bool) for key in capability_keys
        ):
            raise GatewayInputError("raster capability metadata is invalid")
        engine_changed = False
        revision = expected_revision
        with ProjectLock(root):
            try:
                prior_state = self._engine_state(root)
                with self.database.transaction() as connection:
                    row = self._reconcile_row(connection, project_id, prior_state)
                    self._check_revision(cast(int, row["revision"]), expected_revision)
                    revision = cast(int, row["revision"])
                inspection = comic_sol.inspect_handoff(root)
                state = next(
                    (
                        item
                        for item in cast(list[dict[str, object]], inspection["jobs"])
                        if item.get("job_id") == job_id
                    ),
                    None,
                )
                if state is None:
                    raise _handoff.HandoffResultError(
                        ["job_id: does not name a current handoff job"]
                    )
                attempt = state.get("next_attempt")
                if isinstance(attempt, bool) or not isinstance(attempt, int):
                    attempt = max(1, cast(int, state.get("attempts_used", 0)))
                subject_kind = state.get("subject_kind")
                subject_id = state.get("subject_id")
                result = comic_sol.accept_handoff_result(
                    root,
                    job_id=job_id,
                    attempt=attempt,
                    raster_path=staged,
                    executor_kind="external-tool",
                    executor_id="comic-sol-web",
                    capabilities_used=capabilities_used,
                    approve_reference=subject_kind == "reference",
                )
                duplicate = result.get("duplicate") is True
                engine_changed = not duplicate
                if engine_changed:
                    # Acceptance commits retained bytes before panel promotion.
                    # Compute the target now so a promotion failure repairs
                    # SQLite to the already-published engine generation.
                    revision += 1
                if subject_kind == "panel":
                    if not isinstance(subject_id, str) or not isinstance(
                        result.get("raster_path"), str
                    ):
                        raise GatewayError("accepted panel result binding is invalid")
                    comic_sol.promote_attempt(
                        root,
                        subject_id,
                        Path(cast(str, result["raster_path"])),
                    )
                if engine_changed:
                    self._ensure_revision(project_id, expected_revision, revision, root)
                snapshot = self._snapshot_for(project_id, revision, root)
                return snapshot
            except BaseException:
                if engine_changed:
                    self._ensure_revision(project_id, expected_revision, revision, root)
                raise

    def accepted_raster(
        self,
        project_id: str,
        expected_revision: int,
        job_id: str,
    ) -> AcceptedRaster:
        """Read the current canonical panel raster bound to an accepted handoff job."""
        if not isinstance(job_id, str) or _HANDOFF_JOB_ID.fullmatch(job_id) is None:
            raise GatewayInputError("accepted raster job identifier is invalid")
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        with ProjectLock(root, read_only=True), self.database.read() as connection:
            self._checked_row(connection, project_id, expected_revision)
            inspection = comic_sol.inspect_handoff(root)
            state = next(
                (
                    item
                    for item in cast(list[dict[str, object]], inspection["jobs"])
                    if item.get("job_id") == job_id
                ),
                None,
            )
            if (
                state is None
                or state.get("status") != "completed"
                or state.get("subject_kind") != "panel"
            ):
                raise ProjectUnavailableError("accepted raster unavailable")
            relative = state.get("path")
            if not isinstance(relative, str):
                raise GatewayError("accepted raster job binding is invalid")
            job = read_contained_json(root, relative)
            if (
                not isinstance(job, Mapping)
                or job.get("job_id") != job_id
                or job.get("subject_kind") != "panel"
            ):
                raise GatewayError("accepted raster job binding is invalid")
            subject_id = job.get("subject_id")
            retry_limit = job.get("retry_limit")
            if (
                not isinstance(subject_id, str)
                or isinstance(retry_limit, bool)
                or not isinstance(retry_limit, int)
            ):
                raise GatewayError("accepted raster job binding is invalid")
            job_sha256 = _handoff.generation_job_sha256(job)
            accepted_receipts: list[Mapping[str, object]] = []
            for attempt in range(1, retry_limit + 2):
                attempt_id = _handoff.attempt_id(job_id=job_id, attempt=attempt)
                receipt_path = f"generation/receipts/{attempt_id}.json"
                try:
                    receipt = read_contained_json(root, receipt_path)
                except FileNotFoundError:
                    continue
                if not isinstance(receipt, Mapping):
                    raise GatewayError("accepted raster receipt is invalid")
                issues = _handoff.validate_generation_receipt(receipt)
                if issues:
                    raise GatewayError("accepted raster receipt is invalid")
                if receipt.get("job_id") != job_id or receipt.get("job_sha256") != job_sha256:
                    raise GatewayError("accepted raster receipt binding is invalid")
                if receipt.get("outcome") == "success" and receipt.get("category") == "accepted":
                    accepted_receipts.append(receipt)
            if len(accepted_receipts) != 1:
                raise ProjectUnavailableError("accepted raster unavailable")
            receipt = accepted_receipts[0]
            digest = receipt.get("raster_sha256")
            if not isinstance(digest, str):
                raise GatewayError("accepted raster digest binding is invalid")
            canonical_path = f"panels/raw/{subject_id}.png"
            try:
                payload = read_contained_bytes(
                    root,
                    canonical_path,
                    max_bytes=_raster_limits.MAX_ENCODED_RASTER_BYTES,
                )
            except FileNotFoundError as error:
                raise ProjectUnavailableError("accepted raster unavailable") from error
            if hashlib.sha256(payload).hexdigest() != digest:
                raise GatewayError("accepted raster digest mismatch")
            try:
                comic_sol._validate_handoff_raster(payload, job)
            except _handoff.HandoffResultError as error:
                raise GatewayError("accepted raster validation failed") from error
            return AcceptedRaster(payload=payload, media_type="image/png", sha256=digest)

    def run_qa(self, project_id: str, expected_revision: int) -> ProjectSnapshot:
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        with ProjectLock(root), self.database.read() as connection:
            row = self._checked_row(connection, project_id, expected_revision)
            issues = validate_project(root, "all")
            qa = {
                "valid": not issues,
                "issues": tuple(
                    {"field": issue.field, "message": issue.message, "path": issue.path}
                    for issue in issues
                ),
            }
            return self._snapshot_for(
                project_id,
                cast(int, row["revision"]),
                root,
                extra_summary={"qa": qa, "plan": self._plan_summary(root)},
            )

    @contextmanager
    def _review_project(
        self, project_id: str, expected_revision: int, *, mutation: bool = False
    ) -> Iterator[Path]:
        """Bind review work to one engine generation, including interrupted writes."""
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        with ProjectLock(root):
            prior_state = self._engine_state(root)
            if mutation:
                with self.database.transaction() as connection:
                    row = self._reconcile_row(connection, project_id, prior_state)
                    self._check_revision(cast(int, row["revision"]), expected_revision)
            else:
                with self.database.read() as connection:
                    row = self._checked_row(connection, project_id, expected_revision)
                    if row["engine_state"] != prior_state:
                        raise StaleProjectRevisionError(expected_revision, expected_revision + 1)
            try:
                yield root
            finally:
                if mutation and self._engine_state(root) != prior_state:
                    self._ensure_revision(
                        project_id, expected_revision, expected_revision + 1, root
                    )

    def _review_snapshot(
        self, project_id: str, expected_revision: int, root: Path
    ) -> ProjectSnapshot:
        row = self._row(project_id)
        revision = expected_revision + (row["engine_state"] != self._engine_state(root))
        self._ensure_revision(project_id, expected_revision, revision, root)
        return self._snapshot_for(project_id, revision, root)

    @staticmethod
    def _storyboard_panel(root: Path, panel_id: str) -> tuple[dict, list[dict]]:
        if (
            not isinstance(panel_id, str)
            or _core_primitives.PANEL_ID_PATTERN.fullmatch(panel_id) is None
        ):
            raise GatewayInputError("panel ID is invalid")
        storyboard = read_contained_json(root, "plan/storyboard.json")
        panels = comic_sol._storyboard_panels(storyboard)
        panel = next((panel for panel in panels if panel.get("id") == panel_id), None)
        if panel is None:
            raise GatewayInputError("panel is absent from the current storyboard")
        return panel, panels

    @staticmethod
    def _character_context(root: Path, panel_id: str) -> dict:
        return cast(
            dict,
            _character_quality.character_consistency_context(
                read_contained_json(root, "plan/character-identity-pack.json"),
                read_contained_json(root, "plan/character-bible.json"),
                read_contained_json(root, _character_quality.REFERENCE_PLAN_PATH),
                panel_id,
                storyboard=read_contained_json(root, "plan/storyboard.json"),
            ),
        )

    def panel_review_input(
        self, project_id: str, expected_revision: int, panel_id: str
    ) -> VisualReviewRequest:
        with self._review_project(project_id, expected_revision) as root:
            panel, panels = self._storyboard_panel(root, panel_id)
            position = panels.index(panel)
            context = self._character_context(root, panel_id)
            context.update(
                {
                    "panel": panel,
                    "requested_dimensions": {
                        "width": panel["rect"]["width"],
                        "height": panel["rect"]["height"],
                    },
                    "generated_sfx": [
                        item for item in panel.get("text", []) if comic_sol.is_generated_sfx(item)
                    ],
                    "adjacent_panels": panels[max(0, position - 1) : position]
                    + panels[position + 1 : position + 2],
                    "raw_path": f"panels/raw/{panel_id}.png",
                }
            )
            raster = read_contained_bytes(
                root, context["raw_path"], max_bytes=_raster_limits.MAX_ENCODED_RASTER_BYTES
            )
            context["raw_sha256"] = hashlib.sha256(raster).hexdigest()
            return VisualReviewRequest(
                "panel", panel_id, raster, context, _core_primitives.PANEL_CHECK_IDS
            )

    @staticmethod
    def _review_checks(review: VisualReviewResult, expected_ids: tuple[str, ...]) -> list[dict]:
        if not isinstance(review, VisualReviewResult):
            raise GatewayInputError("visual review result is invalid")

        # Provider values are deeply immutable; canonical validators consume JSON values.
        def thaw(value: object) -> object:
            if isinstance(value, Mapping):
                return {key: thaw(item) for key, item in value.items()}
            if isinstance(value, tuple | list):
                return [thaw(item) for item in value]
            return value

        checks = cast(list[dict], thaw(review.checks))
        categories = _quality_records.validate_quality_checks(checks, expected_ids)
        if categories:
            raise GatewayInputError("visual review checks are invalid: " + ", ".join(categories))
        return checks

    @staticmethod
    def _panel_bindings(root: Path, panel_id: str) -> dict:
        relative = f"panels/{panel_id}/normalization.json"
        payload = read_contained_bytes(root, relative)
        normalization = loads_bounded_json(payload, source="normalization record")
        source, clean = normalization["source"], normalization["clean"]
        return {
            "raw_path": source["path"],
            "raw_sha256": source["sha256"],
            "raw_width": source["size"][0],
            "raw_height": source["size"][1],
            "clean_path": clean["path"],
            "clean_sha256": clean["sha256"],
            "clean_width": clean["size"][0],
            "clean_height": clean["size"][1],
            "normalization_path": relative,
            "normalization_sha256": hashlib.sha256(payload).hexdigest(),
        }

    @staticmethod
    def _panels_accepted(root: Path) -> bool:
        panels = comic_sol._storyboard_panels(read_contained_json(root, "plan/storyboard.json"))
        if not panels:
            return False
        for panel in panels:
            try:
                record = read_contained_json(root, f"qa/panels/{panel['id']}.json")
            except FileNotFoundError:
                return False
            if record.get("decision") not in {
                "accept",
                "accept-warning",
            } or comic_sol._accepted_panel_problem(root, record):
                return False
        return True

    @staticmethod
    def _validate_review_warnings(warnings: list[str]) -> None:
        for warning in warnings:
            _input_limits.validate_narrative(
                warning,
                message=_input_limits.WARNING_LIMIT_MESSAGE,
                max_chars=_input_limits.MAX_WARNING_CHARS,
            )

    @staticmethod
    def _record_review_warnings(root: Path, warnings: list[str], transaction) -> None:
        if warnings:
            manifest = read_project_manifest(root / "project.json", normalize_legacy=False)
            manifest["warnings"] = list(dict.fromkeys([*manifest["warnings"], *warnings]))
            transaction.stage_bytes("project.json", canonical_artifact_bytes(manifest))

    def publish_panel_review(
        self, project_id: str, expected_revision: int, panel_id: str, review: VisualReviewResult
    ) -> ProjectSnapshot:
        with self._review_project(project_id, expected_revision, mutation=True) as root:
            panel, _ = self._storyboard_panel(root, panel_id)
            checks = self._review_checks(review, _core_primitives.PANEL_CHECK_IDS)
            identity = checks[0]
            context = self._character_context(root, panel_id)
            if context["characters"]:
                checks[0] = _character_quality.build_character_identity_check(
                    context,
                    [dict(item) for item in review.character_assessments],
                    method=identity["method"],
                    reviewer=identity["reviewer"],
                )
            elif review.character_assessments:
                raise GatewayInputError("character-free panel cannot contain trait assessments")
            if _character_quality.validate_character_identity_check(checks[0]):
                raise GatewayInputError("character review is invalid")
            failures = [
                check
                for check in checks
                if check["result"] == "fail" and check["severity"] == "error"
            ]
            warnings = [
                check["evidence"]
                for check in checks
                if check["result"] == "warning" or check["severity"] == "warning"
            ]
            self._validate_review_warnings(warnings)
            record = {
                "schema_version": "2.0",
                "kind": "panel-qa",
                "subject_id": panel_id,
                "checks": checks,
                "decision": "regenerate"
                if failures
                else "accept-warning"
                if warnings
                else "accept",
                "review": {
                    "method": identity["method"],
                    "reviewer": identity["reviewer"],
                    "reviewed_at": comic_sol._utc_now(),
                },
                "unresolved_warnings": warnings,
            }
            # Validate provider-authored material before normalization can publish files.
            for check in checks:
                if _repair_strategy.validate_defect_regions(check):
                    raise GatewayInputError("visual review defect regions are invalid")
            dimensions = (panel["rect"]["width"], panel["rect"]["height"])
            try:
                bindings = self._panel_bindings(root, panel_id)
                current = not _validation.validate_panel_provenance(
                    root, {"subject_id": panel_id, "bindings": bindings}
                )
                current = (
                    current and (bindings["clean_width"], bindings["clean_height"]) == dimensions
                )
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                current = False
            prepared = None
            if not current:
                prepared = _normalize_panels._prepare(
                    root,
                    _normalize_panels.NormalizationSpec(
                        panel_id, f"panels/raw/{panel_id}.png", dimensions, "exact"
                    ),
                )
                normalization = json.loads(prepared.record_bytes)
                source, clean = normalization["source"], normalization["clean"]
                normalization_path = f"panels/{panel_id}/normalization.json"
                record["bindings"] = {
                    "raw_path": source["path"],
                    "raw_sha256": source["sha256"],
                    "raw_width": source["size"][0],
                    "raw_height": source["size"][1],
                    "clean_path": clean["path"],
                    "clean_sha256": clean["sha256"],
                    "clean_width": clean["size"][0],
                    "clean_height": clean["size"][1],
                    "normalization_path": normalization_path,
                    "normalization_sha256": hashlib.sha256(prepared.record_bytes).hexdigest(),
                }
            else:
                record["bindings"] = self._panel_bindings(root, panel_id)
            issues = list(_validation.validate_panel_record(record))
            if prepared is None:
                issues += list(_validation.validate_panel_provenance(root, record))
            if issues or _character_quality.validate_character_quality_provenance(root, record):
                raise GatewayInputError("panel review provenance is invalid")
            with ProjectTransaction(root, "web-panel-review") as transaction:
                if prepared is not None:
                    transaction.stage_bytes(f"panels/{panel_id}/clean.png", prepared.clean_bytes)
                    transaction.stage_bytes(
                        f"panels/{panel_id}/normalization.json", prepared.record_bytes
                    )
                transaction.stage_bytes(
                    f"qa/panels/{panel_id}.json", canonical_artifact_bytes(record)
                )
                self._record_review_warnings(root, warnings, transaction)
            if failures:
                _repair_strategy.plan_and_write_repair_plan(root, localized_edit_supported=False)
                comic_sol.invalidate_from(root, "generation")
            elif self._panels_accepted(root):
                status = read_project_manifest(root / "project.json")["status"]
                if status == "STORYBOARDED" and comic_sol._references_ready(root):
                    comic_sol.transition(root, "REFERENCES_READY")
                    status = "REFERENCES_READY"
                if status == "REFERENCES_READY":
                    comic_sol.transition(root, "PANELS_READY")
                    status = "PANELS_READY"
                if status == "PANELS_READY":
                    comic_sol.transition(root, "QA_READY")
                comic_sol.record_stage(root, "generation")
            return self._review_snapshot(project_id, expected_revision, root)

    def prepare_pages(self, project_id: str, expected_revision: int) -> ProjectSnapshot:
        with self._review_project(project_id, expected_revision, mutation=True) as root:
            if not self._panels_accepted(root):
                raise GatewayInputError("all current panels require accepted QA before composition")
            status = read_project_manifest(root / "project.json")["status"]
            if status not in {"QA_READY", "LETTERED", "COMPOSED"}:
                raise GatewayInputError("project is not ready for deterministic page preparation")
            stale = {
                action.stage
                for action in comic_sol.build_resume_plan(root)
                if action.artifact == "stage" and action.action in {"regenerate", "rerun"}
            }
            if "lettering" in stale:
                _letter_panels.letter_project(root)
                comic_sol.record_stage(root, "lettering")
            if status == "QA_READY":
                comic_sol.transition(root, "LETTERED")
            if "composition" in stale:
                _compose_pages.compose_project(root)
                comic_sol.record_stage(root, "composition")
            if status != "COMPOSED":
                comic_sol.transition(root, "COMPOSED")
            return self._review_snapshot(project_id, expected_revision, root)

    @staticmethod
    def _storyboard_page(root: Path, page_number: int) -> dict:
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise GatewayInputError("page number is invalid")
        storyboard = read_contained_json(root, "plan/storyboard.json")
        page = next(
            (page for page in storyboard["pages"] if page.get("number") == page_number), None
        )
        if page is None:
            raise GatewayInputError("page is absent from the current storyboard")
        return cast(dict, page)

    def _require_composed_page(self, root: Path, page_number: int) -> dict:
        page = self._storyboard_page(root, page_number)
        if read_project_manifest(root / "project.json")["status"] not in {
            "COMPOSED",
            "EXPORTED",
            "COMPLETE",
            "COMPLETE_WITH_WARNINGS",
        } or not self._panels_accepted(root):
            raise GatewayInputError(
                "page review requires current composed pages and accepted panels"
            )
        return page

    def page_review_input(
        self, project_id: str, expected_revision: int, page_number: int
    ) -> VisualReviewRequest:
        with self._review_project(project_id, expected_revision) as root:
            page = self._require_composed_page(root, page_number)
            subject = f"page-{page_number:03d}"
            raster = read_contained_bytes(
                root, f"pages/{subject}.png", max_bytes=_raster_limits.MAX_ENCODED_RASTER_BYTES
            )
            context = {
                "project_id": project_id,
                "page": page,
                "page_sha256": hashlib.sha256(raster).hexdigest(),
                "lettering": {
                    panel["id"]: read_contained_json(root, f"panels/{panel['id']}/lettering.json")
                    for panel in page["panels"]
                },
            }
            return VisualReviewRequest(
                "page", subject, raster, context, _page_quality.SUBJECTIVE_PAGE_CHECK_IDS
            )

    def publish_page_review(
        self, project_id: str, expected_revision: int, page_number: int, review: VisualReviewResult
    ) -> ProjectSnapshot:
        with self._review_project(project_id, expected_revision, mutation=True) as root:
            self._require_composed_page(root, page_number)
            checks = self._review_checks(review, _page_quality.SUBJECTIVE_PAGE_CHECK_IDS)
            if review.character_assessments:
                raise GatewayInputError("page review cannot contain panel character assessments")
            self._validate_review_warnings(
                [
                    check["evidence"]
                    for check in checks
                    if check["result"] == "warning" or check["severity"] == "warning"
                ]
            )
            _page_quality.publish_page_quality_record(
                root,
                page_number,
                checks,
                reviewer=checks[0]["reviewer"],
                reviewed_at=comic_sol._utc_now(),
            )
            record = read_contained_json(root, f"qa/pages/page-{page_number:03d}.json")
            with ProjectTransaction(root, "web-page-review-warnings") as transaction:
                self._record_review_warnings(root, record["unresolved_warnings"], transaction)
            return self._review_snapshot(project_id, expected_revision, root)

    def finalize(self, project_id: str, expected_revision: int) -> tuple[ProjectSnapshot, Path]:
        with self._review_project(project_id, expected_revision, mutation=True) as root:
            manifest = read_project_manifest(root / "project.json")
            if not self._panels_accepted(root):
                raise GatewayInputError("finalization requires current accepted panel QA")
            for page_number in range(1, manifest["settings"]["page_count"] + 1):
                issues = _page_quality.validate_page_quality(root, page_number)
                if issues:
                    raise GatewayInputError("finalization requires current accepted page QA")
                record = read_contained_json(root, f"qa/pages/page-{page_number:03d}.json")
                if record.get("decision") not in {"accept", "accept-warning"}:
                    raise GatewayInputError("finalization requires current accepted page QA")
            comic_sol.finalize_project(root)
            pdf = contained_project_path(
                root, f"exports/{manifest['project_id']}.pdf", must_exist=True
            )
            if not read_contained_bytes(root, pdf.relative_to(root)).startswith(b"%PDF-"):
                raise GatewayError("finalization did not produce a verified PDF")
            return self._review_snapshot(project_id, expected_revision, root), pdf

    def export(
        self,
        project_id: str,
        expected_revision: int,
        formats: tuple[str, ...],
    ) -> Mapping[str, Path]:
        if not formats or len(set(formats)) != len(formats):
            raise GatewayInputError("export formats must be unique and non-empty")
        if not set(formats) <= {"archive", "pdf"}:
            raise GatewayInputError("unsupported project export format")
        initial = self._row(project_id)
        self._check_revision(cast(int, initial["revision"]), expected_revision)
        root = self._root_from_row(initial)
        engine_changed = False
        revision = expected_revision
        with ProjectLock(root):
            try:
                prior_state = self._engine_state(root)
                with self.database.transaction() as connection:
                    row = self._reconcile_row(connection, project_id, prior_state)
                    self._check_revision(cast(int, row["revision"]), expected_revision)
                    revision = cast(int, row["revision"])
                outputs: dict[str, Path] = {}
                if "pdf" in formats:
                    outputs["pdf"] = guarded_export(root)
                    engine_changed = True
                    revision += 1
                    self._ensure_revision(project_id, expected_revision, revision, root)
                if "archive" in formats:
                    export_directory = contained_project_path(self.exports_root, project_id)
                    export_directory.mkdir(exist_ok=True)
                    stem = f"{root.name}-r{revision}"
                    destination = export_directory / f"{stem}{ARCHIVE_SUFFIX}"
                    sequence = 2
                    while destination.exists():
                        destination = export_directory / f"{stem}-{sequence}{ARCHIVE_SUFFIX}"
                        sequence += 1
                    result = export_handoff_archive(root, destination)
                    archive_path = result.get("archive_path")
                    if not isinstance(archive_path, str):
                        raise GatewayError("archive export did not return an output path")
                    outputs["archive"] = Path(archive_path)
                return outputs
            except BaseException:
                if engine_changed:
                    self._ensure_revision(project_id, expected_revision, revision, root)
                raise
