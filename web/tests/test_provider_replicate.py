from __future__ import annotations

import asyncio
import json
import unittest
from typing import Mapping

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.replicate import ReplicateProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "replicate-canary-secret-that-must-never-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_request(
    *,
    model_options: Mapping[str, object] | None = None,
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id="job-replicate",
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
        provider_options={} if model_options is None else model_options,
    )


class ReplicateProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_prediction_creation_and_flux_provenance(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                201,
                json={"id": "prediction-123", "status": "starting"},
                request=request,
            )

        provider = ReplicateProvider(transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual(
            [("replicate", "black-forest-labs/flux-1.1-pro")],
            [(model.provider, model.model) for model in models],
        )
        self.assertEqual(
            {"currency": "USD", "model": "black-forest-labs/flux-1.1-pro", "unit": "image"},
            await provider.estimate(make_request(), "black-forest-labs/flux-1.1-pro"),
        )
        result = await provider.generate(make_request(), "black-forest-labs/flux-1.1-pro", CANARY)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertEqual("prediction-123", result.external_job_id)
        self.assertEqual(
            {"height": 1024, "model": "black-forest-labs/flux-1.1-pro", "width": 1024},
            result.effective_parameters,
        )
        request = seen[0]
        self.assertEqual(
            "https://api.replicate.com/v1/models/black-forest-labs/flux-1.1-pro/predictions",
            str(request.url),
        )
        self.assertEqual(f"Bearer {CANARY}", request.headers["authorization"])
        self.assertNotIn(CANARY, str(request.url))
        self.assertEqual(
            {
                "input": {
                    "aspect_ratio": "custom",
                    "height": 1024,
                    "output_format": "png",
                    "prompt": "private prompt",
                    "width": 1024,
                }
            },
            json.loads(request.content),
        )

    async def test_bounded_polling_completion_and_duplicate_envelopes(self) -> None:
        polls = 0
        deliveries = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls, deliveries
            if request.url.host == "api.replicate.com":
                polls += 1
                if polls == 1:
                    return httpx.Response(
                        200,
                        json={"id": "prediction-123", "status": "processing"},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": "prediction-123",
                        "status": "succeeded",
                        "output": "https://replicate.delivery/output/prediction-123.png?signature=safe",
                        "metrics": {"predict_time": 1.25, "total_time": 1.75},
                    },
                    request=request,
                )
            if request.url.host == "replicate.delivery":
                deliveries += 1
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )
            self.fail(f"unexpected fixture request: {request.method} {request.url}")

        provider = ReplicateProvider(transport=httpx.MockTransport(handler))
        pending = await provider.poll("prediction-123", CANARY)
        complete = await provider.poll("prediction-123", CANARY)
        duplicate = await provider.poll("prediction-123", CANARY)
        self.assertEqual(JobState.POLLING, pending.state)
        self.assertEqual(JobState.ACCEPTED, complete.state)
        self.assertEqual(complete, duplicate)
        self.assertEqual(PNG, complete.raster_bytes)
        self.assertEqual("image/png", complete.media_type)
        self.assertEqual(
            {"images": 1, "predict_time_seconds": 1.25, "total_time_seconds": 1.75},
            complete.usage,
        )
        self.assertEqual(3, polls)
        self.assertEqual(2, deliveries)

    async def test_failure_and_cancellation_are_translated_and_cancel_is_bounded(self) -> None:
        seen: list[httpx.Request] = []
        responses = iter(
            (
                {"id": "prediction-failed", "status": "failed", "error": CANARY},
                {"id": "prediction-cancelled", "status": "canceled"},
                {"id": "prediction-running", "status": "canceled"},
            )
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=next(responses), request=request)

        provider = ReplicateProvider(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as failed:
            await provider.poll("prediction-failed", CANARY)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, failed.exception.category)
        self.assertNotIn(CANARY, f"{failed.exception!s} {failed.exception!r}")
        cancelled = await provider.poll("prediction-cancelled", CANARY)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        await provider.cancel("prediction-running", CANARY)
        self.assertEqual("POST", seen[-1].method)
        self.assertEqual(
            "https://api.replicate.com/v1/predictions/prediction-running/cancel",
            str(seen[-1].url),
        )

    async def test_output_origin_redirect_mime_byte_and_raster_attacks_fail_closed(self) -> None:
        attack_urls = (
            "http://replicate.delivery/output.png",
            "https://replicate.delivery.evil.example/output.png",
            "https://replicate.delivery@evil.example/output.png",
            "https://replicate.delivery:444/output.png",
            "https://evilreplicate.delivery/output.png",
        )
        for output_url in attack_urls:
            calls = 0

            async def origin_attack(
                request: httpx.Request, fixture_url: str = output_url
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(
                    200,
                    json={"id": "prediction-123", "status": "succeeded", "output": fixture_url},
                    request=request,
                )

            with self.subTest(output_url=output_url), self.assertRaises(ProviderError) as caught:
                await ReplicateProvider(transport=httpx.MockTransport(origin_attack)).poll(
                    "prediction-123", CANARY
                )
            self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
            self.assertEqual(1, calls)

        fixtures = (
            (302, b"", {"location": f"https://replicate.delivery/next?token={CANARY}"}),
            (200, PNG + b"too-large", {"content-type": "image/png"}),
            (200, PNG, {"content-type": "text/html"}),
            (200, b"not-a-raster", {"content-type": "image/png"}),
        )
        for status, content, headers in fixtures:

            async def invalid_delivery(
                request: httpx.Request,
                response_status: int = status,
                response_content: bytes = content,
                response_headers: Mapping[str, str] = headers,
            ) -> httpx.Response:
                if request.url.host == "api.replicate.com":
                    return httpx.Response(
                        200,
                        json={
                            "id": "prediction-123",
                            "status": "succeeded",
                            "output": "https://replicate.delivery/output.png",
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
                provider = ReplicateProvider(
                    transport=httpx.MockTransport(invalid_delivery),
                    max_response_bytes=len(PNG),
                )
                with self.assertRaises(ProviderError) as caught:
                    await provider.poll("prediction-123", CANARY)
                self.assertIn(
                    caught.exception.category,
                    {ErrorCategory.PROVIDER_ERROR, ErrorCategory.INVALID_OUTPUT},
                )
                self.assertNotIn(CANARY, repr(caught.exception))

    async def test_quota_rate_timeout_unknown_models_and_secrets_fail_closed(self) -> None:
        for status, category in (
            (402, ErrorCategory.QUOTA_EXHAUSTED),
            (429, ErrorCategory.RATE_LIMITED),
        ):

            async def error_response(
                request: httpx.Request, response_status: int = status
            ) -> httpx.Response:
                return httpx.Response(
                    response_status,
                    json={"detail": CANARY},
                    request=request,
                )

            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                await ReplicateProvider(transport=httpx.MockTransport(error_response)).generate(
                    make_request(), "black-forest-labs/flux-1.1-pro", CANARY
                )
            self.assertEqual(category, caught.exception.category)
            self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(CANARY, request=request)

        with self.assertRaises(ProviderError) as timed_out:
            await ReplicateProvider(transport=httpx.MockTransport(timeout)).poll(
                "prediction-123", CANARY
            )
        self.assertEqual(ErrorCategory.TIMEOUT, timed_out.exception.category)

        async def cancelled(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError(CANARY)

        with self.assertRaises(ProviderError) as cancellation:
            await ReplicateProvider(transport=httpx.MockTransport(cancelled)).poll(
                "prediction-123", CANARY
            )
        self.assertEqual(ErrorCategory.CANCELLED, cancellation.exception.category)
        self.assertNotIn(CANARY, repr(cancellation.exception))

        calls = 0

        async def forbidden(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, request=request)

        provider = ReplicateProvider(transport=httpx.MockTransport(forbidden))
        for request, model, credential in (
            (make_request(), "community/arbitrary-model:unsafe-schema", CANARY),
            (
                make_request(model_options={"schema": {"arbitrary": True}}),
                "black-forest-labs/flux-1.1-pro",
                CANARY,
            ),
            (make_request(), "black-forest-labs/flux-1.1-pro", None),
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
