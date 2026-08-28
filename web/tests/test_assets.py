from __future__ import annotations

import binascii
import io
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from comic_sol_web.assets import HAS_DIRECTORY_HANDLES, AssetError, AssetStore
from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database
from comic_sol_web.migrations import apply_migrations
from support import make_symlink


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


if __name__ == "__main__":
    unittest.main()
