"""Atomic compare-and-set leasing and bounded queue transitions."""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Callable

from comic_sol_web.generation.store import GenerationJob, GenerationStore, LeasedJob
from comic_sol_web.generation.types import ErrorCategory, JobState

MAX_LEASE_SECONDS = 300
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


class QueueUnavailableError(ValueError):
    """The owner-scoped generation job does not exist."""


class QueueConflictError(ValueError):
    """The requested transition conflicts with durable queue state."""


class QueueRetryLimitError(QueueConflictError):
    """The configured same-provider retry ceiling was reached."""


class DurableGenerationQueue:
    """Lease durable jobs without holding a SQLite transaction during work."""

    def __init__(self, store: GenerationStore, clock: Callable[[], int]) -> None:
        self.store = store
        self.clock = clock

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if not isinstance(worker_id, str) or _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("generation worker identifier is invalid")

    def lease_next(self, worker_id: str, *, lease_seconds: int) -> LeasedJob | None:
        """Claim one eligible job and bound recovery of expired work."""
        self._validate_worker(worker_id)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= MAX_LEASE_SECONDS
        ):
            raise ValueError("generation lease lifetime is outside the allowed bound")
        now = self.clock()
        expires_at = now + lease_seconds
        token = secrets.token_urlsafe(32)
        with self.store.database.transaction() as connection:
            while True:
                row = connection.execute(
                    """
                    SELECT * FROM generation_jobs
                    WHERE state IN ('queued', 'polling')
                       OR (state = 'running' AND lease_expires_at <= ?)
                    ORDER BY CASE state
                                 WHEN 'queued' THEN 0
                                 WHEN 'running' THEN 1
                                 ELSE 2
                             END,
                             CASE WHEN state = 'polling' THEN updated_at ELSE created_at END,
                             job_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    return None

                if (
                    row["state"] == JobState.RUNNING.value
                    and row["retry_count"] >= row["max_retries"]
                ):
                    failed = connection.execute(
                        """
                        UPDATE generation_jobs
                        SET state = ?, lease_token = NULL, lease_owner = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND state = ? AND lease_expires_at <= ?
                          AND retry_count >= max_retries
                        """,
                        (
                            JobState.FAILED.value,
                            now,
                            row["job_id"],
                            JobState.RUNNING.value,
                            now,
                        ),
                    ).rowcount
                    if failed != 1:
                        continue
                    current = self.store.row(connection, row["job_id"])
                    assert current is not None
                    self.store.append_attempt(
                        connection,
                        current,
                        JobState.FAILED,
                        now,
                        error_category=ErrorCategory.TIMEOUT.value,
                        external_job_id=current["external_job_id"],
                    )
                    continue

                updated = connection.execute(
                    """
                    UPDATE generation_jobs
                    SET state = ?,
                        attempt_number = attempt_number + CASE
                            WHEN state = 'running' THEN 1 ELSE 0 END,
                        retry_count = retry_count + CASE
                            WHEN state = 'running' THEN 1 ELSE 0 END,
                        lease_token = ?, lease_owner = ?, lease_expires_at = ?,
                        updated_at = ?
                    WHERE job_id = ? AND (
                        state IN ('queued', 'polling')
                        OR (
                            state = 'running' AND lease_expires_at <= ?
                            AND retry_count < max_retries
                        )
                    )
                    """,
                    (
                        JobState.RUNNING.value,
                        token,
                        worker_id,
                        expires_at,
                        now,
                        row["job_id"],
                        now,
                    ),
                ).rowcount
                if updated != 1:
                    continue
                current = self.store.row(connection, row["job_id"])
                assert current is not None
                self.store.append_attempt(
                    connection,
                    current,
                    JobState.RUNNING,
                    now,
                    external_job_id=current["external_job_id"],
                )
                return LeasedJob(self.store.from_row(current), token, expires_at)

    def get_owned(self, owner_id: str, job_id: str) -> GenerationJob:
        try:
            job = self.store.get(job_id)
        except ValueError:
            raise QueueUnavailableError("generation job is unavailable") from None
        if job is None or job.owner_id != owner_id:
            raise QueueUnavailableError("generation job is unavailable")
        return job

    def retry_same_provider(
        self,
        owner_id: str,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        now = self.clock()
        try:
            self.store.validate_job_id(job_id)
        except ValueError:
            raise QueueUnavailableError("generation job is unavailable") from None
        with self.store.database.transaction() as connection:
            row = self.store.row(connection, job_id)
            self._require_owner(row, owner_id)
            assert row is not None
            if row["project_revision"] != expected_revision:
                raise QueueConflictError("generation project revision is stale")
            if row["state"] != JobState.FAILED.value:
                raise QueueConflictError("generation job is not retryable")
            if row["retry_count"] >= row["max_retries"]:
                raise QueueRetryLimitError("same-provider retry limit reached")
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, attempt_number = attempt_number + 1,
                    retry_count = retry_count + 1, external_job_id = NULL,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    staged_raster_name = NULL, result_checksum = NULL, updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND project_revision = ?
                  AND state = ? AND retry_count < max_retries
                """,
                (
                    JobState.QUEUED.value,
                    now,
                    job_id,
                    owner_id,
                    expected_revision,
                    JobState.FAILED.value,
                ),
            ).rowcount
            if updated != 1:
                raise QueueConflictError("generation retry lost a state race")
            current = self.store.row(connection, job_id)
            assert current is not None
            self.store.append_attempt(connection, current, JobState.QUEUED, now)
        return self.store.from_row(current)

    def pause_for_switch(
        self,
        owner_id: str,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        now = self.clock()
        try:
            self.store.validate_job_id(job_id)
        except ValueError:
            raise QueueUnavailableError("generation job is unavailable") from None
        allowed = (
            JobState.QUEUED.value,
            JobState.POLLING.value,
            JobState.FAILED.value,
            JobState.PAUSED.value,
        )
        with self.store.database.transaction() as connection:
            row = self.store.row(connection, job_id)
            self._require_owner(row, owner_id)
            assert row is not None
            if row["project_revision"] != expected_revision:
                raise QueueConflictError("generation project revision is stale")
            if row["state"] == JobState.AWAITING_PROVIDER_CONFIRMATION.value:
                return self.store.from_row(row)
            if row["state"] not in allowed:
                raise QueueConflictError("generation job cannot pause for a provider switch")
            placeholders = ",".join("?" for _ in allowed)
            updated = connection.execute(
                f"""
                UPDATE generation_jobs
                SET state = ?, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND project_revision = ?
                  AND state IN ({placeholders})
                """,
                (
                    JobState.AWAITING_PROVIDER_CONFIRMATION.value,
                    now,
                    job_id,
                    owner_id,
                    expected_revision,
                    *allowed,
                ),
            ).rowcount
            if updated != 1:
                raise QueueConflictError("generation pause lost a state race")
            current = self.store.row(connection, job_id)
            assert current is not None
            self.store.append_attempt(
                connection,
                current,
                JobState.AWAITING_PROVIDER_CONFIRMATION,
                now,
            )
        return self.store.from_row(current)

    @staticmethod
    def _require_owner(row: sqlite3.Row | None, owner_id: str) -> None:
        if row is None or row["owner_id"] != owner_id:
            raise QueueUnavailableError("generation job is unavailable")
