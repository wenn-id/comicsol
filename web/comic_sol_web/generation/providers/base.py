from __future__ import annotations

from types import MappingProxyType
from typing import Sequence

from ..types import ErrorCategory, ProviderAdapter

_ERROR_MESSAGES = MappingProxyType(
    {
        ErrorCategory.INVALID_CREDENTIALS: "provider credentials were rejected",
        ErrorCategory.QUOTA_EXHAUSTED: "provider quota is exhausted",
        ErrorCategory.RATE_LIMITED: "provider rate limit was reached",
        ErrorCategory.MODERATED: "provider moderation rejected the request",
        ErrorCategory.CAPABILITY_MISSING: "provider capability is unavailable",
        ErrorCategory.TIMEOUT: "provider request timed out",
        ErrorCategory.CANCELLED: "provider request was cancelled",
        ErrorCategory.UNAVAILABLE: "provider is unavailable",
        ErrorCategory.INVALID_OUTPUT: "provider output is invalid",
        ErrorCategory.PROVIDER_ERROR: "provider request failed",
    }
)


class ProviderError(Exception):
    """A provider failure carrying only normalized, non-sensitive details."""

    def __init__(
        self,
        category: ErrorCategory,
        *,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.status_code = status_code
        super().__init__(_ERROR_MESSAGES[category])

    def __repr__(self) -> str:
        return f"ProviderError(category={self.category.value!r}, status_code={self.status_code!r})"


class ProviderRegistry:
    """Immutable provider lookup keyed by each adapter's provider identifier."""

    def __init__(self, providers: Sequence[ProviderAdapter]) -> None:
        by_id: dict[str, ProviderAdapter] = {}
        for provider in providers:
            if not provider.provider_id or provider.provider_id in by_id:
                raise ValueError("provider identifiers must be unique and non-empty")
            by_id[provider.provider_id] = provider
        self._providers = MappingProxyType(by_id)

    def get(self, provider_id: str) -> ProviderAdapter:
        return self._providers[provider_id]
