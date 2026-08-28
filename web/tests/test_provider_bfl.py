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
from comic_sol_web.generation.providers.bfl import BFLProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "bfl-canary-secret-that-must-never-escape"
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
        job_id="job-bfl",
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


class BFLProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_direct_flux_provenance_and_async_request_translation(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "request-123",
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=request-123",
                },
                request=request,
            )

        provider = BFLProvider(transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual([("bfl", "flux-1.1-pro")], [(m.provider, m.model) for m in models])
        self.assertEqual(
            frozenset(
                {
                    "async_jobs",
                    "custom_dimensions",
                    "image_to_image",
                    "reference_images",
                    "text_to_image",
                }
            ),
            models[0].capabilities,
        )
        self.assertEqual(
            {"currency": "USD", "model": "flux-1.1-pro", "unit": "image"},
            await provider.estimate(make_request(), "flux-1.1-pro"),
        )
        result = await provider.generate(make_request(), "flux-1.1-pro", CANARY)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertEqual("request-123", result.external_job_id)
        self.assertIsNone(result.raster_bytes)
        request = seen[0]
        self.assertEqual("https://api.bfl.ai/v1/flux-pro-1.1", str(request.url))
        self.assertEqual(CANARY, request.headers["x-key"])
        self.assertNotIn(CANARY, str(request.url))
        self.assertEqual(
            {
                "height": 1024,
                "output_format": "png",
                "prompt": "private prompt",
                "width": 1024,
            },
            json.loads(request.content),
        )

    async def test_single_reference_is_bounded_base64_and_multiple_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.png"
            reference.write_bytes(PNG)
            seen: list[httpx.Request] = []

            async def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200, json={"id": "request-ref"}, request=request)

            provider = BFLProvider(transport=httpx.MockTransport(handler))
            result = await provider.generate(
                make_request(
                    references=(reference,),
                    required_capabilities=frozenset({"image_to_image", "reference_images"}),
                ),
                "flux-1.1-pro",
                CANARY,
            )
            with self.assertRaises(ProviderError) as multiple:
                await provider.generate(
                    make_request(
                        references=(reference, reference),
                        required_capabilities=frozenset({"reference_images"}),
                    ),
                    "flux-1.1-pro",
                    CANARY,
                )
        self.assertEqual(JobState.POLLING, result.state)
        payload = json.loads(seen[0].content)
        self.assertEqual(PNG, base64.b64decode(payload["image_prompt"]))
        self.assertNotIn(str(reference), seen[0].content.decode())
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, multiple.exception.category)
        self.assertEqual(1, len(seen))

    async def test_poll_translates_pending_then_fetches_and_validates_ready_output(self) -> None:
        seen: list[httpx.Request] = []
        polls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            seen.append(request)
            if request.url.path == "/v1/get_result":
                polls += 1
                if polls == 1:
                    return httpx.Response(200, json={"status": "Pending"}, request=request)
                return httpx.Response(
                    200,
                    json={
                        "status": "Ready",
                        "result": {
                            "sample": "https://delivery.bfl.ai/output/request-123.png?signature=signed-value",
                            "seed": 77,
                        },
                        "cost": 0.04,
                    },
                    request=request,
                )
            if request.url.host == "delivery.bfl.ai":
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )
            self.fail(
                f"unexpected fixture request: {request.method} {request.url.host}{request.url.path}"
            )

        provider = BFLProvider(transport=httpx.MockTransport(handler))
        pending = await provider.poll("request-123", CANARY)
        ready = await provider.poll("request-123", CANARY)
        self.assertEqual(JobState.POLLING, pending.state)
        self.assertEqual("request-123", pending.external_job_id)
        self.assertEqual(JobState.ACCEPTED, ready.state)
        self.assertEqual(PNG, ready.raster_bytes)
        self.assertEqual("image/png", ready.media_type)
        self.assertEqual({"model": "flux-1.1-pro", "seed": 77}, ready.effective_parameters)
        self.assertEqual({"cost_usd": 0.04, "images": 1}, ready.usage)
        poll_requests = [request for request in seen if request.url.path == "/v1/get_result"]
        self.assertEqual(2, len(poll_requests))
        self.assertTrue(all(request.url.params["id"] == "request-123" for request in poll_requests))
        self.assertTrue(all(request.headers["x-key"] == CANARY for request in poll_requests))

    async def test_poll_accepts_strict_regional_delivery_origin(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/get_result":
                return httpx.Response(
                    200,
                    json={
                        "status": "Ready",
                        "result": {"sample": "https://delivery-eu1.bfl.ai/output/request-123.png"},
                    },
                    request=request,
                )
            if request.url.host == "delivery-eu1.bfl.ai":
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )
            self.fail(f"unexpected fixture request: {request.url}")

        result = await BFLProvider(transport=httpx.MockTransport(handler)).poll(
            "request-123", CANARY
        )
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual(
            ["api.bfl.ai", "delivery-eu1.bfl.ai"], [request.url.host for request in seen]
        )

    async def test_poll_rejects_noncanonical_delivery_lookalikes_before_fetch(self) -> None:
        samples = (
            "http://delivery-eu1.bfl.ai/output.png",
            "https://delivery-eu1.bfl.ai:444/output.png",
            "https://delivery-eu1.bfl.ai.evil.example/output.png",
            "https://delivery-eu1.bfl.ai@evil.example/output.png",
            "https://delivery.eu1.bfl.ai/output.png",
            "https://delivery-eu1.evil.bfl.ai/output.png",
        )
        for sample in samples:
            calls = 0

            async def handler(request: httpx.Request, result_url: str = sample) -> httpx.Response:
                nonlocal calls
                calls += 1
                if request.url.path == "/v1/get_result":
                    return httpx.Response(
                        200,
                        json={"status": "Ready", "result": {"sample": result_url}},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )

            with self.subTest(sample=sample):
                with self.assertRaises(ProviderError) as caught:
                    await BFLProvider(transport=httpx.MockTransport(handler)).poll(
                        "request-123", CANARY
                    )
                self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
                self.assertEqual(1, calls)

    async def test_moderation_quota_rate_invalid_credentials_and_redaction_are_normalized(
        self,
    ) -> None:
        fixtures = (
            (200, {"status": "Content Moderated", "details": CANARY}, ErrorCategory.MODERATED),
            (401, {"detail": CANARY}, ErrorCategory.INVALID_CREDENTIALS),
            (402, {"detail": CANARY}, ErrorCategory.QUOTA_EXHAUSTED),
            (429, {"detail": CANARY}, ErrorCategory.RATE_LIMITED),
            (503, {"detail": CANARY}, ErrorCategory.UNAVAILABLE),
        )
        for status, payload, category in fixtures:

            async def handler(
                request: httpx.Request,
                response_status: int = status,
                response_payload: Mapping[str, object] = payload,
            ) -> httpx.Response:
                return httpx.Response(response_status, json=response_payload, request=request)

            with self.subTest(status=status, category=category):
                provider = BFLProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    if status == 200:
                        await provider.poll("request-123", CANARY)
                    else:
                        await provider.generate(make_request(), "flux-1.1-pro", CANARY)
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_redirect_byte_limit_invalid_mime_and_cancellation_fail_closed(self) -> None:
        fixtures = (
            (302, b"", {"location": f"https://delivery.bfl.ai/output?token={CANARY}"}),
            (200, PNG + b"too-large", {"content-type": "image/png"}),
            (200, b"<html>not an image</html>", {"content-type": "text/html"}),
        )
        for status, content, headers in fixtures:

            async def handler(
                request: httpx.Request,
                response_status: int = status,
                response_content: bytes = content,
                response_headers: dict[str, str] = headers,
            ) -> httpx.Response:
                if request.url.path == "/v1/get_result":
                    return httpx.Response(
                        200,
                        json={
                            "status": "Ready",
                            "result": {"sample": "https://delivery.bfl.ai/output.png"},
                        },
                        request=request,
                    )
                return httpx.Response(
                    response_status,
                    content=response_content,
                    headers=response_headers,
                    request=request,
                )

            with self.subTest(status=status):
                provider = BFLProvider(
                    transport=httpx.MockTransport(handler),
                    max_response_bytes=len(PNG),
                )
                with self.assertRaises(ProviderError) as caught:
                    await provider.poll("request-123", CANARY)
                self.assertIn(
                    caught.exception.category,
                    {ErrorCategory.PROVIDER_ERROR, ErrorCategory.INVALID_OUTPUT},
                )
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

        provider = BFLProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
        )
        with self.assertRaises(ProviderError) as cancelled:
            await provider.cancel("request-123", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, cancelled.exception.category)
        with self.assertRaises(ProviderError) as invalid_job:
            await provider.poll(f"request?token={CANARY}", CANARY)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, invalid_job.exception.category)
        self.assertNotIn(CANARY, repr(invalid_job.exception))
        with self.assertRaises(ProviderError) as missing_credential:
            await provider.generate(make_request(), "flux-1.1-pro", None)
        self.assertEqual(ErrorCategory.INVALID_CREDENTIALS, missing_credential.exception.category)


if __name__ == "__main__":
    unittest.main()
