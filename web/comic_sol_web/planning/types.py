"""Immutable, non-persistable values used by planning providers."""

from __future__ import annotations

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
    usage: Mapping[str, int | float | str] = field(repr=False)

    def __post_init__(self) -> None:
        if set(self.plan) != _PLAN_KEYS or any(
            not isinstance(value, str) or not value or len(value) > _MAX_SOURCE
            for value in self.plan.values()
        ):
            raise ValueError("planning result is invalid")
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


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
        ):
            raise ValueError("visual review request is invalid")
        check_ids = tuple(self.check_ids)
        expected = _PANEL_CHECK_IDS if self.kind == "panel" else _SUBJECTIVE_PAGE_CHECK_IDS
        if check_ids != expected:
            raise ValueError("visual review check IDs are invalid")
        object.__setattr__(self, "check_ids", check_ids)
        object.__setattr__(self, "context", _freeze_mapping(self.context))


@dataclass(frozen=True)
class VisualReviewResult:
    checks: tuple[Mapping[str, object], ...]
    character_assessments: tuple[Mapping[str, object], ...]
    usage: Mapping[str, int | float | str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(_freeze_mapping(item) for item in self.checks))
        object.__setattr__(
            self,
            "character_assessments",
            tuple(_freeze_mapping(item) for item in self.character_assessments),
        )
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


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
