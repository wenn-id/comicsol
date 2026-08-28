from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.openai import OpenAIProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "openai-canary-secret-that-must-never-escape"
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
        job_id="job-openai",
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


def image_response(*, data: str | None = None) -> dict[str, object]:
    return {
        "data": [{"b64_json": base64.b64encode(PNG).decode() if data is None else data}],
        "usage": {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
    }


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_model_and_text_request_translation(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=image_response(), request=request)

        provider = OpenAIProvider(transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual([("openai", "gpt-image-1")], [(m.provider, m.model) for m in models])
        self.assertEqual(
            frozenset({"custom_dimensions", "image_to_image", "reference_images", "text_to_image"}),
            models[0].capabilities,
        )
        estimate = await provider.estimate(make_request(), "gpt-image-1")
        self.assertEqual({"currency": "USD", "model": "gpt-image-1", "unit": "image"}, estimate)

        result = await provider.generate(make_request(), "gpt-image-1", CANARY)
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual("image/png", result.media_type)
        self.assertEqual(
            {"images": 1, "input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
            result.usage,
        )
        self.assertEqual(
            {"height": 1024, "model": "gpt-image-1", "width": 1024}, result.effective_parameters
        )
        self.assertEqual(1, len(seen))
        request = seen[0]
        self.assertEqual("https://api.openai.com/v1/images/generations", str(request.url))
        self.assertEqual(f"Bearer {CANARY}", request.headers["authorization"])
        self.assertNotIn(CANARY, str(request.url))
        self.assertEqual(
            {
                "model": "gpt-image-1",
                "n": 1,
                "output_format": "png",
                "prompt": "private prompt",
                "size": "1024x1024",
            },
            json.loads(request.content),
        )

    async def test_reference_input_uses_bounded_multipart_edit_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.png"
            reference.write_bytes(PNG)
            seen: list[httpx.Request] = []

            async def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200, json=image_response(), request=request)

            provider = OpenAIProvider(transport=httpx.MockTransport(handler))
            result = await provider.generate(
                make_request(
                    references=(reference,),
                    required_capabilities=frozenset({"image_to_image", "reference_images"}),
                ),
                "gpt-image-1",
                CANARY,
            )
        self.assertEqual(JobState.ACCEPTED, result.state)
        request = seen[0]
        self.assertEqual("https://api.openai.com/v1/images/edits", str(request.url))
        self.assertTrue(
            request.headers["content-type"].startswith("multipart/form-data; boundary=")
        )
        self.assertIn(PNG, request.content)
        self.assertIn(b'name="image[]"', request.content)
        self.assertIn(b'name="prompt"', request.content)
        self.assertNotIn(str(reference).encode(), request.content)

    async def test_errors_moderation_timeout_cancellation_and_redaction_are_normalized(
        self,
    ) -> None:
        cases = (
            (401, {"error": {"message": CANARY}}, ErrorCategory.INVALID_CREDENTIALS),
            (402, {"error": {"message": CANARY}}, ErrorCategory.QUOTA_EXHAUSTED),
            (429, {"error": {"message": CANARY}}, ErrorCategory.RATE_LIMITED),
            (
                400,
                {"error": {"code": "content_policy_violation", "message": CANARY}},
                ErrorCategory.MODERATED,
            ),
            (503, {"error": {"message": CANARY}}, ErrorCategory.UNAVAILABLE),
        )
        for status, payload, category in cases:

            async def handler(
                request: httpx.Request,
                response_status: int = status,
                response_payload: Mapping[str, object] = payload,
            ) -> httpx.Response:
                return httpx.Response(response_status, json=response_payload, request=request)

            with self.subTest(status=status):
                provider = OpenAIProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), "gpt-image-1", CANARY)
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(CANARY, request=request)

        with self.assertRaises(ProviderError) as timed_out:
            await OpenAIProvider(transport=httpx.MockTransport(timeout)).generate(
                make_request(), "gpt-image-1", CANARY
            )
        self.assertEqual(ErrorCategory.TIMEOUT, timed_out.exception.category)

        async def cancelled(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError(CANARY)

        with self.assertRaises(ProviderError) as cancelled_error:
            await OpenAIProvider(transport=httpx.MockTransport(cancelled)).generate(
                make_request(), "gpt-image-1", CANARY
            )
        self.assertEqual(ErrorCategory.CANCELLED, cancelled_error.exception.category)
        self.assertNotIn(CANARY, repr(cancelled_error.exception))

    async def test_invalid_raster_capabilities_dimensions_and_missing_credential_fail_closed(
        self,
    ) -> None:
        async def malformed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=image_response(data=base64.b64encode(b"not-raster").decode()),
                request=request,
            )

        with self.assertRaises(ProviderError) as invalid:
            await OpenAIProvider(transport=httpx.MockTransport(malformed)).generate(
                make_request(), "gpt-image-1", CANARY
            )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, invalid.exception.category)

        calls = 0

        async def forbidden(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, request=request)

        provider = OpenAIProvider(transport=httpx.MockTransport(forbidden))
        for request, model, credential in (
            (make_request(), "unknown", CANARY),
            (make_request(required_capabilities=frozenset({"async_jobs"})), "gpt-image-1", CANARY),
            (make_request(width=512, height=512), "gpt-image-1", CANARY),
            (make_request(), "gpt-image-1", None),
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
