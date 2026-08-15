import concurrent.futures
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from comic_sol_product.cli import _load_engine
from comic_sol_product.providers import (
    GenerationFailure,
    GenerationProvider,
    GenerationRequest,
    GenerationResult,
    retain_generation_result,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def generate(self, request: GenerationRequest) -> GenerationResult:
        buffer = io.BytesIO()
        Image.new("RGB", (request.width, request.height), "navy").save(buffer, "PNG")
        return GenerationResult.from_bytes(
            buffer.getvalue(),
            media_type="image/png",
            width=request.width,
            height=request.height,
            provider="fake",
            model="fixture-v1",
            request_id="request-001",
            seed=request.seed,
            references_used=request.reference_paths,
        )


class ProviderContractTests(unittest.TestCase):
    @staticmethod
    def _result(color: str) -> GenerationResult:
        buffer = io.BytesIO()
        Image.new("RGB", (512, 512), color).save(buffer, "PNG")
        return GenerationResult.from_bytes(
            buffer.getvalue(), media_type="image/png", width=512, height=512
        )

    @staticmethod
    def _generation_state(project: Path) -> tuple[bytes | None, bytes | None]:
        counters = project / "logs/generation-counters.json"
        events = project / "logs/events.jsonl"
        return (
            counters.read_bytes() if counters.is_file() else None,
            events.read_bytes() if events.is_file() else None,
        )

    def test_request_is_immutable_and_serializes_canonically(self):
        request = GenerationRequest(
            panel_id="p01-01",
            prompt="A courier under a blue sky.",
            width=640,
            height=960,
            reference_paths=("references/characters/mira.png",),
            provider="fake",
            model="fixture-v1",
            seed=7,
        )
        self.assertIsInstance(FakeProvider(), GenerationProvider)
        self.assertEqual(
            {
                "height": 960,
                "model": "fixture-v1",
                "panel_id": "p01-01",
                "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
                "provider": "fake",
                "reference_paths": ["references/characters/mira.png"],
                "seed": 7,
                "width": 640,
            },
            request.to_record(),
        )
        canonical = request.canonical_json()
        self.assertEqual(canonical, json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":")))
        with self.assertRaisesRegex(AttributeError, "cannot assign"):
            request.width = 512  # type: ignore[misc]

    def test_request_rejects_unsafe_paths_dimensions_and_secret_fields(self):
        for path in ("/tmp/secret.png", "../secret.png", "C:\\secret.png"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "relative project path"):
                GenerationRequest("p01-01", "prompt", 640, 960, (path,))
        with self.assertRaisesRegex(ValueError, "at least 512"):
            GenerationRequest("p01-01", "prompt", 511, 960)
        with self.assertRaises(TypeError):
            GenerationRequest(  # type: ignore[call-arg]
                "p01-01", "prompt", 640, 960, api_key="must-not-be-accepted"
            )

    def test_result_hash_and_allowlisted_record(self):
        request = GenerationRequest("p01-01", "prompt", 512, 512, seed=3)
        result = FakeProvider().generate(request)
        self.assertEqual(hashlib.sha256(result.image_bytes).hexdigest(), result.sha256)
        self.assertEqual(
            {
                "height": 512,
                "media_type": "image/png",
                "model": "fixture-v1",
                "provider": "fake",
                "reference_paths": [],
                "request_id": "request-001",
                "seed": 3,
                "sha256": result.sha256,
                "width": 512,
            },
            result.to_record(),
        )
        self.assertNotIn("image_bytes", result.to_record())

    def test_failure_sanitizes_absolute_paths_and_has_stable_category(self):
        for raw_path in ("/tmp/private/payload.json", r"C:\private\payload.json"):
            with self.subTest(raw_path=raw_path):
                failure = GenerationFailure("transient", f"provider failed at {raw_path}")
                self.assertEqual("transient", failure.category)
                self.assertNotIn(raw_path, failure.message)
                self.assertIn("<path>", failure.message)
                self.assertEqual(
                    {"category": "transient", "message": failure.message}, failure.to_record()
                )

    def test_failure_sanitizes_quoted_paths_with_spaces_as_one_unit(self):
        cases = (
            ("'", "/tmp/Comic Sol/private payload.json"),
            ("'", r"C:\Comic Sol\private payload.json"),
            ('"', "/tmp/Comic Sol/O'Brien/private payload.json"),
            ('"', r"C:\Comic Sol\O'Brien\private payload.json"),
        )
        for quote, raw_path in cases:
            with self.subTest(quote=quote, raw_path=raw_path):
                failure = GenerationFailure(
                    "transient", f"provider failed at {quote}{raw_path}{quote}"
                )
                self.assertEqual(f"provider failed at {quote}<path>{quote}", failure.message)
                self.assertNotIn(raw_path, failure.message)

    def test_fake_provider_result_is_retained_through_engine_accounting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            request = GenerationRequest("p01-01", "fixture", 640, 960, seed=11)
            result = FakeProvider().generate(request)

            counters = retain_generation_result(
                project, "p01-01", "initial", result
            )

            self.assertEqual(1, counters["initial"])
            retained = project / "panels/attempts/p01-01/initial-1.png"
            self.assertTrue(retained.is_file())
            self.assertEqual(
                result.sha256, hashlib.sha256(retained.read_bytes()).hexdigest()
            )

    def test_rejected_provider_attempt_does_not_overwrite_retained_image(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            first = self._result("red")
            rejected = self._result("blue")
            retained = project / "panels/attempts/p01-01/initial-1.png"

            retain_generation_result(project, "p01-01", "initial", first)
            before = self._generation_state(project), retained.read_bytes()
            with self.assertRaisesRegex(ValueError, "one initial attempt"):
                retain_generation_result(project, "p01-01", "initial", rejected)

            self.assertEqual(
                before, (self._generation_state(project), retained.read_bytes())
            )
            self.assertFalse((retained.parent / "initial-2.png").exists())

    def test_invalid_provider_raster_leaves_project_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            invalid = GenerationResult.from_bytes(
                b"not an image", media_type="image/png", width=512, height=512
            )
            before = self._generation_state(project)

            with self.assertRaisesRegex(ValueError, "readable raster"):
                retain_generation_result(project, "p01-01", "initial", invalid)

            self.assertEqual(before, self._generation_state(project))
            attempt_dir = project / "panels/attempts/p01-01"
            self.assertFalse(attempt_dir.exists() and any(attempt_dir.iterdir()))

    def test_visual_retries_are_retained_at_unique_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            first = self._result("red")
            second = self._result("blue")

            retain_generation_result(project, "p01-01", "visual_retry", first)
            counts = retain_generation_result(project, "p01-01", "visual_retry", second)

            attempts = project / "panels/attempts/p01-01"
            self.assertEqual(
                first.image_bytes, (attempts / "visual_retry-1.png").read_bytes()
            )
            self.assertEqual(
                second.image_bytes, (attempts / "visual_retry-2.png").read_bytes()
            )
            self.assertEqual(2, counts["visual_retries"])

    def test_concurrent_provider_attempts_cannot_exceed_budget(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            results = (self._result("red"), self._result("blue"))

            def retain(result: GenerationResult) -> str:
                try:
                    retain_generation_result(project, "p01-01", "initial", result)
                    return "success"
                except ValueError:
                    return "rejected"

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(retain, results))

            self.assertEqual(["rejected", "success"], sorted(outcomes))
            retained = project / "panels/attempts/p01-01/initial-1.png"
            self.assertIn(
                retained.read_bytes(), {result.image_bytes for result in results}
            )
            counters = json.loads(
                (project / "logs/generation-counters.json").read_text("utf-8")
            )
            self.assertEqual(1, counters["panels"]["p01-01"]["initial"])

    def test_publish_failure_rolls_back_attempt_counters_and_event(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            before = self._generation_state(project)
            engine = _load_engine()
            from scripts import project_io

            original_commit = engine.ProjectTransaction.commit
            original_replace = project_io.os.replace

            def fail_counter_publish(source, destination, *args, **kwargs):
                if Path(os.fspath(destination)).name == "generation-counters.json":
                    raise OSError("injected counter publish failure")
                return original_replace(source, destination, *args, **kwargs)

            def commit_with_failure(transaction):
                with patch.object(
                    project_io.os, "replace", side_effect=fail_counter_publish
                ):
                    return original_commit(transaction)

            with patch.object(
                engine.ProjectTransaction, "commit", commit_with_failure
            ), self.assertRaisesRegex(OSError, "injected counter publish failure"):
                retain_generation_result(
                    project, "p01-01", "initial", self._result("red")
                )

            self.assertEqual(before, self._generation_state(project))
            attempt_dir = project / "panels/attempts/p01-01"
            self.assertFalse(attempt_dir.exists() and any(attempt_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
