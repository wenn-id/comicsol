from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence

import httpx

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, TransportPolicy, read_reference_raster, validate_raster

_API_ORIGIN = "https://generativelanguage.googleapis.com"
_MODEL = next(model for model in CATALOG if model.provider == "google")
_GENERATE_URL = f"{_API_ORIGIN}/v1beta/models/{_MODEL.model}:generateContent"
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class GoogleProvider:
    """Direct bounded HTTP adapter for the curated Google image model."""

    provider_id = "google"

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
        return {"currency": "USD", "model": _MODEL.model, "unit": "image"}

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        self._validate_request(request, model)
        parts: list[object] = [{"text": request.prompt}]
        total = 0
        for path in request.references:
            content, media_type = read_reference_raster(path, self._policy.max_response_bytes)
            total += len(content)
            if total > self._policy.max_response_bytes:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            parts.append(
                {
                    "inlineData": {
                        "data": base64.b64encode(content).decode("ascii"),
                        "mimeType": media_type,
                    }
                }
            )
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                _GENERATE_URL,
                headers=_credential_headers(credential),
                payload={
                    "contents": [{"parts": parts}],
                    "generationConfig": {
                        "imageConfig": {"aspectRatio": "1:1"},
                        "responseModalities": ["TEXT", "IMAGE"],
                    },
                },
                error_classifier=_classify_error,
            )
        _raise_if_moderated(response)
        raster, media_type = _response_raster(response, self._policy.max_response_bytes)
        return GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type=media_type,
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

    @staticmethod
    def _validate_request(request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if (request.width, request.height) != (1024, 1024):
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {"x-goog-api-key": credential}


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    del status_code
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    markers = " ".join(
        value.lower() for key in ("code", "status") if isinstance((value := error.get(key)), str)
    )
    if any(marker in markers for marker in ("moderation", "safety", "blocked")):
        return ErrorCategory.MODERATED
    return None


def _raise_if_moderated(response: Mapping[str, object]) -> None:
    feedback = response.get("promptFeedback")
    if isinstance(feedback, Mapping):
        reason = feedback.get("blockReason")
        if isinstance(reason, str) and reason.upper() not in {"", "BLOCK_REASON_UNSPECIFIED"}:
            raise ProviderError(ErrorCategory.MODERATED)
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        finish_reason = candidate.get("finishReason")
        if isinstance(finish_reason, str) and any(
            marker in finish_reason.upper() for marker in ("SAFETY", "BLOCK", "PROHIBITED")
        ):
            raise ProviderError(ErrorCategory.MODERATED)


def _response_raster(response: Mapping[str, object], max_bytes: int) -> tuple[bytes, str]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    image_parts: list[Mapping[str, object]] = []
    for part in parts:
        if isinstance(part, Mapping) and isinstance(part.get("inlineData"), Mapping):
            image_parts.append(part["inlineData"])
    if len(image_parts) != 1:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    encoded = image_parts[0].get("data")
    media_type = image_parts[0].get("mimeType")
    if (
        not isinstance(encoded, str)
        or not isinstance(media_type, str)
        or len(encoded) > ((max_bytes + 2) // 3) * 4
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        raster = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if len(raster) > max_bytes:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return validate_raster(raster, media_type)


def _response_usage(response: Mapping[str, object]) -> Mapping[str, int | float | str]:
    usage: dict[str, int | float | str] = {"images": 1}
    metadata = response.get("usageMetadata")
    if not isinstance(metadata, Mapping):
        return usage
    keys = {
        "promptTokenCount": "input_tokens",
        "candidatesTokenCount": "output_tokens",
        "totalTokenCount": "total_tokens",
    }
    for provider_key, public_key in keys.items():
        value = metadata.get(provider_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[public_key] = value
    return usage
