from __future__ import annotations

from .types import ProviderModel

CATALOG: tuple[ProviderModel, ...] = (
    ProviderModel(
        provider="fake",
        model="fake-raster-v1",
        capabilities=frozenset({"async_jobs", "custom_dimensions", "text_to_image"}),
        enabled=True,
    ),
    ProviderModel(
        provider="openai",
        model="gpt-image-1",
        capabilities=frozenset(
            {
                "custom_dimensions",
                "image_to_image",
                "reference_images",
                "text_to_image",
            }
        ),
        enabled=True,
    ),
    ProviderModel(
        provider="google",
        model="gemini-2.5-flash-image",
        capabilities=frozenset(
            {
                "image_to_image",
                "reference_images",
                "text_to_image",
            }
        ),
        enabled=True,
    ),
    ProviderModel(
        provider="bfl",
        model="flux-1.1-pro",
        capabilities=frozenset(
            {
                "async_jobs",
                "custom_dimensions",
                "text_to_image",
            }
        ),
        enabled=True,
    ),
    ProviderModel(
        provider="xai",
        model="grok-imagine-image-2.0",
        capabilities=frozenset(
            {
                "image_to_image",
                "reference_images",
                "text_to_image",
            }
        ),
        enabled=True,
    ),
    ProviderModel(
        provider="stability",
        model="sd3.5-large",
        capabilities=frozenset({"text_to_image"}),
        enabled=True,
    ),
)
