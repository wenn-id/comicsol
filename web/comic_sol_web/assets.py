"""Bounded, owner-isolated raster uploads addressed only by opaque handles."""

from __future__ import annotations

import binascii
import hashlib
import os
import re
import secrets
import stat
import struct
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from comic_sol_web.auth import SessionPrincipal
from comic_sol_web.database import Database

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_MAX_UPLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_DECODED_BYTES = 160 * 1024 * 1024
_HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
_REPARSE_POINT = 0x400
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
# Anchoring every write to an already-validated directory descriptor closes the
# check/use race between validating the owner directory and writing into it.
# Windows exposes neither ``O_NOFOLLOW`` nor ``dir_fd``, so it falls back to
# component-by-component pathname validation. This mirrors the POSIX-only split
# that `scripts/project_io.py` documents for the deterministic engine.
HAS_DIRECTORY_HANDLES = _O_NOFOLLOW != 0 and {os.open, os.mkdir, os.stat, os.unlink, os.rename} <= (
    os.supports_dir_fd
)


class AssetError(ValueError):
    """An upload or asset lookup failed its bounded trust checks."""


@dataclass(frozen=True)
class AssetHandle:
    asset_id: str
    media_type: str
    byte_size: int
    width: int
    height: int


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _canonical_data_root(path: Path) -> Path:
    """Resolve the macOS native alias in a configured data root.

    macOS exposes its native temporary directories through root-owned aliases
    such as ``/var`` -> ``/private/var``, so an ordinary absolute data root would
    be refused as a symlinked component. Only that leading platform alias is
    canonicalized, matching `external_output_path` in `scripts/project_io.py`;
    every remaining component is still refused if it is a link or reparse point.
    """
    absolute = path.expanduser().absolute()
    if sys.platform != "darwin" or absolute.anchor != "/" or len(absolute.parts) < 2:
        return absolute
    alias = Path("/") / absolute.parts[1]
    if alias.name in {"tmp", "var"} and alias.is_symlink():
        return alias.resolve(strict=True).joinpath(*absolute.parts[2:])
    return absolute


def _ensure_existing_components_are_plain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise AssetError("asset storage path is not a plain directory")


def _make_plain_directory(path: Path) -> None:
    _ensure_existing_components_are_plain(path)
    path.mkdir(parents=True, exist_ok=True)
    _ensure_existing_components_are_plain(path)
    if not path.is_dir() or _is_link_or_reparse(path):
        raise AssetError("asset storage path is not a plain directory")


