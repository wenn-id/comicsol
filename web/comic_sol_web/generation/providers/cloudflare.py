from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence

import httpx

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, TransportPolicy, validate_raster

_API_ORIGIN = "https://api.cloudflare.com"
_MODEL = next(model for model in CATALOG if model.provider == "cloudflare")
_ACCOUNT_ID = re.compile(r"[A-Fa-f0-9]{32}\Z")
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_ALLOWED_RASTER_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class CloudflareProvider:
    """Bounded Workers AI adapter for the curated Cloudflare FLUX model."""

    provider_id = "cloudflare"

    def __init__(
        self,
        account_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
            raise ValueError("Cloudflare account identifier is invalid")
        self._account_id = account_id
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
        url = f"{_API_ORIGIN}/client/v4/accounts/{self._account_id}/ai/run/{_MODEL.model}"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            body, supplied_media_type, _ = await client._request(
                "POST",
                url,
                headers=_credential_headers(credential),
                payload={
                    "height": request.height,
                    "prompt": request.prompt,
                    "width": request.width,
                },
            )
        media_type = supplied_media_type.partition(";")[0].strip().lower()
        if media_type in _ALLOWED_RASTER_MEDIA_TYPES:
            raster, media_type = validate_raster(body, media_type)
        elif media_type == "application/json":
            raster, media_type = _json_raster(body, self._policy.max_response_bytes)
        else:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type=media_type,
            effective_parameters={
                "height": request.height,
                "model": model,
                "width": request.width,
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

    @staticmethod
    def _validate_request(request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.references or request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if (
            not 256 <= request.width <= 2048
            or not 256 <= request.height <= 2048
            or request.width % 8
            or request.height % 8
        ):
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _json_raster(body: bytes, max_bytes: int) -> tuple[bytes, str]:
    try:
        response = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if not isinstance(response, Mapping) or response.get("success") is not True:
        raise ProviderError(_cloudflare_error_category(response))
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    encoded = result.get("image")
    if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        raster = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if not raster or len(raster) > max_bytes:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return validate_raster(raster, "")


def _cloudflare_error_category(response: object) -> ErrorCategory:
    if not isinstance(response, Mapping):
        return ErrorCategory.INVALID_OUTPUT
    errors = response.get("errors")
    if not isinstance(errors, list):
        return ErrorCategory.PROVIDER_ERROR
    markers: list[str] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        for key in ("code", "message"):
            value = error.get(key)
            if isinstance(value, str | int) and not isinstance(value, bool):
                markers.append(str(value).lower())
    rendered = " ".join(markers)
    if any(marker in rendered for marker in ("quota", "limit exceeded", "insufficient")):
        return ErrorCategory.QUOTA_EXHAUSTED
    if any(marker in rendered for marker in ("rate", "too many")):
        return ErrorCategory.RATE_LIMITED
    if any(marker in rendered for marker in ("moderation", "safety", "content policy")):
        return ErrorCategory.MODERATED
    return ErrorCategory.PROVIDER_ERROR


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {
        "accept": "application/json, image/png, image/jpeg, image/webp",
        "authorization": f"Bearer {credential}",
    }
