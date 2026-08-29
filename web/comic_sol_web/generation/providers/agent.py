"""Provider-neutral handoff to the active agent's image capability.

The adapter never resolves a provider credential or performs network I/O. It turns
one already-authorized generation request into deterministic, bounded JSON and
leaves the durable job in a polling state until a page-owned asset is submitted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError

AGENT_MODEL = "active-agent-image"
DEFAULT_MAX_PACKAGE_BYTES = 256 * 1024
_AGENT_JOB_SHA256_OPTION = "_agent_job_sha256"
_AGENT_LOCKED_SCOPE_OPTION = "_agent_locked_scope_sha256"
_MAX_REFERENCES = 16
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
_AGENT_CAPABILITIES = frozenset(
    {
        "custom_dimensions",
        "image_to_image",
        "negative_prompt",
        "reference_images",
        "text_to_image",
    }
)
_MODEL = ProviderModel(
    provider="agent",
    model=AGENT_MODEL,
    capabilities=_AGENT_CAPABILITIES,
    enabled=True,
)


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_request_identity(request: GenerationRequest) -> None:
    if _JOB_ID.fullmatch(request.job_id) is None:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    for value in (request.project_id, request.subject_kind, request.subject_id):
        if _IDENTIFIER.fullmatch(value) is None:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if (
        isinstance(request.project_revision, bool)
        or request.project_revision < 1
        or isinstance(request.width, bool)
        or isinstance(request.height, bool)
        or request.width < 1
        or request.height < 1
        or len(request.references) > _MAX_REFERENCES
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _scope_material(request: GenerationRequest) -> dict[str, object]:
    """Return only bounded job material; never serialize reference paths/options."""
    _validate_request_identity(request)
    return {
        "dimensions": {"height": request.height, "width": request.width},
        "job_id": request.job_id,
        "negative_prompt": request.negative_prompt,
        "project_id": request.project_id,
        "project_revision": request.project_revision,
        "prompt": request.prompt,
        "references": [
            {"ordinal": ordinal} for ordinal, _path in enumerate(request.references, start=1)
        ],
        "required_capabilities": sorted(request.required_capabilities),
        "subject": {"id": request.subject_id, "kind": request.subject_kind},
    }


def _bound_sha256(request: GenerationRequest, field: str) -> str | None:
    value = request.provider_options.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return value


def bind_agent_request(
    request: GenerationRequest,
    *,
    locked_scope_sha256: str,
    job_sha256: str,
) -> GenerationRequest:
    """Attach canonical engine digests without exposing any engine path."""
    if _JOB_ID.fullmatch(locked_scope_sha256) is None or _JOB_ID.fullmatch(job_sha256) is None:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    options = dict(request.provider_options)
    options[_AGENT_LOCKED_SCOPE_OPTION] = locked_scope_sha256
    options[_AGENT_JOB_SHA256_OPTION] = job_sha256
    return replace(request, provider_options=options)


def agent_locked_scope_digest(request: GenerationRequest) -> str:
    """Return the canonical scope digest, or a deterministic boundary fallback."""
    bound = _bound_sha256(request, _AGENT_LOCKED_SCOPE_OPTION)
    if bound is not None:
        return bound
    return hashlib.sha256(_canonical_bytes(_scope_material(request))).hexdigest()


def _package_without_checksum(request: GenerationRequest) -> dict[str, object]:
    material = _scope_material(request)
    return {
        "contract_version": "1.0",
        **material,
        "locked_scope_digest": agent_locked_scope_digest(request),
        "provider_id": "agent",
    }


def agent_job_checksum(request: GenerationRequest) -> str:
    """Return the canonical job digest, or bind a synthetic boundary request."""
    bound = _bound_sha256(request, _AGENT_JOB_SHA256_OPTION)
    if bound is not None:
        return bound
    return hashlib.sha256(_canonical_bytes(_package_without_checksum(request))).hexdigest()


def build_agent_package(request: GenerationRequest) -> dict[str, object]:
    package = _package_without_checksum(request)
    package["job_checksum"] = agent_job_checksum(request)
    return package


class AgentProvider:
    """Produce an offline package for an explicitly active agent capability."""

    provider_id = "agent"

    def __init__(
        self,
        active_capabilities: frozenset[str] = frozenset(),
        *,
        max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    ) -> None:
        if not isinstance(max_package_bytes, int) or isinstance(max_package_bytes, bool):
            raise ValueError("agent package limit must be an integer")
        if max_package_bytes <= 0:
            raise ValueError("agent package limit must be positive")
        capabilities = frozenset(active_capabilities)
        if not capabilities <= _AGENT_CAPABILITIES:
            raise ValueError("active agent capabilities are invalid")
        self.active_capabilities = capabilities
        self.max_package_bytes = max_package_bytes
        # This is a disposable in-process cache only. Durable jobs reconstruct
        # the same bounded bytes from their persisted immutable request.
        self._packages: dict[str, bytes] = {}
        self._cancelled: set[str] = set()

    async def list_models(self) -> Sequence[ProviderModel]:
        return (_MODEL,)

    async def estimate(self, request: GenerationRequest, model: str) -> Mapping[str, object]:
        self._validate_capability(request, model)
        return MappingProxyType({"quantity": 1, "unit": "agent_handoff"})

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        self._validate_capability(request, model)
        external_job_id, _package = self._restore(request, None)
        self._cancelled.discard(external_job_id)
        return self._waiting_result(external_job_id)

    def restore_package(
        self,
        request: GenerationRequest,
        external_job_id: str,
    ) -> Mapping[str, object]:
        """Reconstruct a bounded package and bind it to its durable external ID."""
        self._validate_capability(request, AGENT_MODEL)
        _external_job_id, package = self._restore(request, external_job_id)
        return package

    def _restore(
        self,
        request: GenerationRequest,
        external_job_id: str | None,
    ) -> tuple[str, Mapping[str, object]]:
        try:
            package = build_agent_package(request)
            payload = _canonical_bytes(package)
        except (TypeError, ValueError, UnicodeError) as error:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from error
        if len(payload) > self.max_package_bytes:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        expected_external_job_id = f"agent:{package['job_checksum']}"
        if external_job_id is not None and external_job_id != expected_external_job_id:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        self._packages[expected_external_job_id] = payload
        return expected_external_job_id, json.loads(payload)

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        if external_job_id not in self._packages:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        if external_job_id in self._cancelled:
            return GenerationResult(
                external_job_id=external_job_id,
                state=JobState.CANCELLED,
                raster_bytes=None,
                media_type=None,
                effective_parameters={},
                usage={},
            )
        return self._waiting_result(external_job_id)

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        del credential
        if external_job_id not in self._packages:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        self._cancelled.add(external_job_id)

    def package_bytes(self, external_job_id: str | None) -> bytes:
        if not isinstance(external_job_id, str) or external_job_id not in self._packages:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        return self._packages[external_job_id]

    def package(self, external_job_id: str | None) -> Mapping[str, object]:
        return MappingProxyType(json.loads(self.package_bytes(external_job_id)))

    def _validate_capability(self, request: GenerationRequest, model: str) -> None:
        if model != AGENT_MODEL or not request.required_capabilities <= _AGENT_CAPABILITIES:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if not request.required_capabilities <= self.active_capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)

    @staticmethod
    def _waiting_result(external_job_id: str) -> GenerationResult:
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters={"handoff": "agent", "waiting_for_asset": True},
            usage={},
        )
