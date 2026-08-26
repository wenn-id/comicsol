#!/usr/bin/env python3
"""Bounded, agent-managed ComfyUI adapter for Comic Sol generation jobs.

This reference integration intentionally has no imports from ``scripts`` or
``comic_sol_product``. The deterministic engine remains responsible for result
intake, raster validation, retry accounting, receipts, review, and promotion.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, TextIO


DEFAULT_ENDPOINT = "http://127.0.0.1:8188"
MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_PROFILE_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_JSON_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
MAX_RASTER_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_COLLECTION_ITEMS = 4096
MAX_STRING_LENGTH = 65536
CONNECT_TIMEOUT_SECONDS = 10.0
QUEUE_TIMEOUT_SECONDS = 30.0
EXECUTION_TIMEOUT_SECONDS = 300.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.25

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_HANDOFF_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PANEL_ID_RE = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
_ASPECT_RE = re.compile(r"^([1-9][0-9]*):([1-9][0-9]*)$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SAFE_FILE_RE = re.compile(r"^[^/\\\x00]{1,255}$")
_SAFE_SUBFOLDER_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,99}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?:api[_-]?key|secret|password|token|credential)[=:]\s*\S", re.IGNORECASE),
)

_JOB_KEYS = {
    "schema_version",
    "job_id",
    "subject_kind",
    "subject_id",
    "prompt_path",
    "prompt_sha256",
    "references",
    "requested_dimensions",
    "requested_aspect_ratio",
    "attempt_kind",
    "retry_limit",
    "batch_id",
    "target_path",
}
_PROFILE_KEYS = {
    "schema_version",
    "profile_id",
    "profile_sha256",
    "workflow_sha256",
    "model",
    "output_node_id",
    "inputs",
}
_INPUT_KEYS = {
    "positive_prompt",
    "negative_prompt",
    "seed",
    "width",
    "height",
    "references",
}


class ExecutorError(Exception):
    """A sanitized adapter failure safe to expose to an invoking agent."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _call_with_timeout(callback: object, timeout: float, category: str) -> object:
    if timeout <= 0:
        raise ExecutorError(category)
    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    cancelled = threading.Event()

    def invoke() -> None:
        try:
            if not callable(callback):
                raise TypeError("callback is not callable")
            value = callback()
            if cancelled.is_set():
                close = getattr(value, "close", None)
                if callable(close):
                    close()
                return
            results.put((True, value))
        except BaseException as exc:
            if not cancelled.is_set():
                results.put((False, exc))

    threading.Thread(target=invoke, daemon=True).start()
    try:
        succeeded, value = results.get(timeout=timeout)
    except queue.Empty as exc:
        cancelled.set()
        raise ExecutorError(category) from exc
    if succeeded:
        return value
    if isinstance(value, ExecutorError):
        raise value
    if isinstance(value, BaseException):
        raise value
    raise ExecutorError(category)


