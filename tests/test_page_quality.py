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
                "balloon-subject-obstruction", "bubble-tail-geometry",
                "balloon-crowding",
            ),
            PAGE_CHECK_IDS,
        )
        self.assertEqual(
            frozenset({
                "clipped-text", "text-overlap", "reading-order",
                "layout-border-integrity", "balloon-subject-obstruction",
                "bubble-tail-geometry", "balloon-crowding",
            }),
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
        # Add a clipped box (outside the clean raster) to test clipping detection.
        clipped = dict(geometry["items"][0])
        clipped["id"] = "clipped"
        clipped["reading_order"] = 2
        clipped["box"] = {"x": -10, "y": -10, "width": 100, "height": 100}
        # Add an in-bounds box that overlaps the first item to test overlap detection.
        overlapping = dict(geometry["items"][0])
        overlapping["id"] = "overlap"
        overlapping["reading_order"] = 1
        overlapping["box"] = {"x": 29, "y": 24, "width": 100, "height": 100}
        geometry["items"].extend([clipped, overlapping])
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


BALLOON_LAYOUTS = ROOT / "tests/fixtures/balloon-layouts"


def apply_balloon_layout(project, layout):
    """Rewrite lettering geometry so one named balloon layout is in effect."""
    for panel_id, edits in layout.get("panels", {}).items():
        path = project / f"panels/{panel_id}/lettering.json"
        geometry = json.loads(path.read_text("utf-8"))
        items = {item["id"]: item for item in geometry["items"]}
        for item_id, changes in edits.get("replace", {}).items():
            changes = dict(changes)
            tail_fields = changes.pop("tail_fields", None)
            items[item_id].update(changes)
            if tail_fields:
                items[item_id]["tail"].update(tail_fields)
        geometry["items"] = list(items.values()) + list(edits.get("append", []))
        path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )


