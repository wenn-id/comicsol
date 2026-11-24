"""Exercise the native executable against Python's persisted project protocol."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from comic_sol_product.engine.project_io import ProjectLock, ProjectTransaction  # noqa: E402


def prepare_interrupted_write(project: Path, payload: bytes) -> ProjectTransaction:
    transaction = ProjectTransaction(project, "validate-verification")
    transaction.__enter__()
    try:
        transaction.stage_bytes("project.json", payload)
        transaction._phase = "publishing"
        transaction._write_journal()
        entry = transaction._journal[0]
        os.replace(project / entry["staged"], project / entry["path"])
    except BaseException:
        transaction._lock.__exit__(None, None, None)
        raise
    return transaction


class StatusCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "comic"
        self.project.mkdir()
        self.manifest = self.project / "project.json"
        self.manifest.write_text('{"status":"PLANNED"}', encoding="utf-8")
        self.native = Path(os.environ["COMIC_SOL_NATIVE_TEST_EXE"]).resolve(strict=True)
        self.environment = dict(
            os.environ, COMIC_SOL_PYTHON=str(self.root / "no-python-executable")
        )

    def rust_call(
        self, project: Path, *, command: str = "status", arguments: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [self.native, "--json", command, project, *arguments],
            check=False,
            capture_output=True,
            env=self.environment,
            timeout=15,
        )

    def python_call(
        self, project: Path, *, command: str = "status", arguments: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "comic_sol_product.cli", "--json", command, project, *arguments],
            check=False,
            capture_output=True,
            cwd=ROOT,
            timeout=15,
        )

    def assert_cli_equal(
        self, first: subprocess.CompletedProcess[bytes], second: subprocess.CompletedProcess[bytes]
    ) -> None:
        self.assertEqual(
            (first.returncode, first.stdout, first.stderr),
            (second.returncode, second.stdout, second.stderr),
        )

    def test_schema_defaults_and_rejections_match_python(self) -> None:
        for text in (
            '{"title":"échantillon","number":1e-7,"zero":-0,"f":1e16}',
            '{"schema_version":"1.0"}',
            '{"schema_version":"1.1"}',
            '{"schema_version":"1.10"}',
            '{"schema_version":"0.9"}',
            '{"schema_version":null}',
        ):
            with self.subTest(text=text):
                self.manifest.write_text(text, encoding="utf-8")
                self.assert_cli_equal(self.python_call(self.project), self.rust_call(self.project))
                self.assertEqual(text.encode(), self.manifest.read_bytes())

    def test_rust_waits_for_python_project_lock(self) -> None:
        with ProjectLock(self.project):
            process = subprocess.Popen(
                [self.native, "--json", "status", self.project],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.35)
                self.manifest.write_bytes(b'{"status":"STORYBOARDED"}')
            except BaseException:
                process.kill()
                process.communicate()
                raise
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((0, b""), (process.returncode, stderr))
        self.assertEqual("STORYBOARDED", json.loads(stdout)["data"]["status"])

    def test_rust_rolls_back_python_journal_including_torn_append(self) -> None:
        logs = self.project / "logs"
        logs.mkdir()
        before = b'{"event":"before"}\n{"event":"torn"'
        (logs / "events.jsonl").write_bytes(before)
        transaction = ProjectTransaction(self.project, "status-verification")
        transaction.__enter__()
        try:
            transaction.stage_bytes("project.json", b'{"status":"COMPLETE"}')
            transaction.stage_bytes("new-artifact.bin", b"new")
            transaction.append_bytes(
                "logs/events.jsonl", b'{"event":"after"}\n', repair_torn_jsonl=True
            )
            transaction._phase = "publishing"
            transaction._write_journal()
            for entry in transaction._journal:
                if entry.get("operation") == "append":
                    transaction._apply_append(entry)
                else:
                    os.replace(self.project / entry["staged"], self.project / entry["path"])
        finally:
            transaction._lock.__exit__(None, None, None)
        reference = self.root / "python"
        shutil.copytree(self.project, reference)

        self.assert_cli_equal(self.python_call(reference), self.rust_call(self.project))
        self.assertEqual(b'{"status":"PLANNED"}', self.manifest.read_bytes())
        self.assertEqual(before, (logs / "events.jsonl").read_bytes())
        self.assertFalse((self.project / "new-artifact.bin").exists())
        self.assertEqual([], list((logs / "transactions").iterdir()))

    def test_rust_rejects_journal_traversal_before_undo(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        transaction = self.project / "logs" / "transactions" / "1"
        transaction.mkdir(parents=True)
        journal = transaction / "journal.json"
        journal.write_text(
            '{"schema_version":"1.0","phase":"publishing","targets":'
            '[{"path":"../outside"},{"path":"project.json"}]}',
            encoding="utf-8",
        )
        result = self.rust_call(self.project)
        self.assertEqual(2, result.returncode)
        self.assertEqual("CS-PROJ-001", json.loads(result.stdout)["error"]["code"])
        self.assertEqual(b"keep", outside.read_bytes())
        self.assertEqual(b'{"status":"PLANNED"}', self.manifest.read_bytes())
        self.assertTrue(journal.is_file())

    def test_rust_cleans_committed_journal_and_unpublished_temp(self) -> None:
        transactions = self.project / "logs" / "transactions"
        committed = transactions / "1"
        committed.mkdir(parents=True)
        (committed / "journal.json").write_text(
            '{"schema_version":"1.0","phase":"committed","targets":'
            '[{"path":"project.json","backup":"logs/transactions/1/backup-001-project.json"}]}',
            encoding="utf-8",
        )
        (committed / "backup-001-project.json").write_bytes(b'{"status":"INIT"}')
        unpublished = transactions / "2"
        unpublished.mkdir()
        (unpublished / ".journal.json.a1b2c3d4.tmp").write_bytes(b"partial")
        result = self.rust_call(self.project)
        self.assertEqual(0, result.returncode)
        self.assertEqual(b'{"status":"PLANNED"}', self.manifest.read_bytes())
        self.assertEqual([], list(transactions.iterdir()))

    def test_rust_rejects_linked_journal_parent(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.project / "logs"
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", junction, outside],
                check=True,
                capture_output=True,
            )
            self.addCleanup(junction.rmdir)
        else:
            junction.symlink_to(outside, target_is_directory=True)
        result = self.rust_call(self.project)
        self.assertEqual(2, result.returncode)
        self.assertEqual("CS-SEC-001", json.loads(result.stdout)["error"]["code"])
        self.assertEqual([], list(outside.iterdir()))

    def test_validate_matches_python_across_stages(self) -> None:
        for name in ("valid-one-page", "interrupted-two-page", "project-schema-1.0"):
            fixture = self.root / name
            shutil.copytree(ROOT / "tests" / "fixtures" / name, fixture)
            for stage in ("all", "plan", "storyboard", "panels", "final", "export-ready"):
                with self.subTest(fixture=name, stage=stage):
                    options = {"command": "validate", "arguments": ("--stage", stage)}
                    self.assert_cli_equal(
                        self.python_call(fixture, **options), self.rust_call(fixture, **options)
                    )

    def test_validate_waits_and_recovers_before_inspection(self) -> None:
        fixture = self.root / "valid"
        shutil.copytree(ROOT / "tests" / "fixtures" / "valid-one-page", fixture)
        before = (fixture / "project.json").read_bytes()
        transaction = prepare_interrupted_write(fixture, b"{}\n")
        process = None
        try:
            process = subprocess.Popen(
                [self.native, "--json", "validate", fixture, "--stage", "plan"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.35)
            self.assertEqual(b"{}\n", (fixture / "project.json").read_bytes())
        except BaseException:
            if process is not None:
                process.kill()
                process.communicate()
            raise
        finally:
            transaction._lock.__exit__(None, None, None)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((0, b""), (process.returncode, stderr))
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertEqual(before, (fixture / "project.json").read_bytes())

    def test_validate_checks_current_page_qa_and_lettering_with_no_python(self) -> None:
        from comic_sol_product.engine import comic_sol
        from comic_sol_product.engine.page_quality import write_page_quality_record
        from tests.test_page_quality import PageQualityTests

        fixture = PageQualityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        project = fixture.project
        project_path = project / "project.json"
        manifest = comic_sol.read_json(project_path)
        storyboard = manifest["artifacts"]["storyboard"]
        storyboard["sha256"] = comic_sol.sha256_file(project / storyboard["path"])
        comic_sol.atomic_write_json(project_path, manifest)

        record = fixture._build_record()
        write_page_quality_record(project, 1, record)
        options = {"command": "validate", "arguments": ("--stage", "export-ready")}
        expected = self.python_call(project, **options)
        self.assertEqual(0, expected.returncode, expected.stdout)
        self.assert_cli_equal(expected, self.rust_call(project, **options))

        check = next(item for item in record["checks"] if item["id"] == "bubble-tail-direction")
        check["regions"][0]["speaker"] = "other"
        comic_sol.atomic_write_json(project / "qa/pages/page-001.json", record)
        expected = self.python_call(project, **options)
        self.assertIn(b"bubble-tail-evidence-mismatch: region is stale", expected.stdout)
        self.assert_cli_equal(expected, self.rust_call(project, **options))

    def test_validate_reconciles_handoff_receipts_with_no_python(self) -> None:
        from comic_sol_product.engine import comic_sol
        from tests.test_handoff_lifecycle import HandoffIntakeFailureTests

        fixture = HandoffIntakeFailureTests()
        self.addCleanup(fixture.doCleanups)
        root, project, job = fixture._prepared_reference()
        raster = root / "reference.png"
        fixture._write_raster(raster, (512, 512), (40, 80, 120))
        result = comic_sol.accept_handoff_result(
            project, **fixture._success_arguments(job, raster, approve_reference=True)
        )
        options = {"command": "validate", "arguments": ("--stage", "storyboard")}
        expected = self.python_call(project, **options)
        self.assertEqual(0, expected.returncode, expected.stdout)
        self.assert_cli_equal(expected, self.rust_call(project, **options))

        path = project / result["receipt_path"]
        receipt = comic_sol.read_json(path)
        receipt["job_sha256"] = "0" * 64
        comic_sol.atomic_write_json(path, receipt)
        expected = self.python_call(project, **options)
        self.assertIn(b"does not match the generation job", expected.stdout)
        self.assert_cli_equal(expected, self.rust_call(project, **options))

    def test_resume_preserves_cache_and_repairs_event_tail_without_python(self) -> None:
        from comic_sol_product.engine import comic_sol
        from tests.test_resume import ResumeTests

        fixture = ResumeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        project = fixture.project
        manifest_path = project / "project.json"
        original = comic_sol.read_json(manifest_path)
        original.update(
            status="BLOCKED",
            blocked_from="COMPOSED",
            blocked_reason="quality-failure",
            warnings=["quality failure", "unrelated warning"],
        )
        comic_sol.atomic_write_json(manifest_path, original)
        log_path = project / "logs/events.jsonl"
        prefix = log_path.read_bytes()
        log_path.write_bytes(prefix + b'{"torn":')
        cache = (project / "logs/stage-cache.json").read_bytes()
        panel = (project / "panels/raw/p01-01.png").read_bytes()

        result = self.rust_call(project, command="resume")
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        data = json.loads(result.stdout)["data"]
        self.assertEqual("COMPOSED", data["status"])
        self.assertEqual(
            ["planning", "storyboard", "generation", "lettering", "composition", "export"],
            data["preserved"],
        )
        self.assertEqual([], data["invalidated"])
        resumed = comic_sol.read_json(manifest_path)
        self.assertEqual(
            ("COMPOSED", None, None),
            (
                resumed["status"],
                resumed["blocked_from"],
                resumed["blocked_reason"],
            ),
        )
        self.assertEqual(["unrelated warning"], resumed["warnings"])
        self.assertEqual(cache, (project / "logs/stage-cache.json").read_bytes())
        self.assertEqual(panel, (project / "panels/raw/p01-01.png").read_bytes())
        log = log_path.read_bytes()
        self.assertTrue(log.startswith(prefix))
        events = [json.loads(line) for line in log[len(prefix) :].splitlines()]
        self.assertTrue(all(event["event"] == "artifact.reused" for event in events[:-1]))
        self.assertEqual("project.resumed", events[-1]["event"])
        self.assertEqual(
            {"blocked_reason": "quality-failure", "from": "BLOCKED", "to": "COMPOSED"},
            events[-1]["details"],
        )
        self.assert_cli_equal(result, self.rust_call(project, command="resume"))
        self.assertEqual(log, log_path.read_bytes())
        self.assertEqual([], list((project / "logs/transactions").iterdir()))

    def test_resume_rolls_back_python_journal_before_calculating_stages(self) -> None:
        from tests.test_resume import ResumeTests

        fixture = ResumeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        expected = self.python_call(fixture.project, command="resume")
        manifest_path = fixture.project / "project.json"
        before = manifest_path.read_bytes()
        transaction = prepare_interrupted_write(fixture.project, b'{"status":"partial"}\n')
        process = None
        try:
            process = subprocess.Popen(
                [self.native, "--json", "resume", fixture.project],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.35)
        except BaseException:
            if process is not None:
                process.kill()
                process.communicate()
            raise
        finally:
            transaction._lock.__exit__(None, None, None)

        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(
            (expected.returncode, expected.stdout, expected.stderr),
            (process.returncode, stdout, stderr),
        )
        self.assertEqual(before, manifest_path.read_bytes())
        self.assertEqual([], list((fixture.project / "logs/transactions").iterdir()))

    def test_finalize_renders_and_exports_without_python_after_page_review(self) -> None:
        from PIL import Image

        from comic_sol_product.engine.comic_sol import read_json, sha256_file
        from comic_sol_product.engine.pdf_quality import verify_pdf_payload
        from comic_sol_product.engine.validate_project import require_valid_project
        from tests.test_golden_pipeline import GoldenPipelineTests

        fixture = GoldenPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture._prepare_panel_artifacts()
        project = fixture.project
        clean = project / "panels/p01-01/clean.png"
        before = clean.read_bytes()

        first = self.rust_call(project, command="finalize")
        self.assertEqual((2, b""), (first.returncode, first.stderr))
        self.assertEqual("CS-QA-001", json.loads(first.stdout)["error"]["code"])
        self.assertEqual("COMPOSED", read_json(project / "project.json")["status"])
        lettered = project / "panels/p01-01/lettered.png"
        page = project / "pages/page-001.png"
        lettering = read_json(project / "panels/p01-01/lettering.json")
        self.assertEqual("mira", lettering["items"][0]["attribution"]["speaker"])
        with Image.open(page) as image:
            self.assertEqual((1600, 2400), image.size)
        self.assertNotEqual(before, lettered.read_bytes())
        self.assertEqual(before, clean.read_bytes())
        self.assertFalse(list((project / "exports").glob("*.pdf")))

        fixture._prepare_page_quality()
        require_valid_project(project, "export-ready")
        second = self.rust_call(project, command="finalize")
        self.assertEqual((0, b""), (second.returncode, second.stderr), second.stdout)
        data = json.loads(second.stdout)["data"]
        self.assertEqual("COMPLETE", data["status"])
        require_valid_project(project, "final")
        pdf = project / data["pdf"]
        verification = read_json(project / "exports/pdf-verification.json")
        self.assertEqual(sha256_file(pdf), verification["pdf_sha256"])
        with Image.open(page) as source:
            self.assertEqual(1, verify_pdf_payload(pdf.read_bytes(), [source])["page_count"])
        self.assertEqual(sha256_file(page), verification["source_pages"][0]["sha256"])
        self.assertIn("**COMPLETE**", (project / data["report"]).read_text("utf-8"))

    def test_finalize_rejects_stale_page_review_before_writing_pdf(self) -> None:
        from comic_sol_product.engine.comic_sol import atomic_write_json, read_json
        from tests.test_golden_pipeline import GoldenPipelineTests

        fixture = GoldenPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture._prepare_ready_artifacts()
        qa = fixture.project / "qa/pages/page-001.json"
        review = read_json(qa)
        review["bindings"]["page_sha256"] = "0" * 64
        atomic_write_json(qa, review)
        before = (fixture.project / "project.json").read_bytes()

        python = self.python_call(fixture.project, command="finalize")
        native = self.rust_call(fixture.project, command="finalize")
        self.assert_cli_equal(python, native)
        self.assertEqual(2, native.returncode)
        self.assertEqual("CS-QA-001", json.loads(native.stdout)["error"]["code"])
        self.assertFalse(list((fixture.project / "exports").glob("*.pdf")))
        self.assertEqual(before, (fixture.project / "project.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
