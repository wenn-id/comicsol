import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

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
        failure = GenerationFailure("transient", "provider failed at /tmp/private/payload.json")
        self.assertEqual("transient", failure.category)
        self.assertNotIn("/tmp/private", failure.message)
        self.assertIn("<path>", failure.message)
        self.assertEqual(
            {"category": "transient", "message": failure.message}, failure.to_record()
        )

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
            retained = project / "panels/attempts/p01-01/initial.png"
            self.assertTrue(retained.is_file())
            self.assertEqual(result.sha256, hashlib.sha256(retained.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