class Endpoint:
    """Validated origin whose connections are pinned to resolved numeric addresses."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        allow_non_loopback: bool,
    ) -> None:
        self.host = host
        self.port = port
        self.allow_non_loopback = allow_non_loopback
        self._connection_host: str | None = None

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        allow_non_loopback: bool,
        warning_stream: TextIO,
    ) -> "Endpoint":
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ExecutorError("endpoint-rejected") from exc
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or parsed.path not in {"", "/"}
        ):
            raise ExecutorError("endpoint-rejected")
        if not 1 <= port <= 65535:
            raise ExecutorError("endpoint-rejected")
        endpoint = cls(
            host=parsed.hostname,
            port=port,
            allow_non_loopback=allow_non_loopback,
        )
        if allow_non_loopback:
            warning_stream.write(
                "WARNING: --allow-non-loopback permits a remote ComfyUI origin; "
                "local ComfyUI has no Comic Sol authentication boundary.\n"
            )
        return endpoint

    def resolve_for_connection(self, timeout: float = CONNECT_TIMEOUT_SECONDS) -> str:
        try:
            answers = _call_with_timeout(
                lambda: socket.getaddrinfo(
                    self.host,
                    self.port,
                    type=socket.SOCK_STREAM,
                ),
                timeout,
                "connection-timeout",
            )
        except ExecutorError:
            raise
        except socket.timeout as exc:
            raise ExecutorError("connection-timeout") from exc
        except OSError as exc:
            raise ExecutorError("connection-failed") from exc
        if not isinstance(answers, list):
            raise ExecutorError("endpoint-rejected")
        addresses: list[str] = []
        for answer in answers:
            sockaddr = answer[4]
            if not isinstance(sockaddr, tuple) or not sockaddr:
                raise ExecutorError("endpoint-rejected")
            address = str(sockaddr[0])
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ExecutorError("endpoint-rejected") from exc
            if not self.allow_non_loopback and not parsed.is_loopback:
                raise ExecutorError("endpoint-rejected")
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ExecutorError("endpoint-rejected")
        self._connection_host = addresses[0]
        return self._connection_host

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def connection_url(self, route: str) -> str:
        if not route.startswith("/") or route.startswith("//"):
            raise ExecutorError("endpoint-rejected")
        address = self._connection_host or self.resolve_for_connection()
        host = f"[{address}]" if ":" in address else address
        return f"http://{host}:{self.port}{route}"


class ComfyUIClient:
    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _RejectRedirects(),
        )

    def _request(
        self,
        route: str,
        *,
        method: str,
        body: bytes | None,
        content_type: str | None,
        timeout: float,
        timeout_category: str,
        maximum: int,
    ) -> bytes:
        # Resolve before every connection. The URL uses only the validated numeric
        # address, while Host preserves the approved origin. Each phase timeout is
        # an end-to-end deadline, not a fresh timeout for every streamed read.
        deadline = time.monotonic() + timeout
        self.endpoint.resolve_for_connection(min(CONNECT_TIMEOUT_SECONDS, timeout))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutorError(timeout_category)
        request = urllib.request.Request(
            self.endpoint.connection_url(route),
            data=body,
            method=method,
            headers={"Host": self.endpoint.host_header, "Accept": "application/json"},
        )
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        try:
            response_object = _call_with_timeout(
                lambda: self.opener.open(request, timeout=remaining),
                remaining,
                timeout_category,
            )
            if not hasattr(response_object, "__enter__"):
                raise ExecutorError("unexpected-response")
            with response_object as response:
                status_code = getattr(response, "status", 0)
                if status_code != 200:
                    raise ExecutorError("unexpected-response")
                return _read_stream_bounded(
                    response,
                    maximum,
                    deadline=deadline,
                    timeout_category=timeout_category,
                )
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ExecutorError("redirect-rejected") from exc
            raise ExecutorError("unexpected-response") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ExecutorError(timeout_category) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ExecutorError(timeout_category) from exc
            raise ExecutorError("connection-failed") from exc
        except OSError as exc:
            raise ExecutorError("connection-failed") from exc

    def request_json(
        self,
        route: str,
        *,
        method: str,
        body: bytes | None,
        content_type: str | None,
        timeout: float,
        timeout_category: str,
        maximum: int,
    ) -> object:
        raw = self._request(
            route,
            method=method,
            body=body,
            content_type=content_type,
            timeout=timeout,
            timeout_category=timeout_category,
            maximum=maximum,
        )
        return _parse_json(raw, "malformed-response")

    def upload(self, content: bytes, index: int) -> str:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ExecutorError("size-limit")
        boundary = "comic-sol-comfyui-boundary"
        filename = f"reference-{index}.png"
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        response = self.request_json(
            "/upload/image",
            method="POST",
            body=prefix + content + suffix,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=QUEUE_TIMEOUT_SECONDS,
            timeout_category="queue-timeout",
            maximum=MAX_RESPONSE_JSON_BYTES,
        )
        required_fields = {"name", "subfolder", "type"}
        if not isinstance(response, dict) or not required_fields.issubset(response):
            raise ExecutorError("unexpected-response")
        name = response.get("name")
        subfolder = response.get("subfolder")
        image_type = response.get("type")
        if (
            not isinstance(name, str)
            or not _SAFE_FILE_RE.fullmatch(name)
            or not isinstance(subfolder, str)
            or (subfolder and not _SAFE_SUBFOLDER_RE.fullmatch(subfolder))
            or image_type != "input"
        ):
            raise ExecutorError("unexpected-response")
        return f"{subfolder}/{name}" if subfolder else name

    def queue(self, workflow: dict[str, object]) -> str:
        body = _canonical_compact({"prompt": workflow})
        response = self.request_json(
            "/prompt",
            method="POST",
            body=body,
            content_type="application/json",
            timeout=QUEUE_TIMEOUT_SECONDS,
            timeout_category="queue-timeout",
            maximum=MAX_RESPONSE_JSON_BYTES,
        )
        if not isinstance(response, dict):
            raise ExecutorError("unexpected-response")
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not _STABLE_ID_RE.fullmatch(prompt_id):
            raise ExecutorError("unexpected-response")
        return prompt_id

    def wait_for_output(self, prompt_id: str, output_node_id: str) -> dict[str, str]:
        deadline = time.monotonic() + EXECUTION_TIMEOUT_SECONDS
        route = "/history/" + urllib.parse.quote(prompt_id, safe="")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutorError("execution-timeout")
            response = self.request_json(
                route,
                method="GET",
                body=None,
                content_type=None,
                timeout=min(CONNECT_TIMEOUT_SECONDS, remaining),
                timeout_category="execution-timeout",
                maximum=MAX_HISTORY_BYTES,
            )
            image = _history_image(response, prompt_id, output_node_id)
            if image is not None:
                return image
            time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))

    def download(self, image: dict[str, str]) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": image["filename"],
                "subfolder": image["subfolder"],
                "type": image["type"],
            }
        )
        return self._request(
            f"/view?{query}",
            method="GET",
            body=None,
            content_type=None,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            timeout_category="download-timeout",
            maximum=MAX_RASTER_BYTES,
        )


def _read_stream_bounded(
    stream: BinaryIO,
    maximum: int,
    *,
    deadline: float | None = None,
    timeout_category: str = "connection-timeout",
) -> bytes:
    if maximum < 0:
        raise ExecutorError("size-limit")
    header = getattr(stream, "headers", None)
    if header is not None:
        declared = header.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise ExecutorError("unexpected-response") from exc
            if length < 0 or length > maximum:
                raise ExecutorError("size-limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        amount = min(65536, maximum - total + 1)
        if deadline is None:
            chunk = stream.read(amount)
        else:
            remaining = deadline - time.monotonic()
            chunk = _call_with_timeout(
                lambda: stream.read(amount),
                remaining,
                timeout_category,
            )
            if not isinstance(chunk, bytes):
                raise ExecutorError("unexpected-response")
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ExecutorError("size-limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_file_bounded(path: Path, maximum: int, category: str) -> bytes:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ExecutorError(category)
            if metadata.st_size > maximum:
                raise ExecutorError("size-limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ExecutorError("size-limit")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except ExecutorError:
        raise
    except (OSError, ValueError) as exc:
        raise ExecutorError(category) from exc


def _parse_json(raw: bytes, category: str) -> object:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ExecutorError) as exc:
        raise ExecutorError(category) from exc
    _validate_json_complexity(value, category)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutorError("duplicate-json-key")
        result[key] = value
    return result


def _validate_json_complexity(value: object, category: str) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ExecutorError(category)
        if isinstance(current, str):
            if len(current) > MAX_STRING_LENGTH:
                raise ExecutorError(category)
        elif isinstance(current, dict):
            if len(current) > MAX_COLLECTION_ITEMS:
                raise ExecutorError(category)
            for key, child in current.items():
                if len(key) > MAX_STRING_LENGTH:
                    raise ExecutorError(category)
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > MAX_COLLECTION_ITEMS:
                raise ExecutorError(category)
            pending.extend((child, depth + 1) for child in current)


def _canonical_compact(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _exact_keys(value: object, keys: set[str], category: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ExecutorError(category)
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ExecutorError("invalid-job")
    if value.startswith("/") or _DRIVE_RE.match(value):
        raise ExecutorError("invalid-job")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ExecutorError("invalid-job")
    return value


def _read_project_file(
    project_root: Path,
    relative: object,
    maximum: int,
) -> bytes:
    normalized = _validate_relative_path(relative)
    root = project_root.resolve(strict=True)
    candidate = root.joinpath(*normalized.split("/"))
    current = root
    try:
        for part in normalized.split("/"):
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ExecutorError("invalid-job-input")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except ExecutorError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExecutorError("invalid-job-input") from exc
    return _read_file_bounded(resolved, maximum, "invalid-job-input")


def _validate_job(job_path: Path) -> tuple[dict[str, object], Path, str]:
    raw = _read_file_bounded(job_path, MAX_JOB_BYTES, "invalid-job")
    job = _exact_keys(_parse_json(raw, "invalid-job"), _JOB_KEYS, "invalid-job")
    if job.get("schema_version") != "1.0":
        raise ExecutorError("invalid-job")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not _SHA256_RE.fullmatch(job_id):
        raise ExecutorError("invalid-job")
    if job_path.parent.name != "jobs" or job_path.parent.parent.name != "generation":
        raise ExecutorError("invalid-job")
    if job_path.name != f"{job_id}.json":
        raise ExecutorError("invalid-job")
    kind = job.get("subject_kind")
    subject_id = job.get("subject_id")
    if kind not in {"reference", "panel"} or not isinstance(subject_id, str):
        raise ExecutorError("invalid-job")
    if kind == "panel" and not _PANEL_ID_RE.fullmatch(subject_id):
        raise ExecutorError("invalid-job")
    if kind == "reference" and not _HANDOFF_IDENTIFIER_RE.fullmatch(subject_id):
        raise ExecutorError("invalid-job")
    prompt_path = _validate_relative_path(job.get("prompt_path"))
    expected_prompt = (
        f"prompts/panels/{subject_id}.txt"
        if kind == "panel"
        else f"prompts/references/{subject_id}.txt"
    )
    if prompt_path != expected_prompt or not _digest(job.get("prompt_sha256")):
        raise ExecutorError("invalid-job")
    references = job.get("references")
    if not isinstance(references, list) or len(references) > 64:
        raise ExecutorError("invalid-job")
    seen_paths: set[str] = set()
    for reference in references:
        entry = _exact_keys(reference, {"path", "sha256"}, "invalid-job")
        path = _validate_relative_path(entry.get("path"))
        reference_match = re.fullmatch(
            r"references/(?:characters|scenes)/([a-z0-9-]+)\.png",
            path,
        )
        if (
            reference_match is None
            or _HANDOFF_IDENTIFIER_RE.fullmatch(reference_match.group(1)) is None
            or path in seen_paths
            or not _digest(entry.get("sha256"))
        ):
            raise ExecutorError("invalid-job")
        seen_paths.add(path)
    dimensions = job.get("requested_dimensions")
    aspect = job.get("requested_aspect_ratio")
    if aspect is not None and (not isinstance(aspect, str) or _ASPECT_RE.fullmatch(aspect) is None):
        raise ExecutorError("invalid-job")
    if dimensions is not None:
        dimensions = _exact_keys(dimensions, {"width", "height"}, "invalid-job")
        width = dimensions.get("width")
        height = dimensions.get("height")
        if (
            not _is_int(width)
            or not _is_int(height)
            or width < 512
            or height < 512
            or width * height > 61_440_000
        ):
            raise ExecutorError("invalid-job")
        if isinstance(aspect, str):
            match = _ASPECT_RE.fullmatch(aspect)
            if match is None or width * int(match.group(2)) != height * int(match.group(1)):
                raise ExecutorError("invalid-job")
    if job.get("attempt_kind") not in {"initial", "visual_retry", "transient_repeat"}:
        raise ExecutorError("invalid-job")
    retry_limit = job.get("retry_limit")
    if not _is_int(retry_limit) or not 0 <= retry_limit <= 2:
        raise ExecutorError("invalid-job")
    batch_id = job.get("batch_id")
    if not isinstance(batch_id, str) or not _HANDOFF_IDENTIFIER_RE.fullmatch(batch_id):
        raise ExecutorError("invalid-job")
    target_path = _validate_relative_path(job.get("target_path"))
    attempt_kind = str(job["attempt_kind"])
    target_pattern = (
        rf"panels/attempts/{re.escape(subject_id)}/{re.escape(attempt_kind)}-0*[1-9][0-9]*\.(?:png|jpg|webp)"
        if kind == "panel"
        else rf"references/attempts/{re.escape(subject_id)}/{re.escape(attempt_kind)}-0*[1-9][0-9]*\.(?:png|jpg|webp)"
    )
    if not re.fullmatch(target_pattern, target_path):
        raise ExecutorError("invalid-job")
    identity = {key: value for key, value in job.items() if key not in {"schema_version", "job_id"}}
    expected_id = _sha256(_canonical_compact({"contract_version": "1.0", "job": identity}))
    if job_id != expected_id:
        raise ExecutorError("invalid-job")

    project_root = job_path.parents[2]
    prompt = _read_project_file(project_root, prompt_path, MAX_STRING_LENGTH)
    if _sha256(prompt) != job["prompt_sha256"]:
        raise ExecutorError("stale-job-input")
    try:
        positive_prompt = prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutorError("invalid-job-input") from exc
    total_reference_bytes = 0
    for reference in references:
        content = _read_project_file(project_root, reference["path"], MAX_UPLOAD_BYTES)
        total_reference_bytes += len(content)
        if total_reference_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise ExecutorError("size-limit")
        if _sha256(content) != reference["sha256"]:
            raise ExecutorError("stale-job-input")
    return job, project_root, positive_prompt


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _mapping(
    value: object,
    *,
    category: str,
    value_kind: str | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    keys = {"node_id", "input_name"}
    if value_kind is not None:
        keys.add("value")
    mapping = _exact_keys(value, keys, category)
    node_id = mapping.get("node_id")
    input_name = mapping.get("input_name")
    if (
        not isinstance(node_id, str)
        or not _NODE_ID_RE.fullmatch(node_id)
        or not isinstance(input_name, str)
        or not _NODE_ID_RE.fullmatch(input_name)
    ):
        raise ExecutorError(category)
    if value_kind == "text":
        static_value = mapping.get("value")
        if not isinstance(static_value, str) or len(static_value) > MAX_STRING_LENGTH:
            raise ExecutorError(category)
    if value_kind == "integer" and not _is_int(mapping.get("value")):
        raise ExecutorError(category)
    return mapping


def _validate_profile(profile: object, workflow_raw: bytes) -> dict[str, object]:
    profile = _exact_keys(profile, _PROFILE_KEYS, "invalid-profile")
    if profile.get("schema_version") != "1.0":
        raise ExecutorError("invalid-profile")
    profile_id = profile.get("profile_id")
    if (
        not isinstance(profile_id, str)
        or len(profile_id) > 48
        or not _STABLE_ID_RE.fullmatch(profile_id)
        or _looks_like_secret(profile_id)
    ):
        raise ExecutorError("invalid-profile")
    if not _digest(profile.get("profile_sha256")) or not _digest(profile.get("workflow_sha256")):
        raise ExecutorError("invalid-profile")
    payload = dict(profile)
    expected_profile_hash = payload.pop("profile_sha256")
    if _sha256(_canonical_compact(payload)) != expected_profile_hash:
        raise ExecutorError("stale-profile")
    if _sha256(workflow_raw) != profile["workflow_sha256"]:
        raise ExecutorError("stale-workflow")
    model = profile.get("model")
    if model is not None and (
        not isinstance(model, str)
        or not _SAFE_LABEL_RE.fullmatch(model)
        or _looks_like_secret(model)
    ):
        raise ExecutorError("invalid-profile")
    output_node_id = profile.get("output_node_id")
    if not isinstance(output_node_id, str) or not _NODE_ID_RE.fullmatch(output_node_id):
        raise ExecutorError("invalid-profile")
    inputs = _exact_keys(profile.get("inputs"), _INPUT_KEYS, "invalid-profile")
    positive = _mapping(inputs.get("positive_prompt"), category="invalid-profile")
    if positive is None:
        raise ExecutorError("invalid-profile")
    negative = _mapping(
        inputs.get("negative_prompt"),
        category="invalid-profile",
        value_kind="text",
    )
    seed = _mapping(inputs.get("seed"), category="invalid-profile", value_kind="integer")
    width = _mapping(inputs.get("width"), category="invalid-profile")
    height = _mapping(inputs.get("height"), category="invalid-profile")
    if (width is None) != (height is None):
        raise ExecutorError("invalid-profile")
    references = inputs.get("references")
    if not isinstance(references, list) or len(references) > 64:
        raise ExecutorError("invalid-profile")
    parsed_references: list[dict[str, object]] = []
    seen_indices: set[int] = set()
    for value in references:
        reference = _exact_keys(
            value,
            {"node_id", "input_name", "reference_index"},
            "invalid-profile",
        )
        base = _mapping(
            {"node_id": reference.get("node_id"), "input_name": reference.get("input_name")},
            category="invalid-profile",
        )
        index = reference.get("reference_index")
        if base is None or not _is_int(index) or index < 0 or index in seen_indices:
            raise ExecutorError("invalid-profile")
        seen_indices.add(index)
        parsed_references.append({**base, "reference_index": index})
    normalized_inputs = {
        "positive_prompt": positive,
        "negative_prompt": negative,
        "seed": seed,
        "width": width,
        "height": height,
        "references": parsed_references,
    }
    return {**profile, "inputs": normalized_inputs}


def _validate_workflow(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not value or len(value) > MAX_COLLECTION_ITEMS:
        raise ExecutorError("invalid-workflow")
    for node_id, node in value.items():
        if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
            raise ExecutorError("invalid-workflow")
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ExecutorError("invalid-workflow")
    return value


def _preflight_workflow(
    workflow: dict[str, object],
    profile: dict[str, object],
    job: dict[str, object],
) -> None:
    output_node_id = profile["output_node_id"]
    if not isinstance(output_node_id, str) or output_node_id not in workflow:
        raise ExecutorError("invalid-profile")
    inputs = profile["inputs"]
    if not isinstance(inputs, dict):
        raise ExecutorError("invalid-profile")
    mappings: list[dict[str, object]] = []
    for name in ("positive_prompt", "negative_prompt", "seed", "width", "height"):
        mapping = inputs[name]
        if isinstance(mapping, dict):
            mappings.append(mapping)
    reference_mappings = inputs["references"]
    if not isinstance(reference_mappings, list):
        raise ExecutorError("invalid-profile")
    mappings.extend(mapping for mapping in reference_mappings if isinstance(mapping, dict))
    if len(mappings) != sum(
        isinstance(inputs[name], dict)
        for name in ("positive_prompt", "negative_prompt", "seed", "width", "height")
    ) + len(reference_mappings):
        raise ExecutorError("invalid-profile")
    targets: set[tuple[str, str]] = set()
    for mapping in mappings:
        node_id = mapping.get("node_id")
        input_name = mapping.get("input_name")
        if not isinstance(node_id, str) or not isinstance(input_name, str):
            raise ExecutorError("invalid-profile")
        target = (node_id, input_name)
        if target in targets:
            raise ExecutorError("invalid-profile")
        targets.add(target)
        node = workflow.get(node_id)
        if not isinstance(node, dict):
            raise ExecutorError("invalid-profile")
        node_inputs = node.get("inputs")
        if not isinstance(node_inputs, dict) or input_name not in node_inputs:
            raise ExecutorError("invalid-profile")
    dimensions = job["requested_dimensions"]
    if dimensions is not None and (inputs["width"] is None or inputs["height"] is None):
        raise ExecutorError("unsupported-job")
    references = job["references"]
    if not isinstance(references, list):
        raise ExecutorError("invalid-job")
    indices = {mapping.get("reference_index") for mapping in reference_mappings}
    if indices != set(range(len(references))):
        raise ExecutorError("unsupported-job")


def _patch_workflow(
    workflow: dict[str, object],
    profile: dict[str, object],
    job: dict[str, object],
    positive_prompt: str,
    uploaded: list[str],
) -> tuple[dict[str, object], dict[str, bool]]:
    _preflight_workflow(workflow, profile, job)
    patched = copy.deepcopy(workflow)
    inputs = profile["inputs"]
    assert isinstance(inputs, dict)
    output_node_id = profile["output_node_id"]
    if output_node_id not in patched:
        raise ExecutorError("invalid-profile")
    targets: set[tuple[str, str]] = set()

    def assign(mapping: object, value: object) -> None:
        if mapping is None or not isinstance(mapping, dict):
            return
        node_id = mapping["node_id"]
        input_name = mapping["input_name"]
        if not isinstance(node_id, str) or not isinstance(input_name, str):
            raise ExecutorError("invalid-profile")
        target = (node_id, input_name)
        if target in targets:
            raise ExecutorError("invalid-profile")
        targets.add(target)
        node = patched.get(node_id)
        if not isinstance(node, dict):
            raise ExecutorError("invalid-profile")
        node_inputs = node.get("inputs")
        if not isinstance(node_inputs, dict) or input_name not in node_inputs:
            raise ExecutorError("invalid-profile")
        node_inputs[input_name] = value

    assign(inputs["positive_prompt"], positive_prompt)
    negative = inputs["negative_prompt"]
    if isinstance(negative, dict):
        assign(negative, negative["value"])
    seed = inputs["seed"]
    if isinstance(seed, dict):
        assign(seed, seed["value"])

    dimensions = job["requested_dimensions"]
    dimension_used = False
    if dimensions is not None:
        if inputs["width"] is None or inputs["height"] is None or not isinstance(dimensions, dict):
            raise ExecutorError("unsupported-job")
        assign(inputs["width"], dimensions["width"])
        assign(inputs["height"], dimensions["height"])
        dimension_used = True

    job_references = job["references"]
    reference_mappings = inputs["references"]
    if not isinstance(job_references, list) or not isinstance(reference_mappings, list):
        raise ExecutorError("invalid-profile")
    reference_used = bool(job_references)
    if reference_used:
        indices = {mapping["reference_index"] for mapping in reference_mappings}
        if indices != set(range(len(job_references))) or len(uploaded) != len(job_references):
            raise ExecutorError("unsupported-job")
        for mapping in reference_mappings:
            index = mapping["reference_index"]
            if not isinstance(index, int):
                raise ExecutorError("invalid-profile")
            assign(mapping, uploaded[index])

    return patched, {
        "reference_images": reference_used,
        "dimensions": dimension_used,
        "localized_edit": False,
    }


def _history_image(
    value: object,
    prompt_id: str,
    output_node_id: str,
) -> dict[str, str] | None:
    if value == {}:
        return None
    if not isinstance(value, dict) or set(value) != {prompt_id}:
        raise ExecutorError("unexpected-response")
    entry = value[prompt_id]
    if not isinstance(entry, dict):
        raise ExecutorError("unexpected-response")
    status_value = entry.get("status")
    outputs = entry.get("outputs")
    if not isinstance(status_value, dict) or not isinstance(outputs, dict):
        raise ExecutorError("unexpected-response")
    completed = status_value.get("completed")
    status_text = status_value.get("status_str")
    if not isinstance(completed, bool) or not isinstance(status_text, str):
        raise ExecutorError("unexpected-response")
    if not completed:
        if outputs:
            raise ExecutorError("unexpected-response")
        return None
    if status_text != "success" or set(outputs) != {output_node_id}:
        raise ExecutorError("unexpected-response")
    output = outputs[output_node_id]
    if not isinstance(output, dict) or set(output) != {"images"}:
        raise ExecutorError("unexpected-response")
    images = output["images"]
    if not isinstance(images, list) or len(images) != 1:
        raise ExecutorError("unexpected-response")
    image = _exact_keys(images[0], {"filename", "subfolder", "type"}, "unexpected-response")
    filename = image.get("filename")
    subfolder = image.get("subfolder")
    image_type = image.get("type")
    if (
        not isinstance(filename, str)
        or not _SAFE_FILE_RE.fullmatch(filename)
        or not isinstance(subfolder, str)
        or (subfolder and not _SAFE_SUBFOLDER_RE.fullmatch(subfolder))
        or image_type != "output"
    ):
        raise ExecutorError("unexpected-response")
    return {"filename": filename, "subfolder": subfolder, "type": image_type}


def _publish_output(path: Path, content: bytes) -> None:
    if path.exists() or not path.parent.is_dir():
        raise ExecutorError("output-rejected")
    descriptor = -1
    temporary_path: Path | None = None
    published = False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, path)
        published = True
        temporary_path.unlink()
        temporary_path = None
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except ExecutorError:
        raise
    except OSError as exc:
        if published:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise ExecutorError("output-rejected") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def execute(
    *,
    job_path: Path,
    workflow_path: Path,
    profile_path: Path,
    output_path: Path,
    endpoint_value: str,
    allow_non_loopback: bool,
    warning_stream: TextIO,
) -> dict[str, object]:
    job, project_root, positive_prompt = _validate_job(job_path)
    workflow_raw = _read_file_bounded(workflow_path, MAX_WORKFLOW_BYTES, "invalid-workflow")
    workflow = _validate_workflow(_parse_json(workflow_raw, "invalid-workflow"))
    profile_raw = _read_file_bounded(profile_path, MAX_PROFILE_BYTES, "invalid-profile")
    profile = _validate_profile(_parse_json(profile_raw, "invalid-profile"), workflow_raw)
    _preflight_workflow(workflow, profile, job)
    endpoint = Endpoint.parse(
        endpoint_value,
        allow_non_loopback=allow_non_loopback,
        warning_stream=warning_stream,
    )
    client = ComfyUIClient(endpoint)
    references = job["references"]
    if not isinstance(references, list):
        raise ExecutorError("invalid-job")
    uploaded: list[str] = []
    for index, reference in enumerate(references):
        if not isinstance(reference, dict):
            raise ExecutorError("invalid-job")
        content = _read_project_file(project_root, reference["path"], MAX_UPLOAD_BYTES)
        if _sha256(content) != reference["sha256"]:
            raise ExecutorError("stale-job-input")
        uploaded.append(client.upload(content, index))
    patched, capabilities = _patch_workflow(
        workflow,
        profile,
        job,
        positive_prompt,
        uploaded,
    )
    prompt_id = client.queue(patched)
    image = client.wait_for_output(prompt_id, str(profile["output_node_id"]))
    raster = client.download(image)
    if not raster:
        raise ExecutorError("unexpected-response")
    _publish_output(output_path, raster)
    return {
        "executor_kind": "external-tool",
        "executor_id": "comfyui-local",
        "provider": "comfyui",
        "model": profile["model"],
        "capabilities_used": capabilities,
        "workflow_sha256": profile["workflow_sha256"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "output_sha256": _sha256(raster),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Comic Sol job with local ComfyUI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--job", required=True)
    run.add_argument("--workflow", required=True)
    run.add_argument("--profile", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    run.add_argument("--allow-non-loopback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = execute(
            job_path=Path(arguments.job),
            workflow_path=Path(arguments.workflow),
            profile_path=Path(arguments.profile),
            output_path=Path(arguments.output),
            endpoint_value=arguments.endpoint,
            allow_non_loopback=arguments.allow_non_loopback,
            warning_stream=sys.stderr,
        )
        envelope: dict[str, object] = {"ok": True, "result": result}
        exit_code = 0
    except ExecutorError as exc:
        envelope = {"ok": False, "error": {"category": exc.category}}
        exit_code = 2
    except Exception:
        envelope = {"ok": False, "error": {"category": "internal-error"}}
        exit_code = 1
    sys.stdout.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
