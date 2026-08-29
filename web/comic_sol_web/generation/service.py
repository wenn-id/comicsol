"""Owner-bound orchestration facade for durable generation execution."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import AsyncContextManager, Protocol, cast

from comic_sol_product.cli import _load_engine_module

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.generation.credentials import (
    CredentialBrokerError,
    CredentialUnavailableError,
)
from comic_sol_web.generation.providers.base import ProviderError, ProviderRegistry
from comic_sol_web.generation.queue import (
    MAX_LEASE_SECONDS,
    DurableGenerationQueue,
    QueueConflictError,
    QueueRetryLimitError,
    QueueUnavailableError,
)
from comic_sol_web.generation.store import GenerationJob, GenerationStore, LeasedJob
from comic_sol_web.generation.types import (
    AuthMode,
    ErrorCategory,
    GenerationRequest,
    GenerationResult,
    JobState,
)
from comic_sol_web.migrations import GENERATION_MIGRATIONS, apply_migrations

_handoff = _load_engine_module("handoff")
_input_limits = _load_engine_module("input_limits")
_project_io = _load_engine_module("project_io")
_raster_limits = _load_engine_module("raster_limits")

GenerationUnavailableError = QueueUnavailableError
GenerationConflictError = QueueConflictError
RetryLimitError = QueueRetryLimitError

_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SENSITIVE_EXTERNAL_ID = re.compile(
    r"(?:authorization|bearer|credential|password|secret|token|api[_-]?key|account|acct|email)",
    re.IGNORECASE,
)


class CredentialResolver(Protocol):
    def resolve(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> AsyncContextManager[str | None]: ...


class ProjectGenerationBoundary(Protocol):
    def prepare_generation(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> tuple[GenerationRequest, ...]: ...

    def submit_raster(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        job_id: str,
        raster: Path,
        media_type: str,
        capabilities_used: Mapping[str, object],
    ) -> object: ...

    def snapshot(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int | None = None,
    ) -> object: ...


class GenerationService:
    """Persist generation state and admit provider bytes through WP3 only."""

    def __init__(
        self,
        database: Database,
        projects: ProjectGenerationBoundary,
        providers: ProviderRegistry,
        staging_root: Path,
        *,
        credentials: CredentialResolver,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        apply_migrations(database, GENERATION_MIGRATIONS)
        self._projects = projects
        self._providers = providers
        self._credentials = credentials
        self._clock = clock
        self._staging_root = Path(staging_root)
        if not self._staging_root.is_absolute() or not self._staging_root.is_dir():
            raise ValueError("generation staging root must be an existing absolute directory")
        # Validate every existing path component and reject aliases/symlinks at
        # construction. Individual raster names are resolved through the same
        # canonical containment helper before publication and submission.
        _project_io.contained_project_path(self._staging_root, ".")
        self._store = GenerationStore(database, self._staging_root)
        self._queue = DurableGenerationQueue(self._store, clock)

    def queue(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        *,
        provider: str,
        model: str,
        auth_mode: AuthMode | str,
        max_retries: int = 2,
    ) -> tuple[GenerationJob, ...]:
        """Prepare canonical requests and idempotently enqueue each one."""
        try:
            mode = auth_mode if isinstance(auth_mode, AuthMode) else AuthMode(auth_mode)
        except (TypeError, ValueError):
            raise ValueError("generation authentication mode is invalid") from None
        # Lookup proves there is an explicit provider selection. The service
        # never catches this to choose another provider.
        self._providers.get(provider)
        requests = self._projects.prepare_generation(principal, project_id, expected_revision)
        now = self._clock()
        return tuple(
            self._store.enqueue(
                owner_id=principal.user_id,
                request=request,
                provider=provider,
                model=model,
                auth_mode=mode,
                max_retries=max_retries,
                now=now,
            )
            for request in requests
        )

    def get(self, principal: SessionPrincipal, job_id: str) -> GenerationJob:
        return self._queue.get_owned(principal.user_id, job_id)

    def lease_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 30,
    ) -> LeasedJob | None:
        return self._queue.lease_next(worker_id, lease_seconds=lease_seconds)

    def retry_same_provider(
        self,
        principal: SessionPrincipal,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        return self._queue.retry_same_provider(principal.user_id, job_id, expected_revision)

    def pause_for_switch(
        self,
        principal: SessionPrincipal,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        """Stop at confirmation without selecting or enqueuing a fallback."""
        return self._queue.pause_for_switch(principal.user_id, job_id, expected_revision)

    def attempts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        return self._store.attempts(job_id)

    def receipts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        return self._store.receipts(job_id)

    def _stage_raster(self, job: GenerationJob, payload: bytes, checksum: str) -> Path:
        if not payload or len(payload) > _raster_limits.MAX_ENCODED_RASTER_BYTES:
            raise GenerationConflictError("provider raster is outside the allowed byte bound")
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise GenerationConflictError("provider raster is not a PNG")
        name = f"generation-{job.job_id}-a{job.attempt_number}-{checksum}.png"
        path = _project_io.contained_project_path(self._staging_root, name)
        _project_io.durable_atomic_write(path, payload)
        return path

    @staticmethod
    def _external_job_id_is_safe(value: str) -> bool:
        """Accept only short opaque IDs that cannot carry account or secret text."""
        return (
            _EXTERNAL_JOB_ID.fullmatch(value) is not None
            and _SENSITIVE_EXTERNAL_ID.search(value) is None
        )

    def record_result(
        self,
        job_id: str,
        lease_token: str | None,
        result: GenerationResult,
    ) -> GenerationJob:
        """Reconcile poll/callback results exactly once for the active attempt."""
        if lease_token is not None and (not isinstance(lease_token, str) or not lease_token):
            raise GenerationConflictError("generation lease is invalid")
        try:
            job = self._store.get(job_id)
        except ValueError:
            raise GenerationUnavailableError("generation job is unavailable") from None
        if job is None:
            raise GenerationUnavailableError("generation job is unavailable")

        checksum: str | None = None
        if result.raster_bytes is not None:
            checksum = hashlib.sha256(result.raster_bytes).hexdigest()
        if job.state in (JobState.VALIDATING, JobState.ACCEPTED):
            if (
                result.state is JobState.ACCEPTED
                and checksum is not None
                and checksum == job.result_checksum
            ):
                return job
            raise GenerationConflictError("generation completion conflicts with accepted state")
        callback_completion = (
            lease_token is None
            and result.state is JobState.ACCEPTED
            and result.external_job_id is not None
            and result.external_job_id == job.external_job_id
            and job.state in (JobState.POLLING, JobState.RUNNING)
        )
        if lease_token is None and not callback_completion:
            raise GenerationConflictError("generation callback is not bound to this attempt")
        if lease_token is not None and job.state is not JobState.RUNNING:
            raise GenerationConflictError("generation job is not leased for a result")
        if (
            result.state is JobState.ACCEPTED
            and job.external_job_id is not None
            and result.external_job_id != job.external_job_id
        ):
            raise GenerationConflictError("generation completion has the wrong external job ID")

        external_job_id = result.external_job_id
        if external_job_id is not None and not self._external_job_id_is_safe(external_job_id):
            return self._record_invalid_result(job_id, lease_token, None)

        if result.state is JobState.ACCEPTED:
            if result.raster_bytes is None or result.media_type != "image/png" or checksum is None:
                return self._record_invalid_result(job_id, lease_token, external_job_id)
            try:
                staged = self._stage_raster(job, result.raster_bytes, checksum)
            except GenerationConflictError:
                return self._record_invalid_result(job_id, lease_token, external_job_id)
            try:
                return self._record_accepted_result(
                    job_id,
                    lease_token,
                    result,
                    staged.name,
                    checksum,
                )
            except BaseException:
                with self._store.database.read() as connection:
                    row = self._store.row(connection, job_id)
                    retained = row is not None and row["staged_raster_name"] == staged.name
                if not retained:
                    staged.unlink(missing_ok=True)
                raise
        if result.raster_bytes is not None:
            return self._record_invalid_result(job_id, lease_token, external_job_id)
        if result.state is JobState.POLLING:
            if external_job_id is None:
                return self._record_invalid_result(job_id, lease_token, None)
            assert lease_token is not None
            return self._record_simple_result(
                job_id,
                lease_token,
                JobState.POLLING,
                external_job_id=external_job_id,
            )
        if result.state in (JobState.FAILED, JobState.CANCELLED):
            assert lease_token is not None
            return self._record_simple_result(job_id, lease_token, result.state)
        return self._record_invalid_result(job_id, lease_token, external_job_id)

    def _record_invalid_result(
        self,
        job_id: str,
        lease_token: str | None,
        callback_external_job_id: str | None,
    ) -> GenerationJob:
        """Fail malformed output without retaining any provider-supplied value."""
        if lease_token is not None:
            return self._record_simple_result(
                job_id,
                lease_token,
                JobState.FAILED,
                error_category=ErrorCategory.INVALID_OUTPUT,
            )
        if callback_external_job_id is None:
            raise GenerationConflictError("generation callback is not bound to this attempt")

        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND state IN (?, ?) AND external_job_id = ?
                """,
                (
                    JobState.FAILED.value,
                    now,
                    job_id,
                    JobState.POLLING.value,
                    JobState.RUNNING.value,
                    callback_external_job_id,
                ),
            ).rowcount
            if updated != 1:
                raise GenerationConflictError("generation result lost a lease race")
            current = self._store.row(connection, job_id)
            assert current is not None
            self._store.append_attempt(
                connection,
                current,
                JobState.FAILED,
                now,
                error_category=ErrorCategory.INVALID_OUTPUT.value,
                external_job_id=current["external_job_id"],
            )
        return self._store.from_row(current)

    def _record_accepted_result(
        self,
        job_id: str,
        lease_token: str | None,
        result: GenerationResult,
        staged_name: str,
        checksum: str,
    ) -> GenerationJob:
        now = self._clock()
        with self._store.database.transaction() as connection:
            row = self._store.row(connection, job_id)
            if row is None:
                raise GenerationUnavailableError("generation job is unavailable")
            if row["state"] in (JobState.VALIDATING.value, JobState.ACCEPTED.value):
                if row["result_checksum"] == checksum:
                    return self._store.from_row(row)
                raise GenerationConflictError("generation completion conflicts with accepted state")
            parameters = (
                JobState.VALIDATING.value,
                result.external_job_id,
                staged_name,
                checksum,
                now,
                job_id,
            )
            if lease_token is None:
                updated = connection.execute(
                    """
                    UPDATE generation_jobs
                    SET state = ?, external_job_id = ?, staged_raster_name = ?,
                        result_checksum = ?, lease_token = NULL, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND state IN (?, ?)
                      AND external_job_id = ?
                    """,
                    (
                        *parameters,
                        JobState.POLLING.value,
                        JobState.RUNNING.value,
                        result.external_job_id,
                    ),
                ).rowcount
            else:
                updated = connection.execute(
                    """
                    UPDATE generation_jobs
                    SET state = ?, external_job_id = ?, staged_raster_name = ?,
                        result_checksum = ?, lease_token = NULL, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND state = ? AND lease_token = ?
                    """,
                    (*parameters, JobState.RUNNING.value, lease_token),
                ).rowcount
            if updated != 1:
                raise GenerationConflictError("generation result lost a lease race")
            current = self._store.row(connection, job_id)
            assert current is not None
            self._store.append_attempt(
                connection,
                current,
                JobState.VALIDATING,
                now,
                external_job_id=result.external_job_id,
                result_checksum=checksum,
            )
            self._store.append_receipt(connection, current, result.usage, checksum, now)
        return self._store.from_row(current)

    def _record_simple_result(
        self,
        job_id: str,
        lease_token: str,
        state: JobState,
        *,
        external_job_id: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> GenerationJob:
        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, external_job_id = COALESCE(?, external_job_id),
                    lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_token = ?
                """,
                (
                    state.value,
                    external_job_id,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                    lease_token,
                ),
            ).rowcount
            if updated != 1:
                raise GenerationConflictError("generation result lost a lease race")
            current = self._store.row(connection, job_id)
            assert current is not None
            self._store.append_attempt(
                connection,
                current,
                state,
                now,
                error_category=None if error_category is None else error_category.value,
                external_job_id=current["external_job_id"],
            )
        return self._store.from_row(current)

    def submit_staged_raster(
        self,
        principal: SessionPrincipal,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        """Promote one claimed staged local file through the WP3 boundary."""
        job = self._queue.get_owned(principal.user_id, job_id)
        if job.state is JobState.ACCEPTED:
            return job
        if job.project_revision != expected_revision:
            raise GenerationConflictError("generation project revision is stale")
        if job.state is not JobState.VALIDATING or job.staged_raster is None:
            raise GenerationConflictError("generation job has no staged raster")
        staged = _project_io.contained_project_path(
            self._staging_root,
            job.staged_raster.name,
            must_exist=True,
        )
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET lease_token = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND state = ?
                  AND (lease_token IS NULL OR lease_expires_at <= ?)
                """,
                (
                    token,
                    "wp3-promotion",
                    now + MAX_LEASE_SECONDS,
                    now,
                    job_id,
                    principal.user_id,
                    JobState.VALIDATING.value,
                    now,
                ),
            ).rowcount
            if updated != 1:
                current = self._store.row(connection, job_id)
                if current is not None and current["state"] == JobState.ACCEPTED.value:
                    return self._store.from_row(current)
                raise GenerationConflictError("staged raster promotion is already claimed")

        capabilities = {
            "dimensions": "custom_dimensions" in job.request.required_capabilities,
            "localized_edit": False,
            "reference_images": "reference_images" in job.request.required_capabilities,
        }
        revision = job.project_revision
        try:
            snapshot = self._projects.submit_raster(
                principal,
                job.project_id,
                revision,
                job.request.job_id,
                staged,
                "image/png",
                capabilities,
            )
        except (_handoff.HandoffResultError, _input_limits.InputResourceLimitError):
            failed = self._fail_invalid_staged_raster(job_id, token)
            staged.unlink(missing_ok=True)
            return failed
        except Exception:
            # A newer project revision can represent an unrelated user edit.
            # Never rebind generated bytes to that revision implicitly; retain
            # the staged raster for explicit revision-safe reconciliation.
            self._release_promotion(job_id, token)
            raise

        accepted_revision = cast(int, getattr(snapshot, "revision"))
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, accepted_project_revision = ?, staged_raster_name = NULL,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND state = ? AND lease_token = ?
                """,
                (
                    JobState.ACCEPTED.value,
                    accepted_revision,
                    self._clock(),
                    job_id,
                    principal.user_id,
                    JobState.VALIDATING.value,
                    token,
                ),
            ).rowcount
            if updated != 1:
                current = self._store.row(connection, job_id)
                if current is None or current["state"] != JobState.ACCEPTED.value:
                    raise GenerationConflictError("staged raster promotion lost its claim")
            else:
                current = self._store.row(connection, job_id)
                assert current is not None
                event_time = self._clock()
                self._store.append_attempt(connection, current, JobState.ACCEPTED, event_time)
                # This exact revision was produced by the accepted raster above,
                # so only siblings prepared against its immediate predecessor
                # may advance. An unrelated later project edit remains stale.
                connection.execute(
                    """
                    UPDATE generation_jobs
                    SET project_revision = ?, updated_at = ?
                    WHERE owner_id = ? AND project_id = ? AND project_revision = ?
                      AND job_id <> ? AND state <> ?
                    """,
                    (
                        accepted_revision,
                        event_time,
                        principal.user_id,
                        job.project_id,
                        revision,
                        job_id,
                        JobState.ACCEPTED.value,
                    ),
                )
        staged.unlink(missing_ok=True)
        assert current is not None
        return self._store.from_row(current)

    def _fail_invalid_staged_raster(self, job_id: str, token: str) -> GenerationJob:
        """Fail one claimed raster rejected by the canonical engine validator."""
        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, staged_raster_name = NULL,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_token = ?
                """,
                (
                    JobState.FAILED.value,
                    now,
                    job_id,
                    JobState.VALIDATING.value,
                    token,
                ),
            ).rowcount
            if updated != 1:
                raise GenerationConflictError("staged raster rejection lost its claim")
            current = self._store.row(connection, job_id)
            assert current is not None
            self._store.append_attempt(
                connection,
                current,
                JobState.FAILED,
                now,
                error_category=ErrorCategory.INVALID_OUTPUT.value,
                external_job_id=current["external_job_id"],
                result_checksum=current["result_checksum"],
            )
        return self._store.from_row(current)

    def _release_promotion(self, job_id: str, token: str) -> None:
        with self._store.database.transaction() as connection:
            connection.execute(
                """
                UPDATE generation_jobs
                SET lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_token = ?
                """,
                (self._clock(), job_id, JobState.VALIDATING.value, token),
            )

    async def run_once(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 30,
    ) -> GenerationJob | None:
        """Lease first, then resolve only that job owner's scoped credential."""
        leased = self.lease_next(worker_id, lease_seconds=lease_seconds)
        if leased is None:
            return None
        job = leased.job
        try:
            provider = self._providers.get(job.provider)
        except KeyError:
            return self._record_simple_result(
                job.job_id,
                leased.lease_token,
                JobState.AWAITING_PROVIDER_CONFIRMATION,
                error_category=ErrorCategory.CAPABILITY_MISSING,
            )
        try:
            async with self._credentials.resolve(
                job.owner_id,
                job.provider,
                job.auth_mode,
            ) as credential:
                remaining_lease = max(0, leased.lease_expires_at - self._clock())
                async with asyncio.timeout(remaining_lease):
                    if job.external_job_id:
                        result = await provider.poll(job.external_job_id, credential)
                    else:
                        result = await provider.generate(job.request, job.model, credential)
            recorded = self.record_result(job.job_id, leased.lease_token, result)
        except CredentialUnavailableError:
            return self._record_simple_result(
                job.job_id,
                leased.lease_token,
                JobState.FAILED,
                error_category=ErrorCategory.INVALID_CREDENTIALS,
            )
        except CredentialBrokerError:
            return self._record_simple_result(
                job.job_id,
                leased.lease_token,
                JobState.FAILED,
                error_category=ErrorCategory.UNAVAILABLE,
            )
        except TimeoutError:
            return self._record_simple_result(
                job.job_id,
                leased.lease_token,
                JobState.FAILED,
                error_category=ErrorCategory.TIMEOUT,
            )
        except ProviderError as error:
            if error.category is ErrorCategory.CANCELLED:
                state = JobState.CANCELLED
            elif error.category is ErrorCategory.CAPABILITY_MISSING:
                state = JobState.AWAITING_PROVIDER_CONFIRMATION
            else:
                state = JobState.FAILED
            return self._record_simple_result(
                job.job_id,
                leased.lease_token,
                state,
                error_category=error.category,
            )
        if recorded.state is JobState.VALIDATING:
            principal = SessionPrincipal(job.owner_id, job.owner_id)
            current = self._queue.get_owned(job.owner_id, job.job_id)
            return self.submit_staged_raster(principal, job.job_id, current.project_revision)
        return recorded
