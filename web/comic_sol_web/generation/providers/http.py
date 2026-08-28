from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from ..types import ErrorCategory
from .base import ProviderError

JSONErrorClassifier = Callable[[int, Mapping[str, object]], ErrorCategory | None]
MultipartFile = tuple[str, tuple[str, bytes, str]]
_ALLOWED_RASTER_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_FORMAT_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_MAX_RASTER_PIXELS = 40_000_000
_MAX_DECODED_RASTER_BYTES = 160 * 1024 * 1024
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class TransportPolicy:
    approved_origins: frozenset[str]
    connect_timeout: float
    read_timeout: float
    total_timeout: float
    max_response_bytes: int

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0 or self.read_timeout <= 0 or self.total_timeout <= 0:
            raise ValueError("transport timeouts must be positive")
        if self.max_response_bytes <= 0:
            raise ValueError("response byte limit must be positive")
        origins = frozenset(_canonical_origin(origin) for origin in self.approved_origins)
        object.__setattr__(self, "approved_origins", origins)


DEFAULT_TRANSPORT_POLICY = TransportPolicy(
    approved_origins=frozenset(),
    connect_timeout=5.0,
    read_timeout=30.0,
    total_timeout=60.0,
    max_response_bytes=20 * 1024 * 1024,
)


class BoundedHTTPClient:
    """The sole bounded HTTPX transport boundary for provider adapters."""

    def __init__(
        self,
        policy: TransportPolicy = DEFAULT_TRANSPORT_POLICY,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy
        timeout = httpx.Timeout(
            timeout=policy.read_timeout,
            connect=policy.connect_timeout,
            read=policy.read_timeout,
            write=policy.read_timeout,
            pool=policy.connect_timeout,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> BoundedHTTPClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        body, _ = await self._request_bytes("GET", url, headers=headers)
        return body

    async def get_raster(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[bytes, str]:
        body, media_type = await self._request_bytes("GET", url, headers=headers)
        return validate_raster(body, media_type)

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        error_classifier: JSONErrorClassifier | None = None,
    ) -> Mapping[str, object]:
        return await self._request_json(
            "GET",
            url,
            headers=headers,
            error_classifier=error_classifier,
        )

    async def post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
        error_classifier: JSONErrorClassifier | None = None,
    ) -> Mapping[str, object]:
        return await self._request_json(
            "POST",
            url,
            headers=headers,
            payload=payload,
            error_classifier=error_classifier,
        )

    async def post_multipart(
        self,
        url: str,
        *,
        fields: Mapping[str, str],
        files: Sequence[MultipartFile],
        headers: Mapping[str, str] | None = None,
        error_classifier: JSONErrorClassifier | None = None,
    ) -> Mapping[str, object]:
        return await self._request_json(
            "POST",
            url,
            headers=headers,
            fields=fields,
            files=files,
            error_classifier=error_classifier,
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        payload: Mapping[str, object] | None = None,
        fields: Mapping[str, str] | None = None,
        files: Sequence[MultipartFile] | None = None,
        error_classifier: JSONErrorClassifier | None,
    ) -> Mapping[str, object]:
        body, media_type, status_code = await self._request(
            method,
            url,
            headers=headers,
            payload=payload,
            fields=fields,
            files=files,
            allow_error_status=True,
        )
        parsed = _parse_json_object(body, media_type, required=status_code < 400)
        if status_code >= 400:
            category = None if error_classifier is None else error_classifier(status_code, parsed)
            raise ProviderError(
                _category_for_status(status_code) if category is None else category,
                status_code=status_code,
            )
        return parsed

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
    ) -> tuple[bytes, str]:
        body, media_type, _ = await self._request(method, url, headers=headers)
        return body, media_type

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        payload: Mapping[str, object] | None = None,
        fields: Mapping[str, str] | None = None,
        files: Sequence[MultipartFile] | None = None,
        allow_error_status: bool = False,
    ) -> tuple[bytes, str, int]:
        try:
            origin = _canonical_origin(url)
        except ValueError:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
        if origin not in self.policy.approved_origins:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        try:
            async with asyncio.timeout(self.policy.total_timeout):
                async with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    data=fields,
                    files=files,
                ) as response:
                    if response.is_redirect:
                        raise ProviderError(
                            ErrorCategory.PROVIDER_ERROR,
                            status_code=response.status_code,
                        )
                    if response.is_error and not allow_error_status:
                        raise ProviderError(
                            _category_for_status(response.status_code),
                            status_code=response.status_code,
                        )
                    body = await self._read_bounded(response)
                    media_type = response.headers.get("content-type", "")
                    return body, media_type, response.status_code
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise ProviderError(ErrorCategory.CANCELLED) from None
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(ErrorCategory.TIMEOUT) from None
        except httpx.NetworkError:
            raise ProviderError(ErrorCategory.UNAVAILABLE) from None
        except httpx.HTTPError:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR) from None

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self.policy.max_response_bytes:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            body.extend(chunk)
        return bytes(body)


def read_reference_raster(path: Path, max_bytes: int) -> tuple[bytes, str]:
    """Read one plain local raster without following a final-component link."""
    try:
        if path.is_symlink():
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        descriptor = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
    except ProviderError:
        raise
    except OSError:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(max_bytes + 1)
    except OSError:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    finally:
        os.close(descriptor)
    if not content or len(content) > max_bytes:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return validate_raster(content, "")


def validate_raster(content: bytes, supplied_media_type: str) -> tuple[bytes, str]:
    media_type = supplied_media_type.partition(";")[0].strip().lower()
    if media_type and media_type not in _ALLOWED_RASTER_TYPES:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        with Image.open(io.BytesIO(content)) as raster:
            sniffed = _FORMAT_MEDIA_TYPES.get(raster.format or "")
            width, height = raster.size
            bands = len(raster.getbands())
            if (
                sniffed is None
                or width <= 0
                or height <= 0
                or width * height > _MAX_RASTER_PIXELS
                or width * height * max(1, bands) > _MAX_DECODED_RASTER_BYTES
            ):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            raster.verify()
    except ProviderError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if media_type and media_type != sniffed:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return content, sniffed


def _parse_json_object(body: bytes, media_type: str, *, required: bool) -> Mapping[str, object]:
    normalized_media_type = media_type.partition(";")[0].strip().lower()
    if normalized_media_type != "application/json":
        if required:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return {}
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        if required:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
        return {}
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        if required:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return {}
    return parsed


def _canonical_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid provider origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid provider origin")
    host = parsed.hostname.lower()
    if parsed.scheme != "https" and not _is_loopback_host(host):
        raise ValueError("cleartext provider origins must be loopback")
    default_port = 80 if parsed.scheme == "http" else 443
    port_suffix = "" if port is None or port == default_port else f":{port}"
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{rendered_host}{port_suffix}"


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _category_for_status(status_code: int) -> ErrorCategory:
    if status_code in {401, 403}:
        return ErrorCategory.INVALID_CREDENTIALS
    if status_code == 402:
        return ErrorCategory.QUOTA_EXHAUSTED
    if status_code == 429:
        return ErrorCategory.RATE_LIMITED
    if status_code in {408, 504}:
        return ErrorCategory.TIMEOUT
    if status_code in {502, 503}:
        return ErrorCategory.UNAVAILABLE
    return ErrorCategory.PROVIDER_ERROR
