#!/usr/bin/env python3
"""Full-content raster verification for Comic Sol PDF exports."""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from typing import Sequence

from PIL import Image, ImageChops

PDF_TOLERANCE_VERSION = "1"
PDF_EXPORTER_VERSION = "comic-sol-pillow-raster-v1"

# Calibrated against Pillow 12.2 JPEG-in-PDF round trips containing flat color,
# sharp borders, one-pixel lines, and small lettering. The accepted ceilings add
# margin above the pinned fixture while remaining below every corruption fixture.
MAX_MEAN_ABSOLUTE_CHANNEL_ERROR = 3.1
HIGH_ERROR_CHANNEL_THRESHOLD = 24
MAX_HIGH_ERROR_PIXEL_RATIO = 0.021
GRID_COLUMNS = 8
GRID_ROWS = 8
MAX_GRID_REGION_ERROR = 16.0

_PDF_STREAM_PATTERN = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


class PdfQualityError(ValueError):
    """Raised when a PDF cannot prove full-page raster fidelity."""


@dataclass(frozen=True)
class PdfPageMetrics:
    page_number: int
    dimensions: tuple[int, int]
    mode: str
    compared_pixels: int
    mean_absolute_channel_error: float
    high_error_pixel_ratio: float
    maximum_grid_region_error: float


def _rounded(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def compare_full_page(
    source: Image.Image,
    decoded: Image.Image,
    *,
    page_number: int = 1,
) -> PdfPageMetrics:
    """Compare every source/decoded pixel using deterministic integer sums."""
    if source.size != decoded.size:
        raise PdfQualityError(
            f"decoded PDF page {page_number} dimensions do not match source"
        )
    source_rgb = source.convert("RGB")
    decoded_rgb = decoded.convert("RGB")
    try:
        width, height = source_rgb.size
        difference = ImageChops.difference(source_rgb, decoded_rgb)
        try:
            histogram = difference.histogram()
            total_error = sum(
                value * count
                for channel in range(3)
                for value, count in enumerate(
                    histogram[channel * 256:(channel + 1) * 256]
                )
            )
            high_error_lut = [
                255 if value > HIGH_ERROR_CHANNEL_THRESHOLD else 0
                for value in range(256)
            ]
            masks = [
                channel.point(high_error_lut)
                for channel in difference.split()
            ]
            try:
                high_mask = ImageChops.lighter(
                    ImageChops.lighter(masks[0], masks[1]), masks[2]
                )
                high_error_pixels = width * height - high_mask.histogram()[0]
                high_mask.close()
            finally:
                for mask in masks:
                    mask.close()

            region_means: list[float] = []
            for row in range(GRID_ROWS):
                top = row * height // GRID_ROWS
                bottom = (row + 1) * height // GRID_ROWS
                for column in range(GRID_COLUMNS):
                    left = column * width // GRID_COLUMNS
                    right = (column + 1) * width // GRID_COLUMNS
                    region = difference.crop((left, top, right, bottom))
                    try:
                        region_histogram = region.histogram()
                        region_error = sum(
                            value * count
                            for channel in range(3)
                            for value, count in enumerate(
                                region_histogram[channel * 256:(channel + 1) * 256]
                            )
                        )
                        region_means.append(
                            _rounded(region_error, (right - left) * (bottom - top) * 3)
                        )
                    finally:
                        region.close()
        finally:
            difference.close()
        return PdfPageMetrics(
            page_number=page_number,
            dimensions=(width, height),
            mode="RGB",
            compared_pixels=width * height,
            mean_absolute_channel_error=_rounded(total_error, width * height * 3),
            high_error_pixel_ratio=_rounded(high_error_pixels, width * height),
            maximum_grid_region_error=max(region_means, default=0.0),
        )
    finally:
        source_rgb.close()
        decoded_rgb.close()


def _decode_pdf_frames(payload: bytes) -> list[Image.Image]:
    if not payload.startswith(b"%PDF-") or not payload.rstrip().endswith(b"%%EOF"):
        raise PdfQualityError("PDF payload is corrupt or truncated")
    frames: list[Image.Image] = []
    for match in _PDF_STREAM_PATTERN.finditer(payload):
        stream = match.group(1)
        if not stream.startswith(b"\xff\xd8"):
            continue
        try:
            with Image.open(io.BytesIO(stream)) as image:
                image.load()
                frame = image.convert("RGB")
                frame.load()
                frames.append(frame)
        except (OSError, SyntaxError) as error:
            for frame in frames:
                frame.close()
            raise PdfQualityError("PDF raster frame could not be decoded") from error
    if not frames:
        raise PdfQualityError("PDF payload contains no decodable raster pages")
    return frames


def _metrics_pass(metrics: PdfPageMetrics) -> bool:
    return (
        metrics.mean_absolute_channel_error <= MAX_MEAN_ABSOLUTE_CHANNEL_ERROR
        and metrics.high_error_pixel_ratio <= MAX_HIGH_ERROR_PIXEL_RATIO
        and metrics.maximum_grid_region_error <= MAX_GRID_REGION_ERROR
    )


def verify_pdf_payload(
    payload: bytes,
    source_pages: Sequence[Image.Image],
) -> dict[str, object]:
    """Decode and compare every PDF page against ordered source rasters."""
    frames = _decode_pdf_frames(payload)
    try:
        if len(frames) != len(source_pages):
            raise PdfQualityError("written PDF page count does not match source pages")
        metrics: list[PdfPageMetrics] = []
        for page_number, (source, decoded) in enumerate(
            zip(source_pages, frames), 1
        ):
            page_metrics = compare_full_page(
                source, decoded, page_number=page_number
            )
            if not _metrics_pass(page_metrics):
                raise PdfQualityError(
                    f"written PDF content mismatch at page {page_number}"
                )
            metrics.append(page_metrics)
        return {
            "exporter_version": PDF_EXPORTER_VERSION,
            "page_count": len(metrics),
            "pages": [
                {
                    **asdict(page),
                    "dimensions": list(page.dimensions),
                }
                for page in metrics
            ],
            "tolerance": {
                "grid": [GRID_COLUMNS, GRID_ROWS],
                "high_error_channel_threshold": HIGH_ERROR_CHANNEL_THRESHOLD,
                "max_grid_region_error": MAX_GRID_REGION_ERROR,
                "max_high_error_pixel_ratio": MAX_HIGH_ERROR_PIXEL_RATIO,
                "max_mean_absolute_channel_error": MAX_MEAN_ABSOLUTE_CHANNEL_ERROR,
                "version": PDF_TOLERANCE_VERSION,
            },
        }
    finally:
        for frame in frames:
            frame.close()
