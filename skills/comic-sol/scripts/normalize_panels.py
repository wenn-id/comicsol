#!/usr/bin/env python3
"""Deterministic panel normalization and provenance publication."""

from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

from .project_io import ProjectTransaction, contained_project_path
from .raster_limits import MAX_DECODED_PIXELS


IMPLEMENTATION_VERSION = "1"
PANEL_ID = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
MODES = frozenset({"crop", "fit", "exact"})


@dataclass(frozen=True)
class NormalizationSpec:
    panel_id: str
    source_relative: str
    target_size: tuple[int, int]
    mode: str


@dataclass(frozen=True)
class NormalizationGeometry:
    source_size: tuple[int, int]
    target_size: tuple[int, int]
    mode: str
    crop_box: tuple[int, int, int, int] | None
    resized_size: tuple[int, int]
    paste_origin: tuple[int, int]


@dataclass(frozen=True)
class _PreparedNormalization:
    spec: NormalizationSpec
    clean_bytes: bytes
    record_bytes: bytes


def _positive_size(value: object, name: str) -> tuple[int, int]:
    """Return a validated positive image dimension."""
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value)
    ):
        raise ValueError(f"{name} dimensions must be positive integers")
    width, height = value
    if width * height > MAX_DECODED_PIXELS:
        raise ValueError(f"{name} exceeds the decoded pixel limit")
    return width, height


def normalization_geometry(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    mode: str,
) -> NormalizationGeometry:
    """Compute deterministic geometry in oriented source pixel coordinates."""
    source_width, source_height = _positive_size(source_size, "source size")
    target_width, target_height = _positive_size(target_size, "target size")
    if mode not in MODES:
        raise ValueError(f"normalization mode must be one of {sorted(MODES)}")

    source_ratio = source_width / source_height
    target_ratio = target_width / target_height
    if mode == "exact":
        if source_width * target_height != source_height * target_width:
            raise ValueError("exact normalization requires matching aspect ratios")
        return NormalizationGeometry(
            source_size, target_size, mode, None, target_size, (0, 0)
        )

    if mode == "fit":
        if source_width * target_height >= source_height * target_width:
            resized_width = target_width
            resized_height = max(1, (source_height * target_width) // source_width)
        else:
            resized_height = target_height
            resized_width = max(1, (source_width * target_height) // source_height)
        origin = (
            (target_width - resized_width) // 2,
            (target_height - resized_height) // 2,
        )
        return NormalizationGeometry(
            source_size, target_size, mode, None,
            (resized_width, resized_height), origin,
        )

    if source_ratio > target_ratio:
        crop_width = (source_height * target_width) // target_height
        left = (source_width - crop_width) // 2
        crop_box = (left, 0, left + crop_width, source_height)
    else:
        crop_height = (source_width * target_height) // target_width
        top = (source_height - crop_height) // 2
        crop_box = (0, top, source_width, top + crop_height)
    return NormalizationGeometry(
        source_size, target_size, mode, crop_box, target_size, (0, 0)
    )


def _canonical_json(value: object) -> bytes:
    """Serialize a value as canonical JSON bytes."""
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    """Return the SHA-256 digest of bytes."""
    return hashlib.sha256(payload).hexdigest()


def _png_bytes(image: Image.Image) -> bytes:
    """Encode an image as deterministic PNG bytes."""
    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    return output.getvalue()


def _prepare(project_dir: Path, spec: NormalizationSpec) -> _PreparedNormalization:
    """Prepare an image for canonical panel normalization."""
    if not isinstance(spec, NormalizationSpec):
        raise TypeError("normalization specs must be NormalizationSpec values")
    if PANEL_ID.fullmatch(spec.panel_id) is None:
        raise ValueError("panel_id must match pNN-NN")
    target_size = _positive_size(spec.target_size, "target size")
    if spec.mode not in MODES:
        raise ValueError(f"normalization mode must be one of {sorted(MODES)}")

    source_path = contained_project_path(
        project_dir, spec.source_relative, must_exist=True
    )
    source_bytes = source_path.read_bytes()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source_bytes)) as source:
                source_format = source.format
                encoded_size = source.size
                orientation = source.getexif().get(274, 1)
                if encoded_size[0] * encoded_size[1] > MAX_DECODED_PIXELS:
                    raise ValueError("source image exceeds the decoded pixel limit")
                source.load()
                oriented = ImageOps.exif_transpose(source).convert("RGB")
    except ValueError:
        raise
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError(f"source is not a readable image: {spec.source_relative}") from error

    if source_format not in {"PNG", "JPEG", "WEBP"}:
        raise ValueError("source image format must be PNG, JPEG, or WEBP")
    geometry = normalization_geometry(oriented.size, target_size, spec.mode)
    if geometry.crop_box is not None:
        clean = oriented.crop(geometry.crop_box).resize(
            target_size, Image.Resampling.LANCZOS
        )
    elif spec.mode == "fit":
        resized = oriented.resize(geometry.resized_size, Image.Resampling.LANCZOS)
        clean = Image.new("RGB", target_size, "white")
        clean.paste(resized, geometry.paste_origin)
    else:
        clean = oriented.resize(target_size, Image.Resampling.LANCZOS)

    clean_bytes = _png_bytes(clean)
    clean_relative = f"panels/{spec.panel_id}/clean.png"
    record = {
        "clean": {
            "mode": "RGB",
            "path": clean_relative,
            "sha256": _sha256(clean_bytes),
            "size": list(clean.size),
        },
        "implementation_version": IMPLEMENTATION_VERSION,
        "operation": {
            "crop_box": (
                list(geometry.crop_box) if geometry.crop_box is not None else None
            ),
            "mode": spec.mode,
            "paste_origin": list(geometry.paste_origin),
            "resized_size": list(geometry.resized_size),
        },
        "panel_id": spec.panel_id,
        "schema_version": "1.0",
        "source": {
            "encoded_size": list(encoded_size),
            "exif_orientation": orientation,
            "format": source_format,
            "path": spec.source_relative.replace("\\", "/"),
            "sha256": _sha256(source_bytes),
            "size": list(oriented.size),
        },
        "target_size": list(target_size),
    }
    return _PreparedNormalization(spec, clean_bytes, _canonical_json(record))


def normalize_panels(
    project_dir: Path,
    specs: Iterable[NormalizationSpec],
) -> tuple[Path, ...]:
    """Preflight every panel, then publish the whole batch atomically."""
    project_dir = Path(project_dir)
    prepared = tuple(_prepare(project_dir, spec) for spec in specs)
    if not prepared:
        return ()
    panel_ids = [item.spec.panel_id for item in prepared]
    if len(set(panel_ids)) != len(panel_ids):
        raise ValueError("normalization batch contains duplicate panel IDs")

    with ProjectTransaction(project_dir, "panel-normalization") as transaction:
        for item in prepared:
            panel_id = item.spec.panel_id
            transaction.stage_bytes(
                f"panels/{panel_id}/clean.png", item.clean_bytes
            )
            transaction.stage_bytes(
                f"panels/{panel_id}/normalization.json", item.record_bytes
            )
    return tuple(
        project_dir / f"panels/{item.spec.panel_id}/clean.png" for item in prepared
    )


def normalize_panel(
    project_dir: Path,
    panel_id: str,
    source_relative: str,
    target_size: tuple[int, int],
    mode: str,
) -> Path:
    """Normalize one panel and return its canonical clean image path."""
    return normalize_panels(
        project_dir,
        (NormalizationSpec(panel_id, source_relative, target_size, mode),),
    )[0]
