"""Expiring, one-shot, owner-bound provider-switch proposals."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.generation.router import RouterRecommendation
from comic_sol_web.generation.store import GenerationStore
from comic_sol_web.generation.types import AuthMode, ErrorCategory, JobState
from comic_sol_web.migrations import APPROVAL_MIGRATIONS, apply_migrations

_JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
_PROPOSAL_ID = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_MAX_PROPOSAL_JOBS = 512
_MAX_PROPOSAL_TTL_SECONDS = 15 * 60
_PROPOSABLE_STATES = frozenset(
    {
        JobState.QUEUED.value,
        JobState.POLLING.value,
        JobState.FAILED.value,
        JobState.PAUSED.value,
    }
)


class ApprovalError(ValueError):
    """Base class for sanitized provider-switch failures."""


class ApprovalRequestError(ApprovalError):
    """The proposal or decision request is malformed."""


class ApprovalUnavailableError(ApprovalError):
    """The owner-bound proposal or project is unavailable."""


class ApprovalConflictError(ApprovalError):
    """The proposal conflicts with current durable state."""


@dataclass(frozen=True)
class SwitchProposal:
    proposal_id: str
    job_ids: tuple[str, ...]
    project_id: str
    project_revision: int
    from_provider: str
    to_provider: str
    to_model: str
    reason: ErrorCategory
    expires_at: str


class ProviderSwitchApprovals:
    """Persist provider switches without accepting a destination at decision time."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        apply_migrations(database, APPROVAL_MIGRATIONS)
        self._database = database
        self._clock = clock

    @staticmethod
    def _idempotency_key(value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError):
            raise ApprovalRequestError("provider switch request is invalid") from None
        canonical = str(parsed)
        if value.lower() != canonical:
            raise ApprovalRequestError("provider switch request is invalid")
        return canonical

    @staticmethod
    def _job_ids(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, str | bytes):
            raise ApprovalRequestError("provider switch request is invalid")
        job_ids = tuple(sorted(values))
        if (
            not job_ids
            or len(job_ids) > _MAX_PROPOSAL_JOBS
            or len(set(job_ids)) != len(job_ids)
            or any(
                not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None
                for job_id in job_ids
            )
        ):
            raise ApprovalRequestError("provider switch request is invalid")
        return job_ids

    @staticmethod
    def _revision(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ApprovalRequestError("provider switch request is invalid")
        return value

    @staticmethod
    def _reason(value: ErrorCategory | str) -> ErrorCategory:
        try:
            return value if isinstance(value, ErrorCategory) else ErrorCategory(value)
        except (TypeError, ValueError):
            raise ApprovalRequestError("provider switch request is invalid") from None

    @staticmethod
    def _proposal_id(value: str) -> str:
        if not isinstance(value, str) or _PROPOSAL_ID.fullmatch(value) is None:
            raise ApprovalUnavailableError("provider switch proposal is unavailable")
        return value

    @staticmethod
    def _expires_at(value: int) -> str:
        return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")

    @classmethod
    def _proposal_from_row(cls, row: sqlite3.Row) -> SwitchProposal:
        try:
            decoded = json.loads(cast(str, row["job_ids_json"]))
            job_ids = cls._job_ids(decoded)
            reason = ErrorCategory(cast(str, row["reason"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ApprovalConflictError(
                "provider switch proposal conflicts with current state"
            ) from None
        return SwitchProposal(
            proposal_id=cast(str, row["proposal_id"]),
            job_ids=job_ids,
            project_id=cast(str, row["project_id"]),
            project_revision=cast(int, row["project_revision"]),
            from_provider=cast(str, row["from_provider"]),
            to_provider=cast(str, row["to_provider"]),
            to_model=cast(str, row["to_model"]),
            reason=reason,
            expires_at=cls._expires_at(cast(int, row["expires_at"])),
        )

    @staticmethod
    def _project_row(
        connection: sqlite3.Connection,
        owner_id: str,
        project_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT owner_id, revision FROM web_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise ApprovalUnavailableError("provider switch proposal is unavailable")
        return row

    @staticmethod
    def _job_rows(
        connection: sqlite3.Connection,
        job_ids: tuple[str, ...],
    ) -> tuple[sqlite3.Row, ...]:
        placeholders = ",".join("?" for _ in job_ids)
        rows = connection.execute(
            f"SELECT * FROM generation_jobs WHERE job_id IN ({placeholders}) ORDER BY job_id",
            job_ids,
        ).fetchall()
        return tuple(rows)

    def propose_switch(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        job_ids: Sequence[str],
        recommendation: RouterRecommendation,
        reason: ErrorCategory | str,
        *,
        idempotency_key: str,
        ttl_seconds: int = 5 * 60,
    ) -> SwitchProposal:
        """Atomically pause an exact job set and publish an immutable proposal."""
        revision = self._revision(expected_revision)
        jobs = self._job_ids(job_ids)
        key = self._idempotency_key(idempotency_key)
        category = self._reason(reason)
        if (
            not isinstance(project_id, str)
            or not project_id
            or len(project_id) > 128
            or not isinstance(recommendation, RouterRecommendation)
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= _MAX_PROPOSAL_TTL_SECONDS
        ):
            raise ApprovalRequestError("provider switch request is invalid")
        GenerationStore.validate_identifier(recommendation.provider, "provider")
        GenerationStore.validate_identifier(recommendation.model, "model")
        mode = recommendation.auth_mode
        if not isinstance(mode, AuthMode):
            raise ApprovalRequestError("provider switch request is invalid")
        now = self._clock()
        expires_at = now + ttl_seconds
        proposal_id = secrets.token_urlsafe(32)
        jobs_json = json.dumps(jobs, separators=(",", ":"))

        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM provider_switch_proposals
                WHERE owner_id = ? AND project_id = ? AND idempotency_key = ?
                """,
                (principal.user_id, project_id, key),
            ).fetchone()
            if existing is not None:
                consumed = connection.execute(
                    "SELECT 1 FROM provider_switch_decisions WHERE proposal_id = ?",
                    (existing["proposal_id"],),
                ).fetchone()
                expected = (
                    jobs_json,
                    revision,
                    recommendation.provider,
                    recommendation.model,
                    mode.value,
                    category.value,
                )
                actual = tuple(
                    existing[name]
                    for name in (
                        "job_ids_json",
                        "project_revision",
                        "to_provider",
                        "to_model",
                        "to_auth_mode",
                        "reason",
                    )
                )
                if consumed is not None or actual != expected:
                    raise ApprovalConflictError(
                        "provider switch proposal conflicts with current state"
                    )
                return self._proposal_from_row(existing)

            project = self._project_row(connection, principal.user_id, project_id)
            if project["revision"] != revision:
                raise ApprovalConflictError("provider switch project revision is stale")
            rows = self._job_rows(connection, jobs)
            if len(rows) != len(jobs):
                raise ApprovalUnavailableError("provider switch proposal is unavailable")
            if any(row["owner_id"] != principal.user_id for row in rows):
                raise ApprovalUnavailableError("provider switch proposal is unavailable")
            if any(
                row["project_id"] != project_id
                or row["project_revision"] != revision
                or row["state"] not in _PROPOSABLE_STATES
                for row in rows
            ):
                raise ApprovalConflictError("provider switch proposal conflicts with current state")
            current_pairs = {(row["provider"], row["model"]) for row in rows}
            if len(current_pairs) != 1:
                raise ApprovalConflictError("provider switch proposal conflicts with current state")
            from_provider, from_model = current_pairs.pop()
            if recommendation.provider == from_provider:
                raise ApprovalRequestError("provider switch request is invalid")

            connection.execute(
                """
                INSERT INTO provider_switch_proposals (
                    proposal_id, idempotency_key, owner_id, project_id, project_revision,
                    job_ids_json, from_provider, from_model, to_provider, to_model,
                    to_auth_mode, reason, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    key,
                    principal.user_id,
                    project_id,
                    revision,
                    jobs_json,
                    from_provider,
                    from_model,
                    recommendation.provider,
                    recommendation.model,
                    mode.value,
                    category.value,
                    expires_at,
                    now,
                ),
            )
            for row in rows:
                updated = connection.execute(
                    """
                    UPDATE generation_jobs
                    SET state = ?, lease_token = NULL, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND owner_id = ? AND project_id = ?
                      AND project_revision = ? AND provider = ? AND model = ? AND state = ?
                    """,
                    (
                        JobState.AWAITING_PROVIDER_CONFIRMATION.value,
                        now,
                        row["job_id"],
                        principal.user_id,
                        project_id,
                        revision,
                        from_provider,
                        from_model,
                        row["state"],
                    ),
                ).rowcount
                if updated != 1:
                    raise ApprovalConflictError(
                        "provider switch proposal conflicts with current state"
                    )
                current = connection.execute(
                    "SELECT * FROM generation_jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                assert current is not None
                GenerationStore.append_attempt(
                    connection,
                    current,
                    JobState.AWAITING_PROVIDER_CONFIRMATION,
                    now,
                )
            stored = connection.execute(
                "SELECT * FROM provider_switch_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            assert stored is not None
        return self._proposal_from_row(stored)

    def _decide(
        self,
        principal: SessionPrincipal,
        proposal_id: str,
        decision: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SwitchProposal:
        identifier = self._proposal_id(proposal_id)
        revision = self._revision(expected_revision)
        key = self._idempotency_key(idempotency_key)
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_switch_proposals WHERE proposal_id = ?",
                (identifier,),
            ).fetchone()
            if row is None or row["owner_id"] != principal.user_id:
                raise ApprovalUnavailableError("provider switch proposal is unavailable")
            consumed = connection.execute(
                "SELECT 1 FROM provider_switch_decisions WHERE proposal_id = ?",
                (identifier,),
            ).fetchone()
            if consumed is not None:
                raise ApprovalConflictError("provider switch proposal was already consumed")
            if row["expires_at"] <= now:
                raise ApprovalConflictError("provider switch proposal expired")
            if row["project_revision"] != revision:
                raise ApprovalConflictError("provider switch project revision is stale")
            project = self._project_row(
                connection,
                principal.user_id,
                cast(str, row["project_id"]),
            )
            if project["revision"] != revision:
                raise ApprovalConflictError("provider switch project revision is stale")
            proposal = self._proposal_from_row(row)
            rows = self._job_rows(connection, proposal.job_ids)
            if len(rows) != len(proposal.job_ids) or any(
                job["owner_id"] != principal.user_id
                or job["project_id"] != proposal.project_id
                or job["project_revision"] != revision
                or job["provider"] != row["from_provider"]
                or job["model"] != row["from_model"]
                or job["state"] != JobState.AWAITING_PROVIDER_CONFIRMATION.value
                for job in rows
            ):
                raise ApprovalConflictError("provider switch proposal conflicts with current state")

            for job in rows:
                if decision == "approved":
                    updated = connection.execute(
                        """
                        UPDATE generation_jobs
                        SET state = ?, provider = ?, model = ?, auth_mode = ?,
                            attempt_number = attempt_number + 1, retry_count = 0,
                            external_job_id = NULL, lease_token = NULL, lease_owner = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND owner_id = ? AND project_id = ?
                          AND project_revision = ? AND provider = ? AND model = ?
                          AND state = ?
                        """,
                        (
                            JobState.QUEUED.value,
                            row["to_provider"],
                            row["to_model"],
                            row["to_auth_mode"],
                            now,
                            job["job_id"],
                            principal.user_id,
                            proposal.project_id,
                            revision,
                            row["from_provider"],
                            row["from_model"],
                            JobState.AWAITING_PROVIDER_CONFIRMATION.value,
                        ),
                    ).rowcount
                    next_state = JobState.QUEUED
                else:
                    updated = connection.execute(
                        """
                        UPDATE generation_jobs
                        SET state = ?, lease_token = NULL, lease_owner = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND owner_id = ? AND project_id = ?
                          AND project_revision = ? AND provider = ? AND model = ?
                          AND state = ?
                        """,
                        (
                            JobState.PAUSED.value,
                            now,
                            job["job_id"],
                            principal.user_id,
                            proposal.project_id,
                            revision,
                            row["from_provider"],
                            row["from_model"],
                            JobState.AWAITING_PROVIDER_CONFIRMATION.value,
                        ),
                    ).rowcount
                    next_state = JobState.PAUSED
                if updated != 1:
                    raise ApprovalConflictError(
                        "provider switch proposal conflicts with current state"
                    )
                current = connection.execute(
                    "SELECT * FROM generation_jobs WHERE job_id = ?", (job["job_id"],)
                ).fetchone()
                assert current is not None
                GenerationStore.append_attempt(connection, current, next_state, now)
            connection.execute(
                """
                INSERT INTO provider_switch_decisions (
                    proposal_id, decision, idempotency_key, decided_at
                ) VALUES (?, ?, ?, ?)
                """,
                (identifier, decision, key, now),
            )
        return proposal

    def approve(
        self,
        principal: SessionPrincipal,
        proposal_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SwitchProposal:
        return self._decide(
            principal,
            proposal_id,
            "approved",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def reject(
        self,
        principal: SessionPrincipal,
        proposal_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> SwitchProposal:
        return self._decide(
            principal,
            proposal_id,
            "rejected",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )


def propose_switch(
    approvals: ProviderSwitchApprovals,
    principal: SessionPrincipal,
    project_id: str,
    expected_revision: int,
    job_ids: Sequence[str],
    recommendation: RouterRecommendation,
    reason: ErrorCategory | str,
    *,
    idempotency_key: str,
    ttl_seconds: int = 5 * 60,
) -> SwitchProposal:
    return approvals.propose_switch(
        principal,
        project_id,
        expected_revision,
        job_ids,
        recommendation,
        reason,
        idempotency_key=idempotency_key,
        ttl_seconds=ttl_seconds,
    )


def approve(
    approvals: ProviderSwitchApprovals,
    principal: SessionPrincipal,
    proposal_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> SwitchProposal:
    return approvals.approve(
        principal,
        proposal_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )


def reject(
    approvals: ProviderSwitchApprovals,
    principal: SessionPrincipal,
    proposal_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> SwitchProposal:
    return approvals.reject(
        principal,
        proposal_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
