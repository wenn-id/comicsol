from __future__ import annotations

import json
import unittest
from typing import Mapping

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.fal import FalProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "fal-canary-secret-that-must-never-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_request(
    *,
    provider_options: Mapping[str, object] | None = None,
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id="job-fal",
        project_id="project-1",
        project_revision=1,
        subject_kind="panel",
        subject_id="p01-01",
        prompt="private prompt",
        negative_prompt=None,
        references=(),
        width=width,
        height=height,
        required_capabilities=frozenset({"text_to_image"}),
        provider_options={} if provider_options is None else provider_options,
    )


class FalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_queue_submission_and_flux_provenance(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "request_id": "764cabcf-b745-4b3e-ae38-1200304cf45b",
                    "status_url": "https://queue.fal.run/fal-ai/flux-pro/v1.1/requests/ignored/status",
                    "response_url": "https://queue.fal.run/fal-ai/flux-pro/v1.1/requests/ignored",
                    "cancel_url": "https://queue.fal.run/fal-ai/flux-pro/v1.1/requests/ignored/cancel",
                },
                request=request,
            )

        provider = FalProvider(transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual(
            [("fal", "fal-ai/flux-pro/v1.1")],
            [(model.provider, model.model) for model in models],
        )
        self.assertEqual(
            {"currency": "USD", "model": "fal-ai/flux-pro/v1.1", "unit": "image"},
            await provider.estimate(make_request(), "fal-ai/flux-pro/v1.1"),
        )
        result = await provider.generate(make_request(), "fal-ai/flux-pro/v1.1", CANARY)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertEqual("764cabcf-b745-4b3e-ae38-1200304cf45b", result.external_job_id)
        self.assertEqual(
            {"height": 1024, "model": "fal-ai/flux-pro/v1.1", "width": 1024},
            result.effective_parameters,
        )
        request = seen[0]
        self.assertEqual("https://queue.fal.run/fal-ai/flux-pro/v1.1", str(request.url))
        self.assertEqual(f"Key {CANARY}", request.headers["authorization"])
        self.assertEqual("1", request.headers["x-fal-no-retry"])
        self.assertEqual(
            {
                "image_size": {"height": 1024, "width": 1024},
                "num_images": 1,
                "output_format": "png",
                "prompt": "private prompt",
            },
            json.loads(request.content),
        )

    async def test_bounded_queue_polling_completion_and_duplicate_envelopes(self) -> None:
        status_polls = 0
        result_fetches = 0
        deliveries = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_polls, result_fetches, deliveries
            if request.url.host == "queue.fal.run" and request.url.path.endswith("/status"):
                status_polls += 1
                status = "IN_QUEUE" if status_polls == 1 else "COMPLETED"
                return httpx.Response(
                    200,
                    json={"status": status, "metrics": {"inference_time": 2.5}},
                    request=request,
                )
            if request.url.host == "queue.fal.run":
                result_fetches += 1
                return httpx.Response(
                    200,
                    json={
                        "images": [
                            {
                                "url": "https://v3.fal.media/files/output.png?signature=safe",
                                "content_type": "image/png",
                                "width": 1024,
                                "height": 1024,
                            }
                        ],
                        "seed": 73,
                    },
                    request=request,
                )
            if request.url.host == "v3.fal.media":
                deliveries += 1
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )
            self.fail(f"unexpected fixture request: {request.method} {request.url}")

        provider = FalProvider(transport=httpx.MockTransport(handler))
        pending = await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        complete = await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        duplicate = await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        self.assertEqual(JobState.POLLING, pending.state)
        self.assertEqual(JobState.ACCEPTED, complete.state)
        self.assertEqual(complete, duplicate)
        self.assertEqual(PNG, complete.raster_bytes)
        self.assertEqual("image/png", complete.media_type)
        self.assertEqual(
            {"model": "fal-ai/flux-pro/v1.1", "seed": 73}, complete.effective_parameters
        )
        self.assertEqual({"images": 1, "inference_time_seconds": 2.5}, complete.usage)
        self.assertEqual(3, status_polls)
        self.assertEqual(2, result_fetches)
        self.assertEqual(2, deliveries)

    async def test_failure_cancellation_and_cancel_method_are_translated(self) -> None:
        seen: list[httpx.Request] = []
        mode = "failed"

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "PUT":
                return httpx.Response(
                    202,
                    json={"status": "CANCELLATION_REQUESTED"},
                    request=request,
                )
            if mode == "failed":
                return httpx.Response(
                    200,
                    json={"status": "COMPLETED", "error": CANARY, "error_type": "INTERNAL"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"status": "COMPLETED", "error": CANARY, "error_type": "CANCELLED"},
                request=request,
            )

        provider = FalProvider(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as failed:
            await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, failed.exception.category)
        self.assertNotIn(CANARY, repr(failed.exception))
        mode = "cancelled"
        cancelled = await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        await provider.cancel("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
        self.assertEqual("PUT", seen[-1].method)
        self.assertEqual(
            "https://queue.fal.run/fal-ai/flux-pro/v1.1/requests/764cabcf-b745-4b3e-ae38-1200304cf45b/cancel",
            str(seen[-1].url),
        )

    async def test_output_origin_redirect_mime_byte_and_raster_attacks_fail_closed(self) -> None:
        attack_urls = (
            "http://v3.fal.media/files/output.png",
            "https://v3.fal.media.evil.example/files/output.png",
            "https://v3.fal.media@evil.example/files/output.png",
            "https://v3.fal.media:444/files/output.png",
            "https://evil.fal.media/files/output.png",
        )
        for output_url in attack_urls:
            calls = 0

            async def origin_attack(
                request: httpx.Request, fixture_url: str = output_url
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                if request.url.path.endswith("/status"):
                    return httpx.Response(200, json={"status": "COMPLETED"}, request=request)
                return httpx.Response(
                    200,
                    json={"images": [{"url": fixture_url, "content_type": "image/png"}]},
                    request=request,
                )

            with self.subTest(output_url=output_url), self.assertRaises(ProviderError) as caught:
                await FalProvider(transport=httpx.MockTransport(origin_attack)).poll(
                    "764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY
                )
            self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
            self.assertEqual(2, calls)

        fixtures = (
            (
                302,
                b"",
                {"location": f"https://v3.fal.media/next?token={CANARY}"},
                ErrorCategory.PROVIDER_ERROR,
                1024,
            ),
            (
                200,
                PNG + b"too-large",
                {"content-type": "image/png"},
                ErrorCategory.INVALID_OUTPUT,
                len(PNG),
            ),
            (
                200,
                PNG,
                {"content-type": "text/html"},
                ErrorCategory.INVALID_OUTPUT,
                len(PNG),
            ),
            (
                200,
                b"not-a-raster",
                {"content-type": "image/png"},
                ErrorCategory.INVALID_OUTPUT,
                len(PNG),
            ),
        )
        for status, content, headers, expected_category, response_limit in fixtures:

            async def invalid_delivery(
                request: httpx.Request,
                response_status: int = status,
                response_content: bytes = content,
                response_headers: Mapping[str, str] = headers,
            ) -> httpx.Response:
                if request.url.path.endswith("/status"):
                    return httpx.Response(200, json={"status": "COMPLETED"}, request=request)
                if request.url.host == "queue.fal.run":
                    return httpx.Response(
                        200,
                        json={
                            "images": [
                                {
                                    "url": "https://v3.fal.media/files/output.png",
                                    "content_type": "image/png",
                                }
                            ]
                        },
                        request=request,
                    )
                return httpx.Response(
                    response_status,
                    content=response_content,
                    headers=response_headers,
                    request=request,
                )

            with self.subTest(status=status, content=content[:8]):
                provider = FalProvider(
                    transport=httpx.MockTransport(invalid_delivery),
                    max_response_bytes=response_limit,
                )
                with self.assertRaises(ProviderError) as caught:
                    await provider.poll("764cabcf-b745-4b3e-ae38-1200304cf45b", CANARY)
                self.assertEqual(expected_category, caught.exception.category)
                self.assertNotIn(CANARY, repr(caught.exception))

    async def test_quota_rate_unknown_models_and_secret_redaction_fail_closed(self) -> None:
        for status, category in (
            (402, ErrorCategory.QUOTA_EXHAUSTED),
            (429, ErrorCategory.RATE_LIMITED),
        ):

            async def error_response(
                request: httpx.Request, response_status: int = status
            ) -> httpx.Response:
                return httpx.Response(response_status, json={"detail": CANARY}, request=request)

            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                await FalProvider(transport=httpx.MockTransport(error_response)).generate(
                    make_request(), "fal-ai/flux-pro/v1.1", CANARY
                )
            self.assertEqual(category, caught.exception.category)
            self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

        calls = 0

        async def forbidden(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, request=request)

        provider = FalProvider(transport=httpx.MockTransport(forbidden))
        for request, model, credential in (
            (make_request(), "fal-ai/community/arbitrary", CANARY),
            (
                make_request(provider_options={"schema": {"arbitrary": True}}),
                "fal-ai/flux-pro/v1.1",
                CANARY,
            ),
            (make_request(), "fal-ai/flux-pro/v1.1", None),
        ):
            with self.subTest(model=model), self.assertRaises(ProviderError) as caught:
                await provider.generate(request, model, credential)
            self.assertIn(
                caught.exception.category,
                {ErrorCategory.CAPABILITY_MISSING, ErrorCategory.INVALID_CREDENTIALS},
            )
        self.assertEqual(0, calls)


if __name__ == "__main__":
    unittest.main()
