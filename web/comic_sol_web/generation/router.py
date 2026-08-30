"""Deterministic, credential-aware Assisted provider/model recommendations."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from comic_sol_web.generation.catalog import CATALOG
from comic_sol_web.generation.types import AuthMode, ErrorCategory, GenerationRequest, ProviderModel

_COST_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}\Z")
_LATENCY_ORDER = {"low": 0, "medium": 1, "high": 2}
_HARD_AVAILABILITY_ERRORS = frozenset(
    {
        ErrorCategory.INVALID_CREDENTIALS,
        ErrorCategory.QUOTA_EXHAUSTED,
        ErrorCategory.CAPABILITY_MISSING,
        ErrorCategory.UNAVAILABLE,
    }
)
_DEGRADED_AVAILABILITY_ERRORS = frozenset(
    {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.MODERATED,
        ErrorCategory.TIMEOUT,
        ErrorCategory.PROVIDER_ERROR,
    }
)


@dataclass(frozen=True)
class RouterRecommendation:
    provider: str
    model: str
    auth_mode: AuthMode
    reasons: tuple[str, ...]
    estimated_cost: Mapping[str, object] | None


@dataclass(frozen=True)
class _RankedRecommendation:
    rank: tuple[object, ...]
    recommendation: RouterRecommendation


def _auth_modes(value: object) -> tuple[AuthMode, ...]:
    if isinstance(value, AuthMode | str):
        values: Iterable[object] = (value,)
    elif isinstance(value, Iterable):
        values = value
    else:
        return ()
    modes: set[AuthMode] = set()
    for item in values:
        if not isinstance(item, AuthMode | str):
            continue
        try:
            modes.add(item if isinstance(item, AuthMode) else AuthMode(item))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(modes, key=lambda mode: mode.value))


def _available_auth_modes(
    available_credentials: Mapping[Any, Any],
    provider: str,
    model: str,
) -> tuple[AuthMode, ...]:
    value = available_credentials.get((provider, model))
    if value is None:
        value = available_credentials.get(provider)
    return _auth_modes(value)


def _observation(
    history: Mapping[Any, Any],
    provider: str,
    model: str,
) -> Mapping[str, object]:
    for key in ((provider, model), f"{provider}:{model}", provider):
        value = history.get(key)
        if isinstance(value, Mapping):
            return {str(item_key): item for item_key, item in value.items()}
    return {}


def _error_category(value: object) -> ErrorCategory | None:
    if isinstance(value, ErrorCategory):
        return value
    if isinstance(value, str):
        try:
            return ErrorCategory(value)
        except ValueError:
            return None
    return None


def _availability_rank(observation: Mapping[str, object]) -> tuple[int, str]:
    error = _error_category(observation.get("last_error"))
    if error in _HARD_AVAILABILITY_ERRORS:
        assert error is not None
        return 2, f"Observed provider availability is blocked by {error.value}."
    if error in _DEGRADED_AVAILABILITY_ERRORS:
        assert error is not None
        return 1, f"Observed provider availability is degraded by {error.value}."
    available = observation.get("available")
    if available is True:
        return 0, "Observed provider availability is available."
    if available is False:
        return 2, "Observed provider availability is unavailable."
    return 1, "Provider availability is unknown; no observation was supplied."


def _estimated_cost(
    observation: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, tuple[object, ...], str]:
    raw = observation.get("estimated_cost")
    if not isinstance(raw, Mapping):
        return None, (1, "", "", Decimal(0)), "Estimated cost is unknown; no price was supplied."
    amount = raw.get("amount")
    currency = raw.get("currency")
    unit = raw.get("unit")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int | float)
        or not math.isfinite(float(amount))
        or amount < 0
        or not isinstance(currency, str)
        or len(currency) != 3
        or currency.upper() != currency
        or not isinstance(unit, str)
        or _COST_TOKEN.fullmatch(unit) is None
    ):
        return (
            None,
            (1, "", "", Decimal(0)),
            "Estimated cost is unknown; no comparable price was supplied.",
        )
    value = MappingProxyType({"amount": amount, "currency": currency, "unit": unit})
    rank = (0, currency, unit, Decimal(str(amount)))
    return value, rank, f"Estimated cost is {amount} {currency} per {unit}."


def _latency_rank(observation: Mapping[str, object]) -> tuple[int, str]:
    latency = observation.get("latency_class")
    if isinstance(latency, str) and latency in _LATENCY_ORDER:
        return _LATENCY_ORDER[latency], f"Observed latency class is {latency}."
    return 3, "Latency class is unknown; no observation was supplied."


def recommend(
    request: GenerationRequest,
    available_credentials: Mapping[Any, Any],
    history: Mapping[Any, Any],
    candidates: Iterable[ProviderModel] = CATALOG,
) -> tuple[RouterRecommendation, ...]:
    """Return all eligible candidate entries in a stable, disclosed order.

    The function consumes only caller-supplied candidates, credential
    availability, and observations. It never probes a credential, provider,
    price, or network. ``candidates`` defaults to the curated catalog.
    """
    required = request.required_capabilities
    references_required = bool(request.references)
    ranked: list[_RankedRecommendation] = []
    for entry in candidates:
        if not entry.enabled or not required <= entry.capabilities:
            continue
        if references_required and "reference_images" not in entry.capabilities:
            continue
        modes = _available_auth_modes(available_credentials, entry.provider, entry.model)
        if not modes:
            continue
        observation = _observation(history, entry.provider, entry.model)
        availability_rank, availability_reason = _availability_rank(observation)
        cost, cost_rank, cost_reason = _estimated_cost(observation)
        latency_rank, latency_reason = _latency_rank(observation)
        capability_excess = len(entry.capabilities - required)
        reference_rank = 0
        reference_reason = (
            "Model declares the required reference_images capability."
            if references_required
            else "Reference images were not requested."
        )
        for mode in modes:
            recommendation = RouterRecommendation(
                provider=entry.provider,
                model=entry.model,
                auth_mode=mode,
                reasons=(
                    "Model declares every required capability; "
                    f"{capability_excess} additional capabilities remain.",
                    reference_reason,
                    f"The {mode.value} authentication mode is available for this user.",
                    availability_reason,
                    cost_reason,
                    latency_reason,
                ),
                estimated_cost=cost,
            )
            ranked.append(
                _RankedRecommendation(
                    rank=(
                        availability_rank,
                        capability_excess,
                        reference_rank,
                        cost_rank,
                        latency_rank,
                        mode.value,
                        entry.provider,
                        entry.model,
                    ),
                    recommendation=recommendation,
                )
            )
    ranked.sort(key=lambda item: item.rank)
    return tuple(item.recommendation for item in ranked)
