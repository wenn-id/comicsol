"""Durable, request-driven orchestration over existing Comic Sol boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import (
    GatewayInputError,
    StaleProjectRevisionError,
)
from comic_sol_web.generation.credentials import (
    CredentialBrokerError,
    CredentialUnavailableError,
)
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.service import (
    GenerationConflictError,
    GenerationUnavailableError,
    RetryLimitError,
)
from comic_sol_web.generation.types import AuthMode, ErrorCategory, JobState
from comic_sol_web.migrations import WORKFLOW_MIGRATIONS, apply_migrations
from comic_sol_web.security import redact_text


class WorkflowConflictError(ValueError):
    """A workflow request is stale or conflicts with durable state."""


class WorkflowUnavailableError(ValueError):
    """The owner-visible workflow does not exist."""


@dataclass(frozen=True)
class WorkflowSnapshot:
    project_id: str
    revision: int
    state: str
    phase: str
    planning_job_id: str
    planning_provider: str
    planning_model: str
    image_provider: str
    image_model: str
    image_auth_mode: str
    error_category: str | None
    extra_calls: int


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: int
    project_id: str
    project_revision: int
    type: str
    phase: str
    status: str
    provider: str | None
    model: str | None
    attempt: int | None
    progress: Mapping[str, object]
    summary: str
    created_at: int


_EVENT_TYPES = frozenset(
    {
        "planning.started",
        "planning.repairing",
        "plan.validated",
        "workflow.waiting_for_plan_approval",
        "generation.reference_started",
        "generation.panel_completed",
        "qa.panel_failed",
        "qa.panel_passed",
        "generation.panel_retrying",
        "composition.completed",
        "qa.page_passed",
        "qa.page_failed",
        "export.ready",
        "workflow.blocked",
        "workflow.resumed",
        "workflow.paused",
        "workflow.complete",
    }
)
_PROGRESS_KEYS = frozenset(
    {
        "completed",
        "extra_calls",
        "job_id",
        "max_retries",
        "page_number",
        "project_revision",
        "retry_count",
        "subject_id",
        "subject_kind",
        "total",
    }
)
_TERMINAL_ENGINE_STATES = frozenset({"COMPLETE", "COMPLETE_WITH_WARNINGS"})
_ACTIVE_JOB_STATES = frozenset(
    {JobState.QUEUED, JobState.RUNNING, JobState.POLLING}
)


def _snapshot(row: sqlite3.Row) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        project_id=cast(str, row["project_id"]),
        revision=cast(int, row["current_revision"]),
        state=cast(str, row["state"]),
        phase=cast(str, row["phase"]),
        planning_job_id=cast(str, row["planning_job_id"]),
        planning_provider=cast(str, row["planning_provider"]),
        planning_model=cast(str, row["planning_model"]),
        image_provider=cast(str, row["image_provider"]),
        image_model=cast(str, row["image_model"]),
        image_auth_mode=cast(str, row["image_auth_mode"]),
        error_category=cast(str | None, row["error_category"]),
        extra_calls=cast(int, row["extra_calls"]),
    )


def _event(row: sqlite3.Row) -> WorkflowEvent:
    progress = json.loads(cast(str, row["progress_json"]))
    return WorkflowEvent(
        event_id=cast(int, row["event_id"]),
        project_id=cast(str, row["project_id"]),
        project_revision=cast(int, row["project_revision"]),
        type=cast(str, row["type"]),
        phase=cast(str, row["phase"]),
        status=cast(str, row["status"]),
        provider=cast(str | None, row["provider"]),
        model=cast(str | None, row["model"]),
        attempt=cast(int | None, row["attempt"]),
        progress=MappingProxyType(progress),
        summary=cast(str, row["summary"]),
        created_at=cast(int, row["created_at"]),
    )


class WorkflowService:
    """Coordinate durable services without owning canonical project artifacts."""

    def __init__(
        self,
        database: Database,
        projects: Any,
        planning: Any,
        generation: Any,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        lease_seconds: int = 180,
    ) -> None:
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 600:
            raise ValueError("workflow lease duration is invalid")
        self.database = database
        self.projects = projects
        self.planning = planning
        self.generation = generation
        self.providers = getattr(planning, "providers", {})
        self.credentials = getattr(planning, "credentials", None)
        self._clock = clock
        self._lease_seconds = lease_seconds
        apply_migrations(database, WORKFLOW_MIGRATIONS)

    @staticmethod
    def _uuid(value: str, label: str) -> str:
        try:
            canonical = str(UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise ValueError(f"workflow {label} is invalid") from None
        if value.lower() != canonical:
            raise ValueError(f"workflow {label} is invalid")
        return canonical

    @staticmethod
    def _selection(provider: str, model: str, auth_mode: str) -> AuthMode:
        if (
            not isinstance(provider, str)
            or not 1 <= len(provider) <= 64
            or not isinstance(model, str)
            or not 1 <= len(model) <= 128
        ):
            raise ValueError("workflow image selection is invalid")
        try:
            mode = AuthMode(auth_mode)
        except (TypeError, ValueError):
            raise ValueError("workflow image selection is invalid") from None
        if provider == "openai" and mode is not AuthMode.HOSTED:
            raise ValueError("OpenAI generation requires hosted authentication mode")
        return mode

    def _row(self, project_id: str, owner_id: str | None = None) -> sqlite3.Row:
        with self.database.read() as connection:
            if owner_id is None:
                row = connection.execute(
                    "SELECT * FROM production_workflows WHERE project_id = ?", (project_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM production_workflows WHERE project_id = ? AND owner_id = ?",
                    (project_id, owner_id),
                ).fetchone()
        if row is None:
            raise WorkflowUnavailableError("workflow unavailable")
        return row

    def snapshot(self, principal: SessionPrincipal, project_id: str) -> WorkflowSnapshot:
        self.projects.snapshot(principal, project_id)
        return _snapshot(self._row(project_id, principal.user_id))

    def approve_plan(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        *,
        planning_job_id: str,
        image_provider: str,
        image_model: str,
        image_auth_mode: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        key = self._uuid(idempotency_key, "idempotency key")
        mode = self._selection(image_provider, image_model, image_auth_mode)
        job = self.planning.get(principal, planning_job_id)
        with self.database.read() as connection:
            replay = connection.execute(
                "SELECT * FROM production_workflows "
                "WHERE owner_id = ? AND approval_idempotency_key = ?",
                (principal.user_id, key),
            ).fetchone()
        if replay is not None:
            identity = (
                replay["project_id"], replay["planning_job_id"],
                replay["image_provider"], replay["image_model"], replay["image_auth_mode"],
            )
            if identity != (
                project_id, planning_job_id, image_provider, image_model, mode.value,
            ) or job.published_revision != expected_revision:
                raise WorkflowConflictError("workflow idempotency conflict")
            return _snapshot(replay)

        self.projects.snapshot(principal, project_id, expected_revision)
        if (
            job.project_id != project_id
            or job.state != "ready_for_review"
            or job.published_revision != expected_revision
        ):
            raise WorkflowConflictError("workflow requires a reviewed current Plan")
        options = tuple(getattr(self.generation, "_runtime_options", lambda: ())())
        if not any(
            option.provider == image_provider and option.model == image_model for option in options
        ):
            raise ValueError("workflow image selection is unavailable")

        now = self._clock()
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO production_workflows (
                        project_id, owner_id, current_revision, state, phase,
                        planning_job_id, planning_provider, planning_model,
                        image_provider, image_model, image_auth_mode,
                        approval_idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', 'references', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        principal.user_id,
                        expected_revision,
                        planning_job_id,
                        job.provider,
                        job.model,
                        image_provider,
                        image_model,
                        mode.value,
                        key,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM production_workflows WHERE project_id = ?", (project_id,)
                ).fetchone()
                assert row is not None
                self._append_event(
                    connection,
                    row,
                    "plan.validated",
                    "Reviewed Plan approved; image generation may begin.",
                    provider=job.provider,
                    model=job.model,
                    progress={"project_revision": expected_revision},
                )
        except sqlite3.IntegrityError as error:
            with self.database.read() as connection:
                existing = connection.execute(
                    "SELECT * FROM production_workflows WHERE project_id = ?", (project_id,)
                ).fetchone()
            if existing is not None:
                raise WorkflowConflictError("project production already started") from error
            raise
        return self.snapshot(principal, project_id)

    @staticmethod
    def _progress_json(progress: Mapping[str, object] | None) -> str:
        value = {} if progress is None else dict(progress)
        if not set(value) <= _PROGRESS_KEYS:
            raise ValueError("workflow event progress is invalid")
        for item in value.values():
            if isinstance(item, bool) or item is None:
                continue
            if isinstance(item, int) and 0 <= item <= 1_000_000_000:
                continue
            if isinstance(item, str) and 1 <= len(item) <= 128 and "\n" not in item:
                continue
            raise ValueError("workflow event progress is invalid")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError("workflow event progress is invalid")
        return encoded

    def _append_event(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event_type: str,
        summary: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        attempt: int | None = None,
        progress: Mapping[str, object] | None = None,
        phase: str | None = None,
        status: str | None = None,
        revision: int | None = None,
    ) -> None:
        if event_type not in _EVENT_TYPES:
            raise ValueError("workflow event type is invalid")
        safe_summary = redact_text(summary.replace("\r", " ").replace("\n", " "), limit=512)
        if not safe_summary:
            raise ValueError("workflow event summary is invalid")
        connection.execute(
            """
            INSERT INTO workflow_events (
                owner_id, project_id, project_revision, type, phase, status,
                provider, model, attempt, progress_json, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["owner_id"],
                row["project_id"],
                row["current_revision"] if revision is None else revision,
                event_type,
                row["phase"] if phase is None else phase,
                row["state"] if status is None else status,
                provider,
                model,
                attempt,
                self._progress_json(progress),
                safe_summary,
                self._clock(),
            ),
        )

    def events_after(
        self,
        principal: SessionPrincipal,
        project_id: str,
        cursor: int,
        *,
        limit: int = 100,
    ) -> tuple[WorkflowEvent, ...]:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("workflow event cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("workflow event limit is invalid")
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE owner_id = ? AND project_id = ? AND event_id > ?
                ORDER BY event_id LIMIT ?
                """,
                (principal.user_id, project_id, cursor, limit),
            ).fetchall()
        return tuple(_event(row) for row in rows)

    def _set_state(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        target: str,
    ) -> WorkflowSnapshot:
        try:
            self.projects.snapshot(principal, project_id, expected_revision)
        except StaleProjectRevisionError as stale:
            raise WorkflowConflictError("workflow revision is stale") from stale
        now = self._clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM production_workflows WHERE project_id = ? AND owner_id = ?",
                (project_id, principal.user_id),
            ).fetchone()
            if row is None:
                raise WorkflowUnavailableError("workflow unavailable")
            if row["current_revision"] != expected_revision or row["state"] == "complete":
                raise WorkflowConflictError("workflow revision is stale")
            if target == "paused":
                if row["state"] == "paused":
                    return _snapshot(row)
                event_type, summary = "workflow.paused", "Production paused."
                state_error = row["error_category"]
            else:
                if row["state"] == "running":
                    return _snapshot(row)
                event_type, summary = "workflow.resumed", "Production resumed."
                state_error = None
            connection.execute(
                "UPDATE production_workflows SET state = ?, error_category = ?, "
                "lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE project_id = ?",
                (target, state_error, now, project_id),
            )
            current = connection.execute(
                "SELECT * FROM production_workflows WHERE project_id = ?", (project_id,)
            ).fetchone()
            assert current is not None
            self._append_event(connection, current, event_type, summary)
        return self.snapshot(principal, project_id)

    def pause(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> WorkflowSnapshot:
        return self._set_state(principal, project_id, expected_revision, "paused")

    def resume(
        self, principal: SessionPrincipal, project_id: str, expected_revision: int
    ) -> WorkflowSnapshot:
        return self._set_state(principal, project_id, expected_revision, "running")

    def _lease(self, worker_id: str, lease_seconds: int) -> sqlite3.Row | None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 128:
            raise ValueError("workflow worker is invalid")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 600:
            raise ValueError("workflow lease duration is invalid")
        now = self._clock()
        with self.database.transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM production_workflows WHERE state = 'running' "
                "AND (lease_token IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at, project_id LIMIT 1",
                (now,),
            ).fetchone()
            if candidate is None:
                return None
            recovered = candidate["lease_token"] is not None
            token = str(uuid4())
            updated = connection.execute(
                "UPDATE production_workflows SET lease_token = ?, lease_owner = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE project_id = ? AND state = 'running' "
                "AND (lease_token IS NULL OR lease_expires_at <= ?)",
                (token, worker_id, now + lease_seconds, now, candidate["project_id"], now),
            ).rowcount
            if updated != 1:
                return None
            row = connection.execute(
                "SELECT * FROM production_workflows WHERE project_id = ?",
                (candidate["project_id"],),
            ).fetchone()
            assert row is not None
            if recovered:
                self._append_event(
                    connection,
                    row,
                    "workflow.resumed",
                    "Expired work lease reclaimed after restart.",
                )
            return row

    def _release(self, project_id: str, token: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE production_workflows SET lease_token = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE project_id = ? AND lease_token = ?",
                (self._clock(), project_id, token),
            )

    def _advance_row(
        self,
        row: sqlite3.Row,
        token: str,
        *,
        phase: str | None = None,
        revision: int | None = None,
        state: str = "running",
        error: str | None = None,
        extra_calls: int | None = None,
        event: tuple[str, str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        attempt: int | None = None,
        progress: Mapping[str, object] | None = None,
    ) -> WorkflowSnapshot:
        next_phase = row["phase"] if phase is None else phase
        next_revision = row["current_revision"] if revision is None else revision
        next_extra = row["extra_calls"] if extra_calls is None else extra_calls
        completed_at = self._clock() if state == "complete" else None
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE production_workflows
                SET current_revision = ?, state = ?, phase = ?, error_category = ?,
                    extra_calls = ?, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?, completed_at = ?
                WHERE project_id = ? AND lease_token = ? AND state = 'running'
                """,
                (
                    next_revision,
                    state,
                    next_phase,
                    error,
                    next_extra,
                    self._clock(),
                    completed_at,
                    row["project_id"],
                    token,
                ),
            ).rowcount
            if updated != 1:
                raise WorkflowConflictError("workflow advancement lost its lease")
            current = connection.execute(
                "SELECT * FROM production_workflows WHERE project_id = ?",
                (row["project_id"],),
            ).fetchone()
            assert current is not None
            if event is not None:
                self._append_event(
                    connection,
                    current,
                    event[0],
                    event[1],
                    provider=provider,
                    model=model,
                    attempt=attempt,
                    progress=progress,
                )
        return _snapshot(current)

    def _still_running(self, project_id: str, token: str) -> bool:
        with self.database.read() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM production_workflows "
                    "WHERE project_id = ? AND state = 'running' AND lease_token = ?",
                    (project_id, token),
                ).fetchone()
                is not None
            )

    def _latest_jobs(self, principal: SessionPrincipal, row: sqlite3.Row) -> tuple[Any, ...]:
        jobs = self.generation.list_jobs(
            principal, row["project_id"], row["current_revision"], limit=50
        )
        latest: dict[str, Any] = {}
        for job in jobs:
            artifact = getattr(job.request, "job_id", job.job_id)
            latest.setdefault(artifact, job)
        return tuple(latest.values())

    def _reviewed(self, row: sqlite3.Row, job_id: str) -> bool:
        pattern = f'%"job_id":"{job_id}"%'
        with self.database.read() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM workflow_events WHERE owner_id = ? AND project_id = ? "
                    "AND type IN ('qa.panel_passed', 'qa.panel_failed') "
                    "AND progress_json LIKE ? LIMIT 1",
                    (row["owner_id"], row["project_id"], pattern),
                ).fetchone()
                is not None
            )

    def _panel_failures(self, row: sqlite3.Row, subject_id: str) -> int:
        pattern = f'%"subject_id":"{subject_id}"%'
        with self.database.read() as connection:
            return cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_events WHERE owner_id = ? AND project_id = ? "
                    "AND type = 'qa.panel_failed' AND progress_json LIKE ?",
                    (row["owner_id"], row["project_id"], pattern),
                ).fetchone()[0],
            )

    @staticmethod
    def _review_failed(review: Any) -> bool:
        return any(
            check.get("result") == "fail" and check.get("severity") == "error"
            for check in review.checks
        )

    async def _visual_review(self, row: sqlite3.Row, request: Any) -> Any:
        provider = self.providers.get((row["planning_provider"], row["planning_model"]))
        if provider is None or self.credentials is None:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        async with self.credentials.resolve(
            row["owner_id"], row["planning_provider"], AuthMode.HOSTED
        ) as credential:
            if credential is None:
                raise CredentialUnavailableError("planning credential is unavailable")
            async with asyncio.timeout(max(1, min(60, self._lease_seconds))):
                return await provider.review_visual(request, credential)

    def _block(
        self, row: sqlite3.Row, token: str, category: str, summary: str
    ) -> WorkflowSnapshot:
        return self._advance_row(
            row,
            token,
            state="blocked",
            error=category,
            event=("workflow.blocked", summary),
            provider=row["planning_provider"],
            model=row["planning_model"],
        )

    async def _generation_step(
        self, principal: SessionPrincipal, row: sqlite3.Row, token: str
    ) -> WorkflowSnapshot:
        jobs = self._latest_jobs(principal, row)
        validating = next(
            (
                job
                for job in jobs
                if job.state is JobState.VALIDATING
                and job.project_revision == row["current_revision"]
            ),
            None,
        )
        if validating is not None:
            if not self._still_running(row["project_id"], token):
                raise WorkflowConflictError("workflow paused before promotion")
            accepted = self.generation.submit_staged_raster(
                principal, validating.job_id, validating.project_revision
            )
            revision = accepted.accepted_project_revision or self.projects.snapshot(
                principal, row["project_id"]
            ).revision
            kind = validating.request.subject_kind
            event = None
            progress = None
            if kind == "panel":
                event = ("generation.panel_completed", "Panel raster accepted for visual QA.")
                progress = {
                    "job_id": validating.job_id,
                    "subject_id": validating.request.subject_id,
                    "subject_kind": kind,
                }
            return self._advance_row(
                row,
                token,
                phase="panels" if kind == "panel" else "references",
                revision=revision,
                event=event,
                provider=validating.provider,
                model=validating.model,
                attempt=validating.attempt_number,
                progress=progress,
            )

        active = next(
            (
                job
                for job in jobs
                if job.state in _ACTIVE_JOB_STATES
                and job.project_revision == row["current_revision"]
            ),
            None,
        )
        if active is not None:
            result = await self.generation.run_once(
                f"workflow-{row['project_id']}", lease_seconds=min(60, self._lease_seconds)
            )
            if result is not None and result.project_id == row["project_id"]:
                if result.state in {
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.AWAITING_PROVIDER_CONFIRMATION,
                }:
                    category = "provider_error"
                    attempts = self.generation.attempts(result.job_id)
                    if attempts and isinstance(attempts[-1].get("error_category"), str):
                        category = attempts[-1]["error_category"]
                    return self._block(
                        row, token, category, "Image generation requires user attention."
                    )
            return self._advance_row(row, token)

        try:
            queued = self.generation.queue(
                principal,
                row["project_id"],
                row["current_revision"],
                provider=row["image_provider"],
                model=row["image_model"],
                auth_mode=row["image_auth_mode"],
                max_retries=2,
            )
        except GatewayInputError:
            raise
        except (KeyError, ValueError):
            return self._block(
                row,
                token,
                "capability_missing",
                "Approved image provider is unavailable; no fallback was selected.",
            )
        current_revision = self.projects.snapshot(principal, row["project_id"]).revision
        ready = tuple(
            job
            for job in queued
            if job.state not in {JobState.ACCEPTED, JobState.FAILED, JobState.CANCELLED}
        )
        if ready:
            first = ready[0]
            kind = first.request.subject_kind
            event = (
                ("generation.reference_started", "Reference image generation queued.")
                if kind == "reference"
                else None
            )
            return self._advance_row(
                row,
                token,
                phase="references" if kind == "reference" else "panels",
                revision=current_revision,
                event=event,
                provider=first.provider,
                model=first.model,
                attempt=first.attempt_number,
                progress={
                    "job_id": first.job_id,
                    "subject_id": first.request.subject_id,
                    "subject_kind": kind,
                }
                if event
                else None,
            )

        accepted_panels = tuple(
            job
            for job in jobs
            if job.state is JobState.ACCEPTED and job.request.subject_kind == "panel"
        )
        if accepted_panels:
            return self._advance_row(row, token, phase="panel-qa", revision=current_revision)
        return self._block(
            row, token, "invalid_output", "No generation work was available for the current Plan."
        )

    async def _panel_qa_step(
        self, principal: SessionPrincipal, row: sqlite3.Row, token: str
    ) -> WorkflowSnapshot:
        jobs = self._latest_jobs(principal, row)
        candidates = tuple(
            job
            for job in jobs
            if job.state is JobState.ACCEPTED
            and job.request.subject_kind == "panel"
            and not self._reviewed(row, job.job_id)
        )
        if not candidates:
            return self._advance_row(row, token, phase="lettering")
        job = candidates[0]
        request = self.projects.panel_review_input(
            principal, row["project_id"], row["current_revision"], job.request.subject_id
        )
        review = await self._visual_review(row, request)
        published = self.projects.publish_panel_review(
            principal,
            row["project_id"],
            row["current_revision"],
            job.request.subject_id,
            review,
        )
        failed = self._review_failed(review)
        progress = {
            "job_id": job.job_id,
            "subject_id": job.request.subject_id,
            "subject_kind": "panel",
        }
        if not failed:
            return self._advance_row(
                row,
                token,
                revision=published.revision,
                event=("qa.panel_passed", "Panel passed visual QA."),
                provider=row["planning_provider"],
                model=row["planning_model"],
                attempt=job.attempt_number,
                progress=progress,
            )

        failures = self._panel_failures(row, job.request.subject_id)
        exhausted = failures >= 2 or row["extra_calls"] >= 8
        next_extra = row["extra_calls"] if exhausted else row["extra_calls"] + 1
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM production_workflows WHERE project_id = ? AND lease_token = ?",
                (row["project_id"], token),
            ).fetchone()
            if current is None:
                raise WorkflowConflictError("workflow advancement lost its lease")
            self._append_event(
                connection,
                current,
                "qa.panel_failed",
                "Panel failed visual QA.",
                provider=row["planning_provider"],
                model=row["planning_model"],
                attempt=job.attempt_number,
                progress=progress,
                revision=published.revision,
            )
        if exhausted:
            return self._advance_row(
                row,
                token,
                revision=published.revision,
                state="blocked",
                error="retry_exhausted",
                extra_calls=next_extra,
                event=("workflow.blocked", "Visual retry budget exhausted."),
            )
        return self._advance_row(
            row,
            token,
            phase="panels",
            revision=published.revision,
            extra_calls=next_extra,
            event=("generation.panel_retrying", "Failed panel queued for bounded retry."),
            provider=row["image_provider"],
            model=row["image_model"],
            progress={
                "subject_id": job.request.subject_id,
                "subject_kind": "panel",
                "extra_calls": next_extra,
                "max_retries": 2,
            },
        )

    async def _page_qa_step(
        self, principal: SessionPrincipal, row: sqlite3.Row, token: str
    ) -> WorkflowSnapshot:
        snapshot = self.projects.snapshot(principal, row["project_id"], row["current_revision"])
        page_count = snapshot.summary.get("page_count", 0)
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            return self._block(row, token, "invalid_output", "Page count is unavailable.")
        with self.database.read() as connection:
            passed = {
                cast(int, json.loads(event[0]).get("page_number"))
                for event in connection.execute(
                    "SELECT progress_json FROM workflow_events WHERE owner_id = ? "
                    "AND project_id = ? AND type = 'qa.page_passed'",
                    (row["owner_id"], row["project_id"]),
                ).fetchall()
                if isinstance(json.loads(event[0]).get("page_number"), int)
            }
        page_number = next((number for number in range(1, page_count + 1) if number not in passed), None)
        if page_number is None:
            return self._advance_row(row, token, phase="export")
        try:
            request = self.projects.page_review_input(
                principal, row["project_id"], row["current_revision"], page_number
            )
            review = await self._visual_review(row, request)
            published = self.projects.publish_page_review(
                principal, row["project_id"], row["current_revision"], page_number, review
            )
        except StaleProjectRevisionError:
            current = self.projects.snapshot(principal, row["project_id"])
            if current.revision != row["current_revision"]:
                raise
            # A same-revision stale error is deterministic provenance drift,
            # not permission to bind the completed review to different bytes.
            return self._advance_row(row, token, phase="composition")
        progress = {"page_number": page_number, "subject_id": request.subject_id}
        if self._review_failed(review):
            with self.database.transaction() as connection:
                current = connection.execute(
                    "SELECT * FROM production_workflows WHERE project_id = ? AND lease_token = ?",
                    (row["project_id"], token),
                ).fetchone()
                if current is None:
                    raise WorkflowConflictError("workflow advancement lost its lease")
                self._append_event(
                    connection,
                    current,
                    "qa.page_failed",
                    "Page failed subjective visual QA.",
                    provider=row["planning_provider"],
                    model=row["planning_model"],
                    progress=progress,
                    revision=published.revision,
                )
            return self._advance_row(
                row,
                token,
                revision=published.revision,
                state="blocked",
                error="page_qa_failed",
                event=("workflow.blocked", "Page QA requires user review."),
            )
        return self._advance_row(
            row,
            token,
            revision=published.revision,
            event=("qa.page_passed", "Page passed visual QA."),
            provider=row["planning_provider"],
            model=row["planning_model"],
            progress=progress,
        )

    async def advance_once(
        self, worker_id: str, *, lease_seconds: int | None = None
    ) -> WorkflowSnapshot | None:
        duration = self._lease_seconds if lease_seconds is None else lease_seconds
        row = self._lease(worker_id, duration)
        if row is None:
            return None
        token = cast(str, row["lease_token"])
        principal = SessionPrincipal(cast(str, row["owner_id"]), "workflow-worker")
        try:
            phase = row["phase"]
            if phase in {"references", "panels"}:
                return await self._generation_step(principal, row, token)
            if phase == "panel-qa":
                return await self._panel_qa_step(principal, row, token)
            if phase == "lettering":
                return self._advance_row(row, token, phase="composition")
            if phase == "composition":
                prepared = self.projects.prepare_pages(
                    principal, row["project_id"], row["current_revision"]
                )
                return self._advance_row(
                    row,
                    token,
                    phase="page-qa",
                    revision=prepared.revision,
                    event=("composition.completed", "Lettering and composition completed."),
                )
            if phase == "page-qa":
                return await self._page_qa_step(principal, row, token)
            if phase == "export":
                current = self.projects.snapshot(principal, row["project_id"])
                if current.status in _TERMINAL_ENGINE_STATES:
                    final = current
                else:
                    if current.revision != row["current_revision"]:
                        raise StaleProjectRevisionError(row["current_revision"], current.revision)
                    final, _pdf = self.projects.finalize(
                        principal, row["project_id"], row["current_revision"]
                    )
                with self.database.transaction() as connection:
                    leased = connection.execute(
                        "SELECT * FROM production_workflows WHERE project_id = ? AND lease_token = ?",
                        (row["project_id"], token),
                    ).fetchone()
                    if leased is None:
                        raise WorkflowConflictError("workflow advancement lost its lease")
                    self._append_event(
                        connection,
                        leased,
                        "export.ready",
                        "Verified PDF export is ready.",
                        progress={"project_revision": final.revision},
                        revision=final.revision,
                    )
                return self._advance_row(
                    row,
                    token,
                    phase="complete",
                    revision=final.revision,
                    state="complete",
                    event=("workflow.complete", "Production completed."),
                )
            return self._advance_row(row, token, phase="complete", state="complete")
        except StaleProjectRevisionError:
            self._release(row["project_id"], token)
            raise
        except (CredentialUnavailableError, CredentialBrokerError) as error:
            del error
            return self._block(
                row, token, "invalid_credentials", "Required provider credential is unavailable."
            )
        except ProviderError as error:
            category = getattr(getattr(error, "category", None), "value", "provider_error")
            return self._block(row, token, category, "Provider execution requires user attention.")
        except RetryLimitError:
            return self._block(row, token, "retry_exhausted", "Retry budget exhausted.")
        except (GenerationConflictError, GenerationUnavailableError, GatewayInputError):
            self._release(row["project_id"], token)
            raise
        except BaseException:
            self._release(row["project_id"], token)
            raise
