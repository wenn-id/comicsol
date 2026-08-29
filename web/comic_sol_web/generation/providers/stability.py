from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence

import httpx

from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, MultipartFile, TransportPolicy, validate_raster

_API_ORIGIN = "https://api.stability.ai"
_GENERATE_URL = f"{_API_ORIGIN}/v2beta/stable-image/generate/sd3"
_MODEL = ProviderModel(
    provider="stability",
    model="sd3.5-large",
    capabilities=frozenset({"custom_dimensions", "text_to_image"}),
    enabled=True,
)
_ASPECT_RATIOS = frozenset({"16:9", "1:1", "9:16"})
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class StabilityProvider:
    """Direct bounded HTTP adapter for the curated Stability image model."""

    provider_id = "stability"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._transport = transport
        self._policy = TransportPolicy(
            approved_origins=frozenset({_API_ORIGIN}),
            connect_timeout=5.0,
            read_timeout=30.0,
            total_timeout=60.0,
            max_response_bytes=max_response_bytes,
        )

    async def list_models(self) -> Sequence[ProviderModel]:
        return (_MODEL,)

    async def estimate(self, request: GenerationRequest, model: str) -> Mapping[str, object]:
        self._validate_request(request, model)
        return {"currency": "USD", "model": model, "unit": "image"}

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        self._validate_request(request, model)
        headers = _credential_headers(credential)
        fields: dict[str, str] = {
            "aspect_ratio": _aspect_ratio(request.width, request.height),
            "model": model,
            "output_format": "png",
            "prompt": request.prompt,
        }
        files: tuple[MultipartFile, ...] = (("none", ("none", b"", "application/octet-stream")),)
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_multipart(
                _GENERATE_URL,
                headers=headers,
                fields=fields,
                files=files,
                error_classifier=_classify_error,
            )
        reason = response.get("finish_reason")
        if isinstance(reason, str) and any(
            marker in reason.lower() for marker in ("content_filtered", "moderation", "safety")
        ):
            raise ProviderError(ErrorCategory.MODERATED)
        raster = _response_raster(response, self._policy.max_response_bytes)
        return GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type="image/png",
            effective_parameters={
                "aspect_ratio": _aspect_ratio(request.width, request.height),
                "model": model,
            },
            usage={"images": 1},
        )

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        del external_job_id, credential
        raise ProviderError(ErrorCategory.CAPABILITY_MISSING)

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        del external_job_id, credential
        raise ProviderError(ErrorCategory.CAPABILITY_MISSING)

    def _validate_request(self, request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.references:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if _aspect_ratio(request.width, request.height) not in _ASPECT_RATIOS:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _aspect_ratio(width: int, height: int) -> str:
    divisor = _gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    # Stability uses content negotiation for result retrieval. Requesting JSON
    # keeps the response inside the bounded JSON/base64 transport path.
    return {"accept": "application/json", "authorization": f"Bearer {credential}"}


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    name = payload.get("name")
    if not isinstance(name, str):
        return None
    lowered = name.lower()
    if status_code == 429 and "quota" in lowered:
        return ErrorCategory.QUOTA_EXHAUSTED
    if any(marker in lowered for marker in ("moderation", "safety", "policy")):
        return ErrorCategory.MODERATED
    return None


def _response_raster(response: Mapping[str, object], max_bytes: int) -> bytes:
    encoded = response.get("image")
    if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        raster = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if len(raster) > max_bytes:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    raster, media_type = validate_raster(raster, "image/png")
    if media_type != "image/png":
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return raster
