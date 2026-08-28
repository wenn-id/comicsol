from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError

# Deterministic 1x1 transparent PNG fixture. It never comes from a provider response.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0"
    b"\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)
_MODEL = next(model for model in CATALOG if model.provider == "fake")


@dataclass
class _FakeJob:
    state: JobState
    polls: int = 0


class FakeProvider:
    """Deterministic offline adapter for contract and orchestration tests."""

    provider_id = "fake"

    def __init__(self) -> None:
        self._jobs: dict[str, _FakeJob] = {}

    async def list_models(self) -> Sequence[ProviderModel]:
        return (_MODEL,)

    async def estimate(self, request: GenerationRequest, model: str) -> Mapping[str, object]:
        self._validate_request(request, model)
        return {"currency": "test", "quantity": 1, "unit": "image"}

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        self._validate_request(request, model)
        fixture = request.provider_options.get("fixture", "success")
        if fixture == "quota":
            raise ProviderError(ErrorCategory.QUOTA_EXHAUSTED)
        if fixture == "moderation":
            raise ProviderError(ErrorCategory.MODERATED)
        if fixture == "malformed_raster":
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        if fixture == "success":
            return _accepted_result(None)
        if fixture == "async":
            external_job_id = f"fake:{request.job_id}"
            self._jobs[external_job_id] = _FakeJob(JobState.POLLING)
            return _pending_result(external_job_id)
        raise ProviderError(ErrorCategory.PROVIDER_ERROR)

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        job = self._jobs.get(external_job_id)
        if job is None:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        if job.state is JobState.CANCELLED:
            return GenerationResult(
                external_job_id=external_job_id,
                state=JobState.CANCELLED,
                raster_bytes=None,
                media_type=None,
                effective_parameters={},
                usage={},
            )
        if job.state is JobState.ACCEPTED:
            return _accepted_result(external_job_id)
        job.polls += 1
        if job.polls == 1:
            return _pending_result(external_job_id)
        job.state = JobState.ACCEPTED
        return _accepted_result(external_job_id)

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        del credential
        job = self._jobs.get(external_job_id)
        if job is None:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        if job.state is not JobState.ACCEPTED:
            job.state = JobState.CANCELLED

    @staticmethod
    def _validate_request(request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _pending_result(external_job_id: str) -> GenerationResult:
    return GenerationResult(
        external_job_id=external_job_id,
        state=JobState.POLLING,
        raster_bytes=None,
        media_type=None,
        effective_parameters={},
        usage={},
    )


def _accepted_result(external_job_id: str | None) -> GenerationResult:
    return GenerationResult(
        external_job_id=external_job_id,
        state=JobState.ACCEPTED,
        raster_bytes=_PNG,
        media_type="image/png",
        effective_parameters={"fixture": "deterministic", "width": 1, "height": 1},
        usage={"images": 1},
    )
