"""Bounded HTTP adapters for provider-authored plans and visual reviews."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
import httpx

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.http import BoundedHTTPClient, TransportPolicy
from comic_sol_web.generation.types import ErrorCategory

from .types import PlanRequest, PlanResult, VisualReviewRequest, VisualReviewResult

_OPENAI_ORIGIN = "https://api.openai.com"
_ANTHROPIC_ORIGIN = "https://api.anthropic.com"
_OPENAI_URL = f"{_OPENAI_ORIGIN}/v1/responses"
_ANTHROPIC_URL = f"{_ANTHROPIC_ORIGIN}/v1/messages"
_TOOL_NAME = "submit_comic_sol_result"
_MAX_TEXT = 8_192
_MAX_EVIDENCE = 2_048
_MAX_REGIONS = 64
_MAX_METHOD = 128
_MAX_REGION_BYTES = 8_192
_MAX_ASSESSMENTS = 448
_PLAN_KEYS = ("storyPlan", "characterBible", "storyboard", "visualIdentityPack")
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
_GENERIC_EVIDENCE = frozenset({"verified", "looks good", "ok", "pass"})
_CHARACTER_TRAITS = (
    "face",
    "hair",
    "age-appearance",
    "clothing",
    "accessories",
    "proportions",
    "immutable-traits",
)


def _policy(
    origin: str, transport: httpx.AsyncBaseTransport | None, max_response_bytes: int
) -> tuple[TransportPolicy, httpx.AsyncBaseTransport | None]:
    if max_response_bytes < 1:
        raise ValueError("response byte limit is invalid")
    return (
        TransportPolicy(
            approved_origins=frozenset({origin}),
            connect_timeout=5.0,
            read_timeout=30.0,
            total_timeout=60.0,
            max_response_bytes=max_response_bytes,
        ),
        transport,
    )


def _credential_header(credential: str, name: str, value_prefix: str = "") -> Mapping[str, str]:
    if not isinstance(credential, str) or not credential:
        raise ProviderError(ErrorCategory.INVALID_CREDENTIALS)
    return {name: f"{value_prefix}{credential}"}


def _usage(response: Mapping[str, object]) -> Mapping[str, int | float]:
    source = response.get("usage")
    if not isinstance(source, Mapping):
        return {}
    return {
        key: value
        for key, value in source.items()
        if isinstance(key, str)
        and key.endswith("tokens")
        and isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    }


def _plan_schema() -> dict[str, object]:
    def array_document(name: str) -> dict[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [name],
            "properties": {name: {"type": "array", "items": {"type": "object"}}},
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_PLAN_KEYS),
        "properties": {
            "storyPlan": {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version"],
                "properties": {"schema_version": {"type": "string"}},
            },
            "characterBible": array_document("characters"),
            "storyboard": array_document("pages"),
            "visualIdentityPack": array_document("characters"),
        },
    }


def _visual_schema(check_ids: tuple[str, ...]) -> dict[str, object]:
    check = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "result", "severity", "evidence", "method", "reviewer", "regions"],
        "properties": {
            "id": {"type": "string", "enum": list(check_ids)},
            "result": {"type": "string", "enum": ["pass", "warning", "fail"]},
            "severity": {"type": "string", "enum": ["info", "warning", "error"]},
            "evidence": {"type": "string"},
            "method": {"type": "string"},
            "reviewer": {"type": "string"},
            "regions": {"type": "array", "items": {"type": "object"}},
        },
    }
    assessment = {
        "type": "object",
        "additionalProperties": False,
        "required": ["character_id", "trait", "result", "severity", "evidence"],
        "properties": {
            "character_id": {"type": "string"},
            "trait": {"type": "string", "enum": list(_CHARACTER_TRAITS)},
            "result": {"type": "string", "enum": ["pass", "warning", "fail"]},
            "severity": {"type": "string", "enum": ["warning", "error"]},
            "evidence": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks", "character_assessments"],
        "properties": {
            "checks": {"type": "array", "items": check},
            "character_assessments": {"type": "array", "items": assessment},
        },
    }


def _plan_instruction(request: PlanRequest) -> str:
    repair = ""
    if request.validation_errors:
        repair = " Previous validation errors: " + "; ".join(request.validation_errors)
    return (
        "Create a Comic Sol canonical Plan. Return only the four required JSON object "
        f"documents. Title: {request.title}. Language: {request.language}. Pages: {request.page_count}."
        f" Source: {request.source}.{repair}"
    )


def _visual_instruction(request: VisualReviewRequest) -> str:
    return (
        f"Review the {request.kind} raster for Comic Sol subject {request.subject_id}. "
        f"Return checks in this exact order: {', '.join(request.check_ids)}. "
        "Evidence must identify visible, concrete evidence; do not use generic verdicts. "
        f"Context: {json.dumps(_plain(request.context), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, frozenset | set):
        return sorted((_plain(item) for item in value), key=repr)
    return value


def _decode_json(value: object) -> Mapping[str, object]:
    if not isinstance(value, str) or len(value) > _MAX_TEXT * 128:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if not isinstance(parsed, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return parsed


def _openai_output_text(response: Mapping[str, object]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    texts = [
        item.get("text")
        for message in output
        if isinstance(message, Mapping) and isinstance(message.get("content"), list)
        for item in message["content"]
        if isinstance(item, Mapping) and item.get("type") == "output_text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return texts[0]


def _plan_result(payload: Mapping[str, object], usage: Mapping[str, int | float]) -> PlanResult:
    if set(payload) != set(_PLAN_KEYS) or any(
        not isinstance(payload.get(key), Mapping) for key in _PLAN_KEYS
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    plan = {
        key: json.dumps(payload[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key in _PLAN_KEYS
    }
    try:
        return PlanResult(plan=plan, usage=usage)
    except ValueError:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return normalized


def _bounded_region(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value or len(value) > 16:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    region = _plain(value)
    try:
        encoded = json.dumps(
            region, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if len(encoded) > _MAX_REGION_BYTES:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if not all(isinstance(key, str) and key and len(key) <= 128 for key in value):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return region if isinstance(region, Mapping) else {}


def _character_traits(request: VisualReviewRequest) -> tuple[tuple[str, str], ...]:
    characters = request.context.get("characters")
    if not isinstance(characters, tuple):
        return ()
    return tuple(
        (character["character_id"], trait["trait"])
        for character in characters
        if isinstance(character, Mapping)
        and isinstance(character.get("character_id"), str)
        and isinstance(character.get("traits"), tuple)
        for trait in character["traits"]
        if isinstance(trait, Mapping)
        and isinstance(trait.get("trait"), str)
        and trait["trait"] in _CHARACTER_TRAITS
    )


def _assessments(
    value: object, expected: tuple[tuple[str, str], ...]
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or len(value) > _MAX_ASSESSMENTS or len(value) != len(expected):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    assessments: list[Mapping[str, object]] = []
    for assessment, identity in zip(value, expected):
        if not isinstance(assessment, Mapping) or set(assessment) != {
            "character_id",
            "trait",
            "result",
            "severity",
            "evidence",
        }:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        character_id = _bounded_text(assessment.get("character_id"), 128)
        trait = assessment.get("trait")
        evidence = _bounded_text(assessment.get("evidence"), _MAX_EVIDENCE)
        if (
            (character_id, trait) != identity
            or trait not in _CHARACTER_TRAITS
            or assessment.get("result") not in {"pass", "warning", "fail"}
            or assessment.get("severity") not in {"warning", "error"}
            or evidence.casefold() in _GENERIC_EVIDENCE
        ):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        assessments.append(
            {
                "character_id": character_id,
                "trait": trait,
                "result": assessment["result"],
                "severity": assessment["severity"],
                "evidence": evidence,
            }
        )
    return tuple(assessments)


def _review_result(
    payload: Mapping[str, object],
    request: VisualReviewRequest,
    usage: Mapping[str, int | float],
    reviewer: str,
) -> VisualReviewResult:
    if set(payload) != {"checks", "character_assessments"}:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    raw_checks = payload.get("checks")
    raw_assessments = payload.get("character_assessments")
    if not isinstance(raw_checks, list) or not isinstance(raw_assessments, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if len(raw_checks) != len(request.check_ids):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    checks: list[Mapping[str, object]] = []
    for check_id, raw in zip(request.check_ids, raw_checks):
        if not isinstance(raw, Mapping) or set(raw) != {
            "id",
            "result",
            "severity",
            "evidence",
            "method",
            "reviewer",
            "regions",
        }:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        regions = raw.get("regions")
        evidence = _bounded_text(raw.get("evidence"), _MAX_EVIDENCE)
        method = _bounded_text(raw.get("method"), _MAX_METHOD)
        if (
            raw.get("id") != check_id
            or raw.get("result") not in {"pass", "warning", "fail"}
            or raw.get("severity") not in {"info", "warning", "error"}
            or evidence.casefold() in _GENERIC_EVIDENCE
            or not isinstance(regions, list)
            or len(regions) > _MAX_REGIONS
        ):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        checks.append(
            {
                **dict(raw),
                "evidence": evidence,
                "method": method,
                "reviewer": reviewer,
                "regions": [_bounded_region(region) for region in regions],
            }
        )
    try:
        return VisualReviewResult(
            tuple(checks), _assessments(raw_assessments, _character_traits(request)), usage
        )
    except ValueError:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None


class OpenAIPlanningProvider:
    provider_id = "openai"

    def __init__(
        self,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("OpenAI planning model is invalid")
        self.model = model
        self._policy, self._transport = _policy(_OPENAI_ORIGIN, transport, max_response_bytes)

    async def generate_plan(self, request: PlanRequest, credential: str) -> PlanResult:
        response = await self._post(
            _plan_instruction(request), _plan_schema(), credential, image=None
        )
        return _plan_result(_decode_json(_openai_output_text(response)), _usage(response))

    async def review_visual(
        self, request: VisualReviewRequest, credential: str
    ) -> VisualReviewResult:
        response = await self._post(
            _visual_instruction(request),
            _visual_schema(request.check_ids),
            credential,
            image=request.raster,
        )
        return _review_result(
            _decode_json(_openai_output_text(response)),
            request,
            _usage(response),
            f"openai:{self.model}",
        )

    async def _post(
        self,
        instruction: str,
        schema: Mapping[str, object],
        credential: str,
        *,
        image: bytes | None,
    ) -> Mapping[str, object]:
        content: list[Mapping[str, object]] = [{"type": "input_text", "text": instruction}]
        if image is not None:
            content.append(
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                }
            )
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            return await client.post_json(
                _OPENAI_URL,
                headers=_credential_header(credential, "authorization", "Bearer "),
                payload={
                    "model": self.model,
                    "input": [{"role": "user", "content": content}],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "comic_sol_result",
                            "strict": True,
                            "schema": schema,
                        }
                    },
                },
            )


class AnthropicPlanningProvider:
    provider_id = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("Anthropic planning model is invalid")
        self.model = model
        self._policy, self._transport = _policy(_ANTHROPIC_ORIGIN, transport, max_response_bytes)

    async def generate_plan(self, request: PlanRequest, credential: str) -> PlanResult:
        response = await self._post(
            _plan_instruction(request), _plan_schema(), credential, image=None
        )
        return _plan_result(self._tool_input(response), _usage(response))

    async def review_visual(
        self, request: VisualReviewRequest, credential: str
    ) -> VisualReviewResult:
        response = await self._post(
            _visual_instruction(request),
            _visual_schema(request.check_ids),
            credential,
            image=request.raster,
        )
        return _review_result(
            self._tool_input(response), request, _usage(response), f"anthropic:{self.model}"
        )

    async def _post(
        self,
        instruction: str,
        schema: Mapping[str, object],
        credential: str,
        *,
        image: bytes | None,
    ) -> Mapping[str, object]:
        content: list[Mapping[str, object]] = [{"type": "text", "text": instruction}]
        if image is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image).decode("ascii"),
                    },
                }
            )
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            return await client.post_json(
                _ANTHROPIC_URL,
                headers={
                    **_credential_header(credential, "x-api-key"),
                    "anthropic-version": "2023-06-01",
                },
                payload={
                    "model": self.model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": content}],
                    "tools": [
                        {
                            "name": _TOOL_NAME,
                            "description": "Submit the bounded Comic Sol result.",
                            "input_schema": schema,
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": _TOOL_NAME},
                },
            )

    @staticmethod
    def _tool_input(response: Mapping[str, object]) -> Mapping[str, object]:
        content = response.get("content")
        if not isinstance(content, list):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        matches = [
            item
            for item in content
            if isinstance(item, Mapping)
            and item.get("type") == "tool_use"
            and item.get("name") == _TOOL_NAME
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("input"), Mapping):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return matches[0]["input"]
