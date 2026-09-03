"""Immutable, non-persistable values used by planning providers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

_PLAN_KEYS = frozenset({"storyPlan", "characterBible", "storyboard", "visualIdentityPack"})
_PANEL_CHECK_IDS = (
    "character-identity",
    "anatomy",
    "action",
    "composition",
    "continuity",
    "text-free",
    "technical",
)
_SUBJECTIVE_PAGE_CHECK_IDS = (
    "face-action-obstruction",
    "bubble-tail-direction",
    "accidental-text-watermark",
)
_MAX_SOURCE = 1_048_576
_MAX_RASTER = 20 * 1024 * 1024
_MAX_USAGE = 1_000_000_000_000
_MAX_CONTEXT_DEPTH = 12
_MAX_CONTEXT_ITEMS = 4_096
_MAX_CONTEXT_STRING = 1_048_576
_TOKEN_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}_tokens\Z")
_CHARACTER_TRAITS = frozenset(
    {
        "face",
        "hair",
        "age-appearance",
        "clothing",
        "accessories",
        "proportions",
        "immutable-traits",
    }
)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _validated_context(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_CONTEXT_DEPTH:
        raise ValueError("visual review context is invalid")
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("visual review context is invalid")
    if isinstance(value, str):
        if len(value) <= _MAX_CONTEXT_STRING:
            return
        raise ValueError("visual review context is invalid")
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTEXT_ITEMS or any(
            not isinstance(key, str) or not key or len(key) > 128 for key in value
        ):
            raise ValueError("visual review context is invalid")
        for item in value.values():
            _validated_context(item, depth=depth + 1)
        return
    if isinstance(value, list | tuple):
        if len(value) > _MAX_CONTEXT_ITEMS:
            raise ValueError("visual review context is invalid")
        for item in value:
            _validated_context(item, depth=depth + 1)
        return
    raise ValueError("visual review context is invalid")


def _validated_usage(value: object) -> Mapping[str, int | float]:
    if not isinstance(value, Mapping) or len(value) > 16:
        raise ValueError("provider usage is invalid")
    usage: dict[str, int | float] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or _TOKEN_FIELD.fullmatch(key) is None
            or isinstance(item, bool)
            or not isinstance(item, int | float)
            or not math.isfinite(item)
            or not 0 <= item <= _MAX_USAGE
        ):
            raise ValueError("provider usage is invalid")
        usage[key] = item
    return MappingProxyType(usage)


def _canonical_character_context(context: Mapping[str, object]) -> None:
    characters = context.get("characters")
    if characters is None:
        return
    if not isinstance(characters, list | tuple) or len(characters) > 64:
        raise ValueError("visual review context is invalid")
    identities: set[str] = set()
    for character in characters:
        if not isinstance(character, Mapping):
            raise ValueError("visual review context is invalid")
        character_id = character.get("character_id")
        traits = character.get("traits")
        if (
            not isinstance(character_id, str)
            or not character_id
            or len(character_id) > 128
            or character_id in identities
            or not isinstance(traits, list | tuple)
            or not traits
        ):
            raise ValueError("visual review context is invalid")
        identities.add(character_id)
        trait_ids: set[str] = set()
        for trait in traits:
            trait_id = trait.get("trait") if isinstance(trait, Mapping) else None
            if (
                not isinstance(trait, Mapping)
                or not isinstance(trait_id, str)
                or not trait_id
                or len(trait_id) > 128
                or trait_id not in _CHARACTER_TRAITS
                or trait_id in trait_ids
                or "expected" not in trait
            ):
                raise ValueError("visual review context is invalid")
            trait_ids.add(trait_id)


@dataclass(frozen=True)
class PlanRequest:
    title: str
    source: str = field(repr=False)
    language: str = "en"
    page_count: int = 1
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.title, str)
            or not self.title.strip()
            or len(self.title) > 512
            or not isinstance(self.source, str)
            or not self.source.strip()
            or len(self.source) > _MAX_SOURCE
            or not isinstance(self.language, str)
            or not self.language.strip()
            or len(self.language) > 64
            or isinstance(self.page_count, bool)
            or not 1 <= self.page_count <= 512
        ):
            raise ValueError("planning request is invalid")
        errors = tuple(self.validation_errors)
        if len(errors) > 32 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in errors
        ):
            raise ValueError("planning validation errors are invalid")
        object.__setattr__(self, "validation_errors", errors)


@dataclass(frozen=True)
class PlanResult:
    plan: Mapping[str, str] = field(repr=False)
    usage: Mapping[str, int | float] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan, Mapping)
            or set(self.plan) != _PLAN_KEYS
            or any(
                not isinstance(value, str) or not value or len(value) > _MAX_SOURCE
                for value in self.plan.values()
            )
        ):
            raise ValueError("planning result is invalid")
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "usage", _validated_usage(self.usage))


@dataclass(frozen=True)
class VisualReviewRequest:
    kind: Literal["panel", "page"]
    subject_id: str
    raster: bytes = field(repr=False)
    context: Mapping[str, object] = field(repr=False)
    check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.kind not in {"panel", "page"}
            or not isinstance(self.subject_id, str)
            or not self.subject_id
            or len(self.subject_id) > 128
            or not isinstance(self.raster, bytes)
            or not 0 < len(self.raster) <= _MAX_RASTER
            or not isinstance(self.context, Mapping)
        ):
            raise ValueError("visual review request is invalid")
        check_ids = tuple(self.check_ids)
        expected = _PANEL_CHECK_IDS if self.kind == "panel" else _SUBJECTIVE_PAGE_CHECK_IDS
        if check_ids != expected:
            raise ValueError("visual review check IDs are invalid")
        _validated_context(self.context)
        _canonical_character_context(self.context)
        object.__setattr__(self, "check_ids", check_ids)
        object.__setattr__(self, "context", _freeze_mapping(self.context))


@dataclass(frozen=True)
class VisualReviewResult:
    checks: tuple[Mapping[str, object], ...]
    character_assessments: tuple[Mapping[str, object], ...]
    usage: Mapping[str, int | float] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checks, tuple | list)
            or not isinstance(self.character_assessments, tuple | list)
            or any(not isinstance(item, Mapping) for item in self.checks)
            or any(not isinstance(item, Mapping) for item in self.character_assessments)
        ):
            raise ValueError("visual review result is invalid")
        object.__setattr__(self, "checks", tuple(_freeze_mapping(item) for item in self.checks))
        object.__setattr__(
            self,
            "character_assessments",
            tuple(_freeze_mapping(item) for item in self.character_assessments),
        )
        object.__setattr__(self, "usage", _validated_usage(self.usage))


@dataclass(frozen=True)
class PlanningModel:
    provider: str
    model: str
    enabled: bool
    required_environment_variable: str | None


class PlanningProvider(Protocol):
    provider_id: str
    model: str

    async def generate_plan(self, request: PlanRequest, credential: str) -> PlanResult: ...

    async def review_visual(
        self, request: VisualReviewRequest, credential: str
    ) -> VisualReviewResult: ...
