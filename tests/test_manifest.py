import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import ImageFont

ROOT = Path(__file__).resolve().parents[1]

from scripts.comic_sol import (  # noqa: E402
    append_event,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    doctor,
    init_project,
    layout_rects,
    main,
    read_json,
    rectangles_overlap,
    sha256_file,
    slugify,
    transition,
)
from scripts.project_io import ProjectTransaction  # noqa: E402
from scripts.schema import (  # noqa: E402
    CURRENT_PROJECT_SCHEMA_VERSION,
    MIN_READER_PROJECT_SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    migrate_project_manifest,
    read_project_manifest,
)
from scripts import comic_sol, letter_panels  # noqa: E402


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_template_uses_lettering_cache_version_two_only(self):
        manifest = read_json(ROOT / "templates/manifest.json")

        self.assertEqual("1.0", manifest["schema_version"])
        self.assertEqual(
            {
                "composition": "1",
                "export": "1",
                "generation": "1",
                "lettering": "2",
                "planning": "1",
                "storyboard": "1",
            },
            manifest["stage_versions"],
        )

    def test_project_schema_contract_is_explicit_and_readable(self):
        self.assertEqual("1.0", CURRENT_PROJECT_SCHEMA_VERSION)
        self.assertEqual("1.0", MIN_READER_PROJECT_SCHEMA_VERSION)
        project = init_project(
            self.root,
            "Schema Contract",
            b"Schema contract source",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest = read_project_manifest(project / "project.json")
        self.assertEqual("1.0", manifest["schema_version"])

    def test_unsupported_project_schema_is_rejected_without_mutation(self):
        project = init_project(
            self.root,
            "Unsupported Schema",
            b"Schema contract source",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest_path = project / "project.json"
        original = manifest_path.read_bytes()
        manifest = read_json(manifest_path)
        manifest["schema_version"] = "9.0"
        atomic_write_json(manifest_path, manifest)
        unsupported = manifest_path.read_bytes()
        with self.assertRaisesRegex(UnsupportedSchemaVersionError, "project schema 9.0"):
            read_project_manifest(manifest_path)
        self.assertEqual(unsupported, manifest_path.read_bytes())
        self.assertNotEqual(original, manifest_path.read_bytes())

    def test_migration_failure_preserves_project_bytes(self):
        project = init_project(
            self.root,
            "Migration Failure",
            b"Schema contract source",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest_path = project / "project.json"
        manifest = read_json(manifest_path)
        manifest["schema_version"] = "0.9"
        atomic_write_json(manifest_path, manifest)
        before = manifest_path.read_bytes()
        with self.assertRaisesRegex(UnsupportedSchemaVersionError, "no migration path"):
            migrate_project_manifest(project)
        self.assertEqual(before, manifest_path.read_bytes())

    def test_init_preserves_source_and_creates_complete_skeleton(self):
        request = {"mode": "short_prompt", "language": "en"}
        project = init_project(
            self.root, "Sunlight Courier", b"A courier.\r\nExact bytes.", request
        )

        self.assertEqual("sunlight-courier", project.name)
        self.assertEqual(
            b"A courier.\r\nExact bytes.",
            (project / "source/input.txt").read_bytes(),
        )
        expected_directories = (
            "source",
            "plan",
            "references/characters",
            "references/scenes",
            "prompts/references",
            "prompts/panels",
            "panels/raw",
            "panels/clean",
            "qa/panels",
            "pages",
            "exports",
            "logs",
        )
        for relative in expected_directories:
            self.assertTrue((project / relative).is_dir(), relative)

        manifest = read_json(project / "project.json")
        self.assertEqual("INIT", manifest["status"])
        self.assertEqual("sunlight-courier", manifest["project_id"])
        self.assertEqual("Sunlight Courier", manifest["title"])
        self.assertEqual(
            hashlib.sha256(b"A courier.\r\nExact bytes.").hexdigest(),
            manifest["input"]["source_sha256"],
        )
        self.assertEqual(request, read_json(project / "source/request.json"))
        self.assertEqual(1, len((project / "logs/events.jsonl").read_text("utf-8").splitlines()))

    def test_init_allocates_deterministic_suffix_and_never_overwrites(self):
        request = {"mode": "short_prompt", "language": "en"}
        first = init_project(self.root, "Story", b"First", request)
        second = init_project(self.root, "Story", b"Second", request)
        third = init_project(self.root, "Story", b"Third", request)

        self.assertEqual(["story", "story-2", "story-3"], [first.name, second.name, third.name])
        self.assertEqual(b"First", (first / "source/input.txt").read_bytes())
        self.assertEqual(b"Second", (second / "source/input.txt").read_bytes())
        self.assertEqual(b"Third", (third / "source/input.txt").read_bytes())

    def test_init_rejects_unknown_and_sensitive_request_settings_before_allocation(self):
        for request, message in (
            ({"mode": "short_prompt", "language": "en", "unexpected": True}, "unsupported request setting"),
            ({"api_key": "do-not-persist"}, "sensitive request setting"),
            ({"metadata": {"token": "do-not-persist"}}, "sensitive request setting"),
        ):
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, message):
                    init_project(self.root, "Rejected Request", b"Story", request)
                self.assertEqual([], list(self.root.iterdir()))

    def test_init_rejects_invalid_request_values_before_allocation(self):
        for request in (
            {"mode": "provider_payload", "language": "en"},
            {"mode": "short_prompt", "language": ""},
            {"mode": "short_prompt", "language": "en", "title": 42},
        ):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    init_project(self.root, "Rejected Request", b"Story", request)
                self.assertEqual([], list(self.root.iterdir()))

    def test_transition_rejects_skips_and_allows_blocked_and_warning_terminal(self):
        project = init_project(
            self.root,
            "Story",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        with self.assertRaisesRegex(ValueError, "INIT.*STORYBOARDED"):
            transition(project, "STORYBOARDED")

        result = transition(project, "PLANNED")
        self.assertEqual("PLANNED", result["status"])
        blocked = transition(project, "BLOCKED", "image capability unavailable")
        self.assertEqual("BLOCKED", blocked["status"])
        self.assertIn("image capability unavailable", blocked["warnings"])

        export_project = init_project(
            self.root,
            "Exported Story",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        for state in (
            "PLANNED",
            "SCRIPTED",
            "STORYBOARDED",
            "REFERENCES_READY",
            "PANELS_READY",
            "QA_READY",
            "LETTERED",
            "COMPOSED",
            "EXPORTED",
        ):
            transition(export_project, state)
        # This test isolates transition-graph behavior; final artifact gating is
        # covered by GuardedOperationTests.
        with mock.patch("scripts.validate_project.require_valid_project"):
            terminal = transition(
                export_project, "COMPLETE_WITH_WARNINGS", "minor prop drift"
            )
        self.assertEqual("COMPLETE_WITH_WARNINGS", terminal["status"])
        self.assertIn("minor prop drift", terminal["warnings"])

    def test_canonical_json_read_hash_and_atomic_writes(self):
        self.assertEqual(
            '{"a":"é","z":1}'.encode("utf-8"),
            canonical_json_bytes({"z": 1, "a": "é"}),
        )
        binary_path = self.root / "nested/payload.bin"
        atomic_write_bytes(binary_path, b"payload")
        self.assertEqual(b"payload", binary_path.read_bytes())
        self.assertEqual(hashlib.sha256(b"payload").hexdigest(), sha256_file(binary_path))

        json_path = self.root / "nested/value.json"
        atomic_write_json(json_path, {"z": 1, "a": "é"})
        self.assertEqual(
            '{\n  "a": "é",\n  "z": 1\n}\n'.encode("utf-8"),
            json_path.read_bytes(),
        )
        self.assertEqual({"a": "é", "z": 1}, read_json(json_path))
        self.assertEqual([], list((self.root / "nested").glob("*.tmp")))

    def test_atomic_write_preserves_unrelated_interrupted_temp_file(self):
        interrupted = self.root / ".project.json.interrupted.tmp"
        interrupted.write_bytes(b"recoverable partial data")

        atomic_write_json(self.root / "project.json", {"status": "INIT"})

        self.assertEqual(b"recoverable partial data", interrupted.read_bytes())

    def test_transition_publishes_manifest_only_after_event_succeeds(self):
        project = init_project(
            self.root,
            "Ordering",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        before = (project / "project.json").read_bytes()
        # Mock transaction commit to simulate event write failure
        with mock.patch.object(
            ProjectTransaction, "commit", side_effect=OSError("event write failed")
        ):
            with self.assertRaisesRegex(OSError, "event write failed"):
                transition(project, "PLANNED")
        self.assertEqual(before, (project / "project.json").read_bytes())

    def test_append_event_is_canonical_jsonl_and_redacts_sensitive_values(self):
        project = init_project(
            self.root,
            "Events",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        append_event(
            project,
            "tool.failed",
            {"panel_id": "p01-01", "api_key": "do-not-log", "count": 2},
        )
        lines = (project / "logs/events.jsonl").read_text("utf-8").splitlines()
        event = json.loads(lines[-1])
        self.assertEqual("tool.failed", event["event"])
        self.assertNotIn("api_key", event["details"])
        self.assertNotIn("do-not-log", lines[-1])
        self.assertEqual(canonical_json_bytes(event), lines[-1].encode("utf-8"))

    def test_append_event_rejects_raw_payloads_and_absolute_paths(self):
        project = init_project(
            self.root,
            "Safe Events",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        event_path = project / "logs/events.jsonl"
        before = event_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unsupported event detail"):
            append_event(project, "tool.failed", {"raw_provider_response": "private"})
        with self.assertRaisesRegex(ValueError, "relative project path"):
            append_event(project, "artifact.created", {"source_path": Path("/private/input.txt")})
        with self.assertRaisesRegex(ValueError, "event name"):
            append_event(project, "raw provider response with spaces", {"count": 1})
        self.assertEqual(before, event_path.read_bytes())

    def test_transition_event_records_warning_presence_not_warning_text(self):
        project = init_project(
            self.root,
            "Warning Event",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        warning = "private person@example.test has a token"
        transition(project, "BLOCKED", warning)
        line = (project / "logs/events.jsonl").read_text("utf-8").splitlines()[-1]
        event = json.loads(line)
        self.assertEqual(
            {"from": "INIT", "to": "BLOCKED", "warning_present": True},
            event["details"],
        )
        self.assertNotIn(warning, line)

    def test_slug_helpers_and_layout_interfaces_are_available(self):
        self.assertEqual("hello-world", slugify("  Héllo, World!  "))
        self.assertEqual("comic-sol-project", slugify("---"))
        self.assertTrue(callable(layout_rects))
        self.assertTrue(callable(rectangles_overlap))

    def test_doctor_checks_local_runtime_and_defers_image_capability(self):
        healthy, messages = doctor(self.root / "doctor-output")
        self.assertTrue(healthy, messages)
        for label in (
            "Python 3.11",
            "Pillow 12.3.0",
            "font Comic Neue Regular",
            "font Comic Neue Bold",
            "font Noto Sans fallback",
            "templates",
            "output root",
        ):
            self.assertTrue(any(message.startswith("PASS") and label in message for message in messages), label)
        self.assertIn("INFO image capability: inspect in agent session", messages)

    def test_doctor_accepts_newer_supported_python(self):
        with (
            mock.patch.object(comic_sol.sys, "version_info", (3, 12, 13)),
            mock.patch.object(comic_sol.sys, "version", "3.12.13 (main, test)"),
        ):
            healthy, messages = doctor(self.root / "doctor-output")

        self.assertTrue(healthy, messages)
        self.assertIn("PASS Python 3.11+ (3.12.13)", messages)

    def test_doctor_rejects_python_before_supported_minimum(self):
        with (
            mock.patch.object(comic_sol.sys, "version_info", (3, 10, 14)),
            mock.patch.object(comic_sol.sys, "version", "3.10.14 (main, test)"),
        ):
            healthy, messages = doctor(self.root / "doctor-output")

        self.assertFalse(healthy)
        self.assertIn("FAIL Python 3.11+ required; found 3.10.14", messages)

    def test_doctor_reports_each_font_when_one_face_fails(self):
        real_truetype = ImageFont.truetype

        def fail_bold_only(font, size, *args, **kwargs):
            if Path(font).name == "ComicNeue-Bold.ttf":
                raise OSError("simulated bold font failure")
            return real_truetype(font, size, *args, **kwargs)

        with mock.patch.object(ImageFont, "truetype", side_effect=fail_bold_only):
            healthy, messages = doctor(self.root / "doctor-output")

        self.assertFalse(healthy)
        self.assertTrue(any(message.startswith("PASS font Comic Neue Regular") for message in messages))
        self.assertTrue(any(message.startswith("FAIL font Comic Neue Bold") for message in messages))
        self.assertTrue(any(message.startswith("PASS font Noto Sans fallback") for message in messages))

    def test_font_paths_expose_comic_neue_and_noto_fallback(self):
        regular = ROOT / "assets/fonts/ComicNeue-Regular.ttf"
        bold = ROOT / "assets/fonts/ComicNeue-Bold.ttf"
        fallback = ROOT / "assets/fonts/NotoSans-Regular.ttf"

        self.assertEqual(regular, getattr(comic_sol, "FONT_PATH_COMIC_REGULAR", None))
        self.assertEqual(bold, getattr(comic_sol, "FONT_PATH_COMIC_BOLD", None))
        self.assertEqual(fallback, getattr(comic_sol, "FONT_PATH_FALLBACK", None))
        self.assertEqual(regular, letter_panels.FONT_PATH)
        self.assertEqual(bold, getattr(letter_panels, "FONT_PATH_BOLD", None))
        self.assertEqual(fallback, getattr(letter_panels, "FONT_PATH_FALLBACK", None))
        arguments = letter_panels._build_parser().parse_args([str(self.root)])
        self.assertEqual(self.root, arguments.project_dir)
        self.assertEqual(regular, arguments.font)

    def test_bundled_comic_neue_faces_load_at_42px(self):
        faces = {
            "ComicNeue-Regular.ttf": (
                "a0ee5a37c8b27c4db0700137d928598b1e23b0089e1546a8961909176b779360",
                ("Comic Neue", "Regular"),
            ),
            "ComicNeue-Bold.ttf": (
                "3e7e5fccfd7e0788f317b43312151c1bd5cf058c9697a8d83eac3939050bd61e",
                ("Comic Neue", "Bold"),
            ),
        }
        for filename, (expected_sha256, expected_name) in faces.items():
            with self.subTest(filename=filename):
                path = ROOT / "assets/fonts" / filename
                self.assertTrue(path.is_file(), path)
                self.assertEqual(expected_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
                font = ImageFont.truetype(str(path), 42)
                self.assertEqual(42, font.size)
                self.assertEqual(expected_name, font.getname())

    def test_cli_status_json_reports_manifest(self):
        project = init_project(
            self.root,
            "CLI Story",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["status", os.fspath(project), "--json"])
        self.assertEqual(0, result)
        self.assertEqual("INIT", json.loads(output.getvalue())["status"])


class SourceBoundaryTests(unittest.TestCase):
    def test_source_over_200_kib_creates_no_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "at most 200 KiB"):
                init_project(root, "Too Large", b"a" * (200 * 1024 + 1), {})
            self.assertEqual(list(root.iterdir()), [])

    def test_invalid_utf8_creates_no_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                init_project(root, "Bad Encoding", b"\xff", {})
            self.assertEqual(list(root.iterdir()), [])

    def test_cli_rejects_non_text_source_before_project_allocation(self):
        for suffix in (".pdf", ".json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output_root = root / "output"
                source = root / f"story{suffix}"
                request = root / "request.json"
                source.write_bytes(b"story")
                atomic_write_json(request, {})

                result = main([
                    "init", "--output-root", os.fspath(output_root),
                    "--title", "Bad Source", "--source", os.fspath(source),
                    "--request-json", os.fspath(request),
                ])

                self.assertEqual(1, result)
                self.assertFalse(output_root.exists())


if __name__ == "__main__":
    unittest.main()
