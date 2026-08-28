"""Bounded, owner-isolated raster uploads addressed only by opaque handles."""

from __future__ import annotations

import binascii
import hashlib
import os
import re
import secrets
import stat
import struct
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
        self.data_root = Path(data_root)
        if not self.data_root.is_absolute():
            raise AssetError("asset data root must be absolute")
        if min(max_upload_bytes, max_pixels, max_decoded_bytes) <= 0:
            raise ValueError("asset limits must be positive")
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

    def _owner_directory(self, principal: SessionPrincipal) -> Path:
        directory = self.assets_root / self.owner_storage_id(principal)
        _make_plain_directory(directory)
        return directory

    def create_upload(
        self,
        principal: SessionPrincipal,
        stream: BinaryIO,
        media_type: str,
    ) -> AssetHandle:
        content = self._read_upload(stream)
        sniffed, width, height = self._validate_raster(content, media_type)
        owner_directory = self._owner_directory(principal)
        asset_id = secrets.token_urlsafe(24)
        filename = f"{asset_id}.png"
        destination = owner_directory / filename
        temporary = owner_directory / f".{asset_id}.{secrets.token_urlsafe(8)}.tmp"
        if destination.parent != owner_directory or destination.exists():
            raise AssetError("asset storage collision")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            _ensure_existing_components_are_plain(owner_directory)
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
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
                    "INSERT INTO assets (asset_id, owner_id, storage_name, media_type, byte_size, "
                    "width, height, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            destination.unlink(missing_ok=True)
            raise
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
        owner_directory = self._owner_directory(principal)
        path = owner_directory / f"{asset_id}.png"
        _ensure_existing_components_are_plain(path)
        if _is_link_or_reparse(path):
            raise AssetError("asset file is not plain")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise AssetError("asset is unavailable") from error
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            content = source.read(self.max_upload_bytes + 1)
        if len(content) != row["byte_size"] or len(content) > self.max_upload_bytes:
            raise AssetError("asset bytes do not match bounded metadata")
        return content
