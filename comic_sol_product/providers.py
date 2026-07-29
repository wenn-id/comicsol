"""Provider-neutral image generation contracts.

This module deliberately contains no HTTP client or provider SDK. Providers adapt to
these immutable records while the deterministic engine retains files and owns retry
accounting.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, runtime_checkable

from .cli import _load_engine

AttemptKind = Literal["initial", "visual_retry", "transient_repeat"]
_ALLOWED_FAILURES = {"authentication", "invalid-request", "safety", "transient", "unavailable"}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


def _relative_project_path(value: str) -> str:
    if not value or Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError("reference must be a relative project path")
    normalized = PurePosixPath(value.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or "." in normalized.parts:
        raise ValueError("reference must be a relative project path")
    return normalized.as_posix()


def _optional_text(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a non-empty string when provided")


def _safe_message(message: str) -> str:
    sanitized = str(message).strip() or "provider operation failed"
    # Provider messages may contain local paths. Preserve useful prose but never the
    # private path token itself.
    tokens = sanitized.split()
    for token in tokens:
        candidate = token.strip("'\"(),:;")
        if Path(candidate).is_absolute() or PureWindowsPath(candidate).is_absolute():
            sanitized = sanitized.replace(candidate, "<path>")
    return sanitized


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    panel_id: str
    prompt: str
    width: int
    height: int
    reference_paths: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.panel_id):
            raise ValueError("invalid panel ID")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        for name, value in (("width", self.width), ("height", self.height)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 512:
                raise ValueError(f"{name} must be an integer at least 512")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise ValueError("seed must be an integer when provided")
        _optional_text("provider", self.provider)
        _optional_text("model", self.model)
        object.__setattr__(
            self,
            "reference_paths",
            tuple(_relative_project_path(path) for path in self.reference_paths),
        )

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "height": self.height,
            "panel_id": self.panel_id,
            "prompt_sha256": hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
            "reference_paths": list(self.reference_paths),
            "width": self.width,
        }
        for name in ("provider", "model", "seed"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        return record

    def canonical_json(self) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class GenerationResult:
    image_bytes: bytes
    media_type: str
    width: int
    height: int
    sha256: str
    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    seed: int | None = None
    references_used: tuple[str, ...] = ()

    @classmethod
    def from_bytes(
        cls,
        image_bytes: bytes,
        *,
        media_type: str,
        width: int,
        height: int,
        provider: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
        seed: int | None = None,
        references_used: tuple[str, ...] = (),
    ) -> "GenerationResult":
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise ValueError("image_bytes must be non-empty bytes")
        if media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("unsupported raster media type")
        for name, value in (("width", width), ("height", height)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 512:
                raise ValueError(f"{name} must be an integer at least 512")
        for name, value in (("provider", provider), ("model", model), ("request_id", request_id)):
            _optional_text(name, value)
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed must be an integer when provided")
        references = tuple(_relative_project_path(path) for path in references_used)
        return cls(
            image_bytes=image_bytes,
            media_type=media_type,
            width=width,
            height=height,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            provider=provider,
            model=model,
            request_id=request_id,
            seed=seed,
            references_used=references,
        )

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "height": self.height,
            "media_type": self.media_type,
            "reference_paths": list(self.references_used),
            "sha256": self.sha256,
            "width": self.width,
        }
        for name in ("provider", "model", "request_id", "seed"):
            value = getattr(self, name)
            if value is not None:
                record[name] = value
        return record


@dataclass(frozen=True, slots=True)
class GenerationFailure:
    category: str
    message: str

    def __post_init__(self) -> None:
        if self.category not in _ALLOWED_FAILURES:
            raise ValueError("unknown generation failure category")
        object.__setattr__(self, "message", _safe_message(self.message))

    def to_record(self) -> dict[str, str]:
        return {"category": self.category, "message": self.message}


@runtime_checkable
class GenerationProvider(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate one raster result without mutating project state."""
        ...


def retain_generation_result(
    project_dir: Path,
    panel_id: str,
    kind: AttemptKind,
    result: GenerationResult,
) -> dict[str, int]:
    """Atomically retain a provider result and delegate budget accounting to engine."""
    if not _IDENTIFIER.fullmatch(panel_id):
        raise ValueError("invalid panel ID")
    if kind not in {"initial", "visual_retry", "transient_repeat"}:
        raise ValueError("unknown generation attempt kind")
    engine = _load_engine()
    project = Path(project_dir).resolve(strict=True)
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[result.media_type]
    relative = Path("panels") / "attempts" / panel_id / f"{kind}.{extension}"
    destination = engine._contained_project_path(project, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    engine.atomic_write_bytes(destination, result.image_bytes)
    return engine.record_generation_attempt(project, panel_id, kind, relative)
