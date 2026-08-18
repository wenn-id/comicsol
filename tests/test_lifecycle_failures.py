"""Lifecycle-level failure injection and recovery invariants."""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from scripts import project_io
from scripts.comic_sol import atomic_write_json, read_json
from scripts.export_pdf import PdfExportError, export_pdf
from scripts.project_io import ProjectTransaction


ROOT = Path(__file__).resolve().parents[1]


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

    def test_process_interruption_between_stage_writes_restores_state_and_provenance(self):
        manifest_before = (self.project / "project.json").read_bytes()
        events_before = (self.project / "logs/events.jsonl").read_bytes()
        real_replace = project_io.os.replace
        staged_replacements = 0

        def interrupt_after_first_publish(source, destination, **kwargs):
            nonlocal staged_replacements
            if Path(source).name.startswith("staged-"):
                staged_replacements += 1
                if staged_replacements == 2:
                    raise KeyboardInterrupt("simulated process termination")
            return real_replace(source, destination, **kwargs)

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
        try:
            with mock.patch.object(
                project_io.os, "replace", side_effect=interrupt_after_first_publish
            ):
                with self.assertRaises(KeyboardInterrupt):
                    transaction.commit()
        finally:
            if transaction._lock is not None:
                transaction._lock.__exit__(None, None, None)
                transaction._lock = None

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

    def test_export_failure_preserves_previous_pdf_and_project_state(self):
        manifest_before = (self.project / "project.json").read_bytes()
        Image.new("RGB", (1600, 2400), "navy").save(
            self.project / "pages/page-001.png"
        )
        previous_pdf = self.project / "exports/failure-fixture.pdf"
        previous_pdf.write_bytes(b"previous verified pdf")

        with mock.patch(
            "scripts.export_pdf.durable_atomic_write",
            side_effect=OSError("injected disk full"),
        ):
            with self.assertRaisesRegex(PdfExportError, "disk full"):
                export_pdf(self.project)

        self.assertEqual(b"previous verified pdf", previous_pdf.read_bytes())
        self.assertEqual(manifest_before, (self.project / "project.json").read_bytes())
        self.assertEqual(
            [],
            [path for path in (self.project / "exports").iterdir() if path.name.startswith(".failure-fixture")],
        )


if __name__ == "__main__":
    unittest.main()
