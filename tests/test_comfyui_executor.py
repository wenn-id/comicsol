from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_PATH = ROOT / "integrations" / "comfyui-local" / "comfyui_executor.py"
PROFILE_SCHEMA_PATH = ROOT / "integrations" / "comfyui-local" / "profile.schema.json"
EXAMPLE_PROFILE_PATH = ROOT / "integrations" / "comfyui-local" / "example-profile.json"
PNG_BYTES = b"\x89PNG\r\n\x1a\ncomic-sol-comfyui-test"


def _canonical_compact(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_artifact(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _profile_digest(profile: dict[str, object]) -> str:
    payload = dict(profile)
    payload.pop("profile_sha256", None)
    return _sha256(_canonical_compact(payload))


def _load_executor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comfyui_executor_under_test", EXECUTOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load ComfyUI executor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeComfyUIHandler(BaseHTTPRequestHandler):
    server: "_FakeComfyUIServer"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _record(self, body: bytes = b"") -> None:
        self.server.requests.append((self.command, self.path, dict(self.headers), body))

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        declared_length: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Length",
            str(len(body) if declared_length is None else declared_length),
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, value: object) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        if self.path in self.server.redirects:
            self.send_response(302)
            self.send_header("Location", self.server.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/upload/image":
            if self.server.upload_delay:
                time.sleep(self.server.upload_delay)
            index = self.server.upload_count
            self.server.upload_count += 1
            response = {
                "name": f"reference-{index}.png",
                "subfolder": "comic-sol",
                "type": "input",
            }
            if self.server.upload_asset:
                response["asset"] = {"kind": "input", "name": f"reference-{index}.png"}
            self._send_bytes(200, self._json(response), "application/json")
            return
        if self.path == "/prompt":
            if self.server.queue_delay:
                time.sleep(self.server.queue_delay)
            if self.server.queue_raw is not None:
                body = self.server.queue_raw
            else:
                body = self._json(self.server.queue_response)
            self._send_bytes(200, body, "application/json")
            return
        self._send_bytes(404, b"not found", "text/plain")

    def do_GET(self) -> None:
        self._record()
        if self.path in self.server.redirects:
            self.send_response(302)
            self.send_header("Location", self.server.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/history/"):
            if self.server.history_delay:
                time.sleep(self.server.history_delay)
            if self.server.history_sequence:
                body = self._json(self.server.history_sequence.pop(0))
            elif self.server.history_raw is not None:
                body = self.server.history_raw
            else:
                body = self._json(self.server.history_response)
            self._send_bytes(200, body, "application/json")
            return
        if self.path.startswith("/view?"):
            if self.server.download_delay:
                time.sleep(self.server.download_delay)
            self._send_bytes(
                200,
                self.server.raster,
                "image/png",
                declared_length=self.server.raster_declared_length,
            )
            return
        self._send_bytes(404, b"not found", "text/plain")


class _FakeComfyUIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeComfyUIHandler)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.redirects: dict[str, str] = {}
        self.upload_count = 0
        self.upload_asset = False
        self.upload_delay = 0.0
        self.queue_delay = 0.0
        self.history_delay = 0.0
        self.download_delay = 0.0
        self.queue_raw: bytes | None = None
        self.queue_response: object = {"prompt_id": "prompt-1", "number": 1}
        self.history_raw: bytes | None = None
        self.history_sequence: list[object] = []
        self.history_response: object = {
            "prompt-1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "result.png",
                                "subfolder": "comic-sol",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }
        self.raster = PNG_BYTES
        self.raster_declared_length: int | None = None

    @property
    def endpoint(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class ComfyUIExecutorTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.module = _load_executor()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.workflow_path = self.root / "workflow.json"
        self.profile_path = self.root / "profile.json"
        self.output_path = self.root / "output.png"
        self.server = _FakeComfyUIServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _write_job(
        self,
        *,
        references: tuple[bytes, ...] = (b"reference-image",),
        dimensions: tuple[int, int] | None = (1024, 768),
    ) -> tuple[Path, dict[str, object]]:
        prompt_path = self.project / "prompts" / "panels" / "p01-01.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = b"A precise positive prompt.\n"
        prompt_path.write_bytes(prompt)
        reference_entries: list[dict[str, str]] = []
        for index, content in enumerate(references):
            relative = f"references/characters/character-{index}.png"
            absolute = self.project / relative
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(content)
            reference_entries.append({"path": relative, "sha256": _sha256(content)})
        width, height = dimensions or (0, 0)
        identity = {
            "subject_kind": "panel",
            "subject_id": "p01-01",
            "prompt_path": "prompts/panels/p01-01.txt",
            "prompt_sha256": _sha256(prompt),
            "references": reference_entries,
            "requested_dimensions": (
                {"width": width, "height": height} if dimensions is not None else None
            ),
            "requested_aspect_ratio": (f"{width}:{height}" if dimensions is not None else None),
            "attempt_kind": "initial",
            "retry_limit": 2,
            "batch_id": "page-001",
            "target_path": "panels/attempts/p01-01/initial-001.png",
        }
        job_id = _sha256(_canonical_compact({"contract_version": "1.0", "job": identity}))
        job: dict[str, object] = {
            "schema_version": "1.0",
            "job_id": job_id,
            **identity,
        }
        job_path = self.project / "generation" / "jobs" / f"{job_id}.json"
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_bytes(_canonical_artifact(job))
        return job_path, job

    def _write_workflow(self) -> tuple[dict[str, object], str]:
        workflow: dict[str, object] = {
            "3": {"class_type": "KSampler", "inputs": {"seed": 0}},
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512},
            },
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old-positive"}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "old-negative"}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
            "10": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
        }
        raw = _canonical_artifact(workflow)
        self.workflow_path.write_bytes(raw)
        return workflow, _sha256(raw)

    def _write_profile(
        self,
        workflow_sha256: str,
        *,
        include_references: bool = True,
        include_dimensions: bool = True,
        include_optional_values: bool = True,
    ) -> dict[str, object]:
        inputs: dict[str, object] = {
            "positive_prompt": {"node_id": "6", "input_name": "text"},
            "negative_prompt": (
                {
                    "node_id": "7",
                    "input_name": "text",
                    "value": "low quality, watermark",
                }
                if include_optional_values
                else None
            ),
            "seed": (
                {"node_id": "3", "input_name": "seed", "value": 42}
                if include_optional_values
                else None
            ),
            "width": ({"node_id": "5", "input_name": "width"} if include_dimensions else None),
            "height": ({"node_id": "5", "input_name": "height"} if include_dimensions else None),
            "references": (
                [{"node_id": "10", "input_name": "image", "reference_index": 0}]
                if include_references
                else []
            ),
        }
        profile: dict[str, object] = {
            "schema_version": "1.0",
            "profile_id": "test-sdxl",
            "profile_sha256": "",
            "workflow_sha256": workflow_sha256,
            "model": "local-sdxl",
            "output_node_id": "9",
            "inputs": inputs,
        }
        profile["profile_sha256"] = _profile_digest(profile)
        self.profile_path.write_bytes(_canonical_artifact(profile))
        return profile

    def _run(
        self,
        job_path: Path,
        *,
        endpoint: str | None = None,
        allow_non_loopback: bool = False,
    ) -> tuple[int, dict[str, object], str]:
        argv = [
            "run",
            "--job",
            str(job_path),
            "--workflow",
            str(self.workflow_path),
            "--profile",
            str(self.profile_path),
            "--output",
            str(self.output_path),
            "--endpoint",
            endpoint or self.server.endpoint,
        ]
        if allow_non_loopback:
            argv.append("--allow-non-loopback")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = self.module.main(argv)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        return exit_code, json.loads(lines[0]), stderr.getvalue()

    def _prepare_valid_files(
        self,
        *,
        references: tuple[bytes, ...] = (b"reference-image",),
        dimensions: tuple[int, int] | None = (1024, 768),
        include_references: bool = True,
        include_dimensions: bool = True,
    ) -> tuple[Path, dict[str, object]]:
        job_path, job = self._write_job(references=references, dimensions=dimensions)
        _, workflow_hash = self._write_workflow()
        self._write_profile(
            workflow_hash,
            include_references=include_references,
            include_dimensions=include_dimensions,
        )
        return job_path, job

    def assert_failure(
        self,
        result: tuple[int, dict[str, object], str],
        category: str,
    ) -> None:
        exit_code, envelope, stderr = result
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(envelope.get("ok"), False)
        self.assertEqual(envelope.get("error"), {"category": category})
        combined = json.dumps(envelope) + stderr
        self.assertNotIn(str(self.root), combined)
        self.assertNotIn("old-positive", combined)
        self.assertNotIn("A precise positive prompt", combined)
        self.assertFalse(self.output_path.exists())

    def test_happy_sequence_and_explicit_field_mapping(self) -> None:
        job_path, _ = self._prepare_valid_files()
        exit_code, envelope, stderr = self._run(job_path)

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(
            [(method, path.split("?", 1)[0]) for method, path, _, _ in self.server.requests],
            [
                ("POST", "/upload/image"),
                ("POST", "/prompt"),
                ("GET", "/history/prompt-1"),
                ("GET", "/view"),
            ],
        )
        prompt_request = self.server.requests[1]
        submitted = json.loads(prompt_request[3])
        patched = submitted["prompt"]
        self.assertEqual(patched["6"]["inputs"]["text"], "A precise positive prompt.\n")
        self.assertEqual(patched["7"]["inputs"]["text"], "low quality, watermark")
        self.assertEqual(patched["3"]["inputs"]["seed"], 42)
        self.assertEqual(patched["5"]["inputs"]["width"], 1024)
        self.assertEqual(patched["5"]["inputs"]["height"], 768)
        self.assertEqual(patched["10"]["inputs"]["image"], "comic-sol/reference-0.png")
        self.assertEqual(self.output_path.read_bytes(), PNG_BYTES)
        metadata = envelope["result"]
        self.assertEqual(metadata["executor_kind"], "external-tool")
        self.assertEqual(metadata["executor_id"], "comfyui-local")
        self.assertEqual(metadata["provider"], "comfyui")
        self.assertEqual(metadata["model"], "local-sdxl")
        self.assertEqual(
            metadata["capabilities_used"],
            {"reference_images": True, "dimensions": True, "localized_edit": False},
        )
        self.assertEqual(metadata["output_sha256"], _sha256(PNG_BYTES))
        self.assertEqual(metadata["profile_id"], "test-sdxl")
        self.assertRegex(metadata["profile_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(metadata["workflow_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("output", metadata)

    def test_unmapped_optional_capabilities_are_false(self) -> None:
        job_path, _ = self._prepare_valid_files(
            references=(),
            dimensions=None,
            include_references=False,
            include_dimensions=False,
        )
        _, workflow_hash = self._write_workflow()
        self._write_profile(
            workflow_hash,
            include_references=False,
            include_dimensions=False,
            include_optional_values=False,
        )

        exit_code, envelope, stderr = self._run(job_path)

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(
            envelope["result"]["capabilities_used"],
            {"reference_images": False, "dimensions": False, "localized_edit": False},
        )
        self.assertEqual(
            [path.split("?", 1)[0] for _, path, _, _ in self.server.requests],
            ["/prompt", "/history/prompt-1", "/view"],
        )

    def test_missing_required_mapping_and_unexpected_mapping_targets_fail(self) -> None:
        job_path, _ = self._prepare_valid_files()
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        cases: list[tuple[str, object]] = [
            ("missing", None),
            ("unknown-node", {"node_id": "404", "input_name": "text"}),
            ("unknown-input", {"node_id": "6", "input_name": "missing"}),
        ]
        for name, mapping in cases:
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(profile))
                candidate["inputs"]["positive_prompt"] = mapping
                candidate["profile_sha256"] = _profile_digest(candidate)
                self.profile_path.write_bytes(_canonical_artifact(candidate))
                self.assert_failure(self._run(job_path), "invalid-profile")

    def test_partial_dimension_and_missing_reference_mappings_fail(self) -> None:
        job_path, _ = self._prepare_valid_files()
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile["inputs"]["height"] = None
        profile["profile_sha256"] = _profile_digest(profile)
        self.profile_path.write_bytes(_canonical_artifact(profile))
        self.assert_failure(self._run(job_path), "invalid-profile")

        _, workflow_hash = self._write_workflow()
        self._write_profile(
            workflow_hash,
            include_references=False,
            include_dimensions=True,
        )
        self.assert_failure(self._run(job_path), "unsupported-job")

    def test_stale_job_workflow_profile_prompt_and_reference_hashes_fail_closed(self) -> None:
        job_path, job = self._prepare_valid_files()
        cases: list[tuple[str, callable[[], None], str]] = [
            (
                "job-id",
                lambda: self._mutate_job(job_path, job, "job_id", "0" * 64),
                "invalid-job",
            ),
            (
                "workflow",
                lambda: self.workflow_path.write_bytes(self.workflow_path.read_bytes() + b" "),
                "stale-workflow",
            ),
            (
                "profile",
                lambda: self._mutate_profile_without_digest("model", "changed"),
                "stale-profile",
            ),
            (
                "prompt",
                lambda: (self.project / str(job["prompt_path"])).write_text(
                    "changed", encoding="utf-8"
                ),
                "stale-job-input",
            ),
            (
                "reference",
                lambda: (self.project / str(job["references"][0]["path"])).write_bytes(b"changed"),
                "stale-job-input",
            ),
        ]
        for name, mutate, category in cases:
            with self.subTest(name=name):
                job_path, job = self._prepare_valid_files()
                mutate()
                self.assert_failure(self._run(job_path), category)

    def _mutate_job(
        self,
        job_path: Path,
        job: dict[str, object],
        key: str,
        value: object,
    ) -> None:
        job[key] = value
        job_path.write_bytes(_canonical_artifact(job))

    def _mutate_profile_without_digest(self, key: str, value: object) -> None:
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile[key] = value
        self.profile_path.write_bytes(_canonical_artifact(profile))

    def test_rejects_credential_fragment_and_non_loopback_endpoints(self) -> None:
        job_path, _ = self._prepare_valid_files()
        endpoints = {
            "credentials": "http://user:secret@127.0.0.1:8188",
            "fragment": "http://127.0.0.1:8188/#private",
            "ipv4": "http://192.0.2.10:8188",
            "ipv6": "http://[2001:db8::10]:8188",
        }
        for name, endpoint in endpoints.items():
            with self.subTest(name=name):
                self.assert_failure(self._run(job_path, endpoint=endpoint), "endpoint-rejected")

    def test_dns_non_loopback_and_rebinding_results_fail_closed(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        _, port = self.server.server_address
        endpoint = f"http://comfy.test:{port}"
        loopback = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        non_loopback = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", port))]
        with mock.patch.object(self.module.socket, "getaddrinfo", return_value=non_loopback):
            self.assert_failure(self._run(job_path, endpoint=endpoint), "endpoint-rejected")

        calls = 0
        real_getaddrinfo = socket.getaddrinfo

        def rebind(*args: object, **kwargs: object) -> object:
            nonlocal calls
            if args and args[0] == "comfy.test":
                calls += 1
                return loopback if calls == 1 else non_loopback
            return real_getaddrinfo(*args, **kwargs)

        with mock.patch.object(self.module.socket, "getaddrinfo", side_effect=rebind):
            self.assert_failure(self._run(job_path, endpoint=endpoint), "endpoint-rejected")
        self.assertGreaterEqual(calls, 2)

    def test_non_loopback_override_emits_explicit_warning(self) -> None:
        warning = io.StringIO()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 8188))]
        with mock.patch.object(self.module.socket, "getaddrinfo", return_value=addresses):
            endpoint = self.module.Endpoint.parse(
                "http://comfy.example:8188",
                allow_non_loopback=True,
                warning_stream=warning,
            )
            endpoint.resolve_for_connection()
        message = warning.getvalue().lower()
        self.assertIn("warning", message)
        self.assertIn("no comic sol authentication boundary", message)
        self.assertNotIn("credentials", endpoint.connection_url("/prompt"))

    def test_redirects_are_rejected_even_on_same_origin(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        self.server.redirects["/prompt"] = f"{self.server.endpoint}/prompt-redirected"
        self.assert_failure(self._run(job_path), "redirect-rejected")

    def test_malformed_and_unexpected_json_and_nodes_fail_safely(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        cases: list[tuple[str, callable[[], None], str]] = [
            (
                "queue-malformed",
                lambda: setattr(self.server, "queue_raw", b"not-json"),
                "malformed-response",
            ),
            (
                "queue-shape",
                lambda: setattr(self.server, "queue_response", {"prompt_id": ["bad"]}),
                "unexpected-response",
            ),
            (
                "history-malformed",
                lambda: setattr(self.server, "history_raw", b"{"),
                "malformed-response",
            ),
            (
                "unexpected-node",
                lambda: setattr(
                    self.server,
                    "history_response",
                    {
                        "prompt-1": {
                            "status": {"completed": True, "status_str": "success"},
                            "outputs": {"404": {"images": []}},
                        }
                    },
                ),
                "unexpected-response",
            ),
        ]
        for name, configure, category in cases:
            with self.subTest(name=name):
                self.server.queue_raw = None
                self.server.queue_response = {"prompt_id": "prompt-1", "number": 1}
                self.server.history_raw = None
                self.server.history_response = {
                    "prompt-1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "result.png",
                                        "subfolder": "comic-sol",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
                configure()
                self.assert_failure(self._run(job_path), category)

    def test_workflow_profile_upload_queue_history_and_raster_size_bounds(self) -> None:
        job_path, _ = self._prepare_valid_files()
        cases: list[tuple[str, str, int, callable[[], None]]] = [
            (
                "workflow",
                "MAX_WORKFLOW_BYTES",
                32,
                lambda: setattr(self, "workflow_path", self.workflow_path),
            ),
            (
                "profile",
                "MAX_PROFILE_BYTES",
                32,
                lambda: setattr(self, "profile_path", self.profile_path),
            ),
            (
                "upload",
                "MAX_UPLOAD_BYTES",
                8,
                lambda: None,
            ),
            (
                "queue",
                "MAX_RESPONSE_JSON_BYTES",
                32,
                lambda: setattr(self.server, "queue_raw", b"{" + b" " * 512),
            ),
            (
                "history",
                "MAX_HISTORY_BYTES",
                32,
                lambda: setattr(self.server, "history_raw", b"{" + b" " * 512),
            ),
            (
                "raster",
                "MAX_RASTER_BYTES",
                32,
                lambda: setattr(self.server, "raster", PNG_BYTES + b"x" * 512),
            ),
        ]
        for name, constant, maximum, configure in cases:
            with self.subTest(name=name):
                job_path, _ = self._prepare_valid_files()
                self.output_path.unlink(missing_ok=True)
                self.server.queue_raw = None
                self.server.history_raw = None
                self.server.raster = PNG_BYTES
                configure()
                with mock.patch.object(self.module, constant, maximum):
                    self.assert_failure(self._run(job_path), "size-limit")

    def test_connection_queue_execution_and_download_timeouts(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        with mock.patch.object(
            self.module.socket,
            "getaddrinfo",
            side_effect=socket.timeout(),
        ):
            self.assert_failure(self._run(job_path), "connection-timeout")

        self.server.queue_delay = 0.2
        with mock.patch.object(self.module, "QUEUE_TIMEOUT_SECONDS", 0.05):
            self.assert_failure(self._run(job_path), "queue-timeout")
        self.server.queue_delay = 0.0

        self.server.history_response = {
            "prompt-1": {
                "status": {"completed": False, "status_str": "running"},
                "outputs": {},
            }
        }
        with (
            mock.patch.object(self.module, "EXECUTION_TIMEOUT_SECONDS", 0.05),
            mock.patch.object(self.module, "POLL_INTERVAL_SECONDS", 0.01),
        ):
            self.assert_failure(self._run(job_path), "execution-timeout")

        self.server.history_response = {
            "prompt-1": {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "result.png",
                                "subfolder": "comic-sol",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }
        self.server.download_delay = 0.2
        with mock.patch.object(self.module, "DOWNLOAD_TIMEOUT_SECONDS", 0.05):
            self.assert_failure(self._run(job_path), "download-timeout")

    def test_empty_history_is_pending_until_terminal_result(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        self.server.history_sequence = [{}, self.server.history_response]
        with mock.patch.object(self.module, "POLL_INTERVAL_SECONDS", 0.001):
            exit_code, envelope, stderr = self._run(job_path)
        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(envelope["ok"])
        history_paths = [
            path for _, path, _, _ in self.server.requests if path.startswith("/history/")
        ]
        self.assertEqual(history_paths, ["/history/prompt-1", "/history/prompt-1"])

    def test_all_mapping_targets_are_preflighted_before_upload(self) -> None:
        job_path, _ = self._prepare_valid_files()
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        profile["inputs"]["references"][0]["node_id"] = "404"
        profile["profile_sha256"] = _profile_digest(profile)
        self.profile_path.write_bytes(_canonical_artifact(profile))
        self.assert_failure(self._run(job_path), "invalid-profile")
        self.assertEqual(self.server.requests, [])

    def test_resolver_and_slow_stream_obey_total_deadlines(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())

        def hanging_resolver(*args: object, **kwargs: object) -> object:
            del args, kwargs
            time.sleep(0.2)
            return []

        with (
            mock.patch.object(self.module.socket, "getaddrinfo", side_effect=hanging_resolver),
            mock.patch.object(self.module, "CONNECT_TIMEOUT_SECONDS", 0.02),
        ):
            started = time.monotonic()
            self.assert_failure(self._run(job_path), "connection-timeout")
            self.assertLess(time.monotonic() - started, 0.15)

        class SlowStream:
            headers: dict[str, str] = {}

            def read(self, amount: int) -> bytes:
                del amount
                time.sleep(0.2)
                return b"x"

        with self.assertRaises(self.module.ExecutorError) as raised:
            self.module._read_stream_bounded(
                SlowStream(),
                16,
                deadline=time.monotonic() + 0.02,
                timeout_category="download-timeout",
            )
        self.assertEqual(raised.exception.category, "download-timeout")

    def test_environment_proxy_is_disabled_for_pinned_connections(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        _, port = self.server.server_address
        endpoint = f"http://comfy.test:{port}"
        real_getaddrinfo = socket.getaddrinfo

        def local_origin(host: object, *args: object, **kwargs: object) -> object:
            if host == "comfy.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return real_getaddrinfo(host, *args, **kwargs)

        hostile = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "all_proxy": "http://127.0.0.1:1",
            "no_proxy": "",
        }
        with (
            mock.patch.dict(os.environ, hostile, clear=False),
            mock.patch.object(self.module.socket, "getaddrinfo", side_effect=local_origin),
        ):
            exit_code, envelope, stderr = self._run(job_path, endpoint=endpoint)
        self.assertEqual(exit_code, 0, (envelope, stderr))

    def test_output_publication_never_clobbers_a_racing_destination(self) -> None:
        real_link = os.link

        def competing_link(source: object, destination: object) -> None:
            Path(destination).write_bytes(b"competitor")
            real_link(source, destination)

        with mock.patch.object(self.module.os, "link", side_effect=competing_link):
            with self.assertRaises(self.module.ExecutorError) as raised:
                self.module._publish_output(self.output_path, PNG_BYTES)
        self.assertEqual(raised.exception.category, "output-rejected")
        self.assertEqual(self.output_path.read_bytes(), b"competitor")
        self.assertEqual(list(self.output_path.parent.glob(f".{self.output_path.name}.*")), [])

    def test_aggregate_reference_limit_fails_before_network(self) -> None:
        job_path, _ = self._prepare_valid_files(references=(b"a" * 10, b"b" * 10))
        _, workflow_hash = self._write_workflow()
        profile = self._write_profile(workflow_hash)
        profile["inputs"]["references"] = [
            {"node_id": "10", "input_name": "image", "reference_index": 0},
            {"node_id": "11", "input_name": "image", "reference_index": 1},
        ]
        workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        workflow["11"] = {"class_type": "LoadImage", "inputs": {"image": "old-2.png"}}
        workflow_raw = _canonical_artifact(workflow)
        self.workflow_path.write_bytes(workflow_raw)
        profile["workflow_sha256"] = _sha256(workflow_raw)
        profile["profile_sha256"] = _profile_digest(profile)
        self.profile_path.write_bytes(_canonical_artifact(profile))
        with mock.patch.object(self.module, "MAX_TOTAL_UPLOAD_BYTES", 15):
            self.assert_failure(self._run(job_path), "size-limit")
        self.assertEqual(self.server.requests, [])

    def test_asset_enabled_upload_response_is_accepted_without_persistence(self) -> None:
        job_path, _ = self._prepare_valid_files()
        self.server.upload_asset = True
        exit_code, envelope, stderr = self._run(job_path)
        self.assertEqual(exit_code, 0, stderr)
        self.assertNotIn("asset", json.dumps(envelope))

    def test_skill_bundle_marks_repository_only_route_and_has_valid_links(self) -> None:
        bundled_setup = ROOT / "skills" / "comic-sol" / "references" / "image-provider-setup.md"
        bundled_workflow = ROOT / "skills" / "comic-sol" / "references" / "workflow.md"
        setup_text = bundled_setup.read_text(encoding="utf-8")
        workflow_text = bundled_workflow.read_text(encoding="utf-8")
        self.assertIn("ComfyUI local reference executor", setup_text)
        self.assertIn("repository checkout", setup_text)
        self.assertNotIn("../integrations/comfyui-local", workflow_text)
        self.assertIn(
            "https://github.com/wenn-id/comicsol/blob/main/integrations/comfyui-local/README.md",
            workflow_text,
        )

    def test_rejects_generation_jobs_outside_authoritative_identifier_and_target_grammar(
        self,
    ) -> None:
        from scripts.handoff import validate_generation_job

        for name, updates in (
            ("batch", {"batch_id": "bad_id"}),
            ("target", {"target_path": "panels/attempts/p01-01/initial.png"}),
        ):
            with self.subTest(name=name):
                self.output_path.unlink(missing_ok=True)
                job_path, job = self._prepare_valid_files(references=())
                job.update(updates)
                identity = {
                    key: value
                    for key, value in job.items()
                    if key not in {"schema_version", "job_id"}
                }
                job["job_id"] = _sha256(
                    _canonical_compact({"contract_version": "1.0", "job": identity})
                )
                rewritten = job_path.with_name(f"{job['job_id']}.json")
                job_path.unlink()
                rewritten.write_bytes(_canonical_artifact(job))
                self.assertTrue(validate_generation_job(job))
                self.assert_failure(self._run(rewritten), "invalid-job")

    def test_request_open_obeys_total_queue_deadline(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())

        def slow_open(*args: object, **kwargs: object) -> object:
            del args, kwargs
            time.sleep(0.2)
            raise AssertionError("late opener result must be discarded")

        with (
            mock.patch.object(
                self.module.urllib.request.OpenerDirector,
                "open",
                side_effect=slow_open,
            ),
            mock.patch.object(self.module, "QUEUE_TIMEOUT_SECONDS", 0.02),
        ):
            started = time.monotonic()
            self.assert_failure(self._run(job_path), "queue-timeout")
            self.assertLess(time.monotonic() - started, 0.15)

    def test_credential_shaped_profile_metadata_fails_before_network(self) -> None:
        job_path, _ = self._prepare_valid_files(references=())
        for field, value in (
            ("profile_id", "sk-abcdefghijklmnop"),
            ("model", "sk-ABCDEFGHIJKLMNOP"),
        ):
            with self.subTest(field=field):
                profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
                profile[field] = value
                profile["profile_sha256"] = _profile_digest(profile)
                self.profile_path.write_bytes(_canonical_artifact(profile))
                self.assert_failure(self._run(job_path), "invalid-profile")
                self.assertEqual(self.server.requests, [])
                _, workflow_hash = self._write_workflow()
                self._write_profile(workflow_hash)

    def test_directory_fsync_failure_removes_owned_publication(self) -> None:
        real_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(self.module.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(self.module.ExecutorError) as raised:
                self.module._publish_output(self.output_path, PNG_BYTES)
        self.assertEqual(raised.exception.category, "output-rejected")
        self.assertFalse(self.output_path.exists())
        self.assertEqual(list(self.output_path.parent.glob(f".{self.output_path.name}.*")), [])

    def test_traversal_absolute_paths_and_symlinks_are_rejected(self) -> None:
        job_path, job = self._prepare_valid_files(references=())
        for name, path in {
            "traversal": "../prompt.txt",
            "absolute": "/tmp/prompt.txt",
            "windows": "C:/private/prompt.txt",
            "backslash": "prompts\\panel.txt",
        }.items():
            with self.subTest(name=name):
                self.output_path.unlink(missing_ok=True)
                job_path, job = self._prepare_valid_files(references=())
                job["prompt_path"] = path
                identity = {k: v for k, v in job.items() if k not in {"schema_version", "job_id"}}
                job["job_id"] = _sha256(
                    _canonical_compact({"contract_version": "1.0", "job": identity})
                )
                replacement = job_path.with_name(f"{job['job_id']}.json")
                job_path.unlink()
                replacement.write_bytes(_canonical_artifact(job))
                self.assert_failure(self._run(replacement), "invalid-job")

        job_path, job = self._prepare_valid_files(references=())
        prompt_path = self.project / str(job["prompt_path"])
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        prompt_path.unlink()
        try:
            prompt_path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        self.assert_failure(self._run(job_path), "invalid-job-input")

    def test_schema_and_example_profile_are_strict_and_self_consistent(self) -> None:
        schema = json.loads(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        example = json.loads(EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"],
            "https://github.com/wenn-id/comicsol/integrations/comfyui-local/profile.schema.json",
        )
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(example["schema_version"], "1.0")
        self.assertEqual(example["profile_sha256"], _profile_digest(example))
        self.assertEqual(example["inputs"]["references"], [])
        self.assertIsNone(example["inputs"]["width"])
        self.assertIsNone(example["inputs"]["height"])


if __name__ == "__main__":
    unittest.main()
