from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from urllib.parse import urlencode, urlsplit

import httpx

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, TransportPolicy, read_reference_raster

_API_ORIGIN = "https://api.bfl.ai"
_GENERATE_URL = f"{_API_ORIGIN}/v1/flux-pro-1.1"
_RESULT_URL = f"{_API_ORIGIN}/v1/get_result"
_MODEL = next(model for model in CATALOG if model.provider == "bfl")
_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DELIVERY_HOST = re.compile(r"delivery(?:-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)?\.bfl\.ai\Z")
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class BFLProvider:
    """Direct bounded asynchronous adapter for Black Forest Labs FLUX."""

    provider_id = "bfl"

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
        payload: dict[str, object] = {
            "height": request.height,
            "output_format": "png",
            "prompt": request.prompt,
            "width": request.width,
        }
        if request.references:
            content, _ = read_reference_raster(
                request.references[0], self._policy.max_response_bytes
            )
            payload["image_prompt"] = base64.b64encode(content).decode("ascii")
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                _GENERATE_URL,
                headers=_credential_headers(credential),
                payload=payload,
                error_classifier=_classify_error,
            )
        _raise_if_moderated(response)
        external_job_id = response.get("id")
        if (
            not isinstance(external_job_id, str)
            or _EXTERNAL_JOB_ID.fullmatch(external_job_id) is None
        ):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters={"height": request.height, "model": model, "width": request.width},
            usage={},
        )

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        if (
            not isinstance(external_job_id, str)
            or _EXTERNAL_JOB_ID.fullmatch(external_job_id) is None
        ):
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        headers = _credential_headers(credential)
        url = f"{_RESULT_URL}?{urlencode({'id': external_job_id})}"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.get_json(
                url,
                headers=headers,
                error_classifier=_classify_error,
            )
            _raise_if_moderated(response)
            status = response.get("status")
            if status in {"Pending", "Processing", "Queued", "Running"}:
                return GenerationResult(
                    external_job_id=external_job_id,
                    state=JobState.POLLING,
                    raster_bytes=None,
                    media_type=None,
                    effective_parameters={"model": _MODEL.model},
                    usage={},
                )
            if status == "Cancelled":
                return GenerationResult(
                    external_job_id=external_job_id,
                    state=JobState.CANCELLED,
                    raster_bytes=None,
                    media_type=None,
                    effective_parameters={"model": _MODEL.model},
                    usage={},
                )
            if status != "Ready":
                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
            result = response.get("result")
            if not isinstance(result, Mapping) or not isinstance(result.get("sample"), str):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            sample = result["sample"]
            try:
                delivery_origin = _delivery_origin(sample)
            except ValueError:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
            delivery_policy = replace(self._policy, approved_origins=frozenset({delivery_origin}))
            async with BoundedHTTPClient(
                delivery_policy, transport=self._transport
            ) as delivery_client:
                raster, media_type = await delivery_client.get_raster(sample)
        effective: dict[str, object] = {"model": _MODEL.model}
        seed = result.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            effective["seed"] = seed
        usage: dict[str, int | float | str] = {"images": 1}
        cost = response.get("cost")
        if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
            usage["cost_usd"] = cost
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type=media_type,
            effective_parameters=effective,
            usage=usage,
        )

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        del external_job_id, credential
        raise ProviderError(ErrorCategory.CAPABILITY_MISSING)

    @staticmethod
    def _validate_request(request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if (
            not 256 <= request.width <= 1440
            or not 256 <= request.height <= 1440
            or request.width % 32
            or request.height % 32
        ):
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if len(request.references) > 1:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _delivery_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid BFL delivery URL") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or _DELIVERY_HOST.fullmatch(host) is None
    ):
        raise ValueError("invalid BFL delivery URL")
    return f"https://{host}"


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {"x-key": credential}


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    del status_code
    markers = " ".join(
        value.lower() for key in ("code", "status") if isinstance((value := payload.get(key)), str)
    )
    if any(marker in markers for marker in ("moderation", "moderated", "safety")):
        return ErrorCategory.MODERATED
    return None


def _raise_if_moderated(response: Mapping[str, object]) -> None:
    status = response.get("status")
    if isinstance(status, str) and any(
        marker in status.lower() for marker in ("moderated", "moderation", "safety")
    ):
        raise ProviderError(ErrorCategory.MODERATED)
