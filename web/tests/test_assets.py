from __future__ import annotations

import binascii
import io
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from comic_sol_web.api.assets import create_assets_router
from comic_sol_web.assets import HAS_DIRECTORY_HANDLES, AssetError, AssetStore
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.generation.service import GenerationConflictError, GenerationUnavailableError
from comic_sol_web.generation.types import JobState
from comic_sol_web.migrations import apply_migrations
from web.tests.support import make_symlink


def png_bytes(width: int = 2, height: int = 2, *, trailing_decompressed: int = 0) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + (b"\x00\x40\x80" * width) for _ in range(height))
    rows += b"x" * trailing_decompressed
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class FakeAuth:
    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal

    def require_csrf(self, _request: object) -> SessionPrincipal:
        return self.principal


class RecordingAgentSubmission:
    def __init__(self) -> None:
        self.calls: list[tuple[SessionPrincipal, str, str, int]] = []
        self.package_calls: list[tuple[SessionPrincipal, str, str, int]] = []
        self.package_error: Exception | None = None

    def agent_package(
        self,
        principal: SessionPrincipal,
        project_id: str,
        job_id: str,
        expected_revision: int,
    ) -> dict[str, object]:
        self.package_calls.append((principal, project_id, job_id, expected_revision))
        if self.package_error is not None:
            raise self.package_error
        return {
            "contract_version": "1.0",
            "provider_id": "agent",
            "project_id": project_id,
            "project_revision": expected_revision,
            "job_id": "b" * 64,
            "job_checksum": "c" * 64,
            "locked_scope_digest": "d" * 64,
            "prompt": "bounded prompt",
            "negative_prompt": None,
            "dimensions": {"width": 2, "height": 2},
            "references": [],
            "required_capabilities": ["text_to_image"],
            "subject": {"kind": "panel", "id": "p01-01"},
        }

    def submit_agent_asset(
        self,
        principal: SessionPrincipal,
        job_id: str,
        asset_id: str,
        expected_revision: int,
    ) -> object:
        self.calls.append((principal, job_id, asset_id, expected_revision))
        return SimpleNamespace(
            job_id=job_id,
            project_id="project-1",
            project_revision=expected_revision,
            state=JobState.ACCEPTED,
            provider="agent",
            model="active-agent-image",
            auth_mode=SimpleNamespace(value="agent"),
            attempt_number=1,
            retry_count=0,
            max_retries=2,
            external_job_id="agent:" + "a" * 64,
            accepted_project_revision=expected_revision + 1,
        )


class AssetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "web-data"
        self.database = Database(self.data_root / "application.sqlite3")
        apply_migrations(self.database)
        self.store = AssetStore(
            self.database,
            self.data_root,
            max_upload_bytes=4096,
            max_pixels=100,
            max_decoded_bytes=4096,
        )
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")

    def test_upload_returns_opaque_owner_bound_handle_and_stays_in_data_root(self) -> None:
        content = png_bytes()
        handle = self.store.create_upload(self.alice, io.BytesIO(content), "image/png")
        self.assertNotIn("/", handle.asset_id)
        self.assertNotIn("\\", handle.asset_id)
        self.assertEqual("image/png", handle.media_type)
        self.assertEqual((2, 2), (handle.width, handle.height))
        self.assertEqual(content, self.store.read_bytes(self.alice, handle.asset_id))
        self.assertNotIn(str(self.data_root), repr(handle))

        files = [path for path in self.data_root.rglob("*") if path.is_file()]
        self.assertTrue(any(path.name == "application.sqlite3" for path in files))
        asset_files = [path for path in files if path.suffix == ".png"]
        self.assertEqual(1, len(asset_files))
        self.assertTrue(asset_files[0].is_relative_to(self.data_root))

    def test_native_temporary_data_root_is_accepted(self) -> None:
        # macOS exposes native temporary directories through the root-owned
        # ``/var`` -> ``/private/var`` alias, so a plain absolute data root under
        # the platform temporary directory must not be mistaken for a symlinked
        # component.
        native_root = Path(tempfile.mkdtemp()) / "web-data"
        self.addCleanup(shutil.rmtree, native_root.parent, True)
        store = AssetStore(Database(native_root / "application.sqlite3"), native_root)
        apply_migrations(store.database)
        handle = store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        self.assertEqual(png_bytes(), store.read_bytes(self.alice, handle.asset_id))

    def test_cross_owner_access_fails_closed(self) -> None:
        handle = self.store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        with self.assertRaises(AssetError):
            self.store.get(self.bob, handle.asset_id)
        with self.assertRaises(AssetError):
            self.store.read_bytes(self.bob, handle.asset_id)

    def test_arbitrary_paths_and_urls_are_not_asset_handles(self) -> None:
        invalid = (
            "../outside.png",
            "/etc/passwd",
            "C:\\Windows\\system.ini",
            "https://example.test/image.png",
            "file:///tmp/image.png",
            "asset?id=one",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AssetError):
                self.store.get(self.alice, value)

    def test_mime_is_sniffed_and_spoofed_or_unsupported_media_is_rejected(self) -> None:
        content = png_bytes()
        accepted = self.store.create_upload(
            self.alice, io.BytesIO(content), "application/octet-stream"
        )
        self.assertEqual("image/png", accepted.media_type)
        with self.assertRaises(AssetError):
            self.store.create_upload(self.alice, io.BytesIO(content), "image/jpeg")
        with self.assertRaises(AssetError):
            self.store.create_upload(self.alice, io.BytesIO(b"not a raster"), "image/png")
        with self.assertRaises(AssetError):
            self.store.create_upload(self.alice, io.BytesIO(b"GIF89a" + b"x" * 20), "image/gif")

    def test_upload_size_limit_is_enforced_while_streaming(self) -> None:
        tiny_store = AssetStore(
            self.database,
            self.data_root,
            max_upload_bytes=32,
            max_pixels=100,
            max_decoded_bytes=4096,
        )
        with self.assertRaises(AssetError):
            tiny_store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        self.assertEqual([], list((self.data_root / "assets").rglob("*.png")))

    def test_dimension_and_decompressed_size_limits_reject_bombs(self) -> None:
        with self.assertRaises(AssetError):
            self.store.create_upload(self.alice, io.BytesIO(png_bytes(11, 10)), "image/png")
        with self.assertRaises(AssetError):
            self.store.create_upload(
                self.alice,
                io.BytesIO(png_bytes(2, 2, trailing_decompressed=5000)),
                "image/png",
            )

    def test_symlink_escape_is_rejected_before_asset_write_or_read(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        assets_root = self.data_root / "assets"
        assets_root.mkdir(parents=True)
        owner_directory = assets_root / self.store.owner_storage_id(self.alice)
        make_symlink(self, owner_directory, outside, directory=True)
        with self.assertRaises(AssetError):
            self.store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        self.assertEqual([], list(outside.iterdir()))

    def test_configured_data_root_symlink_is_rejected(self) -> None:
        real_root = Path(self.temporary_directory.name) / "real-root"
        real_root.mkdir()
        linked_root = Path(self.temporary_directory.name) / "linked-root"
        make_symlink(self, linked_root, real_root, directory=True)
        with self.assertRaises(AssetError):
            AssetStore(self.database, linked_root)

    @unittest.skipUnless(
        HAS_DIRECTORY_HANDLES, "directory-handle anchoring is unavailable on this platform"
    )
    def test_directory_swap_cannot_redirect_upload(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside-race"
        outside.mkdir()
        owner = self.data_root / "assets" / self.store.owner_storage_id(self.alice)
        displaced = self.data_root / "displaced"
        real_replace = os.replace

        def swap(source, destination, **kwargs):
            owner.rename(displaced)
            make_symlink(self, owner, outside, directory=True)
            return real_replace(source, destination, **kwargs)

        with patch("comic_sol_web.assets.os.replace", side_effect=swap):
            self.store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual(1, len(list(displaced.glob("*.png"))))


class AssetRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "web-data"
        self.database = Database(self.data_root / "application.sqlite3")
        apply_migrations(self.database)
        self.store = AssetStore(self.database, self.data_root)
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")

    def test_agent_package_route_is_owner_bound_and_never_cached(self) -> None:
        service = RecordingAgentSubmission()
        app = FastAPI()
        app.include_router(
            create_assets_router(lambda _request: self.store, lambda _request: service)
        )
        app.dependency_overrides[require_principal] = lambda: self.alice
        generation_job_id = "a" * 64

        with TestClient(app) as client:
            response = client.get(
                f"/api/assets/agent-handoff/{generation_job_id}",
                params={"project_id": "project-1", "expected_revision": 7},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("bounded prompt", response.json()["prompt"])
        self.assertEqual("private, no-store", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual(
            [(self.alice, "project-1", generation_job_id, 7)],
            service.package_calls,
        )
        rendered = response.text
        for forbidden in ("provider_options", "https://", "file://", "/tmp/", "token"):
            self.assertNotIn(forbidden, rendered)

    def test_agent_package_route_rejects_unbound_input_with_sanitized_response(self) -> None:
        service = RecordingAgentSubmission()
        app = FastAPI()
        app.include_router(
            create_assets_router(lambda _request: self.store, lambda _request: service)
        )
        app.dependency_overrides[require_principal] = lambda: self.alice

        with TestClient(app) as client:
            malformed = client.get(
                "/api/assets/agent-handoff/not-a-job",
                params={"project_id": "../outside", "expected_revision": 0},
            )

        self.assertEqual(400, malformed.status_code)
        self.assertEqual({"detail": "generation request rejected"}, malformed.json())
        self.assertEqual([], service.package_calls)

    def test_agent_package_route_sanitizes_wrong_owner_and_stale_scope(self) -> None:
        service = RecordingAgentSubmission()
        app = FastAPI()
        app.include_router(
            create_assets_router(lambda _request: self.store, lambda _request: service)
        )
        app.dependency_overrides[require_principal] = lambda: self.alice
        path = f"/api/assets/agent-handoff/{'a' * 64}"
        parameters = {"project_id": "project-1", "expected_revision": 7}

        with TestClient(app) as client:
            service.package_error = GenerationUnavailableError("private owner detail")
            wrong_owner = client.get(path, params=parameters)
            service.package_error = GenerationConflictError("private stale scope detail")
            stale = client.get(path, params=parameters)

        self.assertEqual(404, wrong_owner.status_code)
        self.assertEqual({"detail": "generation job unavailable"}, wrong_owner.json())
        self.assertNotIn("private owner detail", wrong_owner.text)
        self.assertEqual(409, stale.status_code)
        self.assertEqual({"detail": "generation state conflict"}, stale.json())
        self.assertNotIn("private stale scope detail", stale.text)

    def test_agent_submission_route_binds_auth_csrf_revision_and_asset_handle(self) -> None:
        service = RecordingAgentSubmission()
        app = FastAPI()
        app.state.auth = FakeAuth(self.alice)
        app.include_router(
            create_assets_router(lambda _request: self.store, lambda _request: service)
        )
        app.dependency_overrides[require_principal] = lambda: self.alice
        handle = self.store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")
        job_id = "a" * 64

        with TestClient(app) as client:
            response = client.post(
                f"/api/assets/{handle.asset_id}/submit-agent",
                json={"job_id": job_id, "expected_revision": 7},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(JobState.ACCEPTED.value, response.json()["state"])
        self.assertEqual(8, response.json()["accepted_project_revision"])
        self.assertEqual([(self.alice, job_id, handle.asset_id, 7)], service.calls)

    def test_agent_submission_route_rejects_bad_revision_and_identity_change(self) -> None:
        service = RecordingAgentSubmission()
        app = FastAPI()
        app.state.auth = FakeAuth(self.alice)
        app.include_router(
            create_assets_router(lambda _request: self.store, lambda _request: service)
        )
        app.dependency_overrides[require_principal] = lambda: self.alice
        handle = self.store.create_upload(self.alice, io.BytesIO(png_bytes()), "image/png")

        with TestClient(app) as client:
            invalid = client.post(
                f"/api/assets/{handle.asset_id}/submit-agent",
                json={"job_id": "a" * 64, "expected_revision": True},
            )
            app.state.auth = FakeAuth(self.bob)
            changed = client.post(
                f"/api/assets/{handle.asset_id}/submit-agent",
                json={"job_id": "a" * 64, "expected_revision": 7},
            )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual(403, changed.status_code)
        self.assertEqual([], service.calls)


if __name__ == "__main__":
    unittest.main()
