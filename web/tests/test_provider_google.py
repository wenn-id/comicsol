from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.google import GoogleProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "google-canary-secret-that-must-never-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_request(
    *,
    references: tuple[Path, ...] = (),
    required_capabilities: frozenset[str] = frozenset({"text_to_image"}),
    provider_options: Mapping[str, object] | None = None,
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id="job-google",
        project_id="project-1",
        project_revision=1,
        subject_kind="panel",
        subject_id="p01-01",
        prompt="private prompt",
        negative_prompt=None,
        references=references,
        width=width,
        height=height,
        required_capabilities=required_capabilities,
        provider_options={} if provider_options is None else provider_options,
    )


def success_response(*, data: str | None = None, mime_type: str = "image/png") -> dict[str, object]:
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": "sanitized provider note"},
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64.b64encode(PNG).decode() if data is None else data,
                            }
                        },
                    ]
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 22,
            "totalTokenCount": 33,
        },
    }


class GoogleProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_model_and_text_request_translation(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=success_response(), request=request)

        provider = GoogleProvider(transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual(
            [("google", "gemini-2.5-flash-image")],
            [(model.provider, model.model) for model in models],
        )
        self.assertEqual(
            frozenset({"image_to_image", "reference_images", "text_to_image"}),
            models[0].capabilities,
        )
        self.assertEqual(
            {"currency": "USD", "model": "gemini-2.5-flash-image", "unit": "image"},
            await provider.estimate(make_request(), "gemini-2.5-flash-image"),
        )
        result = await provider.generate(make_request(), "gemini-2.5-flash-image", CANARY)
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual("image/png", result.media_type)
        self.assertEqual(
            {"images": 1, "input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            result.usage,
        )
        self.assertEqual(
            {"height": 1024, "model": "gemini-2.5-flash-image", "width": 1024},
            result.effective_parameters,
        )
        request = seen[0]
        self.assertEqual(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
            str(request.url),
        )
        self.assertEqual(CANARY, request.headers["x-goog-api-key"])
        self.assertNotIn(CANARY, str(request.url))
        self.assertEqual(
            {
                "contents": [{"parts": [{"text": "private prompt"}]}],
                "generationConfig": {
                    "imageConfig": {"aspectRatio": "1:1"},
                    "responseModalities": ["TEXT", "IMAGE"],
                },
            },
            json.loads(request.content),
        )

    async def test_reference_inputs_are_inline_and_paths_never_leave_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            second = Path(directory) / "second.png"
            first.write_bytes(PNG)
            second.write_bytes(PNG)
            seen: list[httpx.Request] = []

            async def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200, json=success_response(), request=request)

            result = await GoogleProvider(transport=httpx.MockTransport(handler)).generate(
                make_request(
                    references=(first, second),
                    required_capabilities=frozenset({"image_to_image", "reference_images"}),
                ),
                "gemini-2.5-flash-image",
                CANARY,
            )
        self.assertEqual(JobState.ACCEPTED, result.state)
        payload = json.loads(seen[0].content)
        self.assertEqual(
            {
                "imageConfig": {"aspectRatio": "1:1"},
                "responseModalities": ["TEXT", "IMAGE"],
            },
            payload["generationConfig"],
        )
        parts = payload["contents"][0]["parts"]
        self.assertEqual({"text": "private prompt"}, parts[0])
        self.assertEqual(2, len(parts[1:]))
        for part in parts[1:]:
            self.assertEqual("image/png", part["inlineData"]["mimeType"])
            self.assertEqual(PNG, base64.b64decode(part["inlineData"]["data"]))
        rendered = seen[0].content.decode()
        self.assertNotIn(str(first), rendered)
        self.assertNotIn(str(second), rendered)

    async def test_moderation_status_errors_and_canary_redaction(self) -> None:
        payloads = (
            (
                200,
                {"promptFeedback": {"blockReason": "SAFETY", "blockReasonMessage": CANARY}},
                ErrorCategory.MODERATED,
            ),
            (200, {"candidates": [{"finishReason": "SAFETY"}]}, ErrorCategory.MODERATED),
            (
                400,
                {"error": {"status": "FAILED_PRECONDITION", "message": CANARY}},
                ErrorCategory.PROVIDER_ERROR,
            ),
            (401, {"error": {"message": CANARY}}, ErrorCategory.INVALID_CREDENTIALS),
            (402, {"error": {"message": CANARY}}, ErrorCategory.QUOTA_EXHAUSTED),
            (429, {"error": {"message": CANARY}}, ErrorCategory.RATE_LIMITED),
            (503, {"error": {"message": CANARY}}, ErrorCategory.UNAVAILABLE),
        )
        for status, payload, category in payloads:

            async def handler(
                request: httpx.Request,
                response_status: int = status,
                response_payload: Mapping[str, object] = payload,
            ) -> httpx.Response:
                return httpx.Response(response_status, json=response_payload, request=request)

            with self.subTest(status=status, category=category):
                provider = GoogleProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), "gemini-2.5-flash-image", CANARY)
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_invalid_raster_and_unsupported_operations_fail_closed(self) -> None:
        fixtures = (
            success_response(data=base64.b64encode(b"not-raster").decode()),
            success_response(mime_type="text/html"),
            {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
        )
        for fixture in fixtures:

            async def handler(
                request: httpx.Request,
                response_payload: Mapping[str, object] = fixture,
            ) -> httpx.Response:
                return httpx.Response(200, json=response_payload, request=request)

            with self.subTest(fixture=fixture):
                with self.assertRaises(ProviderError) as caught:
                    await GoogleProvider(transport=httpx.MockTransport(handler)).generate(
                        make_request(), "gemini-2.5-flash-image", CANARY
                    )
                self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

        calls = 0

        async def forbidden(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, request=request)

        provider = GoogleProvider(transport=httpx.MockTransport(forbidden))
        for request, model, credential in (
            (make_request(), "unknown", CANARY),
            (
                make_request(required_capabilities=frozenset({"custom_dimensions"})),
                "gemini-2.5-flash-image",
                CANARY,
            ),
            (make_request(width=1536, height=1024), "gemini-2.5-flash-image", CANARY),
            (make_request(), "gemini-2.5-flash-image", None),
        ):
            with self.subTest(model=model, credential=credential):
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(request, model, credential)
                self.assertIn(
                    caught.exception.category,
                    {ErrorCategory.CAPABILITY_MISSING, ErrorCategory.INVALID_CREDENTIALS},
                )
        self.assertEqual(0, calls)
        with self.assertRaises(ProviderError) as poll_error:
            await provider.poll("job", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, poll_error.exception.category)
        with self.assertRaises(ProviderError) as cancel_error:
            await provider.cancel("job", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, cancel_error.exception.category)


if __name__ == "__main__":
    unittest.main()
