from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.stability import StabilityProvider, _MODEL
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "stability-canary-secret-that-must-never-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)

_MODEL_ID = _MODEL.model


def make_request(
    *,
    job_id: str = "job-stability",
    references: tuple[Path, ...] = (),
    required_capabilities: frozenset[str] = frozenset({"text_to_image", "async_jobs"}),
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id=job_id,
        project_id="project-1",
        project_revision=1,
        subject_kind="panel",
        subject_id="p01-01",
        prompt="private stability prompt",
        negative_prompt=None,
        references=references,
        width=width,
        height=height,
        required_capabilities=required_capabilities,
        provider_options={},
    )


def _b64_png() -> str:
    return base64.b64encode(PNG).decode("ascii")


class StabilityCatalogTests(unittest.TestCase):
    def test_curated_stability_model_is_pinned(self) -> None:
        self.assertEqual("stability", _MODEL.provider)
        self.assertEqual(_MODEL_ID, _MODEL.model)
        self.assertTrue(_MODEL.enabled)
        self.assertTrue(_MODEL.capabilities)

    def test_curated_capabilities_omit_unsupported_surface(self) -> None:
        capabilities = frozenset(_MODEL.capabilities)
        for unsupported in {
            "cancellation",
            "seed",
            "negative_prompt",
            "image_to_image",
            "reference_images",
        }:
            self.assertNotIn(unsupported, capabilities)
        self.assertEqual(
            frozenset({"async_jobs", "custom_dimensions", "text_to_image"}),
            capabilities,
        )


class StabilityProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_id_and_list_models_are_curated(self) -> None:
        provider = StabilityProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        self.assertEqual("stability", provider.provider_id)
        models = await provider.list_models()
        self.assertEqual(1, len(models))
        self.assertEqual(_MODEL_ID, models[0].model)
        self.assertEqual("stability", models[0].provider)

    async def test_generate_sends_form_submit_and_returns_polling(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = b"".join(request.stream)
            # The async submit endpoint always sends a multipart body with the
            # curated text fields and an explicit empty "none" part, and the
            # canary secret never appears in the body.
            self.assertIn(b'name="none"', body)
            self.assertIn(b'name="prompt"', body)
            self.assertIn(b"private stability prompt", body)
            self.assertIn(b'name="aspect_ratio"', body)
            self.assertIn(b"1:1", body)
            self.assertIn(b'name="output_format"', body)
            self.assertIn(b"png", body)
            self.assertNotIn(CANARY.encode(), body)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({"generation_id": "generation-123"}).encode(),
                request=request,
            )

        provider = StabilityProvider(transport=httpx.MockTransport(handler))
        result = await provider.generate(make_request(), _MODEL_ID, CANARY)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertEqual("generation-123", result.external_job_id)
        self.assertIsNone(result.raster_bytes)
        self.assertEqual(
            {"aspect_ratio": "1:1", "model": _MODEL_ID},
            dict(result.effective_parameters),
        )
        self.assertEqual({}, dict(result.usage))
        self.assertEqual(1, len(seen))
        self.assertEqual(f"Bearer {CANARY}", seen[0].headers["authorization"])
        self.assertEqual(
            f"https://api.stability.ai/v2beta/stable-image/generate/async/{_MODEL_ID}",
            str(seen[0].url),
        )
        self.assertEqual("POST", seen[0].method)

    async def test_poll_transitions_in_progress_then_accepts_b64_raster(self) -> None:
        seen: list[httpx.Request] = []
        poll_count = 0
        poll_path = "/v2beta/results/generation-123"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_count
            seen.append(request)
            if request.url.path == poll_path:
                poll_count += 1
                if poll_count == 1:
                    return httpx.Response(
                        202,
                        headers={"content-type": "application/json"},
                        content=json.dumps({"status": "FETCH_IN_PROGRESS"}).encode(),
                        request=request,
                    )
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=json.dumps(
                        {
                            "status": "FETCH_SUCCESS",
                            "finish_reason": "SUCCESS",
                            "image": _b64_png(),
                            "aspect_ratio": "1:1",
                        }
                    ).encode(),
                    request=request,
                )
            self.fail(f"unexpected fixture request: {request.method} {request.url}")

        provider = StabilityProvider(transport=httpx.MockTransport(handler))
        pending = await provider.poll("generation-123", CANARY)
        accepted = await provider.poll("generation-123", CANARY)
        self.assertEqual(JobState.POLLING, pending.state)
        self.assertIsNone(pending.raster_bytes)
        self.assertEqual("generation-123", pending.external_job_id)
        self.assertEqual(JobState.ACCEPTED, accepted.state)
        self.assertEqual(PNG, accepted.raster_bytes)
        self.assertEqual("image/png", accepted.media_type)
        self.assertEqual(
            {"aspect_ratio": "1:1", "model": _MODEL_ID}, dict(accepted.effective_parameters)
        )
        self.assertEqual({"images": 1}, dict(accepted.usage))
        poll_requests = [request for request in seen if request.url.path == poll_path]
        self.assertEqual(2, len(poll_requests))
        self.assertTrue(
            all(request.headers["authorization"] == f"Bearer {CANARY}" for request in poll_requests)
        )
        self.assertTrue(
            all(request.headers["accept"] == "application/json" for request in poll_requests)
        )

    async def test_cancel_returns_capability_missing(self) -> None:
        provider = StabilityProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        with self.assertRaises(ProviderError) as cancel_error:
            await provider.cancel("generation-123", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, cancel_error.exception.category)
        with self.assertRaises(ProviderError) as poll_error:
            await provider.poll(f"generation?token={CANARY}", CANARY)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, poll_error.exception.category)
        self.assertNotIn(CANARY, repr(poll_error.exception))

    async def test_seed_negative_prompt_and_references_are_rejected(self) -> None:
        provider = StabilityProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(
                GenerationRequest(
                    job_id="job-2",
                    project_id="project-1",
                    project_revision=1,
                    subject_kind="panel",
                    subject_id="p01-02",
                    prompt="private prompt",
                    negative_prompt="never",
                    references=(),
                    width=1024,
                    height=1024,
                    required_capabilities=frozenset({"text_to_image", "async_jobs"}),
                    provider_options={"seed": 42},
                ),
                _MODEL_ID,
                CANARY,
            )
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, caught.exception.category)
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "ref.png"
            reference.write_bytes(PNG)
            with self.assertRaises(ProviderError) as reference_error:
                await provider.generate(
                    make_request(
                        references=(reference,),
                        required_capabilities=frozenset({"text_to_image", "async_jobs"}),
                    ),
                    _MODEL_ID,
                    CANARY,
                )
            self.assertEqual(ErrorCategory.CAPABILITY_MISSING, reference_error.exception.category)

    async def test_missing_credential_is_invalid_credentials(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"generation_id": "unexpected"}, request=request)

        with self.assertRaises(ProviderError) as caught:
            await StabilityProvider(transport=httpx.MockTransport(handler)).generate(
                make_request(), _MODEL_ID, None
            )
        self.assertEqual(ErrorCategory.INVALID_CREDENTIALS, caught.exception.category)
        self.assertEqual(0, calls)

    async def test_status_codes_normalize_moderation_quota_rate_and_unavailable(self) -> None:
        fixtures = (
            (401, ErrorCategory.INVALID_CREDENTIALS),
            (403, ErrorCategory.INVALID_CREDENTIALS),
            (402, ErrorCategory.QUOTA_EXHAUSTED),
            (429, ErrorCategory.RATE_LIMITED),
            (503, ErrorCategory.UNAVAILABLE),
        )
        for status_code, category in fixtures:

            async def handler(
                request: httpx.Request, response_status: int = status_code
            ) -> httpx.Response:
                return httpx.Response(
                    response_status,
                    headers={"content-type": "application/json"},
                    content=json.dumps(
                        {
                            "name": "policy_violation"
                            if category is ErrorCategory.MODERATED
                            else None,
                            "message": CANARY,
                        }
                    ).encode(),
                    request=request,
                )

            with self.subTest(status=status_code, category=category):
                provider = StabilityProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), _MODEL_ID, CANARY)
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_moderation_finish_reason_is_normalized(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {"status": "FETCH_SUCCESS", "finish_reason": "CONTENT_FILTERED"}
                ).encode(),
                request=request,
            )

        provider = StabilityProvider(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as caught:
            await provider.poll("generation-123", CANARY)
        self.assertEqual(ErrorCategory.MODERATED, caught.exception.category)
        self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_timeout_cancellation_and_malformed_raster_fail_closed(self) -> None:
        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(CANARY, request=request)

        with self.assertRaises(ProviderError) as timed_out:
            await StabilityProvider(transport=httpx.MockTransport(timeout)).generate(
                make_request(), _MODEL_ID, CANARY
            )
        self.assertEqual(ErrorCategory.TIMEOUT, timed_out.exception.category)
        self.assertNotIn(CANARY, repr(timed_out.exception))

        async def cancelled(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError(CANARY)

        with self.assertRaises(ProviderError) as cancelled_error:
            await StabilityProvider(transport=httpx.MockTransport(cancelled)).generate(
                make_request(), _MODEL_ID, CANARY
            )
        self.assertEqual(ErrorCategory.CANCELLED, cancelled_error.exception.category)
        self.assertNotIn(CANARY, repr(cancelled_error.exception))

        async def bad_raster(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {
                        "status": "FETCH_SUCCESS",
                        "finish_reason": "SUCCESS",
                        "image": base64.b64encode(b"not-an-image").decode(),
                    }
                ).encode(),
                request=request,
            )

        with self.assertRaises(ProviderError) as invalid:
            await StabilityProvider(transport=httpx.MockTransport(bad_raster)).poll(
                "generation-123", CANARY
            )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, invalid.exception.category)

    async def test_failed_poll_state_is_provider_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({"status": "FETCH_FAILED"}).encode(),
                request=request,
            )

        provider = StabilityProvider(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as caught:
            await provider.poll("generation-123", CANARY)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, caught.exception.category)


if __name__ == "__main__":
    unittest.main()
