from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

KNOWN_CAPABILITIES = frozenset(
    {
        "async_jobs",
        "cancellation",
        "custom_dimensions",
        "image_to_image",
        "negative_prompt",
        "reference_images",
        "seed",
        "text_to_image",
    }
)


class AuthMode(StrEnum):
    AGENT = "agent"
    HOSTED = "hosted"
    BYOK = "byok"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    POLLING = "polling"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    AWAITING_PROVIDER_CONFIRMATION = "awaiting_provider_confirmation"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCategory(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    MODERATED = "moderated"
    CAPABILITY_MISSING = "capability_missing"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    INVALID_OUTPUT = "invalid_output"
    PROVIDER_ERROR = "provider_error"


def _validate_capabilities(capabilities: frozenset[str]) -> None:
    if not capabilities <= KNOWN_CAPABILITIES:
        raise ValueError("unknown generation capability")


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    project_id: str
    project_revision: int
    subject_kind: str
    subject_id: str
    prompt: str
    negative_prompt: str | None
    references: tuple[Path, ...]
    width: int
    height: int
    required_capabilities: frozenset[str]
    provider_options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        capabilities = frozenset(self.required_capabilities)
        _validate_capabilities(capabilities)
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "provider_options",
            MappingProxyType(dict(self.provider_options)),
        )


@dataclass(frozen=True)
class GenerationResult:
    external_job_id: str | None
    state: JobState
    raster_bytes: bytes | None
    media_type: str | None
    effective_parameters: Mapping[str, object]
    usage: Mapping[str, int | float | str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effective_parameters",
            MappingProxyType(dict(self.effective_parameters)),
        )
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model: str
    capabilities: frozenset[str]
    enabled: bool

    def __post_init__(self) -> None:
        capabilities = frozenset(self.capabilities)
        _validate_capabilities(capabilities)
        object.__setattr__(self, "capabilities", capabilities)


class ProviderAdapter(Protocol):
    provider_id: str

    async def list_models(self) -> Sequence[ProviderModel]: ...

    async def estimate(self, request: GenerationRequest, model: str) -> Mapping[str, object]: ...

    async def generate(
        self, request: GenerationRequest, model: str, credential: str | None
    ) -> GenerationResult: ...

    async def poll(self, external_job_id: str, credential: str | None) -> GenerationResult: ...

    async def cancel(self, external_job_id: str, credential: str | None) -> None: ...
