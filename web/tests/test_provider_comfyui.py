"""RED -> GREEN contracts for the safe ComfyUI provider adapter (issue #263 WP12).

Covers workflow fixture and slot validation, remote submission/polling/cancel/
history/raster, SSRF and redirect refusal, and the local agent-handoff route.
All adapter I/O stays behind ``MockTransport``; no real network is attempted.
"""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.catalog import CATALOG
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.providers.comfyui import ComfyUIProvider, _validate_fixture_payload
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "comfyui"
SDXL_BASE = FIXTURES / "sdxl-base.json"

CANARY = "comfyui-canary-secret-that-must-never-escape"
PROMPT_ID = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)

REMOTE_ORIGIN = "https://comfy.example"
REMOTE_BASE = f"{REMOTE_ORIGIN}/api"


def make_request(
    *,
    prompt: str = "A bounded private prompt.",
    negative_prompt: str | None = "no text, no watermark",
    width: int = 1024,
    height: int = 1024,
    capabilities: frozenset[str] = frozenset(
        {"text_to_image", "custom_dimensions", "negative_prompt"}
    ),
) -> GenerationRequest:
    return GenerationRequest(
        job_id="a" * 64,
        project_id="opaque-project-id",
        project_revision=7,
        subject_kind="panel",
        subject_id="p01-01",
        prompt=prompt,
        negative_prompt=negative_prompt,
        references=(),
        width=width,
        height=height,
        required_capabilities=capabilities,
        provider_options={
            "ignored_token": CANARY,
            "ignored_url": "https://example.test/private.png",
            "ignored_path": "C:/private/secret.png",
        },
    )


def write_fixture(workflow_id: str, data: dict[str, object]) -> Path:
    """Write an adversarial fixture into a temp dir and return its path."""
    directory = Path(tempfile.mkdtemp(prefix="comfyui-fixture-"))
    path = directory / f"{workflow_id}.json"
    path.write_text(json.dumps(data, sort_keys=True))
    return path


class CatalogAndFixtureTests(unittest.TestCase):
    def test_catalog_entry_and_capabilities(self) -> None:
        entries = [entry for entry in CATALOG if entry.provider == "comfyui"]
        self.assertEqual(1, len(entries))
        model = entries[0]
        self.assertTrue(model.enabled)
        self.assertEqual("comfyui", model.provider)
        self.assertIn("text_to_image", model.capabilities)
        self.assertIn("custom_dimensions", model.capabilities)
        self.assertEqual(model.capabilities, frozenset(model.capabilities))

    def test_bounded_fixture_is_valid_and_has_no_secrets_or_endpoints(self) -> None:
        raw = SDXL_BASE.read_bytes()
        self.assertIn(b"workflow", raw)
        data = json.loads(raw)
        self.assertEqual("sdxl-base", data["workflow_id"])
        nodes = data["workflow"]
        for node in nodes.values():
            self.assertIn("class_type", node)
            self.assertIn(node["class_type"], data["allowed_node_classes"])
        rendered = raw.decode("utf-8")
        for forbidden in ("://", "token", "cookie", "authorization", "C:/", "Bearer"):
            self.assertNotIn(forbidden, rendered)
        for required in data["required_node_classes"]:
            self.assertIn(required, data["allowed_node_classes"])


class WorkflowValidationTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, *, fixture_dir: Path | None = None) -> ComfyUIProvider:
        return ComfyUIProvider(fixture_dir=fixture_dir)

    def injected_workflow(self, package: dict[str, object]) -> dict[str, dict[str, object]]:
        workflow = package["workflow"]
        assert isinstance(workflow, dict)
        return workflow  # type: ignore[return-value]

    async def test_valid_workflow_is_accepted_and_slots_injected(self) -> None:
        provider = self.provider()
        request = make_request()
        result = await provider.generate(request, "sdxl-base", None)
        self.assertEqual(JobState.POLLING, result.state)
        assert result.external_job_id is not None
        package = provider.local_package(result.external_job_id)
        injected = self.injected_workflow(package)
        node2 = injected["2"]["inputs"]
        node3 = injected["3"]["inputs"]
        node4 = injected["4"]["inputs"]
        node1 = injected["1"]["inputs"]
        assert isinstance(node2, dict)
        assert isinstance(node3, dict)
        assert isinstance(node4, dict)
        assert isinstance(node1, dict)
        self.assertEqual(request.prompt, node2["text"])
        self.assertEqual(request.negative_prompt, node3["text"])
        self.assertEqual(request.width, node4["width"])
        self.assertEqual(request.height, node4["height"])
        self.assertEqual("sd_xl_base_1.0.safetensors", node1["ckpt_name"])
        self.assertNotIn(CANARY, json.dumps(package, sort_keys=True))

    async def test_seed_slot_injection_when_provided(self) -> None:
        provider = self.provider()
        request = replace(
            make_request(),
            provider_options={**make_request().provider_options, "seed": 42},
        )
        result = await provider.generate(request, "sdxl-base", None)
        assert result.external_job_id is not None
        injected = self.injected_workflow(provider.local_package(result.external_job_id))
        node5 = injected["5"]["inputs"]
        assert isinstance(node5, dict)
        self.assertEqual(42, node5["seed"])

    async def test_estimate_returns_currency_and_model(self) -> None:
        provider = self.provider()
        estimate = await provider.estimate(make_request(), "sdxl-base")
        self.assertEqual("USD", estimate["currency"])
        self.assertEqual("sdxl-base", estimate["model"])

    async def test_missing_required_negative_prompt_is_rejected(self) -> None:
        provider = self.provider()
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(
                make_request(negative_prompt=None),
                "sdxl-base",
                None,
            )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_unapproved_node_class_is_rejected(self) -> None:
        base = json.loads(SDXL_BASE.read_text())
        injected = json.loads(json.dumps(base))
        injected["workflow"]["99"] = {
            "class_type": "DownloadAndLoadGithubModel",
            "inputs": {"repo": "https://evil.example/repo"},
        }
        path = write_fixture("sdxl-base", injected)
        provider = self.provider(fixture_dir=path.parent)
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(make_request(), "sdxl-base", None)
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_missing_required_node_is_rejected(self) -> None:
        base = json.loads(SDXL_BASE.read_text())
        del base["workflow"]["5"]  # KSampler is required
        path = write_fixture("sdxl-base", base)
        provider = self.provider(fixture_dir=path.parent)
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(make_request(), "sdxl-base", None)
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_prompt_injection_only_into_approved_slots(self) -> None:
        provider = self.provider()
        request = make_request(prompt='a", "4": {"class_type": "arbitrary"}')
        result = await provider.generate(request, "sdxl-base", None)
        assert result.external_job_id is not None
        package = provider.local_package(result.external_job_id)
        injected = self.injected_workflow(package)
        node2 = injected["2"]["inputs"]
        assert isinstance(node2, dict)
        self.assertEqual(
            'a", "4": {"class_type": "arbitrary"}',
            node2["text"],
        )
        self.assertEqual(
            set(injected),
            set(json.loads(SDXL_BASE.read_text())["workflow"]),
        )

    async def test_width_height_validation_and_injection(self) -> None:
        provider = self.provider()
        for request in (
            make_request(width=64, height=64),
            make_request(width=100, height=1024),
        ):
            with self.subTest(width=request.width):
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(request, "sdxl-base", None)
                self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_oversized_deep_and_malformed_payload_rejection(self) -> None:
        with self.assertRaises(ProviderError):
            _validate_fixture_payload(b"not json")
        with self.assertRaises(ProviderError):
            _validate_fixture_payload(b"[]")
        deep = json.dumps({"a": {"b": {"c": {"d": {"e": {}}}}}})
        with self.assertRaises(ProviderError):
            _validate_fixture_payload(deep.encode())

    async def test_paths_urls_tokens_cookies_and_machine_paths_rejected(self) -> None:
        base = json.loads(SDXL_BASE.read_text())
        attacks = (
            {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "C:/abs.ckpt"}}},
            {"2": {"class_type": "CLIPTextEncode", "inputs": {"text": "https://evil.example"}}},
            {"2": {"class_type": "CLIPTextEncode", "inputs": {"text": "login=true; session=a"}}},
            {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "/etc/shadow"}}},
            {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "..\\..\\escape"}}},
        )
        for attack in attacks:
            with self.subTest(attack=str(attack)):
                injected = json.loads(json.dumps(base))
                injected["workflow"].update(attack)
                path = write_fixture("sdxl-base", injected)
                provider = self.provider(fixture_dir=path.parent)
                with self.assertRaises(ProviderError) as caught:
                    await provider.generate(make_request(), "sdxl-base", None)
                self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)


class RemoteLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def remote_provider(self, handler: object) -> ComfyUIProvider:
        return ComfyUIProvider(
            approved_origins=frozenset({REMOTE_ORIGIN}),
            transport=httpx.MockTransport(handler),
        )

    def test_remote_route_rejects_loopback_private_linklocal_metadata_credentials(self) -> None:
        for origin in (
            "http://127.0.0.1:8188",
            "http://localhost:8188",
            "http://[::1]:8188",
            "https://10.0.0.5",
            "https://192.168.1.10",
            "https://169.254.169.254",
            "https://fe80::1",
            "https://0.0.0.0",
            "https://user:pass@comfy.example",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                ComfyUIProvider(approved_origins=frozenset({origin}))

    async def test_remote_submission_translation(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            self.assertEqual("/api/prompt", request.url.path)
            prompt = json.loads(request.content)["prompt"]
            self.assertEqual("sd_xl_base_1.0.safetensors", prompt["1"]["inputs"]["ckpt_name"])
            self.assertEqual("A bounded private prompt.", prompt["2"]["inputs"]["text"])
            self.assertEqual("no text, no watermark", prompt["3"]["inputs"]["text"])
            self.assertEqual(1024, prompt["4"]["inputs"]["width"])
            self.assertEqual(1024, prompt["4"]["inputs"]["height"])
            return httpx.Response(200, json={"prompt_id": PROMPT_ID}, request=request)

        provider = self.remote_provider(handler)
        result = await provider.generate(make_request(), "sdxl-base", None)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertEqual(PROMPT_ID, result.external_job_id)
        self.assertEqual("POST", seen[0].method)
        self.assertEqual(f"{REMOTE_BASE}/prompt", str(seen[0].url))

    async def test_remote_workflow_does_not_export_secrets(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            rendered = json.dumps(body)
            for forbidden in (CANARY, "https://example.test", "C:/private"):
                self.assertNotIn(forbidden, rendered)
            return httpx.Response(200, json={"prompt_id": PROMPT_ID}, request=request)

        await self.remote_provider(handler).generate(make_request(), "sdxl-base", None)

    async def test_polling_pending_running_and_completed_states(self) -> None:
        polls = 0
        deliveries = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls, deliveries
            if request.url.path == f"/api/history/{PROMPT_ID}":
                polls += 1
                if polls == 1:
                    return httpx.Response(200, json={}, request=request)
                if polls == 2:
                    return httpx.Response(
                        200,
                        json={PROMPT_ID: {"status": {"completed": False, "status_str": "running"}}},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={
                        PROMPT_ID: {
                            "status": {"completed": True, "status_str": "success"},
                            "outputs": {
                                "9": {
                                    "images": [
                                        {
                                            "filename": "comic_sol_00001_.png",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            },
                        }
                    },
                    request=request,
                )
            if request.url.path == "/api/view":
                deliveries += 1
                return httpx.Response(
                    200,
                    content=PNG,
                    headers={"content-type": "image/png"},
                    request=request,
                )
            self.fail(f"unexpected request: {request.method} {request.url}")

        provider = self.remote_provider(handler)
        pending = await provider.poll(PROMPT_ID, None)
        running = await provider.poll(PROMPT_ID, None)
        complete = await provider.poll(PROMPT_ID, None)
        self.assertEqual(JobState.POLLING, pending.state)
        self.assertEqual(JobState.POLLING, running.state)
        self.assertEqual(JobState.ACCEPTED, complete.state)
        self.assertEqual(PNG, complete.raster_bytes)
        self.assertEqual("image/png", complete.media_type)
        self.assertEqual(1, deliveries)

    async def test_cancellation_behavior(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={}, request=request)

        await self.remote_provider(handler).cancel(PROMPT_ID, None)
        self.assertEqual("POST", seen[0].method)
        self.assertEqual(f"{REMOTE_BASE}/interrupt", str(seen[0].url))

    async def test_output_history_lookup_failure_states(self) -> None:
        states = iter(
            (
                {PROMPT_ID: {"status": {"completed": True, "status_str": "error"}}},
                {
                    PROMPT_ID: {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {},
                    }
                },
            )
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(states), request=request)

        provider = self.remote_provider(handler)
        with self.assertRaises(ProviderError) as failed:
            await provider.poll(PROMPT_ID, None)
        self.assertIn(
            failed.exception.category,
            {ErrorCategory.PROVIDER_ERROR, ErrorCategory.INVALID_OUTPUT},
        )
        with self.assertRaises(ProviderError) as missing:
            await provider.poll(PROMPT_ID, None)
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, missing.exception.category)

    async def test_bounded_raster_mime_and_redirect_refusal(self) -> None:
        async def redirect(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/api/history/{PROMPT_ID}":
                return httpx.Response(
                    302,
                    headers={"location": f"{REMOTE_ORIGIN}/api/history"},
                    request=request,
                )
            return httpx.Response(404, request=request)

        provider = self.remote_provider(redirect)
        with self.assertRaises(ProviderError) as caught:
            await provider.poll(PROMPT_ID, None)
        self.assertEqual(ErrorCategory.PROVIDER_ERROR, caught.exception.category)

        async def bad_mime(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/api/history/{PROMPT_ID}":
                return httpx.Response(
                    200,
                    json={
                        PROMPT_ID: {
                            "status": {"completed": True, "status_str": "success"},
                            "outputs": {
                                "9": {
                                    "images": [
                                        {
                                            "filename": "x.png",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            },
                        }
                    },
                    request=request,
                )
            if request.url.path == "/api/view":
                return httpx.Response(
                    200,
                    content=b"not-a-raster",
                    headers={"content-type": "image/png"},
                    request=request,
                )
            return httpx.Response(404, request=request)

        provider = self.remote_provider(bad_mime)
        with self.assertRaises(ProviderError) as caught:
            await provider.poll(PROMPT_ID, None)
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, caught.exception.category)

    async def test_quota_rate_timeout_and_error_normalization(self) -> None:
        for status, category in (
            (429, ErrorCategory.RATE_LIMITED),
            (500, ErrorCategory.PROVIDER_ERROR),
            (408, ErrorCategory.TIMEOUT),
        ):

            async def error(request: httpx.Request, s: int = status) -> httpx.Response:
                return httpx.Response(s, json={}, request=request)

            provider = self.remote_provider(error)
            with self.assertRaises(ProviderError) as caught:
                await provider.generate(make_request(), "sdxl-base", None)
            self.assertEqual(category, caught.exception.category)

        async def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(CANARY, request=request)

        with self.assertRaises(ProviderError) as timed_out:
            await self.remote_provider(timeout).generate(make_request(), "sdxl-base", None)
        self.assertEqual(ErrorCategory.TIMEOUT, timed_out.exception.category)

    async def test_no_raw_payload_or_secret_leakage_in_errors_receipts_export(self) -> None:
        async def error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": CANARY}, request=request)

        provider = self.remote_provider(error)
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(make_request(), "sdxl-base", None)
        self.assertNotIn(CANARY, f"{caught.exception!s} {caught.exception!r}")

        local = ComfyUIProvider()
        result = await local.generate(make_request(), "sdxl-base", None)
        assert result.external_job_id is not None
        package = local.local_package(result.external_job_id)
        rendered = json.dumps(package, sort_keys=True)
        for forbidden in (CANARY, "https://example.test", "C:/private", "comfy.example"):
            self.assertNotIn(forbidden, rendered)


class LocalHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_route_never_contacts_localhost(self) -> None:
        provider = ComfyUIProvider()
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("network access")):
            result = await provider.generate(make_request(), "sdxl-base", None)
            package = provider.local_package(result.external_job_id)
        self.assertEqual(JobState.POLLING, result.state)
        self.assertNotIn("://", json.dumps(package))

    async def test_local_package_is_deterministic_portable_and_resumable(self) -> None:
        provider = ComfyUIProvider()
        request = make_request()
        first = await provider.generate(request, "sdxl-base", None)
        second = await provider.generate(request, "sdxl-base", None)
        self.assertEqual(first, second)
        assert first.external_job_id is not None
        self.assertEqual(
            provider.local_package(first.external_job_id),
            provider.local_package(second.external_job_id),
        )
        waiting = await provider.poll(first.external_job_id, None)
        self.assertEqual(JobState.POLLING, waiting.state)
        await provider.cancel(first.external_job_id, None)
        cancelled = await provider.poll(first.external_job_id, None)
        self.assertEqual(JobState.CANCELLED, cancelled.state)


class ImportIsolationTests(unittest.TestCase):
    def test_catalog_listing_and_types_import_without_executing_adapter(self) -> None:
        import comic_sol_web.generation.catalog as catalog_module
        import comic_sol_web.generation.providers.base as base_module

        self.assertTrue(hasattr(base_module, "ProviderAdapter"))
        self.assertTrue(any(e.provider == "comfyui" for e in catalog_module.CATALOG))

    def test_fixture_sha256_matches_canonical_workflow_bytes(self) -> None:
        data = json.loads(SDXL_BASE.read_text())
        canonical = json.dumps(
            data["workflow"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            data["workflow_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