class BalloonPlacementQualityTests(unittest.TestCase):
    """Deterministic balloon placement QA over named good and bad layouts."""

    @classmethod
    def setUpClass(cls):
        # Render once: every layout is a cheap copy of the same lettered project,
        # so the fixtures stay comparable and the suite stays fast.
        cls._base_directory = tempfile.TemporaryDirectory()
        cls.base = Path(cls._base_directory.name) / "lettered"
        shutil.copytree(FIXTURE, cls.base)
        letter_project(cls.base)
        compose_all_pages(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls._base_directory.cleanup()

    def _fresh_project(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = Path(directory.name) / "project"
        shutil.copytree(self.base, project)
        return project

    def _record(self, project):
        return build_page_quality_record(
            project,
            1,
            reviewer_checks(project),
            reviewer="fixture-reviewer",
            reviewed_at="2026-08-14T01:02:03Z",
        )

    def _checks(self, project):
        return {check["id"]: check for check in self._record(project)["checks"]}

    def test_named_balloon_layouts_reach_their_expected_verdict(self):
        paths = sorted(BALLOON_LAYOUTS.glob("*.json"))
        self.assertEqual(9, len(paths))
        for path in paths:
            layout = json.loads(path.read_text("utf-8"))
            with self.subTest(layout=path.stem):
                project = self._fresh_project()
                apply_balloon_layout(project, layout)
                record = self._record(project)
                checks = {check["id"]: check for check in record["checks"]}

                self.assertEqual(layout["expected_decision"], record["decision"])
                expected_check = layout["expected_check"]
                if expected_check is None:
                    for check_id in DETERMINISTIC_PAGE_CHECK_IDS:
                        self.assertEqual("pass", checks[check_id]["result"])
                        self.assertEqual([], checks[check_id]["regions"])
                    continue

                check = checks[expected_check]
                self.assertEqual(layout["expected_result"], check["result"])
                self.assertNotEqual([], check["regions"])
                for reason in layout.get("expected_reasons", []):
                    self.assertIn(
                        reason, [region.get("reason") for region in check["regions"]]
                    )

    def test_out_of_bounds_is_measured_in_the_clean_raster_not_the_page_rect(self):
        project = self._fresh_project()
        storyboard = json.loads((project / "plan/storyboard.json").read_text("utf-8"))
        rect = storyboard["pages"][0]["panels"][0]["rect"]
        normalization = json.loads(
            (project / "panels/p01-01/normalization.json").read_text("utf-8")
        )
        clean_width, clean_height = normalization["clean"]["size"]
        layout = json.loads(
            (BALLOON_LAYOUTS / "bad-out-of-bounds-clean-space.json").read_text("utf-8")
        )
        box = layout["panels"]["p01-01"]["replace"]["p01-01-t01"]["box"]
        # The box is only detectable as clipped in clean-raster space: it fits the
        # storyboard page rectangle this panel is later fitted into.
        self.assertLessEqual(box["x"] + box["width"], rect["width"])
        self.assertGreater(box["x"] + box["width"], clean_width)
        self.assertLessEqual(box["y"] + box["height"], clean_height)

        apply_balloon_layout(project, layout)
        checks = self._checks(project)

        self.assertEqual("fail", checks["clipped-text"]["result"])
        self.assertEqual(
            [{"box": box, "item_id": "p01-01-t01", "panel_id": "p01-01"}],
            checks["clipped-text"]["regions"],
        )
        self.assertEqual("pass", checks["reading-order"]["result"])
        self.assertEqual("pass", checks["text-overlap"]["result"])

    def test_subject_obstruction_reports_measured_and_required_clearance(self):
        project = self._fresh_project()
        apply_balloon_layout(
            project,
            json.loads(
                (BALLOON_LAYOUTS / "bad-subject-obstruction-dialogue.json").read_text("utf-8")
            ),
        )
        check = self._checks(project)["balloon-subject-obstruction"]

        self.assertEqual("fail", check["result"])
        self.assertEqual("error", check["severity"])
        self.assertEqual("deterministic-geometry-v1", check["method"])
        self.assertEqual(
            [{
                "clearance": 0.0,
                "item_id": "p01-02-t01",
                "panel_id": "p01-02",
                # 720 * 0.025, the same gap the renderer reserves for a tail.
                "required_clearance": 18.0,
                "subject_text_id": "p01-02-t01",
            }],
            check["regions"],
        )

    def test_panels_without_authored_anchors_have_no_protected_subject(self):
        project = self._fresh_project()
        # p01-01 and p01-03 carry captions only, so no anchor exists to protect.
        for panel_id, item_id in (("p01-01", "p01-01-t01"), ("p01-03", "p01-03-t01")):
            apply_balloon_layout(project, {
                "panels": {
                    panel_id: {"replace": {item_id: {"box": {
                        "height": 120, "width": 200, "x": 40, "y": 60,
                    }}}}
                }
            })
        checks = self._checks(project)

        self.assertEqual("pass", checks["balloon-subject-obstruction"]["result"])
        self.assertEqual([], checks["balloon-subject-obstruction"]["regions"])

    def test_crowding_warning_is_actionable_and_never_blocks_the_page(self):
        project = self._fresh_project()
        apply_balloon_layout(
            project,
            json.loads((BALLOON_LAYOUTS / "warn-crowded-coverage.json").read_text("utf-8")),
        )
        record = self._record(project)
        check = {item["id"]: item for item in record["checks"]}["balloon-crowding"]

        self.assertEqual("warning", check["result"])
        self.assertEqual("warning", check["severity"])
        self.assertEqual("accept-warning", record["decision"])
        self.assertIn("p01-03", check["evidence"])
        self.assertIn(check["evidence"], record["unresolved_warnings"])
        self.assertEqual(0.3133, check["regions"][0]["coverage_ratio"])
        self.assertEqual(0.3, check["regions"][0]["coverage_limit"])

        # A warning is a complete, self-consistent record rather than a blocker.
        write_page_quality_record(project, 1, record)
        self.assertEqual((), validate_page_quality(project, 1))

    def test_crowding_reports_balloons_closer_than_the_readable_separation(self):
        project = self._fresh_project()
        apply_balloon_layout(
            project,
            json.loads(
                (BALLOON_LAYOUTS / "warn-crowded-separation.json").read_text("utf-8")
            ),
        )
        checks = self._checks(project)
        crowding = checks["balloon-crowding"]

        self.assertEqual("warning", crowding["result"])
        self.assertEqual(
            [{"items": ["p01-02-t01", "p01-02-t02"], "separation": 4.0}],
            crowding["regions"][0]["tight_pairs"],
        )
        self.assertEqual(14.4, crowding["regions"][0]["required_separation"])
        # Crowding is not overlap: the balloons still do not intersect.
        self.assertEqual("pass", checks["text-overlap"]["result"])

    def test_clean_layout_reports_no_crowding_and_keeps_empty_regions(self):
        project = self._fresh_project()
        check = self._checks(project)["balloon-crowding"]

        self.assertEqual("pass", check["result"])
        self.assertEqual("info", check["severity"])
        self.assertEqual([], check["regions"])
        self.assertEqual("accept", self._record(project)["decision"])

    def test_overlapping_balloons_report_shared_area_and_ratio(self):
        project = self._fresh_project()
        path = project / "panels/p01-01/lettering.json"
        geometry = json.loads(path.read_text("utf-8"))
        duplicate = dict(geometry["items"][0])
        duplicate.update({
            "box": {"height": 100, "width": 100, "x": 29, "y": 24},
            "id": "p01-01-t02",
            "reading_order": 2,
        })
        geometry["items"].append(duplicate)
        path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        check = self._checks(project)["text-overlap"]

        self.assertEqual("fail", check["result"])
        self.assertEqual(
            [{
                "items": ["p01-01-t01", "p01-01-t02"],
                "overlap_area": 10000,
                "overlap_ratio": 1.0,
                "panel_id": "p01-01",
            }],
            check["regions"],
        )

    def test_tail_geometry_is_verified_against_the_authored_speaker(self):
        cases = (
            ("bad-tail-points-away.json", "tail-does-not-point-at-speaker"),
            ("bad-tail-voice-source.json", "voice-source-mismatch"),
        )
        for name, reason in cases:
            with self.subTest(layout=name):
                project = self._fresh_project()
                apply_balloon_layout(
                    project, json.loads((BALLOON_LAYOUTS / name).read_text("utf-8"))
                )
                check = self._checks(project)["bubble-tail-geometry"]

                self.assertEqual("fail", check["result"])
                self.assertEqual(
                    [{"panel_id": "p01-02", "reason": reason, "text_id": "p01-02-t01"}],
                    check["regions"],
                )

    def test_tail_attachment_must_sit_on_the_drawn_balloon_outline(self):
        project = self._fresh_project()
        geometry = json.loads(
            (project / "panels/p01-02/lettering.json").read_text("utf-8")
        )
        box = next(
            item["box"] for item in geometry["items"] if item["id"] == "p01-02-t01"
        )
        centre = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]

        apply_balloon_layout(project, {
            "panels": {
                "p01-02": {
                    "replace": {"p01-02-t01": {"tail_fields": {"attachment": centre}}}
                }
            }
        })
        check = self._checks(project)["bubble-tail-geometry"]

        # The centre is inside the bounding box, so a box-membership test would
        # accept it. Only the ellipse outline exposes a tail attached to nothing.
        self.assertEqual("fail", check["result"])
        self.assertEqual(
            [{
                "panel_id": "p01-02",
                "reason": "detached-tail",
                "text_id": "p01-02-t01",
            }],
            check["regions"],
        )

    def test_corrupt_balloon_box_fails_the_tail_check_closed(self):
        # The placement box reaching the attachment check is arbitrary retained
        # JSON, so every malformed shape must produce a failed check rather than
        # an exception out of record construction.
        cases = (
            ("missing-dimensions", {"x": 10, "y": 20}),
            ("non-numeric", {"x": 10, "y": 20, "width": "wide", "height": 5}),
            ("float-dimensions", {"x": 10, "y": 20, "width": 5.5, "height": 5.5}),
            ("null-dimensions", {"x": 10, "y": 20, "width": None, "height": 5}),
            # Large enough to overflow the float conversion inside the primitive.
            ("overflowing-dimensions", {"x": 0, "y": 0, "width": 10**400, "height": 10**400}),
        )
        for name, box in cases:
            with self.subTest(box=name):
                project = self._fresh_project()
                apply_balloon_layout(project, {
                    "panels": {"p01-02": {"replace": {"p01-02-t01": {"box": box}}}}
                })
                check = self._checks(project)["bubble-tail-geometry"]

                self.assertEqual("fail", check["result"])
                self.assertEqual(
                    ["detached-tail"],
                    [region["reason"] for region in check["regions"]],
                )

    def test_tail_on_a_non_dialogue_placement_is_rejected(self):
        project = self._fresh_project()
        apply_balloon_layout(project, {
            "panels": {"p01-02": {"replace": {"p01-02-t01": {"kind": "caption"}}}}
        })
        check = self._checks(project)["bubble-tail-geometry"]

        # Only dialogue is drawn as a balloon, so there is no outline to verify.
        self.assertEqual("fail", check["result"])
        self.assertEqual(
            ["placement-kind-mismatch"],
            [region["reason"] for region in check["regions"]],
        )

    def test_tail_geometry_detects_a_stale_speaker_anchor_echo(self):
        project = self._fresh_project()
        path = project / "panels/p01-02/lettering.json"
        geometry = json.loads(path.read_text("utf-8"))
        for item in geometry["items"]:
            if item.get("tail"):
                item["tail"]["speaker_anchor"] = [0.2, 0.2]
        path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        check = self._checks(project)["bubble-tail-geometry"]

        self.assertEqual("fail", check["result"])
        self.assertEqual(
            [{
                "panel_id": "p01-02",
                "reason": "speaker-anchor-mismatch",
                "text_id": "p01-02-t01",
            }],
            check["regions"],
        )

    def test_missing_dialogue_tail_is_rejected_before_the_geometry_check(self):
        project = self._fresh_project()
        path = project / "panels/p01-02/lettering.json"
        geometry = json.loads(path.read_text("utf-8"))
        for item in geometry["items"]:
            item["tail"] = None
        path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        with self.assertRaisesRegex(ValueError, "dialogue tail is missing"):
            build_page_quality_record(
                project,
                1,
                reviewer_checks(),
                reviewer="fixture-reviewer",
                reviewed_at="2026-08-14T01:02:03Z",
            )

    def test_page_context_requires_the_normalization_pixel_space(self):
        project = self._fresh_project()
        (project / "panels/p01-02/normalization.json").unlink()

        with self.assertRaisesRegex(ValueError, "normalization record is missing"):
            self._record(project)

    def test_record_binds_the_normalization_records_it_measured_against(self):
        project = self._fresh_project()
        record = self._record(project)
        bindings = record["bindings"]

        self.assertEqual(
            ["p01-01", "p01-02", "p01-03"],
            [value.split(":")[0] for value in bindings["normalization_sha256s"]],
        )
        for value in bindings["normalization_sha256s"]:
            self.assertRegex(value.split(":")[1], r"^[0-9a-f]{64}$")

        write_page_quality_record(project, 1, record)
        self.assertEqual((), validate_page_quality(project, 1))

        # clean.size defines the space every balloon verdict is measured in, so a
        # re-normalized panel must invalidate the record even though the composed
        # page image is untouched.
        path = project / "panels/p01-02/normalization.json"
        path.write_bytes(path.read_bytes() + b"changed")
        issues = validate_page_quality(project, 1)

        self.assertTrue(any(
            issue.field == "bindings.normalization_sha256s"
            and issue.message.startswith("page-quality-stale:")
            for issue in issues
        ), issues)

    def test_out_of_range_speaker_anchor_is_not_treated_as_a_protected_subject(self):
        project = self._fresh_project()
        storyboard_path = project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        for panel in storyboard["pages"][0]["panels"]:
            for item in panel["text"]:
                if item.get("kind") == "dialogue":
                    item["speaker_anchor"] = [1.5, 0.5]
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        checks = self._checks(project)

        # An anchor outside the raster is a storyboard defect, so it is reported by
        # the tail check rather than silently trusted as a keep-out region.
        self.assertEqual("pass", checks["balloon-subject-obstruction"]["result"])
        self.assertEqual("fail", checks["bubble-tail-geometry"]["result"])
        self.assertEqual(
            ["speaker-anchor-mismatch"],
            [region["reason"] for region in checks["bubble-tail-geometry"]["regions"]],
        )

    def test_deterministic_warning_without_regions_cannot_manufacture_a_warning(self):
        project = self._fresh_project()
        record = self._record(project)
        crowding = next(
            check for check in record["checks"] if check["id"] == "balloon-crowding"
        )
        crowding.update({"result": "warning", "severity": "warning", "regions": []})
        record["decision"] = "accept-warning"
        record["unresolved_warnings"] = [crowding["evidence"]]
        write_page_quality_record(project, 1, record)

        issues = validate_page_quality(project, 1)

        self.assertTrue(any(
            issue.field == "checks"
            and "deterministic failing checks must include failure regions" in issue.message
            for issue in issues
        ), issues)


if __name__ == "__main__":
    unittest.main()
