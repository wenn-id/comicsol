"""Owner-bound orchestration facade for durable generation execution."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import AsyncContextManager, Protocol, cast

from comic_sol_product.cli import _load_engine_module

from comic_sol_web.assets import AssetError, AssetStore, _png_dimensions_and_decode_size
from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.generation.credentials import (
    CredentialBrokerError,
    CredentialUnavailableError,
)
from comic_sol_web.generation.catalog import CATALOG
from comic_sol_web.generation.providers.agent import (
    _MODEL as AGENT_PROVIDER_MODEL,
)
from comic_sol_web.generation.providers.agent import (
    AgentProvider,
    agent_job_checksum,
    agent_locked_scope_digest,
    bind_agent_request,
)
from comic_sol_web.generation.providers.base import ProviderError, ProviderRegistry
from comic_sol_web.generation.queue import (
    MAX_LEASE_SECONDS,
    DurableGenerationQueue,
    QueueConflictError,
    QueueRetryLimitError,
    QueueUnavailableError,
)
from comic_sol_web.generation.store import (
    GenerationJob,
    GenerationStore,
    LeasedJob,
    deserialize_request,
    serialize_request,
)
from comic_sol_web.generation.types import (
    AuthMode,
    ErrorCategory,
    GenerationRequest,
    GenerationResult,
    JobState,
    ProviderModel,
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
_ASSET_HANDLE = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
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
        assets: AssetStore | None = None,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        apply_migrations(database, GENERATION_MIGRATIONS)
        self._projects = projects
        self._providers = providers
        self._credentials = credentials
        self._assets = assets
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

    def _bind_agent_requests(
        self,
        principal: SessionPrincipal,
        project_id: str,
        requests: tuple[GenerationRequest, ...],
    ) -> tuple[GenerationRequest, ...]:
        """Bind packages to canonical handoff digests without exposing paths."""
        if not requests:
            return requests
        revision = requests[0].project_revision
        snapshot = self._projects.snapshot(principal, project_id, revision)
        root = getattr(snapshot, "root", None)
        if not isinstance(root, Path):
            raise GenerationConflictError("agent handoff snapshot is invalid")
        try:
            manifest = _project_io.read_contained_json(root, "handoff/manifest.json")
        except FileNotFoundError:
            # Protocol test doubles need not materialize engine files. Freeze
            # deterministic fallback digests now so sibling revision rebinding
            # cannot silently change an already-issued package identity.
            try:
                return tuple(
                    bind_agent_request(
                        request,
                        locked_scope_sha256=agent_locked_scope_digest(
                            replace(request, project_revision=1)
                        ),
                        job_sha256=agent_job_checksum(replace(request, project_revision=1)),
                    )
                    for request in requests
                )
            except ProviderError as error:
                raise GenerationConflictError("agent handoff binding is invalid") from error
        if not isinstance(manifest, Mapping) or _handoff.validate_handoff_manifest(manifest):
            raise GenerationConflictError("agent handoff manifest is invalid")
        locked_scope = manifest.get("locked_scope_sha256")
        descriptors = manifest.get("jobs")
        if not isinstance(locked_scope, str) or not isinstance(descriptors, list):
            raise GenerationConflictError("agent handoff binding is invalid")
        by_job: dict[str, str] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                raise GenerationConflictError("agent handoff job binding is invalid")
            job_id = descriptor.get("job_id")
            checksum = descriptor.get("sha256")
            if not isinstance(job_id, str) or not isinstance(checksum, str):
                raise GenerationConflictError("agent handoff job binding is invalid")
            by_job[job_id] = checksum
        try:
            return tuple(
                bind_agent_request(
                    request,
                    locked_scope_sha256=locked_scope,
                    job_sha256=by_job[request.job_id],
                )
                for request in requests
            )
        except (KeyError, ProviderError) as error:
            raise GenerationConflictError("agent handoff job binding is invalid") from error

    def _runtime_options(self) -> tuple[ProviderModel, ...]:
        """Return curated models backed by a currently executable adapter."""
        options: list[ProviderModel] = []
        for entry in CATALOG:
            if not entry.enabled:
                continue
            try:
                self._providers.get(entry.provider)
            except KeyError:
                continue
            options.append(entry)
        try:
            agent = self._providers.get("agent")
        except KeyError:
            agent = None
        if (
            isinstance(agent, AgentProvider)
            and AGENT_PROVIDER_MODEL.enabled
            and AGENT_PROVIDER_MODEL.capabilities <= agent.active_capabilities
        ):
            options.append(AGENT_PROVIDER_MODEL)
        return tuple(sorted(options, key=lambda item: (item.provider, item.model)))

    async def available_options(self) -> tuple[ProviderModel, ...]:
        """Return only curated models backed by a registered executable adapter."""
        return self._runtime_options()

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
        self._store.validate_enqueue_options(
            principal.user_id,
            provider,
            model,
            max_retries,
        )
        if not any(
            entry.provider == provider and entry.model == model for entry in self._runtime_options()
        ):
            raise ValueError("generation destination is not currently executable")
        selected_provider = self._providers.get(provider)
        if provider == "agent" and mode is not AuthMode.AGENT:
            raise ValueError("agent generation requires agent authentication mode")
        requests = self._projects.prepare_generation(principal, project_id, expected_revision)
        if provider == "agent":
            if not isinstance(selected_provider, AgentProvider) or any(
                not selected_provider.capability_available(request, model) for request in requests
            ):
                raise ValueError("agent generation capability is unavailable")
            requests = self._bind_agent_requests(principal, project_id, requests)
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

    def list_jobs(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
        *,
        limit: int = 50,
    ) -> tuple[GenerationJob, ...]:
        self._projects.snapshot(principal, project_id, expected_revision)
        bounded_limit = min(50, max(1, limit))
        return self._store.list_jobs(
            principal.user_id,
            project_id,
            limit=bounded_limit,
        )

    def current_accepted(
        self,
        principal: SessionPrincipal,
        project_id: str,
        expected_revision: int,
    ) -> GenerationJob | None:
        self._projects.snapshot(principal, project_id, expected_revision)
        return self._store.current_accepted(
            principal.user_id,
            project_id,
            expected_revision,
        )

    async def cancel(
        self,
        principal: SessionPrincipal,
        job_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        """Cancel one non-promoting job without racing canonical acceptance."""
        job = self._queue.get_owned(principal.user_id, job_id)
        self._projects.snapshot(principal, job.project_id, expected_revision)
        terminal = {JobState.ACCEPTED, JobState.FAILED, JobState.CANCELLED}
        if job.state in terminal:
            return job
        if job.project_revision != expected_revision:
            raise GenerationConflictError("generation project revision is stale")
        if job.state is JobState.VALIDATING:
            raise GenerationConflictError("staged raster promotion cannot be cancelled")
        cancellable = {
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.POLLING,
            JobState.AWAITING_PROVIDER_CONFIRMATION,
            JobState.PAUSED,
        }
        if job.state not in cancellable:
            raise GenerationConflictError("generation job cannot be cancelled")
        if job.state is JobState.RUNNING and job.external_job_id is None:
            raise GenerationConflictError("active provider call cannot be cancelled safely")
        if job.external_job_id is not None and job.state in {
            JobState.RUNNING,
            JobState.POLLING,
        }:
            try:
                provider = self._providers.get(job.provider)
                if job.auth_mode is AuthMode.AGENT:
                    await provider.cancel(job.external_job_id, None)
                else:
                    async with self._credentials.resolve(
                        job.owner_id,
                        job.provider,
                        job.auth_mode,
                    ) as credential:
                        await provider.cancel(job.external_job_id, credential)
            except (KeyError, CredentialBrokerError, ProviderError) as error:
                raise GenerationConflictError("provider cancellation failed") from error
        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND state = ?
                """,
                (
                    JobState.CANCELLED.value,
                    now,
                    job_id,
                    principal.user_id,
                    job.state.value,
                ),
            ).rowcount
            if updated != 1:
                current_row = self._store.row(connection, job_id)
                if current_row is not None:
                    current = self._store.from_row(current_row)
                    if current.state in terminal:
                        return current
                raise GenerationConflictError("generation cancellation lost a state race")
            current_row = self._store.row(connection, job_id)
            assert current_row is not None
            self._store.append_attempt(
                connection,
                current_row,
                JobState.CANCELLED,
                now,
                error_category=ErrorCategory.CANCELLED.value,
                external_job_id=current_row["external_job_id"],
                result_checksum=current_row["result_checksum"],
            )
        return self._store.from_row(current_row)

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
        job = self._queue.get_owned(principal.user_id, job_id)
        if job.state is not JobState.AWAITING_PROVIDER_CONFIRMATION:
            return self._queue.retry_same_provider(principal.user_id, job_id, expected_revision)
        if job.project_revision != expected_revision:
            raise GenerationConflictError("generation project revision is stale")
        if job.provider != "agent" or job.auth_mode is not AuthMode.AGENT:
            raise GenerationConflictError("generation capability is not resumable")
        try:
            provider = self._providers.get("agent")
        except KeyError:
            raise GenerationConflictError("generation capability is unavailable") from None
        if not isinstance(provider, AgentProvider) or not provider.capability_available(
            job.request,
            job.model,
        ):
            raise GenerationConflictError("generation capability is unavailable")
        self._validate_agent_locked_scope(principal, job, expected_revision)

        now = self._clock()
        with self._store.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, external_job_id = NULL,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    staged_raster_name = NULL, result_checksum = NULL, updated_at = ?
                WHERE job_id = ? AND owner_id = ? AND project_revision = ? AND state = ?
                """,
                (
                    JobState.QUEUED.value,
                    now,
                    job_id,
                    principal.user_id,
                    expected_revision,
                    JobState.AWAITING_PROVIDER_CONFIRMATION.value,
                ),
            ).rowcount
            if updated != 1:
                raise GenerationConflictError("generation resume lost a state race")
            current = self._store.row(connection, job_id)
            assert current is not None
            self._store.append_attempt(connection, current, JobState.QUEUED, now)
        return self._store.from_row(current)

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

    @staticmethod
    def _is_raster_validation_error(error: Exception) -> bool:
        """Recognize only canonical handoff findings about the raster bytes."""
        issues = getattr(error, "issues", ())
        return bool(issues) and all(
            isinstance(issue, str) and issue.startswith("result raster:") for issue in issues
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

    def _committed_promotion(
        self,
        principal: SessionPrincipal,
        job: GenerationJob,
    ) -> tuple[int, bool] | None:
        """Prove that WP3 already retained this job's exact staged raster."""
        if job.result_checksum is None:
            return None
        snapshot = self._projects.snapshot(principal, job.project_id)
        current_revision = getattr(snapshot, "revision", None)
        root = getattr(snapshot, "root", None)
        if (
            isinstance(current_revision, bool)
            or not isinstance(current_revision, int)
            or current_revision <= job.project_revision
            or not isinstance(root, Path)
        ):
            return None

        for ordinal in range(1, 4):
            try:
                receipt_id = _handoff.attempt_id(
                    job_id=job.request.job_id,
                    attempt=ordinal,
                )
            except ValueError:
                return None
            try:
                receipt = _project_io.read_contained_json(
                    root,
                    f"generation/receipts/{receipt_id}.json",
                )
            except FileNotFoundError:
                continue
            if not isinstance(receipt, Mapping) or _handoff.validate_generation_receipt(receipt):
                continue
            if (
                receipt.get("outcome") != "success"
                or receipt.get("job_id") != job.request.job_id
                or receipt.get("raster_sha256") != job.result_checksum
            ):
                continue
            retained_path = receipt.get("raster_path")
            if not isinstance(retained_path, str):
                continue
            try:
                retained = _project_io.read_contained_bytes(
                    root,
                    retained_path,
                    max_bytes=_raster_limits.MAX_ENCODED_RASTER_BYTES,
                )
            except FileNotFoundError:
                continue
            if hashlib.sha256(retained).hexdigest() != job.result_checksum:
                continue
            accepted_revision = job.project_revision + 1
            return accepted_revision, current_revision == accepted_revision
        return None

    @staticmethod
    def _canonical_asset_handle(asset_id: str) -> str:
        """Stop request taint before the owner-bound asset store path boundary."""
        if not isinstance(asset_id, str) or _ASSET_HANDLE.fullmatch(asset_id) is None:
            raise GenerationConflictError("agent asset handle is invalid")
        return asset_id

    def agent_package(
        self,
        principal: SessionPrincipal,
        project_id: str,
        job_id: str,
        expected_revision: int,
    ) -> Mapping[str, object]:
        """Return one bounded, owner-authorized, current agent handoff package."""
        if (
            not isinstance(project_id, str)
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise GenerationConflictError("agent package binding is invalid")
        job = self._queue.get_owned(principal.user_id, job_id)
        if (
            job.provider != "agent"
            or job.auth_mode is not AuthMode.AGENT
            or job.state is not JobState.POLLING
            or job.project_id != project_id
            or job.project_id != job.request.project_id
            or job.project_revision != expected_revision
            or job.project_revision != job.request.project_revision
            or job.external_job_id is None
        ):
            raise GenerationConflictError("generation job is not an active agent handoff")
        self._validate_agent_scope(principal, job, expected_revision)
        try:
            provider = self._providers.get("agent")
        except KeyError:
            raise GenerationUnavailableError("agent handoff is unavailable") from None
        if not isinstance(provider, AgentProvider):
            raise GenerationUnavailableError("agent handoff is unavailable")
        try:
            return provider.restore_package(job.request, job.external_job_id)
        except ProviderError as error:
            if error.category is ErrorCategory.CAPABILITY_MISSING:
                raise GenerationConflictError("agent capability is unavailable") from error
            raise GenerationConflictError("agent package cannot be restored") from error

    def _agent_asset_payload(
        self,
        principal: SessionPrincipal,
        job: GenerationJob,
        asset_id: str,
    ) -> tuple[bytes, str]:
        if self._assets is None:
            raise GenerationUnavailableError("agent asset submission is unavailable")
        try:
            canonical_asset_id = self._canonical_asset_handle(asset_id)
            handle = self._assets.get(principal, canonical_asset_id)
            # The path-bearing read is keyed only by the owner-authorized ID
            # returned from durable metadata, never the request path segment.
            payload = self._assets.read_bytes(principal, handle.asset_id)
            width, height = _png_dimensions_and_decode_size(
                payload,
                self._assets.max_decoded_bytes,
            )
        except AssetError as error:
            raise GenerationConflictError("agent asset failed bounded validation") from error
        if (
            handle.media_type != "image/png"
            or handle.byte_size != len(payload)
            or handle.width != width
            or handle.height != height
            or len(payload) > _raster_limits.MAX_ENCODED_RASTER_BYTES
            or width * height > self._assets.max_pixels
            or width * height > _raster_limits.MAX_DECODED_PIXELS
            or width != job.request.width
            or height != job.request.height
            or width * job.request.height != height * job.request.width
        ):
            raise GenerationConflictError("agent asset does not match the prepared job")
        return payload, hashlib.sha256(payload).hexdigest()

    def _validate_agent_scope(
        self,
        principal: SessionPrincipal,
        job: GenerationJob,
        expected_revision: int,
    ) -> None:
        expected_external_id = f"agent:{agent_job_checksum(job.request)}"
        if job.external_job_id != expected_external_id:
            raise GenerationConflictError("agent package checksum is stale")
        self._validate_agent_locked_scope(principal, job, expected_revision)

    def _validate_agent_locked_scope(
        self,
        principal: SessionPrincipal,
        job: GenerationJob,
        expected_revision: int,
    ) -> None:
        """Prove an issued agent request still matches the canonical project."""
        try:
            current_requests = self._projects.prepare_generation(
                principal,
                job.project_id,
                expected_revision,
            )
            current_requests = self._bind_agent_requests(
                principal,
                job.project_id,
                current_requests,
            )
        except ValueError as error:
            raise GenerationConflictError("agent project scope is stale") from error
        current = next(
            (request for request in current_requests if request.job_id == job.request.job_id),
            None,
        )
        if (
            current is None
            or current.project_id != job.project_id
            or current.project_revision != expected_revision
            or agent_job_checksum(current) != agent_job_checksum(job.request)
            or agent_locked_scope_digest(current) != agent_locked_scope_digest(job.request)
        ):
            raise GenerationConflictError("agent locked scope is stale")

    def submit_agent_asset(
        self,
        principal: SessionPrincipal,
        job_id: str,
        asset_id: str,
        expected_revision: int,
    ) -> GenerationJob:
        """Validate a page-owned asset and promote it only through WP5 and WP3."""
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise GenerationConflictError("agent project revision is invalid")
        job = self._queue.get_owned(principal.user_id, job_id)
        if (
            job.provider != "agent"
            or job.auth_mode is not AuthMode.AGENT
            or job.project_id != job.request.project_id
            or job.project_revision != job.request.project_revision
            or job.project_revision != expected_revision
        ):
            raise GenerationConflictError("generation job is not an eligible agent handoff")

        payload, checksum = self._agent_asset_payload(principal, job, asset_id)
        if job.state is JobState.ACCEPTED:
            if job.result_checksum == checksum:
                return job
            raise GenerationConflictError("agent submission conflicts with accepted raster")
        if job.state is not JobState.POLLING:
            raise GenerationConflictError("generation job is not awaiting an agent asset")

        self._validate_agent_scope(principal, job, expected_revision)
        assert job.external_job_id is not None
        return self.record_result(
            job.job_id,
            None,
            GenerationResult(
                external_job_id=job.external_job_id,
                state=JobState.ACCEPTED,
                raster_bytes=payload,
                media_type="image/png",
                effective_parameters={},
                usage={"images": 1},
            ),
        )

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
                claimed = self._store.row(connection, job_id)
                if claimed is not None and claimed["state"] == JobState.ACCEPTED.value:
                    return self._store.from_row(claimed)
                raise GenerationConflictError("staged raster promotion is already claimed")

        capabilities = {
            "dimensions": "custom_dimensions" in job.request.required_capabilities,
            "localized_edit": False,
            "reference_images": "reference_images" in job.request.required_capabilities,
        }
        revision = job.project_revision
        try:
            committed = self._committed_promotion(principal, job)
            if committed is None:
                snapshot = self._projects.submit_raster(
                    principal,
                    job.project_id,
                    revision,
                    job.request.job_id,
                    staged,
                    "image/png",
                    capabilities,
                )
                accepted_revision = cast(int, getattr(snapshot, "revision"))
                rebind_siblings = accepted_revision == revision + 1
            else:
                accepted_revision, rebind_siblings = committed
            current = self._finish_promotion(
                job,
                token,
                accepted_revision,
                rebind_siblings=rebind_siblings,
            )
        except _handoff.HandoffResultError as error:
            if self._is_raster_validation_error(error):
                failed = self._fail_invalid_staged_raster(job_id, token)
                staged.unlink(missing_ok=True)
                return failed
            self._release_promotion(job_id, token)
            raise GenerationConflictError(
                "staged raster conflicts with canonical handoff state"
            ) from error
        except _input_limits.InputResourceLimitError:
            failed = self._fail_invalid_staged_raster(job_id, token)
            staged.unlink(missing_ok=True)
            return failed
        except Exception:
            # A newer project revision can represent an unrelated user edit.
            # Never rebind generated bytes to that revision implicitly; retain
            # the staged raster for explicit revision-safe reconciliation.
            self._release_promotion(job_id, token)
            raise

        staged.unlink(missing_ok=True)
        return current

    def _finish_promotion(
        self,
        job: GenerationJob,
        token: str,
        accepted_revision: int,
        *,
        rebind_siblings: bool,
    ) -> GenerationJob:
        """Atomically finish queue state after a proven canonical acceptance."""
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
                    job.job_id,
                    job.owner_id,
                    JobState.VALIDATING.value,
                    token,
                ),
            ).rowcount
            if updated != 1:
                current = self._store.row(connection, job.job_id)
                if current is None or current["state"] != JobState.ACCEPTED.value:
                    raise GenerationConflictError("staged raster promotion lost its claim")
            else:
                current = self._store.row(connection, job.job_id)
                assert current is not None
                event_time = self._clock()
                self._store.append_attempt(connection, current, JobState.ACCEPTED, event_time)
                if rebind_siblings:
                    # Only the immediate canonical revision produced by this
                    # accepted raster can advance siblings. Re-serialize each
                    # request with the same provider options so its locked-scope
                    # and job digests remain exactly the issued bindings.
                    siblings = connection.execute(
                        """
                        SELECT job_id, request_json, provider, model, auth_mode
                        FROM generation_jobs
                        WHERE owner_id = ? AND project_id = ? AND project_revision = ?
                          AND job_id <> ? AND state <> ?
                        """,
                        (
                            job.owner_id,
                            job.project_id,
                            job.project_revision,
                            job.job_id,
                            JobState.ACCEPTED.value,
                        ),
                    ).fetchall()
                    for sibling in siblings:
                        request = deserialize_request(sibling["request_json"])
                        if (
                            request.project_id != job.project_id
                            or request.project_revision != job.project_revision
                        ):
                            raise GenerationConflictError(
                                "generation sibling request binding is invalid"
                            )
                        rebound_json = serialize_request(
                            replace(request, project_revision=accepted_revision)
                        )
                        rebound_key = self._store.idempotency_key(
                            job.owner_id,
                            rebound_json,
                            sibling["provider"],
                            sibling["model"],
                            AuthMode(sibling["auth_mode"]),
                        )
                        collision = self._store._row_by_idempotency(connection, rebound_key)
                        if collision is not None and collision["job_id"] != sibling["job_id"]:
                            # Enqueue can observe the newly accepted canonical
                            # revision before this transaction rebinds its
                            # already-issued sibling.  That row has not entered
                            # execution yet, so discard it and preserve the
                            # original handoff identity rather than allowing the
                            # UNIQUE key collision to strand the accepted job.
                            removed = connection.execute(
                                """
                                DELETE FROM generation_jobs
                                WHERE job_id = ? AND owner_id = ? AND project_id = ?
                                  AND project_revision = ? AND request_json = ?
                                  AND provider = ? AND model = ? AND auth_mode = ?
                                  AND state = ?
                                  AND NOT EXISTS (
                                      SELECT 1 FROM generation_attempts
                                      WHERE generation_attempts.job_id = generation_jobs.job_id
                                  )
                                  AND NOT EXISTS (
                                      SELECT 1 FROM generation_receipts
                                      WHERE generation_receipts.job_id = generation_jobs.job_id
                                  )
                                """,
                                (
                                    collision["job_id"],
                                    job.owner_id,
                                    job.project_id,
                                    accepted_revision,
                                    rebound_json,
                                    sibling["provider"],
                                    sibling["model"],
                                    sibling["auth_mode"],
                                    JobState.QUEUED.value,
                                ),
                            ).rowcount
                            if removed != 1:
                                raise GenerationConflictError(
                                    "generation sibling revision has an active duplicate"
                                )
                        rebound = connection.execute(
                            """
                            UPDATE generation_jobs
                            SET project_revision = ?, request_json = ?, idempotency_key = ?,
                                updated_at = ?
                            WHERE job_id = ? AND owner_id = ? AND project_id = ?
                              AND project_revision = ? AND state <> ?
                            """,
                            (
                                accepted_revision,
                                rebound_json,
                                rebound_key,
                                event_time,
                                sibling["job_id"],
                                job.owner_id,
                                job.project_id,
                                job.project_revision,
                                JobState.ACCEPTED.value,
                            ),
                        ).rowcount
                        if rebound != 1:
                            raise GenerationConflictError(
                                "generation sibling revision lost its claim"
                            )
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
            remaining_lease = max(0, leased.lease_expires_at - self._clock())
            async with asyncio.timeout(remaining_lease):
                if job.auth_mode is AuthMode.AGENT:
                    if job.external_job_id:
                        if job.provider == "agent":
                            if not isinstance(provider, AgentProvider):
                                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
                            provider.restore_package(job.request, job.external_job_id)
                        result = await provider.poll(job.external_job_id, None)
                    else:
                        result = await provider.generate(job.request, job.model, None)
                else:
                    async with self._credentials.resolve(
                        job.owner_id,
                        job.provider,
                        job.auth_mode,
                    ) as credential:
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
        return recorded
