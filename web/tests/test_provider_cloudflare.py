from __future__ import annotations

import base64
import unittest
from typing import Mapping

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.catalog import CATALOG
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.cloudflare import CloudflareProvider
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
CANARY = "cloudflare-canary-secret-that-must-never-escape"
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
        job_id="job-cloudflare",
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


class CloudflareProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_curated_model_binary_output_and_request_translation(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                content=PNG,
                headers={"content-type": "image/png"},
                request=request,
            )

        provider = CloudflareProvider(ACCOUNT_ID, transport=httpx.MockTransport(handler))
        models = await provider.list_models()
        self.assertEqual(
            [("cloudflare", "@cf/black-forest-labs/flux-1-schnell")],
            [(model.provider, model.model) for model in models],
        )
        self.assertEqual(
            {
                "currency": "USD",
                "model": "@cf/black-forest-labs/flux-1-schnell",
                "unit": "image",
            },
            await provider.estimate(make_request(), "@cf/black-forest-labs/flux-1-schnell"),
        )
        result = await provider.generate(
            make_request(), "@cf/black-forest-labs/flux-1-schnell", CANARY
        )
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual("image/png", result.media_type)
        self.assertEqual(
            {
                "height": 1024,
                "model": "@cf/black-forest-labs/flux-1-schnell",
                "width": 1024,
            },
            result.effective_parameters,
        )
        self.assertEqual({"images": 1}, result.usage)
        request = seen[0]
        self.assertEqual(
            f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell",
            str(request.url),
        )
        self.assertEqual(f"Bearer {CANARY}", request.headers["authorization"])
        self.assertEqual(
            {"height": 1024, "prompt": "private prompt", "width": 1024},
            __import__("json").loads(request.content),
        )

    async def test_base64_output_is_bounded_validated_and_redacted(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "result": {"image": base64.b64encode(PNG).decode("ascii")},
                    "success": True,
                    "errors": [],
                    "messages": [],
                },
                request=request,
            )

        result = await CloudflareProvider(
            ACCOUNT_ID, transport=httpx.MockTransport(handler)
        ).generate(make_request(), "@cf/black-forest-labs/flux-1-schnell", CANARY)
        self.assertEqual(JobState.ACCEPTED, result.state)
        self.assertEqual(PNG, result.raster_bytes)
        self.assertEqual("image/png", result.media_type)
        self.assertNotIn(CANARY, repr(result))

    async def test_quota_rate_redirect_mime_byte_and_malformed_raster_fail_closed(self) -> None:
        fixtures = (
            (
                402,
                {"content-type": "application/json"},
                b'{"errors":[{"message":"' + CANARY.encode() + b'"}]}',
                ErrorCategory.QUOTA_EXHAUSTED,
            ),
            (
                429,
                {"content-type": "application/json"},
                b'{"errors":[{"message":"' + CANARY.encode() + b'"}]}',
                ErrorCategory.RATE_LIMITED,
            ),
            (
                302,
                {"location": f"https://evil.example/output?token={CANARY}"},
                b"",
                ErrorCategory.PROVIDER_ERROR,
            ),
            (
                200,
                {"content-type": "text/html"},
                b"<html>not an image</html>",
                ErrorCategory.INVALID_OUTPUT,
            ),
            (200, {"content-type": "image/png"}, PNG + b"too-large", ErrorCategory.INVALID_OUTPUT),
            (200, {"content-type": "image/png"}, b"not-a-raster", ErrorCategory.INVALID_OUTPUT),
        )
        for status, headers, content, category in fixtures:

            async def handler(
                request: httpx.Request,
                response_status: int = status,
                response_headers: Mapping[str, str] = headers,
                response_content: bytes = content,
            ) -> httpx.Response:
                return httpx.Response(
                    response_status,
                    content=response_content,
                    headers=response_headers,
                    request=request,
                )

            with self.subTest(status=status, category=category):
                provider = CloudflareProvider(
                    ACCOUNT_ID,
                    transport=httpx.MockTransport(handler),
                    max_response_bytes=len(PNG),
                )
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(
                        make_request(), "@cf/black-forest-labs/flux-1-schnell", CANARY
                    )
                self.assertEqual(category, caught.exception.category)
                rendered = f"{caught.exception!s} {caught.exception!r}"
                self.assertNotIn(CANARY, rendered)
                self.assertNotIn(ACCOUNT_ID, rendered)

        malformed_base64 = (
            "not-valid-base64!",
            base64.b64encode(b"not-a-raster").decode("ascii"),
        )
        for encoded in malformed_base64:

            async def invalid_json(
                request: httpx.Request, fixture: str = encoded
            ) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"result": {"image": fixture}, "success": True},
                    request=request,
                )

            with self.subTest(encoded=encoded[:8]), self.assertRaises(ProviderError) as caught:
                await CloudflareProvider(
                    ACCOUNT_ID, transport=httpx.MockTransport(invalid_json)
                ).generate(make_request(), "@cf/black-forest-labs/flux-1-schnell", CANARY)
            self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_unknown_models_options_accounts_and_operations_fail_before_transport(
        self,
    ) -> None:
        calls = 0

        async def forbidden(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, request=request)

        provider = CloudflareProvider(ACCOUNT_ID, transport=httpx.MockTransport(forbidden))
        for request, model, credential in (
            (make_request(), "@cf/community/arbitrary-model", CANARY),
            (
                make_request(provider_options={"schema": {"arbitrary": True}}),
                "@cf/black-forest-labs/flux-1-schnell",
                CANARY,
            ),
            (make_request(), "@cf/black-forest-labs/flux-1-schnell", None),
            (make_request(width=1023), "@cf/black-forest-labs/flux-1-schnell", CANARY),
        ):
            with self.subTest(model=model), self.assertRaises(ProviderError) as caught:
                await provider.generate(request, model, credential)
            self.assertIn(
                caught.exception.category,
                {ErrorCategory.CAPABILITY_MISSING, ErrorCategory.INVALID_CREDENTIALS},
            )
        self.assertEqual(0, calls)

        with self.assertRaises(ValueError):
            CloudflareProvider("../account?token=" + CANARY)
        with self.assertRaises(ProviderError) as poll_error:
            await provider.poll("job", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, poll_error.exception.category)
        with self.assertRaises(ProviderError) as cancel_error:
            await provider.cancel("job", CANARY)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, cancel_error.exception.category)

    def test_catalog_preserves_distinct_provider_model_flux_receipts(self) -> None:
        receipt_provenance = tuple(
            (entry.provider, entry.model) for entry in CATALOG if "flux" in entry.model.lower()
        )
        self.assertEqual(
            (
                ("bfl", "flux-1.1-pro"),
                ("replicate", "black-forest-labs/flux-1.1-pro"),
                ("fal", "fal-ai/flux-pro/v1.1"),
                ("cloudflare", "@cf/black-forest-labs/flux-1-schnell"),
            ),
            receipt_provenance,
        )
        self.assertEqual(len(receipt_provenance), len(set(receipt_provenance)))


if __name__ == "__main__":
    unittest.main()
