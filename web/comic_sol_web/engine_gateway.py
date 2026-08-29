"""The sole Comic Sol Web boundary to deterministic project engine state."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import stat
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

comic_sol = _load_engine_module("comic_sol")
_character_identity = _load_engine_module("character_identity")
_core_primitives = _load_engine_module("core_primitives")
_export_pdf = _load_engine_module("export_pdf")
_handoff = _load_engine_module("handoff")
_handoff_archive = _load_engine_module("handoff_archive")
_input_limits = _load_engine_module("input_limits")
_project_io = _load_engine_module("project_io")
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
                extra_summary={"qa": qa},
            )

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
