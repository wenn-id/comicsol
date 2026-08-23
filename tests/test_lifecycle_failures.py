"""Lifecycle-level failure injection and recovery invariants."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import comic_sol, project_io
from scripts.comic_sol import (
    PROJECT_DIRECTORIES,
    atomic_write_json,
    init_project,
    read_json,
    sha256_file,
)
from scripts.schema import read_project_manifest
from scripts.compose_pages import compose_all_pages
from scripts.export_pdf import guarded_export
from scripts.letter_panels import letter_project
from scripts.page_quality import (
    build_page_quality_record,
    write_page_quality_record,
)
from scripts.project_io import ProjectTransaction
from tests.test_page_quality import reviewer_checks


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/valid-one-page"


class InitializationFailureInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.unrelated = self.root / "unrelated.txt"
        self.unrelated.write_bytes(b"leave me alone")
        self.request = {"mode": "short_prompt", "language": "en"}
        self.source = b"A completely staged project.\n"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _assert_no_partial_project(self, slug="atomic-initialization"):
        self.assertFalse((self.root / slug).exists())
        self.assertEqual([], list(self.root.glob(".comic-sol-*.tmp")))
        self.assertEqual(b"leave me alone", self.unrelated.read_bytes())

    def _assert_valid_initialized_project(self, project, slug="atomic-initialization"):
        self.assertEqual(slug, project.name)
        for relative in PROJECT_DIRECTORIES:
            self.assertTrue((project / relative).is_dir(), relative)
        self.assertEqual(self.source, (project / "source/input.txt").read_bytes())
        self.assertEqual(self.request, read_json(project / "source/request.json"))
        manifest = read_project_manifest(project / "project.json")
        self.assertEqual(slug, manifest["project_id"])
        self.assertEqual("Atomic Initialization", manifest["title"])
        self.assertEqual("INIT", manifest["status"])
        self.assertEqual(
            hashlib.sha256(self.source).hexdigest(),
            manifest["input"]["source_sha256"],
        )
        events = [
            json.loads(line)
            for line in (project / "logs/events.jsonl").read_text("utf-8").splitlines()
        ]
        self.assertEqual(1, len(events))
        self.assertEqual("project.created", events[0]["event"])
        self.assertEqual(slug, events[0]["details"]["project_id"])
        self.assertEqual("source/input.txt", events[0]["details"]["source_path"])
        self.assertEqual(manifest["input"]["source_sha256"], events[0]["details"]["source_sha256"])
        self.assertEqual([], list((project / "logs/transactions").iterdir()))
        self.assertEqual([], list(project.parent.glob(".comic-sol-*.tmp")))

    def _retry_and_assert_base_slug(self):
        project = init_project(
            self.root,
            "Atomic Initialization",
            self.source,
            self.request,
        )
        self._assert_valid_initialized_project(project)

    def test_each_staging_boundary_failure_cleans_up_and_retry_reuses_slug(self):
        real_create_tree = comic_sol._create_project_tree
        real_write_bytes = comic_sol.atomic_write_bytes
        real_write_json = comic_sol.atomic_write_json
        real_append_event = comic_sol.append_event
        real_fsync_tree = comic_sol.fsync_directory_tree

        for boundary in ("allocation", "source", "request", "manifest", "event", "durability"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory(dir=self.root) as scenario,
            ):
                scenario_root = Path(scenario)
                marker = scenario_root / "unrelated.txt"
                marker.write_bytes(b"scenario marker")

                if boundary == "allocation":

                    def fail_after_allocation(project_dir):
                        real_create_tree(project_dir)
                        raise KeyboardInterrupt("injected after staging allocation")

                    patcher = mock.patch.object(
                        comic_sol,
                        "_create_project_tree",
                        side_effect=fail_after_allocation,
                    )
                    expected_error = KeyboardInterrupt
                elif boundary == "source":

                    def fail_after_source(path, payload):
                        real_write_bytes(path, payload)
                        if Path(path).name == "input.txt":
                            raise OSError("injected after source write")

                    patcher = mock.patch.object(
                        comic_sol,
                        "atomic_write_bytes",
                        side_effect=fail_after_source,
                    )
                    expected_error = OSError
                elif boundary in {"request", "manifest"}:
                    expected_name = "request.json" if boundary == "request" else "project.json"

                    def fail_after_json(path, value, *, expected=expected_name):
                        real_write_json(path, value)
                        if Path(path).name == expected:
                            raise OSError(f"injected after {boundary} write")

                    patcher = mock.patch.object(
                        comic_sol,
                        "atomic_write_json",
                        side_effect=fail_after_json,
                    )
                    expected_error = OSError
                elif boundary == "event":

                    def fail_after_event(project_dir, event, details):
                        real_append_event(project_dir, event, details)
                        raise OSError("injected after event write")

                    patcher = mock.patch.object(
                        comic_sol,
                        "append_event",
                        side_effect=fail_after_event,
                    )
                    expected_error = OSError
                else:

                    def fail_after_durability_flush(project_dir):
                        real_fsync_tree(project_dir)
                        raise OSError("injected after staged tree fsync")

                    patcher = mock.patch.object(
                        comic_sol,
                        "fsync_directory_tree",
                        side_effect=fail_after_durability_flush,
                    )
                    expected_error = OSError

                with patcher, self.assertRaises(expected_error):
                    init_project(
                        scenario_root,
                        "Atomic Initialization",
                        self.source,
                        self.request,
                    )

                self.assertFalse((scenario_root / "atomic-initialization").exists())
                self.assertEqual([], list(scenario_root.glob(".comic-sol-*.tmp")))
                self.assertEqual(b"scenario marker", marker.read_bytes())
                project = init_project(
                    scenario_root,
                    "Atomic Initialization",
                    self.source,
                    self.request,
                )
                self._assert_valid_initialized_project(project)

    def test_publication_failure_cleans_only_staging_and_retry_reuses_slug(self):
        with mock.patch.object(
            comic_sol,
            "publish_directory_noreplace",
            side_effect=OSError("injected publication failure"),
        ):
            with self.assertRaisesRegex(OSError, "publication failure"):
                init_project(
                    self.root,
                    "Atomic Initialization",
                    self.source,
                    self.request,
                )

        self._assert_no_partial_project()
        self._retry_and_assert_base_slug()

    def test_publication_race_preserves_winner_and_rebuilds_for_suffix(self):
        real_publish = comic_sol.publish_directory_noreplace
        calls = 0

        def race_once(staging, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                destination.mkdir()
                (destination / "winner.txt").write_bytes(b"race winner")
            return real_publish(staging, destination, **kwargs)

        with mock.patch.object(
            comic_sol,
            "publish_directory_noreplace",
            side_effect=race_once,
        ):
            project = init_project(
                self.root,
                "Atomic Initialization",
                self.source,
                self.request,
            )

        self.assertEqual(
            b"race winner",
            (self.root / "atomic-initialization/winner.txt").read_bytes(),
        )
        self._assert_valid_initialized_project(project, "atomic-initialization-2")

    def test_preexisting_empty_directory_and_file_are_preserved(self):
        for entry_kind in ("directory", "file"):
            with (
                self.subTest(entry_kind=entry_kind),
                tempfile.TemporaryDirectory(dir=self.root) as scenario,
            ):
                scenario_root = Path(scenario)
                occupied = scenario_root / "atomic-initialization"
                if entry_kind == "directory":
                    occupied.mkdir()
                else:
                    occupied.write_bytes(b"pre-existing file")

                project = init_project(
                    scenario_root,
                    "Atomic Initialization",
                    self.source,
                    self.request,
                )

                self.assertEqual("atomic-initialization-2", project.name)
                if entry_kind == "directory":
                    self.assertTrue(occupied.is_dir())
                    self.assertEqual([], list(occupied.iterdir()))
                else:
                    self.assertEqual(b"pre-existing file", occupied.read_bytes())
                self._assert_valid_initialized_project(project, "atomic-initialization-2")

    def test_post_publication_fsync_failure_leaves_one_complete_project(self):
        real_fsync_directory = project_io.fsync_directory

        def fail_after_publication(path):
            if Path(path) == self.root and (self.root / "atomic-initialization").is_dir():
                raise OSError("injected output-root fsync failure")
            return real_fsync_directory(path)

        with mock.patch.object(
            project_io,
            "fsync_directory",
            side_effect=fail_after_publication,
        ):
            with self.assertRaisesRegex(OSError, "output-root fsync failure"):
                init_project(
                    self.root,
                    "Atomic Initialization",
                    self.source,
                    self.request,
                )

        self._assert_valid_initialized_project(self.root / "atomic-initialization")
        self.assertEqual(b"leave me alone", self.unrelated.read_bytes())

    def test_staging_write_failure_with_cleanup_failure_propagates_write_error(self):
        real_write_bytes = comic_sol.atomic_write_bytes

        def fail_after_source(path, payload):
            real_write_bytes(path, payload)
            if Path(path).name == "input.txt":
                raise OSError("injected write failure")

        with (
            mock.patch.object(
                comic_sol,
                "atomic_write_bytes",
                side_effect=fail_after_source,
            ),
            mock.patch.object(
                comic_sol,
                "cleanup_owned_directory",
                side_effect=OSError("injected cleanup failure"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                init_project(
                    self.root,
                    "Atomic Initialization",
                    self.source,
                    self.request,
                )

        self._assert_no_partial_project()


class LifecycleFailureInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        (self.project / "logs").mkdir(parents=True)
        (self.project / "pages").mkdir()
        (self.project / "exports").mkdir()
        (self.project / "source").mkdir()
        source = self.project / "source/input.txt"
        source.write_text("A failure-safe export fixture.\n", encoding="utf-8")
        atomic_write_json(
            self.project / "source/request.json",
            {"mode": "short_prompt", "language": "en"},
        )
        manifest = json.loads((ROOT / "templates/manifest.json").read_text(encoding="utf-8"))
        manifest["project_id"] = "failure-fixture"
        manifest["status"] = "COMPOSED"
        manifest["input"]["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest["settings"].update({"page_count": 1, "panel_count": 1})
        manifest["panels"] = ["p01-01"]
        atomic_write_json(self.project / "project.json", manifest)
        (self.project / "logs/events.jsonl").write_text(
            json.dumps({"event": "project.initialized", "status": "COMPOSED"}) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_process_interruption_leaves_publishing_journal_for_recovery(self):
        manifest_before = (self.project / "project.json").read_bytes()
        events_before = (self.project / "logs/events.jsonl").read_bytes()

        transaction = ProjectTransaction(self.project, "lifecycle-stage")
        transaction.__enter__()
        transaction.stage_bytes(
            "project.json",
            b'{"project_id":"failure-fixture","status":"LETTERED"}\n',
        )
        transaction.stage_bytes(
            "logs/events.jsonl",
            b'{"event":"stage.started","status":"LETTERED"}\n',
        )

        # Model a process dying after the journal became durable and the first
        # replacement was published, before commit could run its rollback path.
        transaction._phase = "publishing"
        transaction._write_journal()
        first = transaction._journal[0]
        project_io.replace_contained(self.project, first["staged"], first["path"])
        lock = transaction._lock
        if lock is None:
            self.fail("transaction lock was not acquired")
        lock.__exit__(None, None, None)
        transaction._lock = None

        journal = read_json(self.project / "logs/transactions/1/journal.json")
        self.assertEqual("publishing", journal["phase"])
        self.assertEqual("LETTERED", read_json(self.project / "project.json")["status"])

        ProjectTransaction.recover(self.project)

        self.assertEqual(manifest_before, (self.project / "project.json").read_bytes())
        self.assertEqual(events_before, (self.project / "logs/events.jsonl").read_bytes())
        self.assertEqual([], list((self.project / "logs/transactions").iterdir()))
        self.assertEqual("COMPOSED", read_json(self.project / "project.json")["status"])
        events = [
            json.loads(line)
            for line in (self.project / "logs/events.jsonl").read_text("utf-8").splitlines()
        ]
        self.assertTrue(all(isinstance(event["event"], str) for event in events))

    def test_guarded_export_failure_rolls_back_pdf_verification_and_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURE, project)
            letter_project(project)
            compose_all_pages(project)
            write_page_quality_record(
                project,
                1,
                build_page_quality_record(
                    project,
                    1,
                    reviewer_checks(project),
                    reviewer="fixture-reviewer",
                    reviewed_at="2026-08-14T01:02:03Z",
                ),
            )
            manifest = read_json(project / "project.json")
            existing_artifacts = manifest.get("artifacts")
            artifacts = dict(existing_artifacts) if isinstance(existing_artifacts, dict) else {}
            for key, relative in {
                "character_bible": "plan/character-bible.json",
                "story_plan": "plan/story-plan.json",
                "storyboard": "plan/storyboard.json",
                "composition_cache": "cache/composition.json",
            }.items():
                artifacts[key] = {
                    "path": relative,
                    "sha256": sha256_file(project / relative),
                }
            manifest["artifacts"] = artifacts
            atomic_write_json(project / "project.json", manifest)

            previous_pdf = project / "exports/valid-one-page.pdf"
            previous_pdf.parent.mkdir(parents=True, exist_ok=True)
            previous_pdf.write_bytes(b"previous verified pdf")
            manifest_before = (project / "project.json").read_bytes()
            verification = project / "exports/pdf-verification.json"
            if verification.exists():
                verification.unlink()

            real_replace_contained = project_io.replace_contained
            real_os_replace = project_io.os.replace

            def fail_verification_contained(root, source, destination):
                if Path(destination).name == "pdf-verification.json":
                    raise OSError("injected disk full")
                return real_replace_contained(root, source, destination)

            def fail_verification_os(source, destination, *args, **kwargs):
                if Path(destination).name == "pdf-verification.json":
                    raise OSError("injected disk full")
                return real_os_replace(source, destination, *args, **kwargs)

            with (
                mock.patch.object(
                    project_io, "replace_contained", side_effect=fail_verification_contained
                ),
                mock.patch.object(project_io.os, "replace", side_effect=fail_verification_os),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    guarded_export(project)

            ProjectTransaction.recover(project)
            self.assertEqual(b"previous verified pdf", previous_pdf.read_bytes())
            self.assertEqual(manifest_before, (project / "project.json").read_bytes())
            self.assertFalse(verification.exists())
            self.assertEqual([], list((project / "logs/transactions").iterdir()))


if __name__ == "__main__":
    unittest.main()
