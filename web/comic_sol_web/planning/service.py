"""Lease-based, revision-bound planning with atomic canonical publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from uuid import UUID, uuid4

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import (
    GatewayInputError,
    ProjectUnavailableError,
    StaleProjectRevisionError,
)
from comic_sol_web.generation.credentials import CredentialBroker, CredentialBrokerError
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.types import AuthMode
from comic_sol_web.migrations import PLANNING_MIGRATIONS, apply_migrations
from comic_sol_web.projects import ProjectService

from .types import PlanResult, PlanningModel, PlanningProvider

_SAFE_VALIDATION_ERROR = "The complete four-document Plan must pass canonical validation."


class PlanningConflictError(ValueError):
    """A planning request conflicts with its durable identity or lease."""


@dataclass(frozen=True)
class PlanningJob:
    job_id: str
    owner_id: str = field(repr=False)
    idempotency_key: str = field(repr=False)
    project_id: str
    project_revision: int
    provider: str
    model: str
    state: str
    attempt_count: int
    usage: Mapping[str, int | float]
    error_category: str | None
    published_revision: int | None
    lease_token: str | None = field(repr=False)
    publication_sha256: str | None = field(repr=False)


def _job(row: sqlite3.Row) -> PlanningJob:
    return PlanningJob(
        **{
            key: row[key]
            for key in (
                "job_id",
                "owner_id",
                "idempotency_key",
                "project_id",
                "project_revision",
                "provider",
                "model",
                "state",
                "attempt_count",
                "error_category",
                "published_revision",
                "lease_token",
                "publication_sha256",
            )
        },
        usage=MappingProxyType(json.loads(row["usage_json"])),
    )


def _digest(plan: Mapping[str, str]) -> str:
    """Hash parsed documents so canonical whitespace does not affect recovery."""
    documents = {key: json.loads(value) for key, value in plan.items()}
    encoded = json.dumps(documents, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PlanningService:
    def __init__(
        self,
        database: Database,
        projects: ProjectService,
        providers: Sequence[PlanningProvider],
        credentials: CredentialBroker,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        lease_seconds: int = 180,
        model_options: Sequence[PlanningModel] | None = None,
    ) -> None:
        if not 60 <= lease_seconds <= 600:
            raise ValueError("planning lease duration is invalid")
        self.database = database
        self.projects = projects
        self.providers = {
            (provider.provider_id, provider.model): provider for provider in providers
        }
        if len(self.providers) != len(providers):
            raise ValueError("planning providers are duplicated")
        self.credentials = credentials
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._options = (
            tuple(model_options)
            if model_options is not None
            else tuple(
                PlanningModel(provider, model, True, None) for provider, model in self.providers
            )
        )
        apply_migrations(database, PLANNING_MIGRATIONS)

    def options(self) -> tuple[PlanningModel, ...]:
        return self._options

    def get(self, principal: SessionPrincipal, job_id: str) -> PlanningJob:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM planning_jobs WHERE job_id = ? AND owner_id = ?",
                (job_id, principal.user_id),
            ).fetchone()
        if row is None:
            raise ProjectUnavailableError("planning job unavailable")
        self.projects._authorize(principal, row["project_id"])
        return _job(row)

    def queue(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        provider: str,
        model: str,
        idempotency_key: str,
    ) -> PlanningJob:
        try:
            canonical_key = str(UUID(idempotency_key))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("planning idempotency key is invalid") from None
        if canonical_key != idempotency_key:
            raise ValueError("planning idempotency key is invalid")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("planning revision is invalid")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise ValueError("planning provider is invalid")
        self.projects._authorize(principal, project_id)
        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT * FROM planning_jobs WHERE owner_id = ? AND idempotency_key = ?",
                (principal.user_id, canonical_key),
            ).fetchone()
        if existing is not None:
            if (
                existing["project_id"],
                existing["project_revision"],
                existing["provider"],
                existing["model"],
            ) != (
                project_id,
                expected_revision,
                provider,
                model,
            ):
                raise PlanningConflictError("planning idempotency conflict")
            return _job(existing)
        if (provider, model) not in self.providers:
            raise ValueError("planning provider is unavailable")
        job_id = str(uuid4())
        now = self._clock()
        try:
            with self.projects.planning_publication(principal, project_id):
                self.projects.planning_input(principal, project_id, expected_revision)
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO planning_jobs (job_id, idempotency_key, owner_id, project_id, "
                        "project_revision, provider, model, state, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (
                            job_id,
                            canonical_key,
                            principal.user_id,
                            project_id,
                            expected_revision,
                            provider,
                            model,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError:
            # A concurrent identical request may have won the insertion.
            with self.database.read() as connection:
                winner = connection.execute(
                    "SELECT * FROM planning_jobs WHERE owner_id = ? AND idempotency_key = ?",
                    (principal.user_id, canonical_key),
                ).fetchone()
            if winner is not None and (
                winner["project_id"],
                winner["project_revision"],
                winner["provider"],
                winner["model"],
            ) == (project_id, expected_revision, provider, model):
                return _job(winner)
            raise PlanningConflictError("planning job already active") from None
        return self.get(principal, job_id)

    def _lease(self, worker_id: str) -> PlanningJob | None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("planning worker is invalid")
        now = self._clock()
        with self.database.read() as connection:
            candidate = connection.execute(
                "SELECT * FROM planning_jobs WHERE state = 'queued' OR "
                "(state IN ('running', 'repairing') AND lease_expires_at <= ?) "
                "ORDER BY created_at, job_id LIMIT 1",
                (now,),
            ).fetchone()
        if candidate is None:
            return None
        principal = SessionPrincipal(candidate["owner_id"], "planning-worker")
        # Lock ordering matches canonical publication: project first, then DB.
        with self.projects.planning_publication(principal, candidate["project_id"]):
            now = self._clock()
            with self.database.transaction() as connection:
                token = str(uuid4())
                cursor = connection.execute(
                    "UPDATE planning_jobs SET state = CASE WHEN attempt_count = 0 THEN 'running' "
                    "ELSE 'repairing' END, lease_token = ?, lease_owner = ?, lease_expires_at = ?, "
                    "started_at = COALESCE(started_at, ?), updated_at = ? WHERE job_id = ? AND (state = 'queued' OR "
                    "(state IN ('running', 'repairing') AND lease_expires_at <= ?))",
                    (token, worker_id, now + self._lease_seconds, now, now, candidate["job_id"], now),
                )
                if cursor.rowcount != 1:
                    return None
                row = connection.execute(
                    "SELECT * FROM planning_jobs WHERE job_id = ?", (candidate["job_id"],)
                ).fetchone()
        return _job(row)

    def _owned(self, connection: sqlite3.Connection, job: PlanningJob) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM planning_jobs WHERE job_id = ? AND lease_token = ? "
            "AND lease_expires_at > ? AND state IN ('running', 'repairing')",
            (job.job_id, job.lease_token, self._clock()),
        ).fetchone()
        if row is None:
            raise PlanningConflictError("planning lease is no longer owned")
        return row

    def _finish(
        self,
        job: PlanningJob,
        state: str,
        *,
        error: str | None = None,
        published_revision: int | None = None,
    ) -> PlanningJob:
        with self.database.transaction() as connection:
            self._owned(connection, job)
            connection.execute(
                "UPDATE planning_jobs SET state = ?, error_category = ?, published_revision = ?, "
                "lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL, completed_at = ?, updated_at = ? "
                "WHERE job_id = ?",
                (state, error, published_revision, self._clock(), self._clock(), job.job_id),
            )
        return self.get(SessionPrincipal(job.owner_id, "planning-worker"), job.job_id)

    def _record_usage(self, job: PlanningJob, result: PlanResult) -> None:
        with self.database.transaction() as connection:
            row = self._owned(connection, job)
            usage = json.loads(row["usage_json"])
            for key, value in result.usage.items():
                usage[key] = min(1_000_000_000_000, usage.get(key, 0) + value)
            connection.execute(
                "UPDATE planning_jobs SET usage_json = ?, updated_at = ? WHERE job_id = ?",
                (
                    json.dumps(usage, sort_keys=True, separators=(",", ":")),
                    self._clock(),
                    job.job_id,
                ),
            )

    async def run_once(self, worker_id: str) -> PlanningJob | None:
        job = self._lease(worker_id)
        if job is None:
            return None
        principal = SessionPrincipal(job.owner_id, "planning-worker")
        try:
            with self.projects.planning_publication(principal, job.project_id):
                with self.database.read() as connection:
                    self._owned(connection, job)
                if job.publication_sha256:
                    snapshot = self.projects.read_plan(principal, job.project_id)
                    plan = snapshot.summary.get("plan")
                    if (
                        isinstance(plan, Mapping)
                        and all(plan.values())
                        and _digest(plan) == job.publication_sha256
                    ):
                        if snapshot.revision in (job.project_revision, job.project_revision + 1):
                            return self._finish(
                                job, "ready_for_review", published_revision=snapshot.revision
                            )
                request = self.projects.planning_input(
                    principal, job.project_id, job.project_revision
                )
            provider = self.providers.get((job.provider, job.model))
            if provider is None:
                return self._finish(job, "failed", error="unavailable")
            for attempt in range(job.attempt_count, 2):
                if attempt:
                    request = replace(request, validation_errors=(_SAFE_VALIDATION_ERROR,))
                with self.database.transaction() as connection:
                    self._owned(connection, job)
                    connection.execute(
                        "UPDATE planning_jobs SET attempt_count = ?, state = ?, lease_expires_at = ?, "
                        "publication_sha256 = NULL, updated_at = ? WHERE job_id = ?",
                        (
                            attempt + 1,
                            "repairing" if attempt else "running",
                            self._clock() + self._lease_seconds,
                            self._clock(),
                            job.job_id,
                        ),
                    )
                async with self.credentials.resolve(
                    job.owner_id, job.provider, AuthMode.HOSTED
                ) as credential:
                    if credential is None:
                        return self._finish(job, "failed", error="invalid_credentials")
                    async with asyncio.timeout(min(60, self._lease_seconds - 5)):
                        result = await provider.generate_plan(request, credential)
                if not isinstance(result, PlanResult):
                    return self._finish(job, "failed", error="invalid_output")
                self._record_usage(job, result)
                try:
                    publication_sha256 = _digest(result.plan)
                except ValueError:
                    continue
                with self.projects.planning_publication(principal, job.project_id):
                    with self.database.transaction() as connection:
                        self._owned(connection, job)
                        connection.execute(
                            "UPDATE planning_jobs SET publication_sha256 = ? WHERE job_id = ?",
                            (publication_sha256, job.job_id),
                        )
                    try:
                        snapshot = self.projects.update_plan(
                            principal, job.project_id, job.project_revision, result.plan
                        )
                    except GatewayInputError:
                        continue
                    return self._finish(
                        job, "ready_for_review", published_revision=snapshot.revision
                    )
            return self._finish(job, "failed", error="invalid_output")
        except PlanningConflictError:
            raise
        except StaleProjectRevisionError:
            return self._finish(job, "failed", error="stale_revision")
        except CredentialBrokerError:
            return self._finish(job, "failed", error="invalid_credentials")
        except ProviderError as error:
            return self._finish(job, "failed", error=error.category.value)
        except TimeoutError:
            return self._finish(job, "failed", error="timeout")
        except Exception:
            return self._finish(job, "failed", error="provider_error")