def _open_absolute_directory(path: Path, *, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
    parts = path.absolute().parts
    descriptor = os.open(parts[0], flags)
    try:
        for component in parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _OwnerDirectory:
    """A validated owner directory that anchors every subsequent operation.

    On POSIX the directory is held open as a descriptor, so a later component
    swap cannot redirect a write outside the configured root. Windows has no
    ``dir_fd`` support, so it revalidates the pathname before each operation and
    relies on ``O_EXCL`` plus a post-open descriptor check instead.
    """

    def __init__(self, path: Path, descriptor: int | None) -> None:
        self.path = path
        self._descriptor = descriptor

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def _validated(self, name: str) -> Path:
        _ensure_existing_components_are_plain(self.path)
        if not self.path.is_dir() or _is_link_or_reparse(self.path):
            raise AssetError("asset storage path is not a plain directory")
        return self.path / name

    def exists(self, name: str) -> bool:
        try:
            if self._descriptor is None:
                os.stat(self._validated(name), follow_symlinks=False)
            else:
                os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def create(self, name: str) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
        if self._descriptor is None:
            return os.open(self._validated(name), flags, 0o600)
        return os.open(name, flags, 0o600, dir_fd=self._descriptor)

    def open_read(self, name: str) -> int:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY
        if self._descriptor is None:
            target = self._validated(name)
            if _is_link_or_reparse(target):
                raise AssetError("asset file is not plain")
            return os.open(target, flags)
        return os.open(name, flags, dir_fd=self._descriptor)

    def publish(self, temporary: str, name: str) -> None:
        if self._descriptor is None:
            os.replace(self._validated(temporary), self._validated(name))
            return
        os.replace(temporary, name, src_dir_fd=self._descriptor, dst_dir_fd=self._descriptor)

    def discard(self, name: str) -> None:
        """Remove a name best-effort, never following a swapped-in link."""
        try:
            if self._descriptor is None:
                os.unlink(self._validated(name))
            else:
                os.unlink(name, dir_fd=self._descriptor)
        except (AssetError, OSError):
            # Cleanup runs on a failure path. A path that no longer validates is
            # left alone rather than deleted through whatever replaced it.
            pass


def _png_dimensions_and_decode_size(data: bytes, max_decoded_bytes: int) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE):
        raise AssetError("unsupported raster media type")
    offset = len(PNG_SIGNATURE)
    width = height = 0
    bit_depth = color_type = -1
    compressed = bytearray()
    seen_header = False
    seen_end = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if payload_end < payload_start or crc_end > len(data):
            raise AssetError("PNG structure is invalid")
        payload = data[payload_start:payload_end]
        expected_crc = struct.unpack(">I", data[payload_end:crc_end])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise AssetError("PNG checksum is invalid")
        if kind == b"IHDR":
            if seen_header or offset != len(PNG_SIGNATURE) or length != 13:
                raise AssetError("PNG header is invalid")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if compression != 0 or filtering != 0 or interlace != 0:
                raise AssetError("unsupported PNG encoding")
            seen_header = True
        elif kind == b"IDAT":
            if not seen_header or seen_end:
                raise AssetError("PNG chunk order is invalid")
            compressed.extend(payload)
        elif kind == b"IEND":
            if length != 0:
                raise AssetError("PNG end marker is invalid")
            seen_end = True
            offset = crc_end
            break
        offset = crc_end
    if not seen_header or not seen_end or offset != len(data) or not compressed:
        raise AssetError("PNG structure is incomplete")
    channels_by_color = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    allowed_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if color_type not in channels_by_color or bit_depth not in allowed_depths[color_type]:
        raise AssetError("unsupported PNG pixel format")
    if width <= 0 or height <= 0:
        raise AssetError("PNG dimensions are invalid")
    row_bytes = (width * channels_by_color[color_type] * bit_depth + 7) // 8
    expected_size = (row_bytes + 1) * height
    if expected_size > max_decoded_bytes:
        raise AssetError("decoded raster exceeds the configured limit")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), expected_size + 1)
        if len(decoded) <= expected_size:
            decoded += decompressor.flush(expected_size + 1 - len(decoded))
    except zlib.error as error:
        raise AssetError("PNG pixel data is invalid") from error
    if len(decoded) != expected_size or not decompressor.eof or decompressor.unused_data:
        raise AssetError("PNG decoded size is invalid")
    stride = row_bytes + 1
    if any(decoded[row * stride] > 4 for row in range(height)):
        raise AssetError("PNG row filter is invalid")
    return width, height


