from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

import httpx

from ..types import ErrorCategory
from .base import ProviderError


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
        try:
            origin = _canonical_origin(url)
        except ValueError:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
        if origin not in self.policy.approved_origins:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        try:
            async with asyncio.timeout(self.policy.total_timeout):
                return await self._get_bounded(url, headers=headers)
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError(ErrorCategory.TIMEOUT) from None
        except httpx.NetworkError:
            raise ProviderError(ErrorCategory.UNAVAILABLE) from None
        except httpx.HTTPError:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR) from None

    async def _get_bounded(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
    ) -> bytes:
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                raise ProviderError(
                    ErrorCategory.PROVIDER_ERROR,
                    status_code=response.status_code,
                )
            if response.is_error:
                raise ProviderError(
                    _category_for_status(response.status_code),
                    status_code=response.status_code,
                )

            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self.policy.max_response_bytes:
                    raise ProviderError(ErrorCategory.INVALID_OUTPUT)
                body.extend(chunk)
            return bytes(body)


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
