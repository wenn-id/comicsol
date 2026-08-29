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

_API_ORIGIN = "https://api.replicate.com"
_MODEL = next(model for model in CATALOG if model.provider == "replicate")
_CREATE_URL = f"{_API_ORIGIN}/v1/models/{_MODEL.model}/predictions"
_PREDICTIONS_URL = f"{_API_ORIGIN}/v1/predictions"
_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z")
_DELIVERY_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?replicate\.delivery\Z")
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class ReplicateProvider:
    """Bounded prediction adapter for the curated official Replicate FLUX model."""

    provider_id = "replicate"

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
        headers = _credential_headers(credential)
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                _CREATE_URL,
                headers=headers,
                payload={
                    "input": {
                        "aspect_ratio": "custom",
                        "height": request.height,
                        "output_format": "png",
                        "prompt": request.prompt,
                        "width": request.width,
                    }
                },
                error_classifier=_classify_error,
            )
            return await self._translate_prediction(
                response,
                client,
                credential,
                expected_job_id=None,
                effective_parameters={
                    "height": request.height,
                    "model": model,
                    "width": request.width,
                },
            )

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        job_id = _validate_job_id(external_job_id)
        headers = _credential_headers(credential)
        url = f"{_PREDICTIONS_URL}/{quote(job_id, safe='')}"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.get_json(
                url,
                headers=headers,
                error_classifier=_classify_error,
            )
            return await self._translate_prediction(
                response,
                client,
                credential,
                expected_job_id=job_id,
                effective_parameters={"model": _MODEL.model},
            )

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        job_id = _validate_job_id(external_job_id)
        response_url = f"{_PREDICTIONS_URL}/{quote(job_id, safe='')}/cancel"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                response_url,
                headers=_credential_headers(credential),
                payload={},
                error_classifier=_classify_error,
            )
        if response.get("id") != job_id or response.get("status") not in {
            "canceled",
            "cancelled",
        }:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)

    async def _translate_prediction(
        self,
        response: Mapping[str, object],
        client: BoundedHTTPClient,
        credential: str | None,
        *,
        expected_job_id: str | None,
        effective_parameters: Mapping[str, object],
    ) -> GenerationResult:
        external_job_id = response.get("id")
        if (
            not isinstance(external_job_id, str)
            or _EXTERNAL_JOB_ID.fullmatch(external_job_id) is None
            or (expected_job_id is not None and external_job_id != expected_job_id)
        ):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        status = response.get("status")
        if status in {"starting", "processing"}:
            return _pending_result(external_job_id, effective_parameters)
        if status in {"canceled", "cancelled"}:
            return GenerationResult(
                external_job_id=external_job_id,
                state=JobState.CANCELLED,
                raster_bytes=None,
                media_type=None,
                effective_parameters=effective_parameters,
                usage={},
            )
        if status == "failed":
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        if status != "succeeded":
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)

        output_url = _single_output_url(response.get("output"))
        try:
            delivery_origin = _delivery_origin(output_url)
        except ValueError:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
        delivery_policy = replace(self._policy, approved_origins=frozenset({delivery_origin}))
        async with BoundedHTTPClient(delivery_policy, transport=self._transport) as delivery_client:
            raster, media_type = await delivery_client.get_raster(
                output_url,
                headers=_credential_headers(credential),
            )
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.ACCEPTED,
            raster_bytes=raster,
            media_type=media_type,
            effective_parameters=effective_parameters,
            usage=_usage(response),
        )

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


def _pending_result(
    external_job_id: str,
    effective_parameters: Mapping[str, object],
) -> GenerationResult:
    return GenerationResult(
        external_job_id=external_job_id,
        state=JobState.POLLING,
        raster_bytes=None,
        media_type=None,
        effective_parameters=effective_parameters,
        usage={},
    )


def _single_output_url(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list) and len(output) == 1 and isinstance(output[0], str):
        return output[0]
    raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _delivery_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid Replicate delivery URL") from None
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
        raise ValueError("invalid Replicate delivery URL")
    return f"https://{host}"


def _validate_job_id(external_job_id: str) -> str:
    if not isinstance(external_job_id, str) or _EXTERNAL_JOB_ID.fullmatch(external_job_id) is None:
        raise ProviderError(ErrorCategory.PROVIDER_ERROR)
    return external_job_id


def _credential_headers(credential: str | None) -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {"authorization": f"Bearer {credential}"}


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    del status_code
    markers = " ".join(
        value.lower()
        for key in ("status", "code", "detail", "error")
        if isinstance((value := payload.get(key)), str)
    )
    if any(marker in markers for marker in ("moderation", "moderated", "safety")):
        return ErrorCategory.MODERATED
    return None


def _usage(response: Mapping[str, object]) -> Mapping[str, int | float | str]:
    usage: dict[str, int | float | str] = {"images": 1}
    metrics = response.get("metrics")
    if not isinstance(metrics, Mapping):
        return usage
    for provider_key, public_key in (
        ("predict_time", "predict_time_seconds"),
        ("total_time", "total_time_seconds"),
    ):
        value = metrics.get(provider_key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            usage[public_key] = value
    return usage
