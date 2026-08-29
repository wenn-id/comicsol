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
from comic_sol_web.generation.providers.xai import (
    XAIProvider,
    _EDIT_MODEL,
    _GENERATE_MODEL,
)
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

CANARY = "xai-canary-secret-that-must-never-escape"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)

_GENERATE_ID = _GENERATE_MODEL.model
_EDIT_ID = _EDIT_MODEL.model


def make_request(
    *,
    job_id: str = "job-xai",
    references: tuple[Path, ...] = (),
    required_capabilities: frozenset[str] = frozenset({"text_to_image"}),
    width: int = 1024,
    height: int = 1024,
) -> GenerationRequest:
    return GenerationRequest(
        job_id=job_id,
        project_id="project-1",
        project_revision=1,
        subject_kind="panel",
        subject_id="p01-01",
        prompt="private xai prompt",
        negative_prompt=None,
        references=references,
        width=width,
        height=height,
        required_capabilities=required_capabilities,
        provider_options={},
    )


def _b64_png() -> str:
    return base64.b64encode(PNG).decode("ascii")


class XAICatalogTests(unittest.TestCase):
    def test_curated_xai_models_are_pinned(self) -> None:
        self.assertEqual("xai", _GENERATE_MODEL.provider)
        self.assertEqual("xai", _EDIT_MODEL.provider)
        self.assertEqual({_GENERATE_ID, _EDIT_ID}, {_GENERATE_MODEL.model, _EDIT_MODEL.model})
        for entry in (_GENERATE_MODEL, _EDIT_MODEL):
            self.assertTrue(entry.enabled)
            self.assertTrue(entry.capabilities)

    def test_curated_capabilities_omit_unsupported_surface(self) -> None:
        generate_caps = frozenset(_GENERATE_MODEL.capabilities)
        edit_caps = frozenset(_EDIT_MODEL.capabilities)
        for unsupported in {"async_jobs", "cancellation", "seed", "negative_prompt"}:
            self.assertNotIn(unsupported, generate_caps)
            self.assertNotIn(unsupported, edit_caps)
        self.assertEqual(frozenset({"text_to_image", "custom_dimensions"}), generate_caps)
        self.assertEqual(
            frozenset({"text_to_image", "image_to_image", "reference_images", "custom_dimensions"}),
            edit_caps,
        )


class XAIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_id_and_list_models_are_curated(self) -> None:
        provider = XAIProvider(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.assertEqual("xai", provider.provider_id)
        models = await provider.list_models()
        self.assertEqual({_GENERATE_ID, _EDIT_ID}, {model.model for model in models})

    async def test_generate_translates_text_to_image_request(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {
                        "data": [{"b64_json": _b64_png()}],
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 22,
                            "total_tokens": 33,
                        },
                    }
                ).encode(),
                request=request,
            )

        provider = XAIProvider(transport=httpx.MockTransport(handler))
        request = make_request()
        result = await provider.generate(request, _GENERATE_ID, CANARY)
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertIsNone(result.external_job_id)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual("image/png", result.media_type)
        self.assertEqual(
            {"height": 1024, "model": _GENERATE_ID, "width": 1024},
            dict(result.effective_parameters),
        )
        self.assertEqual(
            {"images": 1, "input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            dict(result.usage),
        )
        self.assertEqual(1, len(seen))
        request_call = seen[0]
        self.assertEqual("https://api.x.ai/v1/images/generations", str(request_call.url))
        self.assertEqual("POST", request_call.method)
        self.assertEqual(f"Bearer {CANARY}", request_call.headers["authorization"])
        self.assertNotIn(CANARY, str(request_call.url))
        self.assertEqual(
            {
                "model": _GENERATE_ID,
                "n": 1,
                "prompt": "private xai prompt",
                "response_format": {"type": "b64_json"},
                "size": "1024x1024",
            },
            json.loads(request_call.content),
        )

    async def test_edit_translates_reference_images_via_multipart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.png"
            reference.write_bytes(PNG)
            seen: list[httpx.Request] = []

            async def handler(request_call: httpx.Request) -> httpx.Response:
                seen.append(request_call)
                body = b"".join(request_call.stream)
                self.assertIn(b'name="model"', body)
                self.assertIn(_GENERATE_ID.encode(), body)
                self.assertIn(b'name="prompt"', body)
                self.assertIn(b"private xai prompt", body)
                self.assertIn(b"filename=", body)
                self.assertIn(b"Content-Type: image/png", body)
                self.assertNotIn(CANARY.encode(), body)
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    content=json.dumps({"data": [{"b64_json": _b64_png()}]}).encode(),
                    request=request_call,
                )

            provider = XAIProvider(transport=httpx.MockTransport(handler))
            request = make_request(
                references=(reference,),
                required_capabilities=frozenset(
                    {"text_to_image", "image_to_image", "reference_images"}
                ),
            )
            result = await provider.generate(request, _EDIT_ID, CANARY)
            self.assertEqual(JobState.ACCEPTED, result.state)
            self.assertEqual(PNG, result.raster_bytes)
            self.assertEqual(_EDIT_ID, dict(result.effective_parameters)["model"])
            self.assertEqual(1, len(seen))
            self.assertEqual(f"Bearer {CANARY}", seen[0].headers["authorization"])
            self.assertEqual("https://api.x.ai/v1/images/edits", str(seen[0].url))

    async def test_poll_and_cancel_return_capability_missing(self) -> None:
        provider = XAIProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        with self.assertRaises(ProviderError) as poll_error:
            await provider.poll("grok-2-image-123", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, poll_error.exception.category)
        with self.assertRaises(ProviderError) as cancel_error:
            await provider.cancel("grok-2-image-123", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, cancel_error.exception.category)

    async def test_generate_rejects_uncurated_caps_and_options(self) -> None:
        provider = XAIProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        for caps in (
            frozenset({"seed"}),
            frozenset({"negative_prompt"}),
            frozenset({"cancellation"}),
        ):
            with self.subTest(required_capabilities=caps):
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(
                        make_request(required_capabilities=caps), _GENERATE_ID, CANARY
                    )
                self.assertEqual(ErrorCategory.CAPABILITY_MISSING, caught.exception.category)

    async def test_missing_credential_is_invalid_credentials(self) -> None:
        provider = XAIProvider(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        )
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(make_request(), _GENERATE_ID, None)
        self.assertEqual(ErrorCategory.INVALID_CREDENTIALS, caught.exception.category)

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
                            "code": "policy_violation"
                            if category is ErrorCategory.MODERATED
                            else None,
                            "error": CANARY,
                        }
                    ).encode(),
                    request=request,
                )

            with self.subTest(status=status_code, category=category):
                provider = XAIProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), _GENERATE_ID, CANARY)
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_timeout_and_cancellation_are_normalized(self) -> None:
        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(CANARY, request=request)

        with self.assertRaises(ProviderError) as timed_out:
            await XAIProvider(transport=httpx.MockTransport(timeout)).generate(
                make_request(), _GENERATE_ID, CANARY
            )
        self.assertEqual(ErrorCategory.TIMEOUT, timed_out.exception.category)
        self.assertNotIn(CANARY, repr(timed_out.exception))

        async def cancelled(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError(CANARY)

        with self.assertRaises(ProviderError) as cancelled_error:
            await XAIProvider(transport=httpx.MockTransport(cancelled)).generate(
                make_request(), _GENERATE_ID, CANARY
            )
        self.assertEqual(ErrorCategory.CANCELLED, cancelled_error.exception.category)
        self.assertNotIn(CANARY, repr(cancelled_error.exception))

    async def test_moderation_is_normalized_from_200_payload(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {
                        "code": "policy_violation",
                        "error": CANARY,
                        "data": [],
                    }
                ).encode(),
                request=request,
            )

        provider = XAIProvider(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(make_request(), _GENERATE_ID, CANARY)
        self.assertEqual(ErrorCategory.MODERATED, caught.exception.category)
        self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

    async def test_malformed_raster_and_wrong_mime_fail_closed(self) -> None:
        async def wrong_mime(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=json.dumps({"data": [{"b64_json": _b64_png()}]}).encode(),
                request=request,
            )

        async def bad_raster(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {"data": [{"b64_json": base64.b64encode(b"not-an-image").decode()}]}
                ).encode(),
                request=request,
            )

        async def no_data(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({"data": []}).encode(),
                request=request,
            )

        for label, handler in (
            ("wrong_mime", wrong_mime),
            ("bad_raster", bad_raster),
            ("no_data", no_data),
        ):
            with self.subTest(label=label):
                provider = XAIProvider(transport=httpx.MockTransport(handler))
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), _GENERATE_ID, CANARY)
                self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)


if __name__ == "__main__":
    unittest.main()
