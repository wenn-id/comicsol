from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence

import httpx

from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import (
    BoundedHTTPClient,
    TransportPolicy,
    read_reference_raster,
    validate_raster,
)

_API_ORIGIN = "https://api.x.ai"
_GENERATIONS_URL = f"{_API_ORIGIN}/v1/images/generations"
_EDITS_URL = f"{_API_ORIGIN}/v1/images/edits"
_GENERATE_MODEL = ProviderModel(
    provider="xai",
    model="grok-imagine-image-2.0",
    capabilities=frozenset({"custom_dimensions", "text_to_image"}),
    enabled=True,
)
_EDIT_MODEL = ProviderModel(
    provider="xai",
    model="grok-imagine-image-2.0",
    capabilities=frozenset(
        {"custom_dimensions", "image_to_image", "reference_images", "text_to_image"}
    ),
    enabled=True,
)
_MODELS = (_GENERATE_MODEL, _EDIT_MODEL)
_ALLOWED_DIMENSIONS = frozenset({(1024, 1024), (1024, 1536), (1536, 1024)})
_MAX_REFERENCES = 3
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class XAIProvider:
    """Direct bounded HTTP adapter for the curated xAI Grok image models."""

    provider_id = "xai"

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
        return _MODELS

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
        size = f"{request.width}x{request.height}"
        reference_images = self._reference_images(request) if request.references else None
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            if reference_images is not None:
                image_field = "image" if len(reference_images) == 1 else "images"
                response = await client.post_json(
                    _EDITS_URL,
                    headers=headers,
                    payload={
                        image_field: reference_images[0]
                        if len(reference_images) == 1
                        else reference_images,
                        "model": model,
                        "n": 1,
                        "prompt": request.prompt,
                        "response_format": "b64_json",
                        "size": size,
                    },
                    error_classifier=_classify_error,
                )
            else:
                response = await client.post_json(
                    _GENERATIONS_URL,
                    headers=headers,
                    payload={
                        "model": model,
                        "n": 1,
                        "prompt": request.prompt,
                        "response_format": "b64_json",
                        "size": size,
                    },
                    error_classifier=_classify_error,
                )
        _raise_if_moderated(response)
        raster = _response_raster(response, self._policy.max_response_bytes)
        return GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type="image/png",
            effective_parameters={"height": request.height, "model": model, "width": request.width},
            usage=_response_usage(response),
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
        if model not in {_GENERATE_MODEL.model, _EDIT_MODEL.model}:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if (request.width, request.height) not in _ALLOWED_DIMENSIONS:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if not request.references:
            if (
                request.references
                or not request.required_capabilities <= _GENERATE_MODEL.capabilities
            ):
                raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
            return
        if len(request.references) > _MAX_REFERENCES:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if not request.references:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if not request.required_capabilities <= _EDIT_MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)

    def _reference_images(self, request: GenerationRequest) -> tuple[Mapping[str, str], ...]:
        images: list[Mapping[str, str]] = []
        total = 0
        for path in request.references:
            content, media_type = read_reference_raster(path, self._policy.max_response_bytes)
            total += len(content)
            if total > self._policy.max_response_bytes:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            encoded = base64.b64encode(content).decode("ascii")
            images.append({"type": "image_url", "url": f"data:{media_type};base64,{encoded}"})
        return tuple(images)


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {"authorization": f"Bearer {credential}"}


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    error = payload.get("error")
    if status_code == 429 and isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and "insufficient_quota" in message:
            return ErrorCategory.QUOTA_EXHAUSTED
    markers: list[str] = []
    if isinstance(error, Mapping):
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, str):
                markers.append(value.lower())
    else:
        for key in ("code", "error"):
            value = payload.get(key)
            if isinstance(value, str):
                markers.append(value.lower())
    if any(
        substring in marker
        for marker in markers
        for substring in ("policy", "moderation", "safety")
    ):
        return ErrorCategory.MODERATED
    return None


def _raise_if_moderated(response: Mapping[str, object]) -> None:
    code = response.get("code")
    if isinstance(code, str) and any(
        marker in code.lower() for marker in ("policy", "moderation", "safety")
    ):
        raise ProviderError(ErrorCategory.MODERATED)


def _response_raster(response: Mapping[str, object], max_bytes: int) -> bytes:
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if len(content) > max_bytes:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    raster, media_type = validate_raster(content, "image/png")
    if media_type != "image/png":
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return raster


def _response_usage(response: Mapping[str, object]) -> Mapping[str, int | float | str]:
    usage: dict[str, int | float | str] = {"images": 1}
    provider_usage = response.get("usage")
    if not isinstance(provider_usage, Mapping):
        return usage
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = provider_usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value
    return usage
