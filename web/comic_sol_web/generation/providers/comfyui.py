"""Safe ComfyUI execution routes for the Web generation surface (WP12).

The provider implements two bounded routes behind one ``ProviderAdapter``:

- **Remote**: a deployment-configured, approve-listed ComfyUI server origin is
  reached only through the shared ``BoundedHTTPClient``. The approved origin
  must be public HTTPS (never loopback, private, link-local, multicast,
  unspecified, or cloud-metadata) and is never drawn from request/browser data.
- **Local**: the hosted Web server never opens a socket to a user's machine.
  Instead it produces portable, deterministic, non-secret workflow
  instructions through the same canonical-serialization style as the
  agent-native handoff, for the active agent host to execute against a local
  ComfyUI instance.

Only bounded approved workflow fixtures are accepted, and prompt/negative/
dimension/seed values are injected into explicitly named fixture slots after
fail-closed validation. Raw request/response payloads, endpoint URLs, tokens,
cookies, and machine paths are never persisted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from ..catalog import CATALOG
from ..types import ErrorCategory, GenerationRequest, GenerationResult, JobState, ProviderModel
from .base import ProviderError
from .http import BoundedHTTPClient, TransportPolicy

_MODEL = next(model for model in CATALOG if model.provider == "comfyui")
_DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "comfyui"
_DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_MAX_FIXTURE_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_COLLECTION_ITEMS = 4096
_MAX_STRING_LENGTH = 65536

_STABLE_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?:api[_-]?key|secret|password|token|credential)[=:]\s*\S", re.IGNORECASE),
)


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _validate_remote_origin(origin: str) -> str:
    """Fail closed unless ``origin`` is a certificate-bearing public HTTPS host."""
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        raise ValueError("ComfyUI remote origin is invalid") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.path not in {"", "/"})
        or (port is not None and port != 443)
    ):
        raise ValueError("ComfyUI remote origin is invalid")
    lowered = host.lower()
    try:
        address = ipaddress.ip_address(lowered)
        is_ip = True
    except ValueError:
        is_ip = False
        address = None
    if lowered == "localhost" or (is_ip and address is not None and address.is_loopback):
        raise ValueError("ComfyUI remote origin must not be loopback")
    if (
        is_ip
        and address is not None
        and (
            address.is_private
            or address.is_reserved
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or _is_metadata_address(address)
        )
    ):
        raise ValueError("ComfyUI remote origin must be a public HTTPS host")
    default_port = 443
    port_suffix = "" if port is None or port == default_port else f":{port}"
    rendered_host = f"[{lowered}]" if ":" in lowered else lowered
    return f"https://{rendered_host}{port_suffix}"


def _is_metadata_address(address: ipaddress._BaseAddress) -> bool:
    # Cloud metadata endpoints live on 169.254.169.254 (link-local, already
    # rejected); this covers other documented metadata ranges explicitly.
    if isinstance(address, ipaddress.IPv4Address):
        if address.is_link_local:
            return True
        return str(address) in {"169.254.169.254"}
    if isinstance(address, ipaddress.IPv6Address):
        return str(address).lower() in {"fe80::", "::1"} or address.is_link_local
    return False


def _validate_fixture_depth(value: object, depth: int = 1) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    elif isinstance(value, dict):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        for key, child in value.items():
            if len(key) > _MAX_STRING_LENGTH:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            _validate_fixture_depth(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        for child in value:
            _validate_fixture_depth(child, depth + 1)


def load_fixture(fixture_dir: Path, workflow_id: str) -> Mapping[str, object]:
    """Load and validate one bounded workflow fixture.

    ``fixture_dir`` is trusted (deployment or test-owned). The parsed fixture
    is still fail-closed validated and its canonical workflow digest is pinned.
    """
    filename = f"{workflow_id}.json"
    if not isinstance(workflow_id, str) or _STABLE_ID_RE.fullmatch(workflow_id) is None:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    candidate = Path(fixture_dir) / filename
    try:
        raw = candidate.resolve().read_bytes()
    except OSError:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    if len(raw) > _MAX_FIXTURE_BYTES:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return _validate_fixture_payload(raw)


def _validate_fixture_payload(raw: bytes) -> Mapping[str, object]:
    try:
        parsed: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT) from None
    _validate_fixture_depth(parsed)
    if not isinstance(parsed, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    _validate_fixture_schema(parsed)
    return parsed


def _validate_fixture_schema(data: Mapping[str, object]) -> None:
    for key in ("workflow_id", "output_node_id", "max_prompt_chars", "max_negative_prompt_chars"):
        if key not in data:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    allowed = data.get("allowed_node_classes")
    required = data.get("required_node_classes")
    slot_paths = data.get("slot_paths")
    optional = data.get("optional_slots")
    workflow = data.get("workflow")
    if not isinstance(allowed, list) or not isinstance(required, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if not isinstance(slot_paths, Mapping) or not isinstance(workflow, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if not set(required) <= set(allowed):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    for paths in slot_paths.values():
        if not isinstance(paths, list):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        for path in paths:
            if not isinstance(path, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            node_id = path.get("node_id")
            input_name = path.get("input_name")
            if (
                not isinstance(node_id, str)
                or _NODE_ID_RE.fullmatch(node_id) is None
                or not isinstance(input_name, str)
                or _NODE_ID_RE.fullmatch(input_name) is None
            ):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or _NODE_ID_RE.fullmatch(node_id) is None:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or class_type not in allowed:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        for value in node["inputs"].values():
            if isinstance(value, str):
                _validate_safe_string(value)
    present = {node.get("class_type") for node in workflow.values()}
    if not set(required) <= present:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    output_node_id = data.get("output_node_id")
    if not isinstance(output_node_id, str) or output_node_id not in workflow:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if optional is not None and not isinstance(optional, list):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _validate_safe_string(value: str) -> None:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or _DRIVE_RE.match(value)
        or "://" in value
        or "cookie" in value.lower()
        or "authorization" in value.lower()
        or (";" in value and "=" in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or _looks_like_secret(value)
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _validate_request(request: GenerationRequest, model: str) -> None:
    if model != _MODEL.model or not request.required_capabilities <= _MODEL.capabilities:
        raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
    if request.references:
        raise ProviderError(ErrorCategory.CAPABILITY_MISSING)
    if request.width < 1 or request.height < 1:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _inject(
    request: GenerationRequest,
    data: Mapping[str, object],
) -> dict[str, object]:
    """Inject only approved values into explicitly named fixture slots."""
    workflow = json.loads(json.dumps(data["workflow"], sort_keys=True))
    _validate_workflow_structure(workflow)
    model_node_id = data.get("model_node_id")
    model_input_name = data.get("model_input_name")
    model_value = data.get("model_value")
    if not (
        isinstance(model_node_id, str)
        and _NODE_ID_RE.fullmatch(model_node_id) is not None
        and isinstance(model_input_name, str)
        and _NODE_ID_RE.fullmatch(model_input_name) is not None
        and isinstance(model_value, str)
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    target = workflow.get(model_node_id)
    if not isinstance(target, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    inputs = target.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    # The model/checkpoint is pinned by the fixture and never request-supplied.
    if inputs.get(model_input_name) != model_value:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)

    slot_paths = data["slot_paths"]
    optional_raw = data.get("optional_slots")
    if not isinstance(slot_paths, Mapping):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    optional = set(optional_raw) if isinstance(optional_raw, list) else set()

    def slot_value(slot: str) -> object:
        if slot == "positive_prompt":
            return request.prompt
        if slot == "negative_prompt":
            return request.negative_prompt
        if slot == "width":
            return request.width
        if slot == "height":
            return request.height
        if slot == "seed":
            return request.provider_options.get("seed")
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)

    for slot, paths in slot_paths.items():
        value = slot_value(slot)
        if value is None:
            if slot not in optional:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            continue
        if slot == "negative_prompt":
            if not isinstance(value, str):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            limit = data.get("max_negative_prompt_chars")
            if not isinstance(limit, int):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            if len(value) > limit:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        if slot == "positive_prompt":
            if not isinstance(value, str):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            limit = data.get("max_prompt_chars")
            if not isinstance(limit, int):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            if len(value) > limit:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        if slot in {"width", "height"}:
            value = _validated_dimension(value, slot, data)
        if slot == "seed":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            if value < 0 or value > 2**64 - 1:
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        for path in paths:
            if not isinstance(path, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            node_id = path.get("node_id")
            input_name = path.get("input_name")
            if (
                not isinstance(node_id, str)
                or not isinstance(input_name, str)
                or not isinstance(workflow.get(node_id), Mapping)
            ):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            workflow[node_id]["inputs"][input_name] = value
    return workflow


def _validate_workflow_structure(workflow: Mapping[str, object]) -> None:
    for node in workflow.values():
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)


def _validated_dimension(value: object, slot: str, data: Mapping[str, object]) -> int:
    del slot
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    low = data.get("dimension_min")
    high = data.get("dimension_max")
    step = data.get("dimension_step")
    if not isinstance(low, int):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if not isinstance(high, int):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if not isinstance(step, int) or step <= 0:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    if value < low or value > high or value % step:
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return value


class ComfyUIProvider:
    """Bounded remote or local ComfyUI executor behind the ProviderAdapter."""

    provider_id = "comfyui"

    def __init__(
        self,
        *,
        approved_origins: frozenset[str] = frozenset(),
        fixture_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(approved_origins, frozenset):
            raise ValueError("ComfyUI approved origins must be a frozenset")
        remote_origins = frozenset(_validate_remote_origin(origin) for origin in approved_origins)
        self.approved_origins = remote_origins
        self._transport = transport
        self._fixture_dir = (
            Path(fixture_dir) if fixture_dir is not None else Path(_DEFAULT_FIXTURE_DIR)
        )
        self._policy = TransportPolicy(
            approved_origins=remote_origins,
            connect_timeout=5.0,
            read_timeout=30.0,
            total_timeout=60.0,
            max_response_bytes=max_response_bytes,
        )
        self._packages: dict[str, bytes] = {}
        self._cancelled: set[str] = set()

    async def list_models(self) -> Sequence[ProviderModel]:
        return (_MODEL,)

    async def estimate(self, request: GenerationRequest, model: str) -> Mapping[str, object]:
        _validate_request(request, model)
        return {"currency": "USD", "model": model, "unit": "image"}

    async def generate(
        self,
        request: GenerationRequest,
        model: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        _validate_request(request, model)
        data = load_fixture(self._fixture_dir, model)
        if not self.approved_origins:
            external_job_id, payload = self._store_local(request, data)
            return self._waiting_result(external_job_id, {"route": "local"})
        workflow = _inject(request, data)
        return await self._remote_generate(request, model, workflow)

    async def _remote_generate(
        self,
        request: GenerationRequest,
        model: str,
        workflow: Mapping[str, object],
    ) -> GenerationResult:
        base = next(iter(self.approved_origins))
        payload = {"prompt": workflow}
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            response = await client.post_json(
                f"{base}/api/prompt",
                payload=payload,
                error_classifier=_classify_error,
            )
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or _STABLE_ID_RE.fullmatch(prompt_id) is None:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT)
        return GenerationResult(
            external_job_id=prompt_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters={
                "height": request.height,
                "model": model,
                "route": "remote",
                "width": request.width,
            },
            usage={},
        )

    async def poll(
        self,
        external_job_id: str,
        credential: str | None,
    ) -> GenerationResult:
        del credential
        if not self.approved_origins:
            job_id = _validate_job_id(external_job_id)
            if job_id not in self._packages:
                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
            if job_id in self._cancelled:
                return self._cancelled_result(job_id)
            return self._waiting_result(job_id, {"route": "local"})
        return await self._remote_poll(external_job_id)

    async def _remote_poll(self, external_job_id: str) -> GenerationResult:
        job_id = _validate_job_id(external_job_id)
        base = next(iter(self.approved_origins))
        history_url = f"{base}/api/history/{job_id}"
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            history = await client.get_json(
                history_url,
                error_classifier=_classify_error,
            )
            entry = history.get(job_id)
            if entry is None:
                # Empty history object means the work is still queued/pending.
                if not isinstance(history, Mapping) or history:
                    raise ProviderError(ErrorCategory.INVALID_OUTPUT)
                return self._waiting_result(job_id, {"route": "remote"})
            if not isinstance(entry, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            status = entry.get("status")
            if not isinstance(status, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            status_str = status.get("status_str")
            completed = status.get("completed")
            if status_str == "cancelled":
                return self._cancelled_result(job_id, "remote")
            if status_str == "error":
                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
            if status_str != "success" or completed is not True:
                return self._waiting_result(job_id, {"route": "remote"})
            outputs = entry.get("outputs")
            if not isinstance(outputs, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            data = load_fixture(self._fixture_dir, _MODEL.model)
            output_node_id = data["output_node_id"]
            images = outputs.get(output_node_id)
            if (
                not isinstance(images, Mapping)
                or not isinstance(images.get("images"), list)
                or not images["images"]
            ):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            first = images["images"][0]
            if not isinstance(first, Mapping):
                raise ProviderError(ErrorCategory.INVALID_OUTPUT)
            view = _view_query(first)
            try:
                raster, media_type = await client.get_raster(f"{base}/api/view?{view}")
            except ProviderError:
                raise
            return GenerationResult(
                external_job_id=job_id,
                state=JobState.ACCEPTED,
                raster_bytes=raster,
                media_type=media_type,
                effective_parameters={"model": _MODEL.model, "route": "remote"},
                usage={"images": 1},
            )

    async def cancel(self, external_job_id: str, credential: str | None) -> None:
        del credential
        if not self.approved_origins:
            job_id = _validate_job_id(external_job_id)
            if job_id not in self._packages:
                raise ProviderError(ErrorCategory.PROVIDER_ERROR)
            self._cancelled.add(job_id)
            return
        job_id = _validate_job_id(external_job_id)
        base = next(iter(self.approved_origins))
        async with BoundedHTTPClient(self._policy, transport=self._transport) as client:
            await client.post_json(
                f"{base}/api/interrupt",
                payload={},
                error_classifier=_classify_error,
            )

    def _store_local(
        self,
        request: GenerationRequest,
        data: Mapping[str, object],
    ) -> tuple[str, bytes]:
        workflow = _inject(request, data)
        package = {
            "contract_version": "1.0",
            "provider_id": "comfyui",
            "workflow_id": data["workflow_id"],
            "job_id": request.job_id,
            "workflow": workflow,
        }
        try:
            payload = _canonical_bytes(package)
        except (TypeError, ValueError, UnicodeError) as error:
            raise ProviderError(ErrorCategory.INVALID_OUTPUT) from error
        external_job_id = f"comfyui:{_sha256(payload)}"
        self._packages[external_job_id] = payload
        return external_job_id, payload

    def local_package(self, external_job_id: str | None) -> dict[str, object]:
        if not isinstance(external_job_id, str) or external_job_id not in self._packages:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        return json.loads(self._packages[external_job_id])

    @staticmethod
    def _waiting_result(external_job_id: str, effective: Mapping[str, object]) -> GenerationResult:
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.POLLING,
            raster_bytes=None,
            media_type=None,
            effective_parameters=effective,
            usage={},
        )

    @staticmethod
    def _cancelled_result(external_job_id: str, route: str = "local") -> GenerationResult:
        return GenerationResult(
            external_job_id=external_job_id,
            state=JobState.CANCELLED,
            raster_bytes=None,
            media_type=None,
            effective_parameters={"route": route},
            usage={},
        )


def _view_query(image: Mapping[str, object]) -> str:
    filename = image.get("filename")
    subfolder = image.get("subfolder")
    image_type = image.get("type")
    if (
        not isinstance(filename, str)
        or not filename
        or "\x00" in filename
        or "\\" in filename
        or not isinstance(subfolder, str)
        or image_type != "output"
        or "/" in filename
    ):
        raise ProviderError(ErrorCategory.INVALID_OUTPUT)
    return urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})


def _validate_job_id(external_job_id: str) -> str:
    if not isinstance(external_job_id, str):
        raise ProviderError(ErrorCategory.PROVIDER_ERROR)
    if external_job_id.startswith("comfyui:"):
        checksum = external_job_id[len("comfyui:") :]
        if _SHA256_RE.fullmatch(checksum) is None:
            raise ProviderError(ErrorCategory.PROVIDER_ERROR)
        return external_job_id
    if _STABLE_ID_RE.fullmatch(external_job_id) is None:
        raise ProviderError(ErrorCategory.PROVIDER_ERROR)
    return external_job_id


def _classify_error(status_code: int, payload: Mapping[str, object]) -> ErrorCategory | None:
    markers = " ".join(
        value.lower()
        for key in ("error", "detail", "message")
        if isinstance((value := payload.get(key)), str)
    )
    if any(marker in markers for marker in ("moderation", "safety", "content_policy")):
        return ErrorCategory.MODERATED
    if status_code == 429 and "quota" in markers:
        return ErrorCategory.QUOTA_EXHAUSTED
    return None
