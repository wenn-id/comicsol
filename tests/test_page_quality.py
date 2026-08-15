import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.compose_pages import compose_all_pages  # noqa: E402
from scripts.letter_panels import letter_project  # noqa: E402
from scripts.page_quality import (  # noqa: E402
    DETERMINISTIC_PAGE_CHECK_IDS,
    PAGE_CHECK_IDS,
    build_page_quality_record,
    validate_page_quality,
    write_page_quality_record,
)
from scripts.validate_project import validate_project  # noqa: E402


FIXTURE = ROOT / "tests/fixtures/valid-one-page"
SUBJECTIVE = {
    "face-action-obstruction",
    "bubble-tail-direction",
    "accidental-text-watermark",
}


def reviewer_checks(project=None):
    checks = [
        {
            "id": check_id,
            "result": "pass",
            "severity": "error",
            "evidence": f"Reviewer inspected bounded page evidence for {check_id}.",
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "regions": [],
        }
        for check_id in PAGE_CHECK_IDS if check_id in SUBJECTIVE
    ]
    if project is None:
        return checks

    storyboard = json.loads((project / "plan/storyboard.json").read_text("utf-8"))
    regions = []
    for panel in storyboard["pages"][0]["panels"]:
        geometry = json.loads(
            (project / f"panels/{panel['id']}/lettering.json").read_text("utf-8")
        )
        geometry_by_id = {item["id"]: item for item in geometry["items"]}
        for item in panel["text"]:
            if item["kind"] != "dialogue":
                continue
            tail = geometry_by_id[item["id"]]["tail"]
            regions.append({
                "panel_id": panel["id"],
                "text_id": item["id"],
                "speaker": item["speaker"],
                "voice_source": item["voice_source"],
                "speaker_anchor": item["speaker_anchor"],
                "tip": tail["tip"],
                "result": "pass",
            })
    next(check for check in checks if check["id"] == "bubble-tail-direction")[
        "regions"
    ] = regions
    return checks


class PageQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        shutil.copytree(FIXTURE, self.project)
        letter_project(self.project)
        compose_all_pages(self.project)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _build_record(self, checks=None, **provenance):
        return build_page_quality_record(
            self.project,
            1,
            reviewer_checks(self.project) if checks is None else checks,
            reviewer=provenance.get("reviewer", "fixture-reviewer"),
            reviewed_at=provenance.get("reviewed_at", "2026-08-14T01:02:03Z"),
        )

    def test_page_record_uses_supplied_review_provenance(self):
        record = self._build_record(
            reviewer="alwan-review", reviewed_at="2026-08-14T01:02:03Z"
        )

        self.assertEqual("alwan-review", record["review"]["reviewer"])
        self.assertEqual("2026-08-14T01:02:03Z", record["review"]["reviewed_at"])

    def test_page_record_requires_caller_supplied_review_provenance(self):
        with self.assertRaises(TypeError):
            build_page_quality_record(self.project, 1, reviewer_checks(self.project))

    def test_page_validation_rejects_fixture_or_invalid_provenance(self):
        record = self._build_record()
        record["review"]["reviewed_at"] = "fixture-deterministic"
        write_page_quality_record(self.project, 1, record)

        self.assertTrue(any(
            issue.field == "review.reviewed_at"
            for issue in validate_page_quality(self.project, 1)
        ))

    def test_tail_warning_requires_failed_region_and_records_warning(self):
        checks = reviewer_checks(self.project)
        tail = next(check for check in checks if check["id"] == "bubble-tail-direction")
        tail["result"] = "warning"
        tail["severity"] = "warning"
        tail["evidence"] = "One tail terminates slightly wide of its speaker."
        tail["regions"][0]["result"] = "fail"

        record = self._build_record(checks)

        self.assertEqual("accept-warning", record["decision"])
        self.assertEqual([tail["evidence"]], record["unresolved_warnings"])

    def test_tail_warning_cannot_hide_all_passing_regions(self):
        checks = reviewer_checks(self.project)
        tail = next(check for check in checks if check["id"] == "bubble-tail-direction")
        tail.update({"result": "warning", "severity": "warning"})

        with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
            self._build_record(checks)

    def test_tail_warning_severity_cannot_hide_all_passing_or_empty_regions(self):
        checks = reviewer_checks(self.project)
        tail = next(check for check in checks if check["id"] == "bubble-tail-direction")
        tail["severity"] = "warning"

        with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
            self._build_record(checks)

        storyboard_path = self.project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        for panel in storyboard["pages"][0]["panels"]:
            for item in panel["text"]:
                item["kind"] = "caption"
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        tail["regions"] = []
        with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
            self._build_record(checks)

    def test_persisted_page_record_requires_exact_v2_shape_and_bindings(self):
        cases = (
            ("extra", lambda record: record.update({"extra": True})),
            ("review", lambda record: record["review"].update({"extra": True})),
            ("bindings", lambda record: record["bindings"].update({"extra": True})),
            ("bindings.page_path", lambda record: record["bindings"].update({"page_path": "pages/page-999.png"})),
            ("bindings.composition_cache_path", lambda record: record["bindings"].pop("composition_cache_path")),
            ("bindings.page_width", lambda record: record["bindings"].update({"page_width": "1600"})),
        )
        for field, mutate in cases:
            with self.subTest(field=field):
                record = self._build_record()
                mutate(record)
                write_page_quality_record(self.project, 1, record)
                self.assertTrue(any(
                    issue.field == field
                    for issue in validate_page_quality(self.project, 1)
                ))

    def test_exact_check_ids_and_method_boundaries(self):
        self.assertEqual(
            (
                "clipped-text", "text-overlap", "face-action-obstruction",
                "bubble-tail-direction", "reading-order",
                "accidental-text-watermark", "layout-border-integrity",
            ),
            PAGE_CHECK_IDS,
        )
        self.assertEqual(
            frozenset({"clipped-text", "text-overlap", "reading-order", "layout-border-integrity"}),
            DETERMINISTIC_PAGE_CHECK_IDS,
        )

        record = self._build_record()
        checks = {check["id"]: check for check in record["checks"]}
        self.assertEqual(set(PAGE_CHECK_IDS), set(checks))
        for check_id in DETERMINISTIC_PAGE_CHECK_IDS:
            self.assertEqual("deterministic-geometry-v1", checks[check_id]["method"])
            self.assertEqual("comic-sol", checks[check_id]["reviewer"])
            self.assertEqual([], checks[check_id]["regions"])
        for check_id in SUBJECTIVE:
            self.assertEqual("bounded-visual-review", checks[check_id]["method"])
            self.assertEqual("fixture-reviewer", checks[check_id]["reviewer"])

    def test_missing_or_generic_subjective_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "quality-check-ids"):
            self._build_record([])
        generic = reviewer_checks(self.project)
        generic[0]["evidence"] = "ok"
        with self.assertRaisesRegex(ValueError, "quality-evidence-generic"):
            self._build_record(generic)

    def test_subjective_page_checks_use_their_normative_subset(self):
        checks = reviewer_checks(self.project)
        checks[0], checks[1] = checks[1], checks[0]
        with self.assertRaisesRegex(ValueError, "quality-check-ids"):
            self._build_record(checks)

    def test_page_context_rejects_lettering_panel_count_mismatch(self):
        checks = reviewer_checks(self.project)
        geometry = self.project / "panels/p01-01/lettering.json"
        geometry.unlink()
        with self.assertRaisesRegex(ValueError, "lettering|panel"):
            self._build_record(checks)

    def test_tail_direction_requires_one_current_region_per_dialogue(self):
        missing = reviewer_checks(self.project)
        tail_check = next(
            check for check in missing if check["id"] == "bubble-tail-direction"
        )
        tail_check["regions"] = []
        with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
            self._build_record(missing)

        stale = reviewer_checks(self.project)
        tail_check = next(
            check for check in stale if check["id"] == "bubble-tail-direction"
        )
        tail_check["regions"][0]["tip"][0] += 1
        with self.assertRaisesRegex(ValueError, "bubble-tail-evidence-mismatch"):
            self._build_record(stale)

    def test_record_binds_page_cache_layout_storyboard_and_ordered_lettering(self):
        record = self._build_record()
        bindings = record["bindings"]
        self.assertEqual("2.0", record["schema_version"])
        self.assertEqual("page-qa", record["kind"])
        self.assertEqual("page-001", record["subject_id"])
        self.assertEqual("hero-top-two-bottom", bindings["layout_name"])
        self.assertEqual("1", bindings["layout_version"])
        self.assertEqual("pages/page-001.png", bindings["page_path"])
        self.assertEqual((1600, 2400), (bindings["page_width"], bindings["page_height"]))
        self.assertRegex(bindings["page_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(bindings["composition_cache_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(bindings["storyboard_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(3, len(bindings["lettering_sha256s"]))
        self.assertTrue(all(value.startswith("p01-0") and len(value.split(":")) == 2
                            for value in bindings["lettering_sha256s"]))

    def test_write_is_canonical_and_current_record_validates(self):
        record = self._build_record()
        path = write_page_quality_record(self.project, 1, record)
        self.assertEqual((self.project / "qa/pages/page-001.json").resolve(), path)
        loaded = json.loads(path.read_text("utf-8"))
        self.assertEqual(record, loaded)
        self.assertEqual(
            json.dumps(loaded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            path.read_text("utf-8"),
        )
        self.assertEqual((), validate_page_quality(self.project, 1))

    def test_persisted_deterministic_pass_checks_reject_failure_regions(self):
        record = self._build_record()
        deterministic = next(
            check for check in record["checks"]
            if check["id"] in DETERMINISTIC_PAGE_CHECK_IDS
        )
        deterministic["regions"] = [{"scope": "page"}]
        write_page_quality_record(self.project, 1, record)

        issues = validate_page_quality(self.project, 1)

        self.assertTrue(any(
            issue.field == "checks"
            and "deterministic passing checks" in issue.message
            for issue in issues
        ), issues)

    def test_page_cache_storyboard_layout_and_lettering_drift_are_stale(self):
        record = self._build_record()
        write_page_quality_record(self.project, 1, record)
        cases = (
            ("pages/page-001.png", "bindings.page_sha256", "append"),
            ("cache/composition.json", "bindings.composition_cache_sha256", "append"),
            ("plan/storyboard.json", "bindings.storyboard_sha256", "append"),
            ("panels/p01-02/lettering.json", "bindings.lettering_sha256s", "append"),
        )
        for relative, field, operation in cases:
            with self.subTest(relative=relative):
                path = self.project / relative
                before = path.read_bytes()
                path.write_bytes(before + b"changed")
                issues = validate_page_quality(self.project, 1)
                self.assertTrue(any(
                    issue.field == field
                    and issue.message.startswith("page-quality-stale:")
                    for issue in issues
                ), issues)
                path.write_bytes(before)

        storyboard_path = self.project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        storyboard["pages"][0]["layout"] = "custom"
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        issues = validate_page_quality(self.project, 1)
        self.assertTrue(any(issue.field == "bindings.layout_name" for issue in issues), issues)

    def test_geometry_detects_clipping_overlap_and_bad_reading_order(self):
        geometry_path = self.project / "panels/p01-01/lettering.json"
        geometry = json.loads(geometry_path.read_text("utf-8"))
        duplicate = dict(geometry["items"][0])
        duplicate["id"] = "overlap"
        duplicate["reading_order"] = 1
        duplicate["box"] = {"x": -10, "y": -10, "width": 100, "height": 100}
        geometry["items"].append(duplicate)
        geometry_path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        record = self._build_record()
        checks = {check["id"]: check for check in record["checks"]}
        self.assertEqual("fail", checks["clipped-text"]["result"])
        self.assertEqual("fail", checks["text-overlap"]["result"])
        self.assertEqual("fail", checks["reading-order"]["result"])
        self.assertEqual("regenerate", record["decision"])

    def test_export_ready_gate_requires_current_schema_two_page_quality(self):
        record = self._build_record()
        write_page_quality_record(self.project, 1, record)
        current = validate_project(self.project, "export-ready")
        self.assertFalse(any(
            issue.path == "qa/pages/page-001.json"
            and (
                issue.message.startswith("page-quality-stale:")
                or "migration" in issue.message
            )
            for issue in current
        ), current)

        page = self.project / "pages/page-001.png"
        page.write_bytes(page.read_bytes() + b"stale")
        stale = validate_project(self.project, "export-ready")
        self.assertTrue(any(
            issue.path == "qa/pages/page-001.json"
            and issue.field == "bindings.page_sha256"
            and issue.message.startswith("page-quality-stale:")
            for issue in stale
        ), stale)


if __name__ == "__main__":
    unittest.main()
