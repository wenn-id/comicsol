from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from urllib.parse import quote, urlsplit

import httpx

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, TransportPolicy

_API_ORIGIN = "https://queue.fal.run"
_MODEL = next(model for model in CATALOG if model.provider == "fal")
_QUEUE_URL = f"{_API_ORIGIN}/{_MODEL.model}"
_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z")
_DELIVERY_HOSTS = frozenset({"fal.media", "v3.fal.media"})
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class FalProvider:
    """Bounded queue adapter for the curated fal.ai FLUX endpoint."""

    provider_id = "fal"

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
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                _QUEUE_URL,
                headers=_credential_headers(credential),
                payload={
                    "image_size": {"height": request.height, "width": request.width},
                    "num_images": 1,
                    "output_format": "png",
                    "prompt": request.prompt,
                },
                error_classifier=_classify_error,
            )
        external_job_id = response.get("request_id")
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
            effective_parameters={
                "height": request.height,
                "model": model,
                "width": request.width,
            },
            usage={},
        )

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        job_id = _validate_job_id(external_job_id)
        headers = _credential_headers(credential)
        request_url = f"{_QUEUE_URL}/requests/{quote(job_id, safe='')}"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            status_response = await client.get_json(
                f"{request_url}/status",
                headers=headers,
                error_classifier=_classify_error,
            )
            status = status_response.get("status")
            if status in {"IN_QUEUE", "IN_PROGRESS"}:
                return GenerationResult(
                    external_job_id=job_id,
                    state=JobState.POLLING,
                    raster_bytes=None,
                    media_type=None,
                    effective_parameters={"model": _MODEL.model},
                    usage={},
                )
            if status != "COMPLETED":
                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
            terminal = _terminal_error(status_response)
            if terminal is ErrorCategory.CANCELLED:
                return GenerationResult(
                    external_job_id=job_id,
                    state=JobState.CANCELLED,
                    raster_bytes=None,
                    media_type=None,
                    effective_parameters={"model": _MODEL.model},
                    usage={},
                )
            if terminal is not None:
                raise ProviderError(terminal)
            response = await client.get_json(
                request_url,
                headers=headers,
                error_classifier=_classify_error,
            )

        result = response.get("data")
        if result is None:
            result = response
        if not isinstance(result, Mapping):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        images = result.get("images")
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], Mapping):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        output_url = images[0].get("url")
        declared_media_type = images[0].get("content_type")
        if not isinstance(output_url, str) or not isinstance(declared_media_type, str):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        try:
            delivery_origin = _delivery_origin(output_url)
        except ValueError:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
        delivery_policy = replace(self._policy, approved_origins=frozenset({delivery_origin}))
        async with BoundedHTTPClient(delivery_policy, transport=self._transport) as delivery_client:
            raster, media_type = await delivery_client.get_raster(output_url)
        if declared_media_type.partition(";")[0].strip().lower() != media_type:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)

        effective: dict[str, object] = {"model": _MODEL.model}
        seed = result.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            effective["seed"] = seed
        usage: dict[str, int | float | str] = {"images": 1}
        metrics = status_response.get("metrics")
        if isinstance(metrics, Mapping):
            inference_time = metrics.get("inference_time")
            if (
                isinstance(inference_time, int | float)
                and not isinstance(inference_time, bool)
                and inference_time >= 0
            ):
                usage["inference_time_seconds"] = inference_time
        return GenerationResult(
            external_job_id=job_id,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type=media_type,
            effective_parameters=effective,
            usage=usage,
        )

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        job_id = _validate_job_id(external_job_id)
        url = f"{_QUEUE_URL}/requests/{quote(job_id, safe='')}/cancel"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client._request_json(
                "PUT",
                url,
                headers=_credential_headers(credential),
                error_classifier=_classify_error,
            )
        if response.get("status") != "CANCELLATION_REQUESTED":
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)

    @staticmethod
    def _validate_request(request: GenerationRequest, model: str) -> None:
        if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if request.references or request.negative_prompt is not None or request.provider_options:
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
        if (
            not 256 <= request.width <= 1440
            or not 256 <= request.height <= 1440
            or request.width % 32
            or request.height % 32
        ):
            raise ProviderError(ErrorCategory.CAPABILITY_MISSING)


def _delivery_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid fal.ai delivery URL") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host not in _DELIVERY_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("invalid fal.ai delivery URL")
    return f"https://{host}"


def _terminal_error(response: Mapping[str, object]) -> ErrorCategory | None:
    error = response.get("error")
    error_type = response.get("error_type")
    if not isinstance(error, str) and not isinstance(error_type, str):
        return None
    marker = error_type.lower() if isinstance(error_type, str) else ""
    if "cancel" in marker:
        return ErrorCategory.CANCELLED
    if any(value in marker for value in ("moderation", "safety", "content_policy")):
        return ErrorCategory.MODERATED
    return ErrorCategory.PROVIDER_ERROR


def _validate_job_id(external_job_id: str) -> str:
    if not isinstance(external_job_id, str) or _EXTERNAL_JOB_ID.fullmatch(external_job_id) is None:
        raise ProviderError(ErrorCategory.PROVIDER_ERROR)
    return external_job_id


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {
        "authorization": f"Key {credential}",
        "x-fal-no-retry": "1",
    }


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    del status_code
    markers = " ".join(
        value.lower()
        for key in ("status", "code", "detail", "error_type")
        if isinstance((value := payload.get(key)), str)
    )
    if any(marker in markers for marker in ("moderation", "safety", "content_policy")):
        return ErrorCategory.MODERATED
    return None
