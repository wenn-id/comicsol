"""Lifecycle-level failure injection and recovery invariants."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import project_io
from scripts.comic_sol import atomic_write_json, read_json, sha256_file
from scripts.compose_pages import compose_all_pages
from scripts.export_pdf import PdfExportError, guarded_export
from scripts.letter_panels import letter_project
from scripts.page_quality import (
    build_page_quality_record,
    write_page_quality_record,
)
from scripts.project_io import ProjectTransaction
from tests.test_page_quality import reviewer_checks


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/valid-one-page"


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
        manifest = json.loads(
            (ROOT / "templates/manifest.json").read_text(encoding="utf-8")
        )
        manifest["project_id"] = "failure-fixture"
        manifest["status"] = "COMPOSED"
        manifest["input"]["source_sha256"] = hashlib.sha256(
            source.read_bytes()
        ).hexdigest()
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
        project_io.replace_contained(
            self.project, first["staged"], first["path"]
        )
        lock = transaction._lock
        if lock is None:
            self.fail("transaction lock was not acquired")
        lock.__exit__(None, None, None)
        transaction._lock = None

        journal = read_json(
            self.project / "logs/transactions/1/journal.json"
        )
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

            real_replace = project_io.replace_contained

            def fail_verification_publish(root, source, destination):
                if destination == "exports/pdf-verification.json":
                    raise OSError("injected disk full")
                return real_replace(root, source, destination)

            with mock.patch.object(
                project_io, "replace_contained", side_effect=fail_verification_publish
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
