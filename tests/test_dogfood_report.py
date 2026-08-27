import hashlib
import json
import socket
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from comic_sol_product.version import VERSION
from scripts.comic_sol import init_project
from scripts.dogfood_report import (
    DogfoodReportError,
    build_report,
    canonical_report_bytes,
    derive_project_metrics,
    render_preview,
    validate_creator_inputs,
    validate_report,
    validate_report_file,
    write_report,
)
from scripts.handoff import (
    attempt_id,
    build_generation_job,
    build_generation_receipt,
    generation_job_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REPORT = ROOT / "tests/fixtures/dogfood/valid-report-v1.0.json"
PRIVACY_CANARIES = (
    "private-title-canary",
    "mira-private-name",
    "draw-a-secret-castle",
    "negative-prompt-canary",
    "sk-live-provider-secret",
    "https://private.example.invalid/v1",
    "/home/alice/private/comic",
    r"C:\\Users\\Alice\\Private\\comic",
    "a" * 64,
    "provider-raw-response-canary",
)


class DogfoodReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root,
            "private-title-canary",
            b"draw-a-secret-castle",
            {"mode": "short_prompt", "language": "en"},
            page_count=1,
        )
        self._seed_project()
        self.creator = {
            "setup_minutes": 12,
            "first_project_minutes": 19,
            "pdf_minutes": 47,
            "manual_intervention": False,
            "would_use_again": True,
            "failed_resume_attempts": 2,
            "friction_categories": ["installation", "handoff", "installation"],
            "cohort_alias": "creator-cohort-07",
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_json(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _seed_project(self):
        pdf = self.project / "exports/private-title-canary.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\nprivate fixture\n")
        pdf_digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        page = self.project / "pages/page-001.png"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(b"retained-page-evidence")
        page_qa = self._write_json(
            "qa/pages/page-001.json",
            {"private": "mira-private-name", "status": "reviewed"},
        )
        verification = {
            "kind": "pdf-verification",
            "page_count": 1,
            "pdf_path": "exports/private-title-canary.pdf",
            "pdf_sha256": pdf_digest,
            "schema_version": "1.0",
            "source_pages": [
                {
                    "dimensions": [1600, 2400],
                    "page_qa_path": "qa/pages/page-001.json",
                    "page_qa_sha256": hashlib.sha256(page_qa.read_bytes()).hexdigest(),
                    "path": "pages/page-001.png",
                    "sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
                }
            ],
            "verified_at": "2026-08-24T00:00:00Z",
        }
        verification_path = self._write_json("exports/pdf-verification.json", verification)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["status"] = "COMPLETE_WITH_WARNINGS"
        manifest["settings"]["page_count"] = 1
        manifest["settings"]["panel_count"] = 2
        manifest["panels"] = ["p01-01", "p01-02"]
        manifest["warnings"] = ["provider-raw-response-canary"]
        manifest["blocked_from"] = None
        manifest["blocked_reason"] = None
        manifest["capability"] = {
            "status": "available",
            "name": "sk-live-provider-secret",
            "supports_reference_images": True,
            "supports_dimensions": True,
            "detected_at": "2026-08-24T00:00:00Z",
        }
        manifest["artifacts"].update(
            {
                "pdf": {
                    "path": "exports/private-title-canary.pdf",
                    "sha256": pdf_digest,
                },
                "pdf_verification": {
                    "path": "exports/pdf-verification.json",
                    "sha256": hashlib.sha256(verification_path.read_bytes()).hexdigest(),
                },
            }
        )
        self._write_json("project.json", manifest)
        self._write_json(
            "plan/storyboard.json",
            {
                "schema_version": "1.0",
                "pages": [
                    {
                        "number": 1,
                        "panels": [
                            {
                                "id": "p01-01",
                                "characters": ["mira-private-name"],
                                "prompt": "draw-a-secret-castle",
                            },
                            {"id": "p01-02", "negative_prompt": "negative-prompt-canary"},
                        ],
                    }
                ],
            },
        )
        source = self.project / "source/input.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "/home/alice/private/comic C:\\Users\\Alice\\Private\\comic",
            encoding="utf-8",
        )
        self._write_json(
            "logs/stage-cache.json",
            {
                "schema_version": "1.0",
                "stages": {
                    "generation": {"private": "draw-a-secret-castle"},
                    "planning": {"private": "mira-private-name"},
                    "storyboard": {"private": "negative-prompt-canary"},
                },
            },
        )
        self._write_json(
            "logs/generation-counters.json",
            {
                "schema_version": "1.0",
                "global_extra_calls": 2,
                "panels": {
                    "p01-01": {"initial": 1, "transient_repeats": 1, "visual_retries": 1},
                    "p01-02": {"initial": 1, "transient_repeats": 0, "visual_retries": 0},
                },
            },
        )
        events = [
            {
                "event": "project.transitioned",
                "details": {
                    "from": "STORYBOARDED",
                    "to": "BLOCKED",
                    "blocked_reason": "image-capability-unavailable",
                    "warning_present": True,
                },
                "timestamp": "2026-08-24T01:00:00Z",
            },
            {
                "event": "project.transitioned",
                "details": {
                    "from": "STORYBOARDED",
                    "to": "BLOCKED",
                    "blocked_reason": "mira-private-name",
                    "warning_present": True,
                },
                "timestamp": "2026-08-24T01:02:00Z",
            },
            {
                "event": "project.resumed",
                "details": {
                    "from": "BLOCKED",
                    "to": "STORYBOARDED",
                    "blocked_reason": "image-capability-unavailable",
                },
                "timestamp": "2026-08-24T01:05:00Z",
            },
            {
                "event": "handoff.prepared",
                "details": {"count": 2, "kind": "panel", "project_id": "private-title-canary"},
                "timestamp": "2026-08-24T01:06:00Z",
            },
            {
                "event": "handoff.result-accepted",
                "details": {"attempt_id": "attempt-safe", "job_id": "b" * 64, "kind": "panel"},
                "timestamp": "2026-08-24T01:06:30Z",
            },
            *[
                {
                    "event": "stage.recorded",
                    "details": {"action": stage},
                    "timestamp": f"2026-08-24T01:06:{31 + index:02d}Z",
                }
                for index, stage in enumerate(("planning", "storyboard", "generation"))
            ],
            {
                "event": "panel.overridden",
                "details": {"panel_id": "p01-02", "action": "accepted"},
                "timestamp": "2026-08-24T01:07:00Z",
            },
        ]
        event_path = self.project / "logs/events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
            encoding="utf-8",
        )
        for index, panel_id in enumerate(("p01-01", "p01-02"), start=1):
            target = f"panels/attempts/{panel_id}/initial-001.png"
            job = build_generation_job(
                subject_kind="panel",
                subject_id=panel_id,
                prompt_path=f"prompts/panels/{panel_id}.txt",
                prompt_sha256="b" * 64,
                references=[],
                requested_dimensions=None,
                requested_aspect_ratio=None,
                attempt_kind="initial",
                retry_limit=1,
                batch_id="batch-one",
                target_path=target,
            )
            self._write_json(f"generation/jobs/{job['job_id']}.json", job)
            identifier = attempt_id(job_id=job["job_id"], attempt=1)
            success = index == 1
            if success:
                raster = self.project / target
                raster.parent.mkdir(parents=True, exist_ok=True)
                raster.write_bytes(b"retained-raster-evidence")
                raster_path = target
                raster_sha256 = hashlib.sha256(raster.read_bytes()).hexdigest()
            else:
                raster_path = None
                raster_sha256 = None
            receipt = build_generation_receipt(
                attempt_id=identifier,
                job_id=job["job_id"],
                job_sha256=generation_job_sha256(job),
                raster_path=raster_path,
                raster_sha256=raster_sha256,
                executor_kind="native-tool" if success else "external-tool",
                executor_id=f"executor-{index}",
                provider="private-provider",
                model="private-model",
                capabilities_used={
                    "reference_images": success,
                    "dimensions": not success,
                    "localized_edit": False,
                },
                outcome="success" if success else "failure",
                category="accepted" if success else "provider-refusal",
            )
            self._write_json(f"generation/receipts/{identifier}.json", receipt)
        self._write_json(
            "generation/private-payload.json",
            {
                "endpoint": "https://private.example.invalid/v1",
                "raw_response": "provider-raw-response-canary",
                "secret": "sk-live-provider-secret",
            },
        )

    def _build(self, *, consent=True, creator=None):
        return build_report(
            self.project,
            comic_sol_version=VERSION,
            creator_inputs=self.creator if creator is None else creator,
            consent_to_share=consent,
        )

    def test_exact_valid_report_and_root_contract(self):
        report = self._build()
        expected = json.loads(EXPECTED_REPORT.read_text(encoding="utf-8"))

        self.assertEqual(expected, report)
        self.assertEqual(
            {
                "kind",
                "schema_version",
                "comic_sol_version",
                "project_schema_version",
                "derived",
                "creator",
                "consent",
                "limitations",
            },
            set(report),
        )
        self.assertNotIn("timestamp", canonical_report_bytes(report).decode("utf-8"))
        self.assertNotIn("report_id", report)
        validate_report(report, require_consent=True)

    def test_complete_blocked_resumed_handoff_retry_override_warning_and_failed_metrics(self):
        derived = derive_project_metrics(self.project)
        self.assertEqual("COMPLETE_WITH_WARNINGS", derived["terminal_status"])
        self.assertEqual(["image-capability-unavailable", "other"], derived["blocked_categories"])
        self.assertEqual(1, derived["successful_resumes"])
        self.assertEqual(2, derived["handoff_count"])
        self.assertEqual(1, derived["handoff_completions"])
        self.assertEqual([], derived["handoff_routes"])
        self.assertEqual({"initial": 2, "retries": 2, "total": 4}, derived["generation_attempts"])
        self.assertEqual({"numerator": 2, "denominator": 4}, derived["retry_rate"])
        self.assertEqual(1, derived["manual_override_count"])
        self.assertEqual(1, derived["unresolved_warning_count"])

        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["status"] = "BLOCKED"
        manifest["blocked_from"] = "EXPORTED"
        manifest["blocked_reason"] = "provider-refusal"
        manifest["artifacts"] = {}
        self._write_json("project.json", manifest)
        blocked = derive_project_metrics(self.project)
        self.assertEqual("BLOCKED", blocked["terminal_status"])
        self.assertFalse(blocked["final_pdf_verified"])
        self.assertEqual(
            ["image-capability-unavailable", "other", "provider-refusal"],
            blocked["blocked_categories"],
        )

    def test_privacy_allowlist_excludes_every_seeded_canary_from_json_and_preview(self):
        report = self._build()
        serialized = canonical_report_bytes(report).decode("utf-8")
        preview = render_preview(report)
        for canary in PRIVACY_CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, serialized)
                self.assertNotIn(canary, preview)
        self.assertNotIn("project_id", serialized)
        self.assertNotIn("provider", serialized)
        self.assertNotIn("model", serialized)
        self.assertNotIn("path", serialized)
        self.assertNotIn("sha256", serialized)

    def test_creator_bounds_enums_slug_and_deduplication(self):
        validated = validate_creator_inputs(**self.creator)
        self.assertEqual(["installation", "handoff"], validated["friction_categories"])
        invalid = (
            {**self.creator, "setup_minutes": -1},
            {**self.creator, "pdf_minutes": 10081},
            {**self.creator, "manual_intervention": "no"},
            {**self.creator, "would_use_again": 1},
            {**self.creator, "failed_resume_attempts": 1001},
            {**self.creator, "friction_categories": ["free form secret"]},
            {**self.creator, "cohort_alias": "Alice Smith"},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(DogfoodReportError):
                    validate_creator_inputs(**values)

    def test_deterministic_canonical_bytes_use_no_current_clock(self):
        first = canonical_report_bytes(self._build())
        with patch("time.time", side_effect=AssertionError("clock must not be read")):
            second = canonical_report_bytes(self._build())
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(first, EXPECTED_REPORT.read_bytes())

    def test_legacy_project_is_read_without_migration_or_mutation(self):
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("schema_version")
        manifest.pop("handoff", None)
        self._write_json("project.json", manifest)
        before = {
            p.relative_to(self.project): p.read_bytes()
            for p in self.project.rglob("*")
            if p.is_file()
        }

        report = self._build()

        after = {
            p.relative_to(self.project): p.read_bytes()
            for p in self.project.rglob("*")
            if p.is_file()
        }
        self.assertEqual("1.0", report["project_schema_version"])
        self.assertEqual(before, after)

    def test_preview_never_writes_and_persisted_report_requires_consent_and_external_output(self):
        preview_report = self._build(consent=False)
        before = {
            p.relative_to(self.project): p.read_bytes()
            for p in self.project.rglob("*")
            if p.is_file()
        }
        preview = render_preview(preview_report)
        self.assertFalse(json.loads(preview)["consent"]["share_report"])
        self.assertEqual(preview_report, json.loads(preview))
        self.assertEqual(
            before,
            {
                p.relative_to(self.project): p.read_bytes()
                for p in self.project.rglob("*")
                if p.is_file()
            },
        )

        with self.assertRaisesRegex(DogfoodReportError, "consent"):
            write_report(
                self.root / "without-consent.json", preview_report, project_dir=self.project
            )
        for output in (self.project / "report.json", self.project / "nested/report.json"):
            with self.subTest(output=output), self.assertRaisesRegex(DogfoodReportError, "outside"):
                write_report(output, self._build(), project_dir=self.project)
        output = self.root / "submission/report.json"
        write_report(output, self._build(), project_dir=self.project)
        self.assertEqual(EXPECTED_REPORT.read_bytes(), output.read_bytes())

    def test_export_and_validation_are_offline_read_only_and_do_not_mutate_project(self):
        before = {
            p.relative_to(self.project): p.read_bytes()
            for p in self.project.rglob("*")
            if p.is_file()
        }
        output = self.root / "report.json"
        original_socket = socket.socket
        with (
            patch("socket.socket", side_effect=AssertionError("network must not be used")),
            patch("urllib.request.urlopen", side_effect=AssertionError("network must not be used")),
        ):
            write_report(output, self._build(), project_dir=self.project)
            validated = validate_report_file(output, require_consent=True)
        socket.socket = original_socket
        self.assertEqual(self._build(), validated)
        self.assertEqual(
            before,
            {
                p.relative_to(self.project): p.read_bytes()
                for p in self.project.rglob("*")
                if p.is_file()
            },
        )

    def test_incomplete_pdf_verification_is_not_reported_as_verified(self):
        verification_path = self.project / "exports/pdf-verification.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        verification["source_pages"] = []
        self._write_json("exports/pdf-verification.json", verification)
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["artifacts"]["pdf_verification"]["sha256"] = hashlib.sha256(
            verification_path.read_bytes()
        ).hexdigest()
        self._write_json("project.json", manifest)

        derived = derive_project_metrics(self.project)

        self.assertFalse(derived["final_pdf_verified"])

    def test_malformed_or_unbound_receipt_is_rejected_not_counted(self):
        receipt_path = next((self.project / "generation/receipts").glob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["raw_response"] = "provider-raw-response-canary"
        self._write_json(receipt_path.relative_to(self.project), receipt)

        with self.assertRaisesRegex(DogfoodReportError, "receipt evidence is invalid"):
            derive_project_metrics(self.project)

    def test_output_rejects_symlinked_parent_and_project_redirect(self):
        real_output = self.root / "real-output"
        real_output.mkdir()
        linked_output = self.root / "linked-output"
        try:
            linked_output.symlink_to(real_output, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(DogfoodReportError, "symlink"):
            write_report(linked_output / "report.json", self._build(), project_dir=self.project)

        project_redirect = self.project / "redirect"
        project_redirect.symlink_to(real_output, target_is_directory=True)
        with self.assertRaisesRegex(DogfoodReportError, "outside"):
            write_report(project_redirect / "report.json", self._build(), project_dir=self.project)

    def test_report_validation_rejects_unknown_fields_schema_and_private_free_text(self):
        report = self._build()
        invalid = []
        unknown = deepcopy(report)
        unknown["project_id"] = "private-title-canary"
        invalid.append(unknown)
        mixed = deepcopy(report)
        mixed["schema_version"] = "2.0"
        invalid.append(mixed)
        free_text = deepcopy(report)
        free_text["creator"]["friction_categories"] = ["draw-a-secret-castle"]
        invalid.append(free_text)
        no_consent = deepcopy(report)
        no_consent["consent"]["share_report"] = False
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(DogfoodReportError):
                validate_report(candidate)
        with self.assertRaises(DogfoodReportError):
            validate_report(no_consent, require_consent=True)

    def test_malformed_manifest_counters_and_cache_cannot_create_false_metrics(self):
        manifest_path = self.project / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["private-extra"] = "provider-raw-response-canary"
        self._write_json("project.json", manifest)
        with self.assertRaisesRegex(DogfoodReportError, "manifest evidence is invalid"):
            derive_project_metrics(self.project)

        manifest.pop("private-extra")
        self._write_json("project.json", manifest)
        counters_path = self.project / "logs/generation-counters.json"
        counters = json.loads(counters_path.read_text(encoding="utf-8"))
        counters["global_extra_calls"] = 0
        self._write_json("logs/generation-counters.json", counters)
        with self.assertRaisesRegex(DogfoodReportError, "retry counters are inconsistent"):
            derive_project_metrics(self.project)

        counters["global_extra_calls"] = 2
        self._write_json("logs/generation-counters.json", counters)
        event_path = self.project / "logs/events.jsonl"
        events = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") != "stage.recorded"
        ]
        event_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        self.assertEqual([], derive_project_metrics(self.project)["completed_stages"])

    def test_handoff_counts_use_prepared_units_and_committed_completion_events(self):
        derived = derive_project_metrics(self.project)
        self.assertEqual((2, 1), (derived["handoff_count"], derived["handoff_completions"]))

        event_path = self.project / "logs/events.jsonl"
        events = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") != "handoff.result-accepted"
        ]
        event_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        without_completion = derive_project_metrics(self.project)
        self.assertEqual(2, without_completion["handoff_count"])
        self.assertEqual(0, without_completion["handoff_completions"])
        self.assertEqual(["external-tool", "native-tool"], without_completion["executor_kinds"])

    def test_blocked_report_has_an_exact_complete_projection(self):
        expected = self._build()
        manifest = json.loads((self.project / "project.json").read_text(encoding="utf-8"))
        manifest["status"] = "BLOCKED"
        manifest["blocked_from"] = "EXPORTED"
        manifest["blocked_reason"] = "provider-refusal"
        manifest["artifacts"] = {}
        self._write_json("project.json", manifest)
        expected["derived"]["terminal_status"] = "BLOCKED"
        expected["derived"]["blocked_categories"] = [
            "image-capability-unavailable",
            "other",
            "provider-refusal",
        ]
        expected["derived"]["final_pdf_verified"] = False
        self.assertEqual(expected, self._build())

    def test_incomplete_transaction_is_rejected_without_recovery_or_mutation(self):
        transaction = self.project / "logs/transactions/999"
        transaction.mkdir(parents=True)
        self._write_json(
            "logs/transactions/999/journal.json",
            {
                "schema_version": "1.0",
                "operation": "handoff-prepare",
                "phase": "publishing",
                "targets": [],
            },
        )
        before = {
            path.relative_to(self.project): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(DogfoodReportError, "incomplete transaction"):
            derive_project_metrics(self.project)

        after = {
            path.relative_to(self.project): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_release_inventory_requires_runtime_report_module(self):
        from comic_sol_product.release import REQUIRED_SDIST_SUFFIXES, REQUIRED_WHEEL_MEMBERS

        setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('REQUIRED_RUNTIME_SCRIPTS = frozenset({"dogfood_report.py"})', setup_text)
        self.assertIn("comic_sol_product/engine/dogfood_report.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("/scripts/dogfood_report.py", REQUIRED_SDIST_SUFFIXES)


if __name__ == "__main__":
    unittest.main()
