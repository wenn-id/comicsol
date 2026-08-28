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


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, tuple | list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    project_id: str
    project_revision: int
    subject_kind: str
    subject_id: str
    prompt: str = field(repr=False)
    negative_prompt: str | None = field(repr=False)
    references: tuple[Path, ...] = field(repr=False)
    width: int
    height: int
    required_capabilities: frozenset[str]
    provider_options: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        capabilities = frozenset(self.required_capabilities)
        _validate_capabilities(capabilities)
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "provider_options",
            _freeze_mapping(self.provider_options),
        )


@dataclass(frozen=True)
class GenerationResult:
    external_job_id: str | None
    state: JobState
    raster_bytes: bytes | None = field(repr=False)
    media_type: str | None
    effective_parameters: Mapping[str, object] = field(repr=False)
    usage: Mapping[str, int | float | str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effective_parameters",
            _freeze_mapping(self.effective_parameters),
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
