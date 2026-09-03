from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from typing import cast

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.config import WebConfig
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.planning.providers import AnthropicPlanningProvider, OpenAIPlanningProvider
from comic_sol_web.planning.types import (
    PlanRequest,
    PlanResult,
    VisualReviewRequest,
    VisualReviewResult,
)


CANARY = "planning-provider-secret-that-must-not-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def plan_payload() -> dict[str, object]:
    return {
        "storyPlan": {"schema_version": "1.0"},
        "characterBible": {"characters": []},
        "storyboard": {"pages": []},
        "visualIdentityPack": {"characters": []},
    }


def visual_payload(check_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "checks": [
            {
                "id": check_id,
                "result": "pass",
                "severity": "info",
                "evidence": f"Visible {check_id} evidence.",
                "method": "bounded visual review",
                "reviewer": "openai:gpt-5.4-mini",
                "regions": [],
            }
            for check_id in check_ids
        ],
        "character_assessments": [],
    }


class PlanningProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = PlanRequest(
            title="Private title",
            source="Private source",
            language="en",
            page_count=1,
        )
        self.visual_request = VisualReviewRequest(
            kind="panel",
            subject_id="p01-01",
            raster=PNG,
            context={"characters": []},
            check_ids=(
                "character-identity",
                "anatomy",
                "action",
                "composition",
                "continuity",
                "text-free",
                "technical",
            ),
        )

    async def test_openai_returns_complete_four_document_plan(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": json.dumps(plan_payload())}
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 34},
                },
                request=request,
            )

        provider = OpenAIPlanningProvider(
            model="gpt-5.4-mini", transport=httpx.MockTransport(handler)
        )
        result = await provider.generate_plan(self.request, CANARY)

        self.assertEqual(
            {"storyPlan", "characterBible", "storyboard", "visualIdentityPack"},
            set(result.plan),
        )
        self.assertEqual(
            json.dumps(plan_payload()["storyPlan"], separators=(",", ":"), sort_keys=True),
            result.plan["storyPlan"],
        )
        self.assertEqual({"input_tokens": 12, "output_tokens": 34}, dict(result.usage))
        self.assertNotIn(CANARY, repr(result))
        self.assertEqual("https://api.openai.com/v1/responses", str(seen[0].url))
        self.assertEqual(f"Bearer {CANARY}", seen[0].headers["authorization"])
        body = json.loads(seen[0].content)
        self.assertEqual("gpt-5.4-mini", body["model"])
        self.assertEqual("json_schema", body["text"]["format"]["type"])
        self.assertIn("Private source", json.dumps(body))

    async def test_anthropic_rejects_text_without_required_tool_result(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "not a tool result"}]},
                request=request,
            )

        provider = AnthropicPlanningProvider(
            model="claude-sonnet-4-6", transport=httpx.MockTransport(handler)
        )
        with self.assertRaisesRegex(ProviderError, "output is invalid") as caught:
            await provider.generate_plan(self.request, CANARY)
        self.assertNotIn(CANARY, repr(caught.exception))

    async def test_anthropic_forces_a_bounded_tool_and_transient_image_input(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "submit_comic_sol_result",
                            "input": visual_payload(self.visual_request.check_ids),
                        }
                    ]
                },
                request=request,
            )

        provider = AnthropicPlanningProvider(
            model="claude-sonnet-4-6", transport=httpx.MockTransport(handler)
        )
        result = await provider.review_visual(self.visual_request, CANARY)

        self.assertEqual(
            self.visual_request.check_ids, tuple(check["id"] for check in result.checks)
        )
        self.assertNotIn(CANARY, repr(result))
        self.assertEqual("https://api.anthropic.com/v1/messages", str(seen[0].url))
        self.assertEqual(CANARY, seen[0].headers["x-api-key"])
        body = json.loads(seen[0].content)
        self.assertEqual({"type": "tool", "name": "submit_comic_sol_result"}, body["tool_choice"])
        self.assertFalse(body["tools"][0]["input_schema"]["additionalProperties"])
        self.assertEqual("base64", body["messages"][0]["content"][1]["source"]["type"])
        self.assertEqual(
            base64.b64encode(PNG).decode(), body["messages"][0]["content"][1]["source"]["data"]
        )

    async def test_visual_review_is_bounded_to_requested_check_ids(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        visual_payload(self.visual_request.check_ids)
                                    ),
                                }
                            ],
                        }
                    ]
                },
                request=request,
            )

        provider = OpenAIPlanningProvider(
            model="gpt-5.4-mini", transport=httpx.MockTransport(handler)
        )
        result = await provider.review_visual(self.visual_request, CANARY)
        self.assertEqual(
            self.visual_request.check_ids,
            tuple(check["id"] for check in result.checks),
        )
        self.assertNotIn(CANARY, repr(result))
        body = json.loads(seen[0].content)
        self.assertEqual(
            "data:image/png;base64," + base64.b64encode(PNG).decode(),
            body["input"][0]["content"][1]["image_url"],
        )

    async def test_character_context_requires_one_bounded_assessment_per_trait(self) -> None:
        request = VisualReviewRequest(
            kind="panel",
            subject_id="p01-01",
            raster=PNG,
            context={
                "characters": [
                    {
                        "character_id": "mira",
                        "traits": [{"trait": "face", "expected": "round face"}],
                    }
                ]
            },
            check_ids=self.visual_request.check_ids,
        )

        async def handler(http_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(visual_payload(request.check_ids)),
                                }
                            ],
                        }
                    ]
                },
                request=http_request,
            )

        provider = OpenAIPlanningProvider(
            model="gpt-5.4-mini", transport=httpx.MockTransport(handler)
        )
        with self.assertRaises(ProviderError):
            await provider.review_visual(request, CANARY)

    async def test_provider_rejects_oversized_region_fields(self) -> None:
        payload = visual_payload(self.visual_request.check_ids)
        checks = cast(list[dict[str, object]], payload["checks"])
        checks[0]["regions"] = [{"evidence": "x" * 9_000}]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(payload)}],
                        }
                    ]
                },
                request=request,
            )

        provider = OpenAIPlanningProvider(
            model="gpt-5.4-mini", transport=httpx.MockTransport(handler)
        )
        with self.assertRaises(ProviderError):
            await provider.review_visual(self.visual_request, CANARY)

    async def test_provider_rejects_oversized_review_method(self) -> None:
        payload = visual_payload(self.visual_request.check_ids)
        checks = cast(list[dict[str, object]], payload["checks"])
        checks[0]["method"] = "x" * 129

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(payload)}],
                        }
                    ]
                },
                request=request,
            )

        provider = OpenAIPlanningProvider(
            model="gpt-5.4-mini", transport=httpx.MockTransport(handler)
        )
        with self.assertRaises(ProviderError):
            await provider.review_visual(self.visual_request, CANARY)

    def test_values_are_immutable_and_redact_source_and_raster(self) -> None:
        self.assertNotIn("Private source", repr(self.request))
        self.assertNotIn(base64.b64encode(PNG).decode(), repr(self.visual_request))
        with self.assertRaises(TypeError):
            self.visual_request.context["new"] = "value"  # type: ignore[index]

    def test_malformed_context_results_and_usage_fail_deterministically(self) -> None:
        with self.assertRaises(ValueError):
            VisualReviewRequest(
                kind="panel",
                subject_id="p01-01",
                raster=PNG,
                context=[],  # type: ignore[arg-type]
                check_ids=self.visual_request.check_ids,
            )
        with self.assertRaises(ValueError):
            PlanResult(
                plan={key: "{}" for key in plan_payload()},
                usage=cast(dict[str, int | float], {"debug": "secret"}),
            )
        with self.assertRaises(ValueError):
            VisualReviewResult(
                checks=(), character_assessments=(), usage={"input_tokens": float("nan")}
            )


class PlanningConfigurationTests(unittest.TestCase):
    def test_local_config_exposes_only_credentialed_planning_models(self) -> None:
        root = Path("C:/comic-sol-test-data").resolve()
        config = WebConfig.local_from_env(
            {
                "COMIC_SOL_WEB_DATA_ROOT": str(root),
                "OPENAI_API_KEY": "test-openai-key",
                "COMIC_SOL_WEB_OPENAI_PLANNING_MODEL": "gpt-5.4-mini",
                "COMIC_SOL_WEB_ANTHROPIC_PLANNING_MODEL": "claude-sonnet-4-6",
            }
        )
        self.assertEqual("gpt-5.4-mini", config.openai_planning_model)
        self.assertEqual("claude-sonnet-4-6", config.anthropic_planning_model)
        self.assertEqual({"openai": "gpt-5.4-mini"}, dict(config.planning_model_options))


if __name__ == "__main__":
    unittest.main()
