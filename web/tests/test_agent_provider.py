"""RED -> GREEN contracts for the agent-native generation handoff."""

from __future__ import annotations

import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.generation.providers.agent import (
    AGENT_MODEL,
    AgentProvider,
    agent_job_checksum,
    agent_locked_scope_digest,
    bind_agent_request,
)
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.types import ErrorCategory, GenerationRequest, JobState


class AgentProviderTests(unittest.IsolatedAsyncioTestCase):
    def make_request(
        self,
        *,
        prompt: str = "A bounded private prompt.",
        references: tuple[Path, ...] = (),
        capabilities: frozenset[str] = frozenset({"text_to_image", "custom_dimensions"}),
    ) -> GenerationRequest:
        return GenerationRequest(
            job_id="a" * 64,
            project_id="opaque-project-id",
            project_revision=7,
            subject_kind="panel",
            subject_id="p01-01",
            prompt=prompt,
            negative_prompt="no lettering",
            references=references,
            width=1024,
            height=1024,
            required_capabilities=capabilities,
            provider_options={
                "ignored_path": "/private/project/secret.png",
                "ignored_url": "https://example.test/private.png",
                "token": "must-never-enter-package",
            },
        )

    async def test_package_is_deterministic_bounded_and_contains_no_paths_or_urls(self) -> None:
        provider = AgentProvider(
            frozenset({"text_to_image", "custom_dimensions", "reference_images"})
        )
        request = self.make_request(
            references=(Path("/private/project/reference.png"),),
            capabilities=frozenset({"text_to_image", "custom_dimensions", "reference_images"}),
        )

        first = await provider.generate(request, AGENT_MODEL, "ignored-agent-token")
        first_bytes = provider.package_bytes(first.external_job_id)
        second = await provider.generate(request, AGENT_MODEL, None)
        second_bytes = provider.package_bytes(second.external_job_id)

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertLessEqual(len(first_bytes), provider.max_package_bytes)
        package = json.loads(first_bytes)
        self.assertEqual("agent", package["provider_id"])
        self.assertEqual(request.job_id, package["job_id"])
        self.assertEqual(request.project_id, package["project_id"])
        self.assertEqual(request.project_revision, package["project_revision"])
        self.assertEqual(agent_job_checksum(request), package["job_checksum"])
        self.assertEqual(agent_locked_scope_digest(request), package["locked_scope_digest"])
        self.assertEqual([{"ordinal": 1}], package["references"])
        rendered = first_bytes.decode("utf-8")
        for forbidden in (
            "/private/project/reference.png",
            "/private/project/secret.png",
            "https://example.test/private.png",
            "must-never-enter-package",
            "ignored-agent-token",
            "cookie",
            "subscription",
            "quota",
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_canonical_engine_digests_are_preserved_in_the_package(self) -> None:
        provider = AgentProvider(frozenset({"text_to_image", "custom_dimensions"}))
        request = bind_agent_request(
            self.make_request(),
            locked_scope_sha256="e" * 64,
            job_sha256="f" * 64,
        )

        waiting = await provider.generate(request, AGENT_MODEL, None)
        package = json.loads(provider.package_bytes(waiting.external_job_id))

        self.assertEqual("e" * 64, package["locked_scope_digest"])
        self.assertEqual("f" * 64, package["job_checksum"])
        self.assertEqual("agent:" + "f" * 64, waiting.external_job_id)

    async def test_missing_active_image_capability_is_normalized_and_resumable(self) -> None:
        provider = AgentProvider(frozenset({"text_to_image"}))
        with self.assertRaises(ProviderError) as caught:
            await provider.generate(self.make_request(), AGENT_MODEL, None)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, caught.exception.category)
        self.assertNotIn("prompt", str(caught.exception).lower())

    async def test_wrong_model_and_oversized_material_fail_closed(self) -> None:
        provider = AgentProvider(frozenset({"text_to_image", "custom_dimensions"}))
        with self.assertRaises(ProviderError) as wrong_model:
            await provider.generate(self.make_request(), "arbitrary-provider-model", None)
        self.assertEqual(ErrorCategory.CAPABILITY_MISSING, wrong_model.exception.category)

        with self.assertRaises(ProviderError) as oversized:
            await provider.generate(
                self.make_request(prompt="x" * (provider.max_package_bytes + 1)),
                AGENT_MODEL,
                None,
            )
        self.assertEqual(ErrorCategory.INVALID_OUTPUT, oversized.exception.category)

    async def test_adapter_never_uses_network_or_agent_account_state(self) -> None:
        provider = AgentProvider(frozenset({"text_to_image", "custom_dimensions"}))
        account_canaries = (
            "chatgpt-cookie-canary",
            "codex-session-token-canary",
            "plus-plan-canary",
            "quota-canary",
        )
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("network access")):
            for canary in account_canaries:
                result = await provider.generate(self.make_request(), AGENT_MODEL, canary)
                package = provider.package_bytes(result.external_job_id)
                self.assertNotIn(canary, package.decode("utf-8"))
        self.assertEqual(
            1, len({agent_job_checksum(self.make_request()) for _ in account_canaries})
        )

    async def test_poll_waits_and_cancel_is_idempotent(self) -> None:
        provider = AgentProvider(frozenset({"text_to_image", "custom_dimensions"}))
        started = await provider.generate(self.make_request(), AGENT_MODEL, None)
        self.assertEqual(JobState.POLLING, started.state)
        assert started.external_job_id is not None
        waiting = await provider.poll(started.external_job_id, "ignored-token")
        self.assertEqual(started, waiting)
        await provider.cancel(started.external_job_id, None)
        await provider.cancel(started.external_job_id, "ignored-token")
        cancelled = await provider.poll(started.external_job_id, None)
        self.assertEqual(JobState.CANCELLED, cancelled.state)
        self.assertIsNone(cancelled.raster_bytes)


if __name__ == "__main__":
    unittest.main()
