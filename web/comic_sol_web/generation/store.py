"""Durable generation jobs plus append-only attempts and receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast

from comic_sol_web.database import Database
from comic_sol_web.generation.receipts import receipt_value
from comic_sol_web.generation.types import AuthMode, GenerationRequest, JobState

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REQUEST_JSON_BYTES = 1024 * 1024


class GenerationStoreError(ValueError):
    """Durable queue input or state is invalid."""


@dataclass(frozen=True)
class GenerationJob:
    job_id: str
    owner_id: str
    project_id: str
    project_revision: int
    request: GenerationRequest = field(repr=False)
    provider: str
    model: str
    auth_mode: AuthMode
    state: JobState
    attempt_number: int
    retry_count: int
    max_retries: int
    external_job_id: str | None
    staged_raster: Path | None
    result_checksum: str | None
    accepted_project_revision: int | None


@dataclass(frozen=True)
class LeasedJob:
    job: GenerationJob
    lease_token: str = field(repr=False)
    lease_expires_at: int


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(cast(str, item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise GenerationStoreError("generation request contains unsupported values")


def serialize_request(request: GenerationRequest) -> str:
    value = {
        "job_id": request.job_id,
        "project_id": request.project_id,
        "project_revision": request.project_revision,
        "subject_kind": request.subject_kind,
        "subject_id": request.subject_id,
        "prompt": request.prompt,
        "negative_prompt": request.negative_prompt,
        "references": [str(path) for path in request.references],
        "width": request.width,
        "height": request.height,
        "required_capabilities": sorted(request.required_capabilities),
        "provider_options": _plain(request.provider_options),
    }
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise GenerationStoreError("generation request cannot be persisted") from error
    if len(encoded.encode("utf-8")) > _MAX_REQUEST_JSON_BYTES:
        raise GenerationStoreError("generation request is too large")
    return encoded


def deserialize_request(value: str) -> GenerationRequest:
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError
        references = payload["references"]
        capabilities = payload["required_capabilities"]
        options = payload["provider_options"]
        if not isinstance(references, list) or not isinstance(capabilities, list):
            raise ValueError
        if not isinstance(options, dict):
            raise ValueError
        return GenerationRequest(
            job_id=payload["job_id"],
            project_id=payload["project_id"],
            project_revision=payload["project_revision"],
            subject_kind=payload["subject_kind"],
            subject_id=payload["subject_id"],
            prompt=payload["prompt"],
            negative_prompt=payload["negative_prompt"],
            references=tuple(Path(item) for item in references),
            width=payload["width"],
            height=payload["height"],
            required_capabilities=frozenset(capabilities),
            provider_options=options,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise GenerationStoreError("persisted generation request is invalid") from error


class GenerationStore:
    """Serialize durable queue state through the application database."""

    def __init__(self, database: Database, staging_root: Path) -> None:
        self.database = database
        self.staging_root = Path(staging_root)

    @staticmethod
    def validate_identifier(value: str, label: str) -> str:
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise GenerationStoreError(f"generation {label} is invalid")
        return value

    @classmethod
    def validate_enqueue_options(
        cls,
        owner_id: str,
        provider: str,
        model: str,
        max_retries: int,
    ) -> None:
        """Validate caller-controlled queue options before project mutation."""
        cls.validate_identifier(owner_id, "owner")
        cls.validate_identifier(provider, "provider")
        cls.validate_identifier(model, "model")
        if isinstance(max_retries, bool) or not 0 <= max_retries <= 10:
            raise GenerationStoreError("generation retry limit is invalid")

    @staticmethod
    def validate_job_id(job_id: str) -> str:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise GenerationStoreError("generation job is unavailable")
        return job_id

    @staticmethod
    def idempotency_key(
        owner_id: str,
        request_json: str,
        provider: str,
        model: str,
        auth_mode: AuthMode,
    ) -> str:
        digest = hashlib.sha256()
        for item in (owner_id, request_json, provider, model, auth_mode.value):
            digest.update(item.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def enqueue(
        self,
        *,
        owner_id: str,
        request: GenerationRequest,
        provider: str,
        model: str,
        auth_mode: AuthMode,
        max_retries: int,
        now: int,
    ) -> GenerationJob:
        self.validate_enqueue_options(owner_id, provider, model, max_retries)
        request_json = serialize_request(request)
        key = self.idempotency_key(owner_id, request_json, provider, model, auth_mode)
        job_id = key
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO generation_jobs (
                    job_id, idempotency_key, owner_id, project_id, project_revision,
                    request_json, provider, model, auth_mode, state, attempt_number,
                    retry_count, max_retries, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    job_id,
                    key,
                    owner_id,
                    request.project_id,
                    request.project_revision,
                    request_json,
                    provider,
                    model,
                    auth_mode.value,
                    JobState.QUEUED.value,
                    max_retries,
                    now,
                    now,
                ),
            )
            row = self._row(connection, job_id)
            if row is None:
                row = self._row_by_idempotency(connection, key)
            if row is None:
                raise GenerationStoreError("generation enqueue failed")
            if row["idempotency_key"] != key:
                raise GenerationStoreError("generation job identity collision")
        return self.from_row(row)

    @staticmethod
    def _row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

    @staticmethod
    def _row_by_idempotency(connection: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM generation_jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()

    def row(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        self.validate_job_id(job_id)
        return self._row(connection, job_id)

    def get(self, job_id: str) -> GenerationJob | None:
        self.validate_job_id(job_id)
        with self.database.read() as connection:
            row = self._row(connection, job_id)
        return None if row is None else self.from_row(row)

    def list_jobs(
        self,
        owner_id: str,
        project_id: str,
        *,
        limit: int,
    ) -> tuple[GenerationJob, ...]:
        self.validate_identifier(owner_id, "owner")
        self.validate_identifier(project_id, "project")
        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise GenerationStoreError("generation list limit is invalid")
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM generation_jobs
                WHERE owner_id = ? AND project_id = ?
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """,
                (owner_id, project_id, limit),
            ).fetchall()
        return tuple(self.from_row(row) for row in rows)

    def current_accepted(
        self,
        owner_id: str,
        project_id: str,
        accepted_project_revision: int,
    ) -> GenerationJob | None:
        self.validate_identifier(owner_id, "owner")
        self.validate_identifier(project_id, "project")
        if isinstance(accepted_project_revision, bool) or accepted_project_revision < 1:
            raise GenerationStoreError("accepted project revision is invalid")
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM generation_jobs
                WHERE owner_id = ? AND project_id = ? AND state = ?
                  AND accepted_project_revision <= ?
                  AND json_extract(request_json, '$.subject_kind') = 'panel'
                ORDER BY accepted_project_revision DESC, updated_at DESC, job_id DESC
                LIMIT 1
                """,
                (
                    owner_id,
                    project_id,
                    JobState.ACCEPTED.value,
                    accepted_project_revision,
                ),
            ).fetchone()
        return None if row is None else self.from_row(row)

    def from_row(self, row: sqlite3.Row) -> GenerationJob:
        staged_name = cast(str | None, row["staged_raster_name"])
        staged = None if staged_name is None else self.staging_root / staged_name
        return GenerationJob(
            job_id=cast(str, row["job_id"]),
            owner_id=cast(str, row["owner_id"]),
            project_id=cast(str, row["project_id"]),
            project_revision=cast(int, row["project_revision"]),
            request=deserialize_request(cast(str, row["request_json"])),
            provider=cast(str, row["provider"]),
            model=cast(str, row["model"]),
            auth_mode=AuthMode(cast(str, row["auth_mode"])),
            state=JobState(cast(str, row["state"])),
            attempt_number=cast(int, row["attempt_number"]),
            retry_count=cast(int, row["retry_count"]),
            max_retries=cast(int, row["max_retries"]),
            external_job_id=cast(str | None, row["external_job_id"]),
            staged_raster=staged,
            result_checksum=cast(str | None, row["result_checksum"]),
            accepted_project_revision=cast(int | None, row["accepted_project_revision"]),
        )

    @staticmethod
    def append_attempt(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        state: JobState,
        now: int,
        *,
        error_category: str | None = None,
        external_job_id: str | None = None,
        result_checksum: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO generation_attempts (
                job_id, attempt_number, provider, model, auth_mode, state,
                error_category, external_job_id, result_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["job_id"],
                row["attempt_number"],
                row["provider"],
                row["model"],
                row["auth_mode"],
                state.value,
                error_category,
                external_job_id,
                result_checksum,
                now,
            ),
        )

    @staticmethod
    def append_receipt(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        usage: Mapping[str, int | float | str],
        checksum: str,
        now: int,
    ) -> None:
        from comic_sol_web.generation.receipts import sanitize_usage

        sanitized = dict(sanitize_usage(usage))
        usage_json = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
        connection.execute(
            """
            INSERT OR IGNORE INTO generation_receipts (
                job_id, attempt_number, provider, model, auth_mode,
                usage_json, checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["job_id"],
                row["attempt_number"],
                row["provider"],
                row["model"],
                row["auth_mode"],
                usage_json,
                checksum,
                now,
            ),
        )

    def attempts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        self.validate_job_id(job_id)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT attempt_number, provider, model, auth_mode, state,
                       error_category, external_job_id, result_checksum, created_at
                FROM generation_attempts WHERE job_id = ? ORDER BY event_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def receipts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        self.validate_job_id(job_id)
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, auth_mode, usage_json, checksum
                FROM generation_receipts WHERE job_id = ? ORDER BY receipt_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(
            receipt_value(
                provider=cast(str, row["provider"]),
                model=cast(str, row["model"]),
                auth_mode=cast(str, row["auth_mode"]),
                usage=cast(dict[str, int | float | str], json.loads(row["usage_json"])),
                checksum=cast(str, row["checksum"]),
            )
            for row in rows
        )
