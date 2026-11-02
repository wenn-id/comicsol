#!/usr/bin/env python3
"""Shared encoded and decoded raster safety limits for Comic Sol image readers."""

MIN_RASTER_DIMENSION = 512
MAX_DECODED_PIXELS = 1600 * 2400 * 16
MAX_ENCODED_RASTER_BYTES = 128 * 1024 * 1024


def exceeds_decoded_pixel_limit(width: int, height: int, pillow_limit: int | None) -> bool:
    pixel_count = width * height
    return pixel_count > MAX_DECODED_PIXELS or (
        pillow_limit is not None and pixel_count > pillow_limit
    )
