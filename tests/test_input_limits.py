"""Resource-limit hardening for untrusted project inputs (issue #207).

Covers the bounded JSON parser, the bounded no-follow readers, the encoded
raster ceiling, persisted narrative-field limits, the CS-SEC-002 structured
error, and CLI/MCP parity for every new rejection.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from comic_sol_product.errors import (
    ERROR_DEFINITIONS,
    classify_exception,
    error_payload,
)

from scripts import comic_sol
from scripts.input_limits import (
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_ENTRIES,
    MAX_JSON_STRING_CHARS,
    MAX_OVERRIDE_REASON_CHARS,
    MAX_TITLE_CHARS,
    MAX_WARNING_CHARS,
    InputResourceLimitError,
    loads_bounded_json,
    validate_narrative,
)
from scripts.project_io import (
    MAX_READ_BYTES,
    read_bytes_nofollow,
    read_contained_bytes,
    read_contained_json,
    read_json_nofollow,
)
from scripts.raster_limits import MAX_DECODED_PIXELS, MAX_ENCODED_RASTER_BYTES
from scripts.validate_project import validate_manifest, validate_panel_record

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    from scripts import mcp_server
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ModuleNotFoundError:
        from mcp.server.mcpserver.exceptions import ToolError


def bomb_png_bytes(width: int, height: int) -> bytes:
    """Build a tiny PNG whose header claims a huge canvas.

    Pillow parses the header lazily, so a 45-byte file can demand a
    gigapixel decode; the size checks must reject it before any pixel
    buffer is allocated.
    """
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@contextmanager
def temporary_directory():
    with tempfile.TemporaryDirectory() as name:
        yield Path(name)


class BoundedJsonParserTests(unittest.TestCase):
    """Every documented JSON bound rejects at the boundary, not past it."""

    def test_valid_document_with_realistic_nesting_passes(self):
        document = {"pages": [{"panels": [{"id": "p01-01", "text": ["a", "b"]}]}]}
        self.assertEqual(loads_bounded_json(json.dumps(document)), document)

    def test_payload_at_exactly_the_byte_limit_passes(self):
        payload = b'{"a":1' + b" " * (MAX_JSON_BYTES - 7) + b"}"
        self.assertEqual(len(payload), MAX_JSON_BYTES)
        self.assertEqual(loads_bounded_json(payload, source="test"), {"a": 1})

    def test_payload_one_byte_over_the_limit_is_rejected(self):
        payload = b'{"a":1' + b" " * (MAX_JSON_BYTES - 6) + b"}"
        self.assertEqual(len(payload), MAX_JSON_BYTES + 1)
        with self.assertRaises(InputResourceLimitError) as raised:
            loads_bounded_json(payload, source="test")
        self.assertIn("JSON size limit", str(raised.exception))

    def test_string_payload_is_measured_as_encoded_bytes(self):
        payload = '"' + "ä" * (MAX_JSON_BYTES // 2) + '"'
        self.assertGreater(len(payload.encode("utf-8")), MAX_JSON_BYTES)
        with self.assertRaises(InputResourceLimitError):
            loads_bounded_json(payload, source="test")

    def test_nesting_at_exactly_the_depth_limit_passes(self):
        payload = "[" * MAX_JSON_DEPTH + "1" + "]" * MAX_JSON_DEPTH
        self.assertIsInstance(loads_bounded_json(payload, source="test"), list)

    def test_deep_nesting_is_rejected_before_parser_recursion(self):
        payload = b"[" * (MAX_JSON_DEPTH + 1) + b"1" + b"]" * (MAX_JSON_DEPTH + 1)
        with self.assertRaises(InputResourceLimitError) as raised:
            loads_bounded_json(payload, source="test")
        self.assertIn("nesting depth limit", str(raised.exception))

    def test_ten_thousand_deep_nesting_is_rejected_without_recursion_error(self):
        payload = b"[" * 10000 + b"]" * 10000
        try:
            loads_bounded_json(payload, source="test")
        except InputResourceLimitError as error:
            self.assertIn("nesting depth limit", str(error))
        except RecursionError:  # pragma: no cover - failure mode under test
            self.fail("deep nesting must fail as a structured limit, not RecursionError")

    def test_brackets_inside_strings_do_not_count_as_nesting(self):
        document = '{"prose": "' + "[" * 200 + ' " , "n": 1}'
        value = loads_bounded_json(document, source="test")
        self.assertEqual(value["n"], 1)

    def test_list_at_exactly_the_entry_limit_passes(self):
        payload = json.dumps(list(range(MAX_JSON_ENTRIES)))
        self.assertIsInstance(loads_bounded_json(payload, source="test"), list)

    def test_oversized_list_is_rejected(self):
        payload = json.dumps(list(range(MAX_JSON_ENTRIES + 1)))
        with self.assertRaises(InputResourceLimitError) as raised:
            loads_bounded_json(payload, source="test")
        self.assertIn("collection size limit", str(raised.exception))

    def test_oversized_object_is_rejected_while_parsing(self):
        payload = json.dumps({f"k{index}": index for index in range(MAX_JSON_ENTRIES + 1)})
        with self.assertRaises(InputResourceLimitError) as raised:
            loads_bounded_json(payload, source="test")
        self.assertIn("collection size limit", str(raised.exception))

    def test_string_at_exactly_the_length_limit_passes(self):
        payload = json.dumps("a" * MAX_JSON_STRING_CHARS)
        self.assertIsInstance(loads_bounded_json(payload, source="test"), str)

    def test_oversized_string_is_rejected(self):
        payload = json.dumps("a" * (MAX_JSON_STRING_CHARS + 1))
        with self.assertRaises(InputResourceLimitError) as raised:
            loads_bounded_json(payload, source="test")
        self.assertIn("string length limit", str(raised.exception))

    def test_malformed_json_still_raises_json_decode_error(self):
        with self.assertRaises(json.JSONDecodeError):
            loads_bounded_json(b"{not json}", source="test")


class BoundedReaderTests(unittest.TestCase):
    """The no-follow readers enforce a byte ceiling before reading."""

    def test_read_contained_bytes_at_the_cap_boundary(self):
        with temporary_directory() as root:
            project = root / "project"
            project.mkdir()
            (project / "small.bin").write_bytes(b"x" * 16)
            self.assertEqual(
                len(read_contained_bytes(project, "small.bin", max_bytes=16)), 16
            )
            with self.assertRaises(InputResourceLimitError):
                read_contained_bytes(project, "small.bin", max_bytes=15)

    def test_default_read_cap_is_the_documented_raster_ceiling(self):
        self.assertEqual(MAX_READ_BYTES, MAX_ENCODED_RASTER_BYTES)
        self.assertEqual(MAX_ENCODED_RASTER_BYTES, 128 * 1024 * 1024)

    def test_oversized_file_is_refused_before_reading_via_default_json_cap(self):
        with temporary_directory() as root:
            document = root / "huge.json"
            document.write_bytes(b'{"padding": "' + b"a" * MAX_JSON_BYTES + b'"}')
            self.assertGreater(document.stat().st_size, MAX_JSON_BYTES)
            with self.assertRaises(InputResourceLimitError):
                read_json_nofollow(document)

    def test_read_json_nofollow_returns_objects_and_rejects_arrays(self):
        with temporary_directory() as root:
            document = root / "object.json"
            document.write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(read_json_nofollow(document), {"ok": True})
            array = root / "array.json"
            array.write_text("[1]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                read_json_nofollow(array)

    def test_read_contained_json_rejects_deep_nesting(self):
        with temporary_directory() as root:
            project = root / "project"
            project.mkdir()
            (project / "deep.json").write_bytes(
                b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)
            )
            with self.assertRaises(InputResourceLimitError):
                read_contained_json(project, "deep.json")

    def test_read_bytes_nofollow_enforces_cap(self):
        with temporary_directory() as root:
            document = root / "data.bin"
            document.write_bytes(b"y" * 32)
            self.assertEqual(len(read_bytes_nofollow(document, max_bytes=32)), 32)
            with self.assertRaises(InputResourceLimitError):
                read_bytes_nofollow(document, max_bytes=31)


class RasterEncodedLimitTests(unittest.TestCase):
    """Encoded bytes are bounded before decode; decoded pixels stay bounded."""

    def test_payload_over_the_encoded_ceiling_is_rejected_before_decode(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_ENCODED_RASTER_BYTES + 1)
        with self.assertRaises(InputResourceLimitError) as raised:
            comic_sol._verify_raster_payload(payload, "image/png", 512, 512)
        self.assertIn("encoded raster size limit", str(raised.exception))

    def test_file_over_the_encoded_ceiling_is_rejected_before_decode(self):
        with temporary_directory() as root:
            raster = root / "huge.png"
            raster.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 + 1))
            with mock.patch.object(
                comic_sol, "MAX_ENCODED_RASTER_BYTES", 1024
            ):
                with self.assertRaises(InputResourceLimitError) as raised:
                    comic_sol._verify_raster(raster)
            self.assertIn("encoded raster size limit", str(raised.exception))

    def test_decompression_bomb_header_is_rejected_by_the_pixel_limit(self):
        # 8000x8000 is under Pillow's own bomb threshold but over the
        # documented Comic Sol decoded-pixel ceiling, so the rejection and
        # its message are ours and deterministic.
        width = height = 8000
        self.assertLess(width * height, 89_478_485)
        self.assertGreater(width * height, MAX_DECODED_PIXELS)
        with self.assertRaisesRegex(ValueError, "decoded pixel limit"):
            comic_sol._verify_raster_payload(
                bomb_png_bytes(width, height), "image/png", width, height
            )

    def test_file_backed_decompression_bomb_is_rejected(self):
        with temporary_directory() as root:
            raster = root / "bomb.png"
            raster.write_bytes(bomb_png_bytes(8000, 8000))
            with self.assertRaisesRegex(ValueError, "decoded pixel limit"):
                comic_sol._verify_raster(raster)


class NarrativeFieldTests(unittest.TestCase):
    """Central limits and secret hygiene for persisted narrative fields."""

    def test_limit_constants_match_the_documented_contract(self):
        self.assertEqual(MAX_TITLE_CHARS, 200)
        self.assertEqual(MAX_WARNING_CHARS, 500)
        self.assertEqual(MAX_OVERRIDE_REASON_CHARS, 1000)

    def test_title_at_the_boundary(self):
        self.assertEqual(
            len(validate_narrative("T" * 200, message="title", max_chars=200)), 200
        )
        with self.assertRaises(ValueError):
            validate_narrative("T" * 201, message="title", max_chars=200)

    def test_warning_and_reason_boundaries(self):
        validate_narrative("w" * MAX_WARNING_CHARS, message="w", max_chars=MAX_WARNING_CHARS)
        with self.assertRaises(ValueError):
            validate_narrative("w" * (MAX_WARNING_CHARS + 1), message="w", max_chars=MAX_WARNING_CHARS)
        validate_narrative(
            "r" * MAX_OVERRIDE_REASON_CHARS,
            message="r",
            max_chars=MAX_OVERRIDE_REASON_CHARS,
        )
        with self.assertRaises(ValueError):
            validate_narrative(
                "r" * (MAX_OVERRIDE_REASON_CHARS + 1),
                message="r",
                max_chars=MAX_OVERRIDE_REASON_CHARS,
            )

    def test_obvious_secrets_are_rejected(self):
        for secret in (
            "api_key=abc123",
            "password: hunter2",
            "-----BEGIN RSA PRIVATE KEY-----",
            "token sk-abcdefghijklmnopqrst",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "a" * 30,
        ):
            with self.subTest(secret=secret[:24]):
                with self.assertRaisesRegex(ValueError, "narrative field"):
                    validate_narrative(f"note {secret}", message="note", max_chars=100)

    def test_benign_narrative_prose_is_not_rejected(self):
        for prose in (
            "The gatekeeper forgot the password to the bridge.",
            "Reviewer accepted the motion blur as an artistic choice.",
            "secret agent storyline approved for teen rating",
        ):
            self.assertEqual(
                validate_narrative(prose, message="note", max_chars=500), prose
            )


class EngineNarrativeSurfaceTests(unittest.TestCase):
    """The engine write paths enforce the narrative limits themselves."""

    def test_init_project_rejects_oversized_title(self):
        with temporary_directory() as root:
            with self.assertRaisesRegex(
                ValueError, "title must be a non-empty string of at most 200"
            ):
                comic_sol.init_project(
                    root, "T" * 201, b"source", {"mode": "short_prompt"}
                )

    def test_init_project_rejects_secret_title(self):
        with temporary_directory() as root:
            with self.assertRaisesRegex(ValueError, "narrative field"):
                comic_sol.init_project(
                    root, "Story with api_key=abc123 inside", b"source", {}
                )

    def test_validate_request_settings_rejects_oversized_request_title(self):
        with self.assertRaisesRegex(
            ValueError, "request title must be a non-empty string of at most 200"
        ):
            comic_sol.validate_request_settings({"title": "T" * 201})

    def test_transition_rejects_oversized_warning(self):
        with temporary_directory() as root:
            project = root / "project"
            project.mkdir()
            with self.assertRaisesRegex(
                ValueError,
                "transition warning must be a non-empty string of at most 500",
            ):
                comic_sol.transition(project, "PLANNED", "w" * 501)

    def test_transition_rejects_secret_warning(self):
        with temporary_directory() as root:
            project = root / "project"
            project.mkdir()
            with self.assertRaisesRegex(ValueError, "narrative field"):
                comic_sol.transition(project, "PLANNED", "password: hunter2")

    def test_record_override_rejects_oversized_reason_before_touching_disk(self):
        with temporary_directory() as root:
            project = root / "missing-project"
            with self.assertRaisesRegex(
                ValueError,
                "override reason must be a non-empty string of at most 1000",
            ):
                comic_sol.record_override(project, "p01-01", "r" * 1001)
            self.assertFalse(project.exists())

    def test_validate_manifest_flags_oversized_title_and_warnings(self):
        def manifest(title: str, warnings: list[str]) -> dict[str, object]:
            return {
                "project_id": "sample-project",
                "title": title,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "status": "PLANNED",
                "input": {},
                "settings": {},
                "capability": {},
                "artifacts": {},
                "stage_versions": {},
                "panels": [],
                "warnings": warnings,
            }

        issues = validate_manifest(manifest("T" * 201, []))
        self.assertTrue(
            any(issue.field == "title" and "at most 200" in issue.message for issue in issues)
        )
        issues = validate_manifest(manifest("Fine", ["w" * 501]))
        self.assertTrue(
            any(
                issue.field == "warnings[0]" and "at most 500" in issue.message
                for issue in issues
            )
        )
        issues = validate_manifest(manifest("Fine", ["api_key=abc123"]))
        self.assertTrue(
            any(
                issue.field == "warnings[0]"
                and "secrets or credentials" in issue.message
                for issue in issues
            )
        )

    def test_validate_panel_record_flags_oversized_override_reason(self):
        def record(reason: str) -> dict[str, object]:
            return {
                "schema_version": "1.0",
                "panel_id": "p01-01",
                "source_prompt_path": None,
                "raw_path": None,
                "clean_path": None,
                "raw_sha256": None,
                "dimensions": None,
                "attempts": 0,
                "generation": {},
                "checks": [],
                "decision": "accept_with_warnings",
                "retry_reason": None,
                "failure_category": "visual_qa",
                "override_reason": reason,
                "unresolved_warnings": [reason],
            }

        issues = validate_panel_record(record("r" * 1001))
        self.assertTrue(
            any(
                issue.field == "override_reason" and "at most 1000" in issue.message
                for issue in issues
            )
        )
        issues = validate_panel_record(record("api_key=abc123"))
        self.assertTrue(
            any(
                issue.field == "override_reason"
                and "secrets or credentials" in issue.message
                for issue in issues
            )
        )


class StructuredResourceErrorTests(unittest.TestCase):
    """Resource-limit failures classify to CS-SEC-002 on both surfaces."""

    def test_registry_documents_the_new_code(self):
        self.assertIn("CS-SEC-002", ERROR_DEFINITIONS)
        definition = ERROR_DEFINITIONS["CS-SEC-002"]
        self.assertEqual(definition.category, "security-error")
        documentation = (
            Path(__file__).resolve().parents[1] / "docs" / "structured-errors.md"
        ).read_text(encoding="utf-8")
        self.assertIn("CS-SEC-002", documentation)

    def test_resource_limit_error_classifies_identically_for_cli_and_mcp(self):
        error = InputResourceLimitError("the JSON size limit of 2097152 bytes")
        for surface in ("cli", "mcp"):
            with self.subTest(surface=surface):
                payload = error_payload(error, command="status", surface=surface)
                self.assertEqual("CS-SEC-002", payload["code"])
                self.assertEqual("security-error", payload["category"])

    def test_other_security_errors_still_classify_to_cs_sec_001(self):
        classified = classify_exception(
            ValueError("security-error: project contains a symlink")
        )
        self.assertEqual("CS-SEC-001", classified.code)

    def test_error_message_carries_no_payload_content(self):
        error = InputResourceLimitError("the JSON nesting depth limit of 64")
        rendered = str(error_payload(error, command="status"))
        self.assertIn("CS-SEC-002", rendered)
        self.assertNotIn("C:\\", rendered)


def _minimal_manifest_bytes() -> bytes:
    return (
        json.dumps(
            {
                "project_id": "bomb-project",
                "schema_version": "1.0",
                "status": "INIT",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


@unittest.skipUnless(MCP_AVAILABLE, "MCP extra is not installed")
class McpInputLimitParityTests(unittest.TestCase):
    """The MCP surface rejects the same inputs with the same messages."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "output"
        mcp_server._configure_root(self.root.absolute())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _tool_payload(self, error: ToolError) -> dict:
        return json.loads(str(error))

    def test_transition_rejects_oversized_warning_as_invalid_request(self):
        project = self.root / "project"
        project.mkdir()
        (project / "project.json").write_bytes(_minimal_manifest_bytes())
        with self.assertRaises(ToolError) as raised:
            mcp_server.comic_transition("project", "PLANNED", "w" * 501)
        payload = self._tool_payload(raised.exception)
        self.assertEqual("CS-MCP-001", payload["code"])
        self.assertEqual("invalid-request", payload["category"])
        self.assertIn(
            "transition warning must be a non-empty string of at most 500",
            payload["legacy_message"],
        )

    def test_transition_warning_message_matches_the_engine(self):
        engine_error = None
        try:
            comic_sol.transition(Path(self.root) / "none", "PLANNED", "w" * 501)
        except ValueError as error:
            engine_error = str(error)
        mcp_error = None
        try:
            mcp_server.comic_transition("project", "PLANNED", "w" * 501)
        except ToolError as error:
            mcp_error = self._tool_payload(error)["legacy_message"].replace(
                "invalid-request: ", "", 1
            )
        self.assertEqual(engine_error, mcp_error)

    def test_override_panel_rejects_secret_reason_as_invalid_request(self):
        project = self.root / "project"
        project.mkdir()
        (project / "project.json").write_bytes(_minimal_manifest_bytes())
        with self.assertRaises(ToolError) as raised:
            mcp_server.comic_override_panel("project", "p01-01", "api_key=abc123")
        payload = self._tool_payload(raised.exception)
        self.assertEqual("CS-MCP-001", payload["code"])
        self.assertIn("narrative field", payload["legacy_message"])

    def test_init_rejects_secret_title_before_project_allocation(self):
        before = list(self.root.iterdir())
        with self.assertRaises(ToolError) as raised:
            mcp_server.comic_init("api_key=abc123 story", "source", {})
        payload = self._tool_payload(raised.exception)
        self.assertEqual("CS-MCP-001", payload["code"])
        self.assertEqual(before, list(self.root.iterdir()))

    def test_status_reports_deeply_nested_manifest_as_cs_sec_002(self):
        project = self.root / "deep-project"
        project.mkdir()
        (project / "project.json").write_bytes(
            b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1)
        )
        with self.assertRaises(ToolError) as raised:
            mcp_server.comic_status("deep-project")
        payload = self._tool_payload(raised.exception)
        self.assertEqual("CS-SEC-002", payload["code"])
        self.assertEqual("security-error", payload["category"])
        self.assertIn("input exceeds", payload["legacy_message"])

    def test_status_reports_oversized_manifest_as_cs_sec_002(self):
        project = self.root / "huge-project"
        project.mkdir()
        (project / "project.json").write_bytes(
            b'{"padding": "' + b"a" * MAX_JSON_BYTES + b'"}'
        )
        with self.assertRaises(ToolError) as raised:
            mcp_server.comic_status("huge-project")
        payload = self._tool_payload(raised.exception)
        self.assertEqual("CS-SEC-002", payload["code"])


class CliInputLimitParityTests(unittest.TestCase):
    """The CLI surface reports the same resource limits as structured errors."""

    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        from comic_sol_product import cli

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_status_reports_oversized_manifest_as_cs_sec_002(self):
        with temporary_directory() as root:
            project = root / "huge-project"
            project.mkdir()
            (project / "project.json").write_bytes(
                b'{"padding": "' + b"a" * MAX_JSON_BYTES + b'"}'
            )
            code, stdout, stderr = self.invoke(
                ["--json", "status", str(project)]
            )
            self.assertEqual(2, code)
            payload = json.loads(stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("CS-SEC-002", payload["error"]["code"])
            self.assertEqual("security-error", payload["error"]["category"])

    def test_engine_cli_transition_reports_the_shared_warning_message(self):
        with temporary_directory() as root:
            project = root / "project"
            project.mkdir()
            (project / "project.json").write_bytes(_minimal_manifest_bytes())
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = comic_sol.main(
                    ["transition", str(project), "PLANNED", "--warning", "w" * 501]
                )
            self.assertEqual(1, code)
            self.assertIn(
                "transition warning must be a non-empty string of at most 500",
                stderr.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