class AssetStore:
    def __init__(
        self,
        database: Database,
        data_root: Path | str,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        configured = Path(data_root)
        if not configured.is_absolute():
            raise AssetError("asset data root must be absolute")
        if min(max_upload_bytes, max_pixels, max_decoded_bytes) <= 0:
            raise ValueError("asset limits must be positive")
        self.data_root = _canonical_data_root(configured)
        _ensure_existing_components_are_plain(self.data_root)
        if self.data_root.exists() and (
            not self.data_root.is_dir() or _is_link_or_reparse(self.data_root)
        ):
            raise AssetError("asset data root must be a plain directory")
        self.assets_root = self.data_root / "assets"
        self.max_upload_bytes = max_upload_bytes
        self.max_pixels = max_pixels
        self.max_decoded_bytes = max_decoded_bytes
        self._clock = clock

    def _owner_directory(self, principal: SessionPrincipal) -> _OwnerDirectory:
        owner = self.owner_storage_id(principal)
        path = self.assets_root / owner
        if not HAS_DIRECTORY_HANDLES:
            _make_plain_directory(path)
            return _OwnerDirectory(path, None)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
        try:
            root_fd = _open_absolute_directory(self.data_root, create=True)
            try:
                try:
                    os.mkdir("assets", 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                assets_fd = os.open("assets", flags, dir_fd=root_fd)
            finally:
                os.close(root_fd)
            try:
                try:
                    os.mkdir(owner, 0o700, dir_fd=assets_fd)
                except FileExistsError:
                    pass
                descriptor = os.open(owner, flags, dir_fd=assets_fd)
            finally:
                os.close(assets_fd)
        except OSError as error:
            raise AssetError("asset storage path is not a plain directory") from error
        return _OwnerDirectory(path, descriptor)

    def owner_storage_id(self, principal: SessionPrincipal) -> str:
        if not principal.user_id:
            raise AssetError("asset owner is invalid")
        return hashlib.sha256(principal.user_id.encode("utf-8")).hexdigest()[:32]

    def _read_upload(self, stream: BinaryIO) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(min(64 * 1024, self.max_upload_bytes - total + 1))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise AssetError("upload stream must produce bytes")
            total += len(chunk)
            if total > self.max_upload_bytes:
                raise AssetError("upload exceeds the configured limit")
            chunks.append(chunk)
        if total == 0:
            raise AssetError("upload is empty")
        return b"".join(chunks)

    def _validate_raster(self, content: bytes, supplied_media_type: str) -> tuple[str, int, int]:
        media_type = supplied_media_type.partition(";")[0].strip().lower()
        if content.startswith(PNG_SIGNATURE):
            sniffed = "image/png"
            width, height = _png_dimensions_and_decode_size(content, self.max_decoded_bytes)
        else:
            raise AssetError("unsupported raster media type")
        if media_type not in {"", "application/octet-stream", sniffed}:
            raise AssetError("supplied media type does not match uploaded bytes")
        if width * height > self.max_pixels:
            raise AssetError("raster dimensions exceed the configured limit")
        return sniffed, width, height

    def create_upload(
        self,
        principal: SessionPrincipal,
        stream: BinaryIO,
        media_type: str,
    ) -> AssetHandle:
        content = self._read_upload(stream)
        sniffed, width, height = self._validate_raster(content, media_type)
        owner = self._owner_directory(principal)
        asset_id = secrets.token_urlsafe(24)
        filename = f"{asset_id}.png"
        temporary = f".{asset_id}.{secrets.token_urlsafe(8)}.tmp"
        try:
            if owner.exists(filename):
                raise AssetError("asset storage collision")
            descriptor = owner.create(temporary)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                owner.publish(temporary, filename)
            except BaseException:
                owner.discard(temporary)
                owner.discard(filename)
                raise
            now = int(self._clock())
            storage_name = f"{self.owner_storage_id(principal)}/{filename}"
            try:
                with self.database.transaction() as connection:
                    connection.execute(
                        "INSERT INTO users (user_id, login, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET login = excluded.login, "
                        "updated_at = excluded.updated_at",
                        (principal.user_id, principal.login, now),
                    )
                    connection.execute(
                        "INSERT INTO assets (asset_id, owner_id, storage_name, media_type, "
                        "byte_size, width, height, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            asset_id,
                            principal.user_id,
                            storage_name,
                            sniffed,
                            len(content),
                            width,
                            height,
                            now,
                        ),
                    )
            except BaseException:
                owner.discard(filename)
                raise
        finally:
            owner.close()
        return AssetHandle(asset_id, sniffed, len(content), width, height)

    def _row_for(self, principal: SessionPrincipal, asset_id: str):
        if not _HANDLE_PATTERN.fullmatch(asset_id):
            raise AssetError("asset handle is invalid")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT asset_id, storage_name, media_type, byte_size, width, height "
                "FROM assets WHERE asset_id = ? AND owner_id = ?",
                (asset_id, principal.user_id),
            ).fetchone()
        if row is None:
            raise AssetError("asset is unavailable")
        expected_name = f"{self.owner_storage_id(principal)}/{asset_id}.png"
        if row["storage_name"] != expected_name:
            raise AssetError("asset storage metadata is invalid")
        return row

    def get(self, principal: SessionPrincipal, asset_id: str) -> AssetHandle:
        row = self._row_for(principal, asset_id)
        return AssetHandle(
            row["asset_id"], row["media_type"], row["byte_size"], row["width"], row["height"]
        )

    def read_bytes(self, principal: SessionPrincipal, asset_id: str) -> bytes:
        row = self._row_for(principal, asset_id)
        owner = self._owner_directory(principal)
        try:
            descriptor = owner.open_read(f"{asset_id}.png")
        except OSError as error:
            raise AssetError("asset is unavailable") from error
        finally:
            owner.close()
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise AssetError("asset file is not plain")
            content = source.read(self.max_upload_bytes + 1)
        if len(content) != row["byte_size"] or len(content) > self.max_upload_bytes:
            raise AssetError("asset bytes do not match bounded metadata")
        return content
