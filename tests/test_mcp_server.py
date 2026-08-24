import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

from tests.support import bounded_tail_regions, make_symlink  # noqa: E402
from scripts.materialize_sample import materialize_sample  # noqa: E402
from scripts.comic_sol import atomic_write_json, read_json  # noqa: E402
from scripts.page_quality import build_page_quality_record, write_page_quality_record  # noqa: E402

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

if MCP_AVAILABLE:
    from scripts import mcp_server  # noqa: E402
    from mcp import ClientSession, StdioServerParameters  # noqa: E402
    from mcp.client.stdio import stdio_client  # noqa: E402

    try:
        from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
    except ModuleNotFoundError:
        from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


TOOL_NAMES = {
    "comic_doctor",
    "comic_init",
    "comic_status",
    "comic_transition",
    "comic_validate",
    "comic_resume_plan",
    "comic_resume",
    "comic_invalidate",
    "comic_record_stage",
    "comic_record_attempt",
    "comic_promote_attempt",
    "comic_override_panel",
    "comic_letter",
    "comic_compose",
    "comic_render_report",
    "comic_export",
    "comic_finalize",
}


def copy_sample(destination: Path) -> None:
    shutil.copytree(ROOT / "samples/sunlight-courier", destination)
    materialize_sample(destination)


def result_is_error(result: Any) -> bool:
    """Read MCP tool errors across SDK 1.x and 2.x result models."""
    return bool(getattr(result, "isError", getattr(result, "is_error", False)))


def structured_content(result: Any) -> Any:
    """Read structured MCP content across SDK 1.x and 2.x result models."""
    return getattr(result, "structuredContent", getattr(result, "structured_content", None))


def tool_error_payload(error: Exception) -> dict[str, Any]:
    """Decode the canonical JSON error envelope carried by ToolError."""
    return json.loads(str(error))


def tool_error_legacy(error: Exception) -> str:
    """Read the backward-compatible human/error-category rendering."""
    return tool_error_payload(error)["legacy_message"]


def valid_page_reviewer_checks(project: Path, page_number: int):
    return [
        {
            "id": "face-action-obstruction",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected every panel region for face and action obstruction.",
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "regions": [{"scope": "all-panels"}],
        },
        {
            "id": "bubble-tail-direction",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected every bubble tail against its intended speaker.",
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "regions": bounded_tail_regions(project, page_number),
        },
        {
            "id": "accidental-text-watermark",
            "result": "pass",
            "severity": "error",
            "evidence": "Reviewer inspected the full page for accidental text and watermarks.",
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "regions": [{"scope": "page"}],
        },
    ]


