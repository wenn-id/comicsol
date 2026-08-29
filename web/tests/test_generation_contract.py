from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import AsyncIterator, Mapping, Sequence, get_type_hints

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.catalog import CATALOG
from comic_sol_web.generation.providers.base import ProviderError, ProviderRegistry
from comic_sol_web.generation.providers.fake import FakeProvider
from comic_sol_web.generation.providers.http import BoundedHTTPClient, TransportPolicy
from comic_sol_web.generation.types import (
    AuthMode,
    ErrorCategory,
    GenerationRequest,
    GenerationResult,
    JobState,
    ProviderAdapter,
    ProviderModel,
)


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk


class GenerationTypeTests(unittest.TestCase):
    def test_enum_values_are_the_pinned_contract(self) -> None:
        self.assertEqual(
            ["agent", "hosted", "byok"],
            [item.value for item in AuthMode],
        )
        self.assertEqual(
            [
                "queued",
                "running",
                "polling",
                "validating",
                "accepted",
                "awaiting_provider_confirmation",
                "paused",
                "failed",
                "cancelled",
            ],
            [item.value for item in JobState],
        )
        self.assertEqual(
            [
                "invalid_credentials",
                "quota_exhausted",
                "rate_limited",
                "moderated",
                "capability_missing",
                "timeout",
                "cancelled",
                "unavailable",
                "invalid_output",
                "provider_error",
            ],
            [item.value for item in ErrorCategory],
        )

    def test_dataclass_fields_are_the_pinned_contract(self) -> None:
        self.assertEqual(
            [
                "job_id",
                "project_id",
                "project_revision",
                "subject_kind",
                "subject_id",
                "prompt",
                "negative_prompt",
                "references",
                "width",
                "height",
                "required_capabilities",
                "provider_options",
            ],
            [field.name for field in dataclasses.fields(GenerationRequest)],
        )
        self.assertEqual(
            [
                "external_job_id",
                "state",
                "raster_bytes",
                "media_type",
                "effective_parameters",
                "usage",
            ],
            [field.name for field in dataclasses.fields(GenerationResult)],
        )
        self.assertEqual(
            ["provider", "model", "capabilities", "enabled"],
            [field.name for field in dataclasses.fields(ProviderModel)],
        )
        self.assertTrue(dataclasses.fields(GenerationRequest))
        self.assertTrue(getattr(GenerationRequest, "__dataclass_params__").frozen)
        self.assertTrue(getattr(GenerationResult, "__dataclass_params__").frozen)
        self.assertTrue(getattr(ProviderModel, "__dataclass_params__").frozen)
        self.assertTrue(getattr(ProviderAdapter, "_is_protocol", False))

    def test_field_types_and_adapter_signatures_are_the_pinned_contract(self) -> None:
        self.assertEqual(
            {
                "job_id": str,
                "project_id": str,
                "project_revision": int,
                "subject_kind": str,
                "subject_id": str,
                "prompt": str,
                "negative_prompt": str | None,
                "references": tuple[Path, ...],
                "width": int,
                "height": int,
                "required_capabilities": frozenset[str],
                "provider_options": Mapping[str, object],
            },
            get_type_hints(GenerationRequest),
        )
        self.assertEqual(
            {
                "external_job_id": str | None,
                "state": JobState,
                "raster_bytes": bytes | None,
                "media_type": str | None,
                "effective_parameters": Mapping[str, object],
                "usage": Mapping[str, int | float | str],
            },
            get_type_hints(GenerationResult),
        )
        self.assertEqual(
            {
                "provider": str,
                "model": str,
                "capabilities": frozenset[str],
                "enabled": bool,
            },
            get_type_hints(ProviderModel),
        )
        provider_options = dataclasses.fields(GenerationRequest)[-1]
        self.assertIs(dict, provider_options.default_factory)

        expected_signatures = {
            "list_models": (["self"], {"return": Sequence[ProviderModel]}),
            "estimate": (
                ["self", "request", "model"],
                {
                    "request": GenerationRequest,
                    "model": str,
                    "return": Mapping[str, object],
                },
            ),
            "generate": (
                ["self", "request", "model", "credential"],
                {
                    "request": GenerationRequest,
                    "model": str,
                    "credential": str | None,
                    "return": GenerationResult,
                },
            ),
            "poll": (
                ["self", "external_job_id", "credential"],
                {
                    "external_job_id": str,
                    "credential": str | None,
                    "return": GenerationResult,
                },
            ),
            "cancel": (
                ["self", "external_job_id", "credential"],
                {
                    "external_job_id": str,
                    "credential": str | None,
                    "return": type(None),
                },
            ),
        }
        self.assertEqual({"provider_id": str}, get_type_hints(ProviderAdapter))
        for method_name, (parameters, hints) in expected_signatures.items():
            method = getattr(ProviderAdapter, method_name)
            with self.subTest(method=method_name):
                self.assertEqual(parameters, list(inspect.signature(method).parameters))
                self.assertEqual(hints, get_type_hints(method))

    def test_types_are_immutable_and_copy_mappings(self) -> None:
        nested_options = {"seed": 7}
        options = {"fixture": "success", "parameters": nested_options}
        request = make_request(provider_options=options)
        options["fixture"] = "quota"
        nested_options["seed"] = 99
        self.assertEqual("success", request.provider_options["fixture"])
        self.assertEqual(7, request.provider_options["parameters"]["seed"])  # type: ignore[index]
        self.assertIsInstance(request.provider_options, MappingProxyType)
        with self.assertRaises(TypeError):
            request.provider_options["fixture"] = "moderation"  # type: ignore[index]
        with self.assertRaises(TypeError):
            request.provider_options["parameters"]["seed"] = 2  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.width = 1  # type: ignore[misc]

        sampler = {"steps": 20}
        parameters = {"seed": 7, "sampler": sampler}
        usage: dict[str, int | float | str] = {"images": 1}
        result = GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=b"raster",
            media_type="image/png",
            effective_parameters=parameters,
            usage=usage,
        )
        parameters["seed"] = 99
        sampler["steps"] = 40
        usage["images"] = 2
        self.assertEqual(7, result.effective_parameters["seed"])
        self.assertEqual(20, result.effective_parameters["sampler"]["steps"])  # type: ignore[index]
        self.assertEqual(1, result.usage["images"])
        with self.assertRaises(TypeError):
            result.usage["images"] = 3  # type: ignore[index]

    def test_sensitive_generation_content_is_redacted_from_reprs(self) -> None:
        request_secret = "request-secret"
        result_secret = "result-secret"
        request = make_request(
            provider_options={"token": request_secret, "nested": {"token": request_secret}}
        )
        result = GenerationResult(
            external_job_id=None,
            state=JobState.ACCEPTED,
            raster_bytes=result_secret.encode(),
            media_type="image/png",
            effective_parameters={"prompt": result_secret},
            usage={"private": result_secret},
        )
        self.assertNotIn("private prompt", repr(request))
        self.assertNotIn(request_secret, repr(request))
        self.assertNotIn(result_secret, repr(result))

    def test_unknown_required_capability_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            make_request(required_capabilities=frozenset({"not-a-real-capability"}))
        self.assertNotIn("prompt", str(caught.exception).lower())


class CatalogAndRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_is_curated_and_pinned(self) -> None:
        self.assertEqual(
            (
                ("fake", "fake-raster-v1", True),
                ("openai", "gpt-image-1", True),
                ("google", "gemini-2.5-flash-image", True),
                ("bfl", "flux-1.1-pro", True),
                ("replicate", "black-forest-labs/flux-1.1-pro", True),
                ("fal", "fal-ai/flux-pro/v1.1", True),
                ("cloudflare", "@cf/black-forest-labs/flux-1-schnell", True),
            ),
            tuple((entry.provider, entry.model, entry.enabled) for entry in CATALOG),
        )
        self.assertTrue(all(entry.capabilities for entry in CATALOG))

    def test_provider_and_model_are_separate_and_flux_uses_runtime_provider(self) -> None:
        flux = next(entry for entry in CATALOG if entry.model == "flux-1.1-pro")
        self.assertEqual("bfl", flux.provider)
        self.assertNotEqual(flux.provider, flux.model)
        self.assertNotIn("flux", {entry.provider for entry in CATALOG})

    async def test_registry_lookup_is_deterministic(self) -> None:
        fake = FakeProvider()
        registry = ProviderRegistry((fake,))
        self.assertIs(fake, registry.get("fake"))
        self.assertIs(fake, registry.get("fake"))
        with self.assertRaises(KeyError):
            registry.get("missing")
        with self.assertRaises(ValueError):
            ProviderRegistry((fake, FakeProvider()))


class BoundedHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleartext_origins_are_limited_to_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "cleartext"):
            make_policy(approved_origins=frozenset({"http://provider.example"}))

        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"ok", request=request)

        for origin in ("http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"):
            with self.subTest(origin=origin):
                async with BoundedHTTPClient(
                    make_policy(approved_origins=frozenset({origin})),
                    transport=httpx.MockTransport(handler),
                ) as client:
                    self.assertEqual(b"ok", await client.get_bytes(f"{origin}/output"))
        self.assertEqual(3, calls)

    async def test_approved_origin_is_required_before_transport(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b"ok", request=request)

        policy = make_policy(approved_origins=frozenset({"https://allowed.example"}))
        async with BoundedHTTPClient(policy, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes("https://unapproved.example/output.png")
            with self.assertRaises(ProviderError) as malformed:
                await client.get_bytes("https://user:endpoint-secret@allowed.example/output")
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, malformed.exception.category)
        self.assertNotIn("endpoint-secret", repr(malformed.exception))
        self.assertEqual(0, calls)

    async def test_response_limit_stops_streaming_at_first_excess_chunk(self) -> None:
        stream = CountingStream((b"1234", b"56", b"must-not-be-read"))

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream, request=request)

        policy = make_policy(max_response_bytes=5)
        async with BoundedHTTPClient(policy, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes("https://allowed.example/output.png")
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
        self.assertEqual(2, stream.yielded)
        self.assertNotIn("must-not-be-read", str(caught.exception))

    async def test_post_json_is_bounded_and_refuses_redirects(self) -> None:
        stream = CountingStream((b'{"value":"', b"oversized", b'"}'))

        async def oversized(request: httpx.Request) -> httpx.Response:
            self.assertEqual("POST", request.method)
            self.assertEqual({"request": "sanitized"}, json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
                request=request,
            )

        async with BoundedHTTPClient(
            make_policy(max_response_bytes=12),
            transport=httpx.MockTransport(oversized),
        ) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.post_json(
                    "https://allowed.example/generate",
                    payload={"request": "sanitized"},
                )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)
        self.assertEqual(2, stream.yielded)

        secret = "post-redirect-canary"

        async def redirected(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                307,
                headers={"location": f"https://allowed.example/result?token={secret}"},
                request=request,
            )

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(redirected)
        ) as client:
            with self.assertRaises(ProviderError) as redirected_error:
                await client.post_json("https://allowed.example/generate", payload={})
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, redirected_error.exception.category)
        self.assertNotIn(secret, repr(redirected_error.exception))

    async def test_json_transport_rejects_wrong_mime_and_normalizes_cancellation(self) -> None:
        async def wrong_mime(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b'{"looks":"json"}',
                headers={"content-type": "text/html"},
                request=request,
            )

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(wrong_mime)
        ) as client:
            with self.assertRaises(ProviderError) as malformed:
                await client.post_json("https://allowed.example/generate", payload={})
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, malformed.exception.category)

        secret = "cancelled-transport-canary"

        async def cancelled(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError(secret)

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(cancelled)
        ) as client:
            with self.assertRaises(ProviderError) as cancelled_error:
                await client.post_json("https://allowed.example/generate", payload={})
        self.assertEqual(ErrorCategory.CANCELLED, cancelled_error.exception.category)
        self.assertNotIn(secret, repr(cancelled_error.exception))

    async def test_caller_cancellation_is_re_raised_without_normalization(self) -> None:
        started = asyncio.Event()

        async def blocked(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(blocked)
        ) as client:
            task = asyncio.create_task(client.get_bytes("https://allowed.example/output"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())

    async def test_redirect_is_refused_and_location_is_redacted(self) -> None:
        secret = "redirect-secret-token"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"location": f"https://allowed.example/output?token={secret}"},
                request=request,
            )

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes("https://allowed.example/start")
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, caught.exception.category)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))

    async def test_explicit_timeouts_and_timeout_normalization(self) -> None:
        policy = make_policy(connect_timeout=1.25, read_timeout=2.5, total_timeout=3.75)
        self.assertEqual(1.25, policy.connect_timeout)
        self.assertEqual(2.5, policy.read_timeout)
        self.assertEqual(3.75, policy.total_timeout)
        secret = "timeout-secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(secret, request=request)

        async with BoundedHTTPClient(policy, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes("https://allowed.example/output.png")
        self.assertEqual(ErrorCategory.TIMEOUT, caught.exception.category)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(caught.exception))

    async def test_total_timeout_is_enforced(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=b"late", request=request)

        policy = make_policy(total_timeout=0.001)
        async with BoundedHTTPClient(policy, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes("https://allowed.example/output.png")
        self.assertEqual(ErrorCategory.TIMEOUT, caught.exception.category)

    async def test_status_payload_headers_endpoint_and_account_are_redacted(self) -> None:
        secret_body = "raw-provider-body-secret"
        credential = "Bearer credential-secret"
        account = "account-12345"
        endpoint_secret = "query-secret"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=secret_body.encode(), request=request)

        async with BoundedHTTPClient(
            make_policy(), transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(ProviderError) as caught:
                await client.get_bytes(
                    f"https://allowed.example/output?token={endpoint_secret}",
                    headers={"authorization": credential, "x-account-id": account},
                )
        rendered = f"{caught.exception!s} {caught.exception!r}"
        for forbidden in (secret_body, credential, account, endpoint_secret, "output"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, caught.exception.category)

    async def test_status_codes_are_normalized_without_response_content(self) -> None:
        cases = (
            (401, ErrorCategory.INVALID_CREDENTIALS),
            (402, ErrorCategory.QUOTA_EXHAUSTED),
            (429, ErrorCategory.RATE_LIMITED),
            (503, ErrorCategory.UNAVAILABLE),
            (500, ErrorCategory.PROVIDER_ERROR),
        )
        for status, category in cases:

            async def handler(
                request: httpx.Request, response_status: int = status
            ) -> httpx.Response:
                return httpx.Response(
                    response_status,
                    content=b"provider payload must stay private",
                    request=request,
                )

            with self.subTest(status=status):
                async with BoundedHTTPClient(
                    make_policy(), transport=httpx.MockTransport(handler)
                ) as client:
                    with self.assertRaises(ProviderError) as caught:
                        await client.get_bytes("https://allowed.example/output")
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn("provider payload", str(caught.exception))


class FakeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = FakeProvider()

    async def test_synchronous_success_is_deterministic(self) -> None:
        request = make_request(provider_options={"fixture": "success"})
        first = await self.provider.generate(request, "fake-raster-v1", "ignored-secret")
        second = await self.provider.generate(request, "fake-raster-v1", None)
        self.assertEqual(first, second)
        self.assertEqual(JobState.ACCEPTED, first.state)
        self.assertEqual("image/png", first.media_type)
        self.assertIsNotNone(first.raster_bytes)
        assert first.raster_bytes is not None
        self.assertTrue(first.raster_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(first.external_job_id)

    async def test_asynchronous_polling_lifecycle_and_duplicate_completion(self) -> None:
        request = make_request(job_id="async-job", provider_options={"fixture": "async"})
        started = await self.provider.generate(request, "fake-raster-v1", None)
        self.assertEqual(JobState.POLLING, started.state)
        self.assertEqual("fake:async-job", started.external_job_id)
        assert started.external_job_id is not None
        polling = await self.provider.poll(started.external_job_id, None)
        completed = await self.provider.poll(started.external_job_id, None)
        duplicate = await self.provider.poll(started.external_job_id, "ignored-secret")
        self.assertEqual(JobState.POLLING, polling.state)
        self.assertEqual(JobState.ACCEPTED, completed.state)
        self.assertEqual(completed, duplicate)

    async def test_quota_and_moderation_are_normalized(self) -> None:
        for fixture, category in (
            ("quota", ErrorCategory.QUOTA_EXHAUSTED),
            ("moderation", ErrorCategory.MODERATED),
        ):
            with self.subTest(fixture=fixture):
                with self.assertRaises(ProviderError) as caught:
                    await self.provider.generate(
                        make_request(provider_options={"fixture": fixture}),
                        "fake-raster-v1",
                        "must-not-appear",
                    )
                self.assertEqual(category, caught.exception.category)
                self.assertNotIn("must-not-appear", str(caught.exception))

    async def test_malformed_raster_is_rejected(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            await self.provider.generate(
                make_request(provider_options={"fixture": "malformed_raster"}),
                "fake-raster-v1",
                None,
            )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_cancellation_is_idempotent_and_terminal(self) -> None:
        started = await self.provider.generate(
            make_request(job_id="cancel-job", provider_options={"fixture": "async"}),
            "fake-raster-v1",
            None,
        )
        assert started.external_job_id is not None
        await self.provider.cancel(started.external_job_id, None)
        await self.provider.cancel(started.external_job_id, "ignored-secret")
        cancelled = await self.provider.poll(started.external_job_id, None)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertIsNone(cancelled.raster_bytes)

    async def test_required_capabilities_and_model_are_enforced(self) -> None:
        with self.assertRaises(ProviderError) as model_error:
            await self.provider.generate(make_request(), "unknown-model", None)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, model_error.exception.category)

        with self.assertRaises(ProviderError) as capability_error:
            await self.provider.generate(
                make_request(required_capabilities=frozenset({"cancellation"})),
                "fake-raster-v1",
                None,
            )
        self.assertEqual(
            ErrorCategory.CAPABILITY_MISSING,
            capability_error.exception.category,
        )


def make_request(
    *,
    job_id: str = "job-1",
    provider_options: Mapping[str, object] | None = None,
    required_capabilities: frozenset[str] = frozenset({"text_to_image"}),
) -> GenerationRequest:
    return GenerationRequest(
        job_id=job_id,
        project_id="project-1",
        project_revision=3,
        subject_kind="panel",
        subject_id="panel-1",
        prompt="private prompt that must never enter errors",
        negative_prompt=None,
        references=(),
        width=1024,
        height=1024,
        required_capabilities=required_capabilities,
        provider_options={} if provider_options is None else provider_options,
    )


def make_policy(
    *,
    approved_origins: frozenset[str] = frozenset({"https://allowed.example"}),
    connect_timeout: float = 1.0,
    read_timeout: float = 1.0,
    total_timeout: float = 1.0,
    max_response_bytes: int = 1024,
) -> TransportPolicy:
    return TransportPolicy(
        approved_origins=approved_origins,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        total_timeout=total_timeout,
        max_response_bytes=max_response_bytes,
    )


if __name__ == "__main__":
    unittest.main()
