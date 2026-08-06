import asyncio
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tests.support import bounded_tail_regions, make_symlink  # noqa: E402
from page_quality import build_page_quality_record, write_page_quality_record  # noqa: E402

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

if MCP_AVAILABLE:
    import mcp_server  # noqa: E402
    from mcp import ClientSession, StdioServerParameters  # noqa: E402
    from mcp.client.stdio import stdio_client  # noqa: E402
    from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402


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
                project, page_number, valid_page_reviewer_checks(project, page_number)
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

    def test_root_must_be_absolute(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            mcp_server._configure_root(Path("relative-output"))

    def test_project_id_rejects_absolute_traversal_and_sibling_prefix(self):
        for project_id in ("/tmp/outside", "../outside", "project/child", "C:\\outside"):
            with self.subTest(project_id=project_id):
                with self.assertRaisesRegex(ToolError, "invalid project ID"):
                    mcp_server._resolve_project(project_id)

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

    def test_init_rejects_oversized_utf8_before_project_allocation(self):
        before = list(self.root.iterdir())
        with self.assertRaisesRegex(ToolError, "at most 200 KiB"):
            mcp_server.comic_init("Too Large", "a" * (200 * 1024 + 1), {})
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

    def test_tool_errors_never_leak_local_paths_or_sensitive_values(self):
        project = self.root / "project"
        project.mkdir()
        with self.assertRaisesRegex(ToolError, "invalid project ID"):
            mcp_server.comic_transition("C:\\\\Users\\\\secret-user", "PLANNED")
        with self.assertRaisesRegex(ToolError, "sensitive request setting") as context:
            mcp_server.comic_init("Story", "source", {"api_key": "do-not-leak"})
        self.assertNotIn("do-not-leak", str(context.exception))
        with self.assertRaisesRegex(ToolError, "unknown validation stage"):
            mcp_server.comic_validate("project", "../secret")
        with self.assertRaisesRegex(ToolError, "relative project path"):
            mcp_server.comic_record_attempt("project", "p01-01", "initial", "C:escape.png")
        self.assertEqual(
            "<path>",
            mcp_server._safe_message(FileNotFoundError(str(self.root / "secret-file"))),
        )
        self.assertNotIn(str(self.root), mcp_server._safe_message(FileNotFoundError(str(self.root / "secret-file"))))
        for quote, path in (
            ("'", "/tmp/Comic Sol/private payload.json"),
            ('"', r"C:\\\\Comic Sol\\\\private payload.json"),
        ):
            with self.subTest(quote=quote, path=path):
                message = mcp_server._safe_message(RuntimeError(f"provider failed at {quote}{path}{quote}"))
                self.assertEqual(f"provider failed at {quote}<path>{quote}", message)
                self.assertNotIn(path, message)
        for raw, expected in (
            ("provider returned api_key=sk-live-1234", "provider returned api_key=<redacted>"),
            ("config value sk-live-1234 is invalid", "config value sk-live-1234 is invalid"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(expected, mcp_server._safe_message(RuntimeError(raw)))

    def test_init_rejects_non_string_request_setting_keys(self):
        before = list(self.root.iterdir())
        with self.assertRaisesRegex(ToolError, "keys must be strings"):
            mcp_server.comic_init("Story", "source", {1: "short_prompt"})
        self.assertEqual(before, list(self.root.iterdir()))

    def test_symlink_scan_is_cached_per_project_and_invalidated_on_change(self):
        project = self.root / "cached-project"
        project.mkdir()
        mcp_server._resolve_project("cached-project")
        cached = mcp_server._SYMLINK_SCAN_CACHE.get("cached-project")
        self.assertIsNotNone(cached)
        mcp_server._resolve_project("cached-project")
        self.assertIs(cached, mcp_server._SYMLINK_SCAN_CACHE.get("cached-project"))
        (project / "arbitrary.txt").write_text("changed")
        mcp_server._resolve_project("cached-project")
        self.assertIsNot(cached, mcp_server._SYMLINK_SCAN_CACHE.get("cached-project"))


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
                    self.assertFalse(health.isError)
                    self.assertTrue(health.structuredContent["healthy"])
                    created = await session.call_tool("comic_init", {
                        "title": "Installed Wire Test",
                        "source_text": "An installed protocol smoke test.",
                        "request_settings": {"language": "en", "mode": "short_prompt"},
                    })
                    self.assertFalse(created.isError)
                    project_id = created.structuredContent["result"]
                    status = await session.call_tool("comic_status", {"project_id": project_id})
                    self.assertFalse(status.isError)
                    self.assertEqual("INIT", status.structuredContent["status"])


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_protocol_exercises_all_tools(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            output_root.mkdir()
            sample = ROOT / "samples/sunlight-courier"
            project = output_root / "sunlight-courier"
            shutil.copytree(sample, project)
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
                        self.assertEqual(error, result.isError, name)
                        if error:
                            self.assertIsNone(result.structuredContent)
                        else:
                            self.assertIsNotNone(result.structuredContent)
                        return result.structuredContent

                    health = await call("comic_doctor")
                    self.assertTrue(health["healthy"])
                    self.assertTrue(any("PASS Python 3.11" in item for item in health["messages"]))

                    created = await call("comic_init", {
                        "title": "Wire Test",
                        "source_text": "A protocol smoke test.",
                        "request_settings": {"language": "en", "mode": "short_prompt"},
                    })
                    self.assertEqual("wire-test", created["result"])
                    status = await call("comic_status", {"project_id": "wire-test"})
                    self.assertEqual("INIT", status["status"])
                    transitioned = await call("comic_transition", {
                        "project_id": "wire-test", "target": "PLANNED",
                    })
                    self.assertEqual("PLANNED", transitioned["status"])

                    validated = await call("comic_validate", {
                        "project_id": "sunlight-courier", "stage": "panels",
                    })
                    self.assertEqual([], validated["result"])
                    resume = await call("comic_resume_plan", {
                        "project_id": "sunlight-courier",
                    })
                    self.assertGreaterEqual(len(resume["result"]), 6)
                    recorded = await call("comic_record_stage", {
                        "project_id": "sunlight-courier", "stage": "planning",
                    })
                    self.assertEqual("planning", recorded["stage"])
                    counters = await call("comic_record_attempt", {
                        "project_id": "sunlight-courier",
                        "panel_id": "p01-01",
                        "kind": "initial",
                        "relative_path": "panels/raw/p01-01-attempt.png",
                    })
                    self.assertEqual(1, counters["initial"])
                    promoted = await call("comic_promote_attempt", {
                        "project_id": "sunlight-courier",
                        "panel_id": "p01-01",
                        "relative_path": "panels/raw/p01-01-attempt.png",
                    })
                    self.assertEqual("panels/raw/p01-01.png", promoted["result"])
                    await call("comic_override_panel", {
                        "project_id": "sunlight-courier",
                        "panel_id": "p01-01",
                        "reason": "protocol smoke test",
                    }, error=True)
                    lettered = await call("comic_letter", {
                        "project_id": "sunlight-courier",
                    })
                    self.assertEqual(4, len(lettered["result"]))
                    pages = await call("comic_compose", {
                        "project_id": "sunlight-courier",
                    })
                    self.assertEqual(2, len(pages["result"]))
                    write_current_page_qa_records(project, (1, 2))
                    exported = await call("comic_export", {
                        "project_id": "sunlight-courier",
                    })
                    self.assertEqual("exports/sunlight-courier.pdf", exported["result"])
                    invalidated = await call("comic_invalidate", {
                        "project_id": "sunlight-courier", "stage": "export",
                    })
                    self.assertIn("qa_report", invalidated["result"])

    async def test_finalize_lifecycle_produces_terminal_artifacts(self):
        """Full deterministic finalize: lettering → composition → page-QA → export → report → transition."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            output_root.mkdir()
            sample = ROOT / "samples/sunlight-courier"
            project = output_root / "sunlight-courier"
            shutil.copytree(sample, project)

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
                    blocked = await session.call_tool("comic_finalize", {"project_id": "sunlight-courier"})
                    self.assertTrue(blocked.isError, "comic_finalize must gate on page QA")
                    write_page_qa_records()

                    result = await session.call_tool("comic_finalize", {"project_id": "sunlight-courier"})
                    self.assertFalse(result.isError, "comic_finalize")
                    content: Any = result.structuredContent
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
                    validated = await session.call_tool("comic_validate", {
                        "project_id": "sunlight-courier", "stage": "final",
                    })
                    self.assertFalse(validated.isError)
                    self.assertEqual([], validated.structuredContent["result"])

                    # Terminal status matches warning state (no warnings → COMPLETE).
                    status = await session.call_tool("comic_status", {"project_id": "sunlight-courier"})
                    self.assertEqual("COMPLETE", status.structuredContent["status"])


if __name__ == "__main__":
    unittest.main()