def write_current_page_qa_records(project: Path, page_numbers):
    for page_number in page_numbers:
        write_page_quality_record(
            project,
            page_number,
            build_page_quality_record(
                project,
                page_number,
                valid_page_reviewer_checks(project, page_number),
                reviewer="fixture-reviewer",
                reviewed_at="2026-08-14T01:02:03Z",
            ),
        )


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class McpServerUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "output"
        mcp_server._configure_root(self.root.absolute())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_doctor_forwards_image_capability_with_cli_parity(self):
        capability = {
            "status": "available",
            "name": "agent-image-generation",
            "supports_reference_images": True,
            "supports_dimensions": False,
        }
        result = mcp_server.comic_doctor(image_capability=capability)

        check = next(check for check in result["checks"] if check["id"] == "image-capability")
        self.assertEqual("warn", check["status"])
        self.assertEqual(
            {"readiness": "partial", "capability": capability},
            check["details"],
        )
        self.assertTrue(result["ready"])
        self.assertTrue(result["healthy"])

    def test_root_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            mcp_server._configure_root(Path("relative-output"))

    def test_project_id_rejects_absolute_traversal_and_sibling_prefix(self):
        for project_id in ("/tmp/outside", "../outside", "project/child", "C:\\outside"):
            with self.subTest(project_id=project_id):
                with self.assertRaisesRegex(ToolError, "invalid project ID"):
                    mcp_server._resolve_project(project_id)

    def test_missing_or_uninitialized_project_is_rejected(self):
        with self.assertRaisesRegex(ToolError, "project directory is not an initialized"):
            mcp_server._resolve_project("missing")
        (self.root / "file-project").write_text("not a project", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "project directory is not an initialized"):
            mcp_server._resolve_project("file-project")
        (self.root / "empty-project").mkdir()
        with self.assertRaisesRegex(ToolError, "project directory is not an initialized"):
            mcp_server._resolve_project("empty-project")

    def test_project_symlink_cannot_escape_root(self):
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        make_symlink(self, self.root / "escape", outside, directory=True)

        with self.assertRaisesRegex(ToolError, "outside output root"):
            mcp_server._resolve_project("escape")

    def test_file_symlink_cannot_escape_project(self):
        project = self.root / "project"
        project.mkdir()
        outside = Path(self.temporary_directory.name) / "secret.txt"
        outside.write_text("outside output root")
        make_symlink(self, project / "arbitrary-file.txt", outside)

        with self.assertRaisesRegex(ToolError, "symlink"):
            mcp_server._resolve_project("project")

    def test_exposes_exact_approved_tool_surface(self):
        tools = asyncio.run(mcp_server.mcp.list_tools())
        self.assertEqual(TOOL_NAMES, {tool.name for tool in tools})
        doctor_tool = next(tool for tool in tools if tool.name == "comic_doctor")
        schema = getattr(doctor_tool, "inputSchema", None)
        if schema is None:
            schema = doctor_tool.input_schema
        capability_schema = next(
            option
            for option in schema["properties"]["image_capability"]["anyOf"]
            if option.get("type") == "object"
        )
        self.assertFalse(capability_schema["additionalProperties"])
        self.assertEqual(
            {
                "status",
                "name",
                "supports_reference_images",
                "supports_dimensions",
            },
            set(capability_schema["properties"]),
        )

    def test_status_routes_through_locked_recovery_without_changing_response(self):
        project = self.root / "project"
        project.mkdir()
        (project / "project.json").write_text("{}\n", encoding="utf-8")
        expected = {"project_id": "project", "status": "BLOCKED"}

        with mock.patch.object(
            mcp_server,
            "read_project_status",
            return_value=expected,
        ) as status:
            self.assertIs(expected, mcp_server.comic_status("project"))

        status.assert_called_once_with(project.resolve())

    def test_init_rejects_oversized_utf8_before_project_allocation(self):
        before = list(self.root.iterdir())
        with self.assertRaisesRegex(ToolError, "at most 200 KiB"):
            mcp_server.comic_init("Too Large", "a" * (200 * 1024 + 1), {})
        self.assertEqual(before, list(self.root.iterdir()))

    def test_init_accepts_page_scope_without_an_interactive_flow(self):
        project_id = mcp_server.comic_init(
            "Scoped Story",
            "A story with a selected scope.",
            {"language": "en", "mode": "short_prompt"},
            page_count=3,
        )
        manifest = read_json(self.root / project_id / "project.json")
        self.assertEqual(3, manifest["settings"]["page_count"])

    def test_init_rejects_invalid_page_scope_before_project_allocation(self):
        before = list(self.root.iterdir())
        for page_count in (0, 5, True):
            with self.subTest(page_count=page_count):
                with self.assertRaisesRegex(ToolError, "page count"):
                    mcp_server.comic_init(
                        "Rejected Scope",
                        "Story",
                        {"language": "en", "mode": "short_prompt"},
                        page_count=page_count,
                    )
                self.assertEqual(before, list(self.root.iterdir()))

    def test_attempt_tools_reject_same_drive_relative_path_as_cli(self):
        project = self.root / "project"
        project.mkdir()
        for tool, arguments in (
            (mcp_server.comic_record_attempt, ("project", "p01-01", "initial", "C:outside.png")),
            (mcp_server.comic_promote_attempt, ("project", "p01-01", "C:outside.png")),
        ):
            with self.subTest(tool=tool.__name__):
                with self.assertRaisesRegex(ToolError, "relative project path"):
                    tool(*arguments)

    def test_relative_path_validation_rejects_repetition_without_backtracking(self):
        with self.assertRaisesRegex(ToolError, "relative project path"):
            mcp_server._validate_relative_path("-/" * 30 + "!")

    def test_tool_request_validation_rejects_unsafe_inputs(self):
        project = self.root / "project"
        project.mkdir()
        with self.assertRaisesRegex(ToolError, "invalid project ID"):
            mcp_server.comic_transition("C:\\\\Users\\\\secret-user", "PLANNED")
        with self.assertRaisesRegex(ToolError, "unknown validation stage"):
            mcp_server.comic_validate("project", "../secret")
        with self.assertRaisesRegex(ToolError, "relative project path"):
            mcp_server.comic_record_attempt("project", "p01-01", "initial", "C:escape.png")

    def test_tool_error_maps_exception_categories_to_safe_messages(self):
        cases = (
            (
                FileNotFoundError("password=file-value"),
                "not-found: required project data was not found",
            ),
            (
                PermissionError("password=permission-value"),
                "permission-denied: project data could not be accessed",
            ),
            (OSError("password=os-value"), "io-error: project data operation failed"),
            (
                UnicodeError("password=unicode-value"),
                "invalid-data: project data encoding is invalid",
            ),
            (
                ValueError("password=value-error"),
                "invalid-data: tool request or project data is invalid",
            ),
            (
                TypeError("password=type-error"),
                "invalid-data: tool request or project data is invalid",
            ),
            (RuntimeError("password=runtime-value"), "internal-error: tool operation failed"),
        )
        for error, expected in cases:
            with self.subTest(error_type=type(error).__name__):
                converted = tool_error_payload(mcp_server._tool_error(error))
                self.assertEqual(expected.split(":", 1)[0], converted["category"])
                self.assertRegex(converted["code"], r"^CS-[A-Z]+-[0-9]{3}$")
                self.assertIn("reason", converted)
                self.assertIn("recovery", converted)
                self.assertEqual(expected, converted["legacy_message"])
                self.assertNotIn("password=", json.dumps(converted))

    def test_tool_errors_never_leak_credentials_or_paths(self):
        cases = (
            ('password="top secret"', ("top secret",)),
            ("PASSWORD='mixed case value'", ("mixed case value",)),
            ('{"outer":{"Api_Key":"nested json value"}}', ("nested json value",)),
            ("authorization: Bearer bearer-value", ("bearer-value",)),
            ("Bearer standalone-value", ("standalone-value",)),
            (
                "token=first-value; client_secret: second value",
                ("first-value", "second value"),
            ),
        )
        for raw, secrets in cases:
            with self.subTest(raw=raw):
                converted = tool_error_payload(mcp_server._tool_error(RuntimeError(raw)))
                self.assertEqual("internal-error", converted["category"])
                for secret in secrets:
                    self.assertNotIn(secret, json.dumps(converted))

        raw_path = str(self.root / "private payload.json")
        converted = tool_error_payload(mcp_server._tool_error(FileNotFoundError(raw_path)))
        self.assertEqual("not-found", converted["category"])
        self.assertNotIn(raw_path, json.dumps(converted))

    def test_init_uses_only_allowlisted_request_error_messages(self):
        before = list(self.root.iterdir())
        with self.assertRaises(ToolError) as context:
            mcp_server.comic_init("Story", "source", {"api_key": "do-not-leak"})
        payload = tool_error_payload(context.exception)
        self.assertEqual("CS-MCP-001", payload["code"])
        self.assertEqual("invalid-request", payload["category"])
        self.assertEqual(
            "invalid-request: sensitive request setting is not allowed",
            payload["legacy_message"],
        )
        self.assertNotIn("api_key", json.dumps(payload))
        self.assertNotIn("do-not-leak", json.dumps(payload))

        with self.assertRaises(ToolError) as context:
            mcp_server.comic_init("Story", "source", {1: "short_prompt"})
        self.assertEqual(
            "invalid-request: request setting keys must be strings",
            tool_error_legacy(context.exception),
        )
        self.assertEqual(before, list(self.root.iterdir()))

    def test_invalidate_rejects_blocked_project_without_mutating_manifest(self):
        project_id = mcp_server.comic_init(
            "Blocked project", "A story", {"mode": "short_prompt", "language": "en"}
        )
        project = self.root / project_id
        manifest = read_json(project / "project.json")
        manifest.update(
            {
                "status": "BLOCKED",
                "blocked_from": "STORYBOARDED",
                "blocked_reason": "image-capability-unavailable",
            }
        )
        atomic_write_json(project / "project.json", manifest)

        with self.assertRaisesRegex(
            ToolError, "invalid-data: tool request or project data is invalid"
        ):
            mcp_server.comic_invalidate(project_id, "generation")

        persisted = read_json(project / "project.json")
        self.assertEqual("BLOCKED", persisted["status"])
        self.assertEqual("STORYBOARDED", persisted["blocked_from"])
        self.assertEqual("image-capability-unavailable", persisted["blocked_reason"])

        project = self.root / "nested-project"
        nested = project / "existing-child"
        nested.mkdir(parents=True)
        (project / "project.json").write_text("{}", encoding="utf-8")
        mcp_server._resolve_project("nested-project")
        outside = Path(self.temporary_directory.name) / "nested-secret.txt"
        outside.write_text("secret")
        make_symlink(self, nested / "late-link", outside)
        with self.assertRaisesRegex(ToolError, "symlink"):
            mcp_server._resolve_project("nested-project")

    @unittest.skipIf(sys.platform == "win32", "Windows requires a fresh symlink scan")
    def test_symlink_scan_is_cached_per_project_and_invalidated_on_change(self):
        project = self.root / "cached-project"
        project.mkdir()
        project = project.resolve()
        (project / "project.json").write_text("{}", encoding="utf-8")
        mcp_server._resolve_project("cached-project")
        cached = mcp_server._SYMLINK_SCAN_CACHE.get("cached-project")
        self.assertIsNotNone(cached)
        mcp_server._resolve_project("cached-project")
        self.assertIs(cached, mcp_server._SYMLINK_SCAN_CACHE.get("cached-project"))
        cached_root_identity = cached[1]["."][:6]
        real_lstat = Path.lstat
        (project / "arbitrary.txt").write_text("changed")
        changed_project_metadata = SimpleNamespace(
            st_mode=project.lstat().st_mode,
            st_dev=cached_root_identity[0],
            st_ino=cached_root_identity[1],
            st_mtime_ns=cached_root_identity[2] + 1_000_000_000,
            st_ctime_ns=cached_root_identity[3],
            st_size=cached_root_identity[4],
            st_nlink=cached_root_identity[5],
        )
        changed_root_identity = (
            *cached_root_identity[:2],
            cached_root_identity[2] + 1_000_000_000,
            *cached_root_identity[3:],
        )
        self.assertEqual(
            mcp_server._directory_identity(changed_project_metadata), changed_root_identity
        )

        def deterministic_lstat(path):
            return changed_project_metadata if path == project else real_lstat(path)

        with mock.patch.object(Path, "lstat", new=deterministic_lstat):
            mcp_server._resolve_project("cached-project")
        self.assertIsNot(cached, mcp_server._SYMLINK_SCAN_CACHE.get("cached-project"))

    def test_symlink_scan_cache_is_bounded_lru(self):
        for name in ("cache-lru-a", "cache-lru-b", "cache-lru-c"):
            project = self.root / name
            project.mkdir()
            (project / "project.json").write_text("{}", encoding="utf-8")

        self.assertEqual(128, mcp_server._SYMLINK_SCAN_CACHE_MAX_ENTRIES)
        cache = mcp_server._SYMLINK_SCAN_CACHE
        previous = dict(cache)
        cache.clear()
        try:
            with mock.patch.object(mcp_server, "_SYMLINK_SCAN_CACHE_MAX_ENTRIES", 2):
                mcp_server._resolve_project("cache-lru-a")
                mcp_server._resolve_project("cache-lru-b")
                self.assertEqual(["cache-lru-a", "cache-lru-b"], list(cache))

                mcp_server._resolve_project("cache-lru-a")
                mcp_server._resolve_project("cache-lru-c")

            self.assertEqual(["cache-lru-a", "cache-lru-c"], list(cache))
            self.assertNotIn("cache-lru-b", cache)
        finally:
            cache.clear()
            cache.update(previous)

    def test_symlink_cache_concurrent_eviction_does_not_fail(self):
        projects = {}
        for name in ("cache-race-a", "cache-race-b", "cache-race-c"):
            project = self.root / name
            project.mkdir()
            (project / "project.json").write_text("{}", encoding="utf-8")
            projects[name] = project.resolve()

        entered_move = Event()
        release_move = Event()
        scanned_c = Event()
        cache_type = type(mcp_server._SYMLINK_SCAN_CACHE)

        class BlockingCache(cache_type):
            armed = False

            def move_to_end(self, key, last=True):
                if self.armed and key == "cache-race-a" and not entered_move.is_set():
                    entered_move.set()
                    if not release_move.wait(5):
                        raise AssertionError("timed out waiting to release cache hit")
                return super().move_to_end(key, last=last)

        cache = BlockingCache()
        real_scan_subtree = mcp_server._scan_subtree

        def scan_subtree(project_dir, relative, snapshots):
            if project_dir == projects["cache-race-c"]:
                scanned_c.set()
            return real_scan_subtree(project_dir, relative, snapshots)

        with (
            mock.patch.object(mcp_server, "_SYMLINK_SCAN_CACHE", cache),
            mock.patch.object(mcp_server, "_SYMLINK_SCAN_CACHE_MAX_ENTRIES", 2),
            mock.patch.object(mcp_server, "_scan_subtree", side_effect=scan_subtree),
        ):
            mcp_server._resolve_project("cache-race-a")
            mcp_server._resolve_project("cache-race-b")
            cache.armed = True

            with ThreadPoolExecutor(max_workers=2) as pool:
                cached_result = pool.submit(mcp_server._resolve_project, "cache-race-a")
                self.assertTrue(entered_move.wait(5))
                evicting_result = pool.submit(mcp_server._resolve_project, "cache-race-c")
                scanned_c.wait(2)
                release_move.set()
                self.assertEqual(projects["cache-race-a"], cached_result.result(timeout=5))
                self.assertEqual(projects["cache-race-c"], evicting_result.result(timeout=5))

    @unittest.skipIf(sys.platform == "win32", "Windows requires a fresh symlink scan")
    def test_symlink_cache_hit_avoids_directory_rescan(self):
        project = self.root / "unchanged-project"
        (project / "nested").mkdir(parents=True)
        (project / "project.json").write_text("{}", encoding="utf-8")
        expected = mcp_server._resolve_project("unchanged-project")

        with mock.patch.object(
            mcp_server.os,
            "scandir",
            side_effect=AssertionError("cache hit rescanned the project"),
        ):
            actual = mcp_server._resolve_project("unchanged-project")

        self.assertEqual(expected, actual)

    def test_windows_rescan_catches_child_symlink_when_parent_metadata_is_stale(self):
        project = self.root / "windows-cache-project"
        nested = project / "nested"
        nested.mkdir(parents=True)
        (project / "project.json").write_text("{}", encoding="utf-8")
        mcp_server._resolve_project("windows-cache-project")
        outside = Path(self.temporary_directory.name) / "windows-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        make_symlink(self, nested / "late-link", outside)

        with mock.patch.object(mcp_server.os, "name", "nt"):
            with self.assertRaisesRegex(ToolError, "symlink"):
                mcp_server._resolve_project("windows-cache-project")

    @unittest.skipIf(sys.platform == "win32", "requires POSIX symlink semantics")
    def test_symlink_created_during_scan_is_rejected(self):
        project = self.root / "racing-project"
        nested = project / "nested"
        nested.mkdir(parents=True)
        nested = nested.resolve()
        (project / "project.json").write_text("{}", encoding="utf-8")
        outside = Path(self.temporary_directory.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        real_scandir = mcp_server.os.scandir
        real_lstat = Path.lstat
        stable_nested_metadata = nested.lstat()
        changed_nested_metadata = SimpleNamespace(
            st_mode=stable_nested_metadata.st_mode,
            st_dev=stable_nested_metadata.st_dev,
            st_ino=stable_nested_metadata.st_ino,
            st_mtime_ns=stable_nested_metadata.st_mtime_ns + 1_000_000_000,
            st_ctime_ns=stable_nested_metadata.st_ctime_ns,
            st_size=stable_nested_metadata.st_size,
            st_nlink=stable_nested_metadata.st_nlink,
        )
        stable_identity = mcp_server._directory_identity(stable_nested_metadata)
        changed_identity = mcp_server._directory_identity(changed_nested_metadata)
        self.assertEqual(changed_identity[2], stable_identity[2] + 1_000_000_000)
        self.assertEqual(changed_identity[:2], stable_identity[:2])
        self.assertEqual(changed_identity[3:], stable_identity[3:])
        injected = False

        class InjectOnExhaustion:
            def __init__(self, iterator):
                self.iterator = iterator

            def __enter__(self):
                self.iterator.__enter__()
                return self

            def __exit__(self, *args):
                return self.iterator.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal injected
                try:
                    return next(self.iterator)
                except StopIteration:
                    if not injected:
                        make_symlink(self_test, nested / "late-link", outside)
                        injected = True
                    raise

        self_test = self

        def injecting_scandir(path):
            iterator = real_scandir(path)
            if Path(path) == nested and not injected:
                return InjectOnExhaustion(iterator)
            return iterator

        def deterministic_lstat(path):
            if path == nested:
                return changed_nested_metadata if injected else stable_nested_metadata
            return real_lstat(path)

        with (
            mock.patch.object(Path, "lstat", new=deterministic_lstat),
            mock.patch.object(mcp_server.os, "scandir", side_effect=injecting_scandir),
        ):
            with self.assertRaisesRegex(ToolError, "symlink|changed during validation"):
                mcp_server._resolve_project("racing-project")

    @unittest.skipIf(sys.platform == "win32", "requires POSIX symlink semantics")
    def test_disappearing_project_root_cannot_leave_trusted_empty_cache(self):
        project = self.root / "recreated-project"
        project.mkdir()
        (project / "project.json").write_text("{}", encoding="utf-8")
        resolved = mcp_server._resolve_project("recreated-project")
        real_is_dir = Path.is_dir
        removed = False

        def remove_after_root_validation(path):
            nonlocal removed
            result = real_is_dir(path)
            if path == resolved and result and not removed:
                shutil.rmtree(path)
                removed = True
            return result

        with mock.patch.object(Path, "is_dir", new=remove_after_root_validation):
            with self.assertRaises(ToolError):
                mcp_server._resolve_project("recreated-project")

        project.mkdir()
        (project / "project.json").write_text("{}", encoding="utf-8")
        outside = Path(self.temporary_directory.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        make_symlink(self, project / "late-link", outside)
        with self.assertRaisesRegex(ToolError, "symlink"):
            mcp_server._resolve_project("recreated-project")

    def test_override_tool_accepts_valid_v2_visual_failure(self):
        project = self.root / "sunlight-courier"
        copy_sample(project)
        record_path = project / "qa/panels/p01-01.json"
        record = json.loads(record_path.read_text("utf-8"))
        record["checks"][0].update({"result": "fail", "severity": "error"})
        record["decision"] = "regenerate"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = mcp_server.comic_override_panel(
            "sunlight-courier", "p01-01", "minor prop drift is acceptable"
        )

        self.assertEqual("p01-01: accepted with warnings", result)
        updated = json.loads(record_path.read_text("utf-8"))
        self.assertEqual("accept-warning", updated["decision"])
        self.assertEqual("minor prop drift is acceptable", updated["override_reason"])


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class InstalledMcpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_cli_launches_exact_tool_surface(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "comic_sol_product.cli",
                    "mcp",
                    "--root",
                    str(output_root),
                ],
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.assertEqual(TOOL_NAMES, {tool.name for tool in listed.tools})
                    health = await session.call_tool("comic_doctor", {})
                    self.assertFalse(result_is_error(health))
                    self.assertTrue(structured_content(health)["healthy"])
                    capability = {
                        "status": "available",
                        "name": "agent-image-generation",
                        "supports_reference_images": True,
                        "supports_dimensions": False,
                    }
                    observed = await session.call_tool(
                        "comic_doctor", {"image_capability": capability}
                    )
                    self.assertFalse(result_is_error(observed))
                    observed_check = next(
                        check
                        for check in structured_content(observed)["checks"]
                        if check["id"] == "image-capability"
                    )
                    self.assertEqual(
                        {"readiness": "partial", "capability": capability},
                        observed_check["details"],
                    )
                    malformed_inputs = (
                        (
                            {**capability, "credential": "super-secret-provider-token"},
                            "super-secret-provider-token",
                        ),
                        (
                            "scalar-secret-provider-token-XYZ123",
                            "scalar-secret-provider-token-XYZ123",
                        ),
                        (
                            ["list-secret-provider-token-XYZ123"],
                            "list-secret-provider-token-XYZ123",
                        ),
                    )
                    for malformed_input, secret in malformed_inputs:
                        malformed = await session.call_tool(
                            "comic_doctor", {"image_capability": malformed_input}
                        )
                        self.assertFalse(result_is_error(malformed))
                        malformed_payload = structured_content(malformed)
                        malformed_check = next(
                            check
                            for check in malformed_payload["checks"]
                            if check["id"] == "image-capability"
                        )
                        self.assertEqual("unknown", malformed_check["details"]["readiness"])
                        self.assertNotIn(secret, json.dumps(malformed_payload, ensure_ascii=False))
                        self.assertNotIn(secret, repr(malformed.content))
                    created = await session.call_tool(
                        "comic_init",
                        {
                            "title": "Installed Wire Test",
                            "source_text": "An installed protocol smoke test.",
                            "request_settings": {"language": "en", "mode": "short_prompt"},
                            "page_count": 3,
                        },
                    )
                    self.assertFalse(result_is_error(created))
                    project_id = structured_content(created)["result"]
                    status = await session.call_tool("comic_status", {"project_id": project_id})
                    self.assertFalse(result_is_error(status))
                    self.assertEqual("INIT", structured_content(status)["status"])
                    self.assertEqual(3, structured_content(status)["settings"]["page_count"])
                    for index, page_count in enumerate((True, 2.0, "2")):
                        rejected = await session.call_tool(
                            "comic_init",
                            {
                                "title": f"Rejected Wire {index}",
                                "source_text": "This project must not be created.",
                                "request_settings": {
                                    "language": "en",
                                    "mode": "short_prompt",
                                },
                                "page_count": page_count,
                            },
                        )
                        self.assertTrue(result_is_error(rejected))
                        self.assertIsNone(structured_content(rejected))
                        error_text = " ".join(
                            getattr(item, "text", "") for item in rejected.content
                        )
                        self.assertIn("CS-MCP-001", error_text)
                        self.assertFalse((output_root / f"rejected-wire-{index}").exists())


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_protocol_exercises_all_tools(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            output_root.mkdir()
            project = output_root / "sunlight-courier"
            copy_sample(project)
            attempt = project / "panels/raw/p01-01-attempt.png"
            shutil.copy2(project / "panels/raw/p01-01.png", attempt)

            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    str(ROOT / "scripts/mcp_server.py"),
                    "--root",
                    str(output_root),
                ],
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.assertEqual(TOOL_NAMES, {tool.name for tool in listed.tools})

                    async def call(
                        name: str,
                        arguments: dict[str, Any] | None = None,
                        *,
                        error: bool = False,
                    ) -> Any:
                        result = await session.call_tool(name, arguments or {})
                        self.assertEqual(
                            error, result_is_error(result), f"{name}: {result.content}"
                        )
                        if error:
                            self.assertIsNone(structured_content(result))
                        else:
                            self.assertIsNotNone(structured_content(result))
                        return structured_content(result)

                    health = await call("comic_doctor")
                    self.assertTrue(health["healthy"])
                    self.assertTrue(any("PASS Python 3.11" in item for item in health["messages"]))

                    created = await call(
                        "comic_init",
                        {
                            "title": "Wire Test",
                            "source_text": "A protocol smoke test.",
                            "request_settings": {"language": "en", "mode": "short_prompt"},
                        },
                    )
                    self.assertEqual("wire-test", created["result"])
                    status = await call("comic_status", {"project_id": "wire-test"})
                    self.assertEqual("INIT", status["status"])
                    transitioned = await call(
                        "comic_transition",
                        {
                            "project_id": "wire-test",
                            "target": "PLANNED",
                        },
                    )
                    self.assertEqual("PLANNED", transitioned["status"])

                    validated = await call(
                        "comic_validate",
                        {
                            "project_id": "sunlight-courier",
                            "stage": "panels",
                        },
                    )
                    self.assertEqual([], validated["result"])
                    resume = await call(
                        "comic_resume_plan",
                        {
                            "project_id": "sunlight-courier",
                        },
                    )
                    self.assertGreaterEqual(len(resume["result"]), 6)
                    recorded = await call(
                        "comic_record_stage",
                        {
                            "project_id": "sunlight-courier",
                            "stage": "planning",
                        },
                    )
                    self.assertEqual("planning", recorded["stage"])
                    counters = await call(
                        "comic_record_attempt",
                        {
                            "project_id": "sunlight-courier",
                            "panel_id": "p01-01",
                            "kind": "initial",
                            "relative_path": "panels/raw/p01-01-attempt.png",
                        },
                    )
                    self.assertEqual(1, counters["initial"])
                    promoted = await call(
                        "comic_promote_attempt",
                        {
                            "project_id": "sunlight-courier",
                            "panel_id": "p01-01",
                            "relative_path": "panels/raw/p01-01-attempt.png",
                        },
                    )
                    self.assertEqual("panels/raw/p01-01.png", promoted["result"])
                    await call(
                        "comic_override_panel",
                        {
                            "project_id": "sunlight-courier",
                            "panel_id": "p01-01",
                            "reason": "protocol smoke test",
                        },
                        error=True,
                    )
                    lettered = await call(
                        "comic_letter",
                        {
                            "project_id": "sunlight-courier",
                        },
                    )
                    self.assertEqual(4, len(lettered["result"]))
                    pages = await call(
                        "comic_compose",
                        {
                            "project_id": "sunlight-courier",
                        },
                    )
                    self.assertEqual(2, len(pages["result"]))
                    write_current_page_qa_records(project, (1, 2))
                    exported = await call(
                        "comic_export",
                        {
                            "project_id": "sunlight-courier",
                        },
                    )
                    self.assertEqual("exports/sunlight-courier.pdf", exported["result"])
                    invalidated = await call(
                        "comic_invalidate",
                        {
                            "project_id": "sunlight-courier",
                            "stage": "export",
                        },
                    )
                    self.assertIn("qa_report", invalidated["result"])

    async def test_finalize_lifecycle_produces_terminal_artifacts(self):
        """Full deterministic finalize: lettering → composition → page-QA → export → report → transition."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            output_root.mkdir()
            project = output_root / "sunlight-courier"
            copy_sample(project)

            # Downgrade to pre-lettering state and remove terminal artifacts.
            manifest_path = project / "project.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["status"] = "QA_READY"
            manifest["warnings"] = []
            for key in ("pdf", "qa_report", "composition_cache"):
                manifest["artifacts"].pop(key, None)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
            for rel in ("exports/sunlight-courier.pdf", "qa/report.md", "cache/composition.json"):
                p = project / rel
                if p.is_file():
                    p.unlink()

            def write_page_qa_records():
                """Record review of the pages as they currently exist on disk."""
                write_current_page_qa_records(project, (1, 2))

            parameters = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "scripts/mcp_server.py"), "--root", str(output_root)],
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    # The documented two-pass flow: the first call letters and
                    # composes, then fails closed on the page-QA gate. Hashing
                    # the committed sample pages up front would not work, since
                    # recomposition re-encodes pixel-identical PNGs whose bytes
                    # differ by platform zlib.
                    blocked = await session.call_tool(
                        "comic_finalize", {"project_id": "sunlight-courier"}
                    )
                    self.assertTrue(result_is_error(blocked), "comic_finalize must gate on page QA")
                    write_page_qa_records()

                    result = await session.call_tool(
                        "comic_finalize", {"project_id": "sunlight-courier"}
                    )
                    self.assertFalse(result_is_error(result), "comic_finalize")
                    content: Any = structured_content(result)
                    self.assertEqual("COMPLETE", content["status"])
                    self.assertEqual("exports/sunlight-courier.pdf", content["pdf"])
                    self.assertEqual("qa/report.md", content["report"])

                    # PDF exists and is readable.
                    pdf_path = project / "exports/sunlight-courier.pdf"
                    self.assertTrue(pdf_path.is_file())
                    self.assertGreater(pdf_path.stat().st_size, 0)

                    # Report exists.
                    self.assertTrue((project / "qa/report.md").is_file())

                    # Export cache exists.
                    self.assertTrue((project / "cache/composition.json").is_file())

                    # Final validator returns no issues.
                    validated = await session.call_tool(
                        "comic_validate",
                        {
                            "project_id": "sunlight-courier",
                            "stage": "final",
                        },
                    )
                    self.assertFalse(result_is_error(validated))
                    self.assertEqual([], structured_content(validated)["result"])

                    # Terminal status matches warning state (no warnings → COMPLETE).
                    status = await session.call_tool(
                        "comic_status", {"project_id": "sunlight-courier"}
                    )
                    self.assertEqual("COMPLETE", structured_content(status)["status"])


if __name__ == "__main__":
    unittest.main()
