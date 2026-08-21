import hashlib
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

from scripts import page_quality  # noqa: E402
from scripts.compose_pages import compose_all_pages  # noqa: E402
from scripts.letter_panels import letter_project  # noqa: E402
from scripts.page_quality import (  # noqa: E402
    DETERMINISTIC_PAGE_CHECK_IDS,
    PAGE_CHECK_IDS,
    build_page_quality_record,
    publish_page_quality_record,
    validate_page_quality,
    write_page_quality_record,
)
from scripts.project_io import ProjectLock  # noqa: E402
from scripts.validate_project import (  # noqa: E402
    validate_lettering_provenance,
    validate_project,
)

from tests.support import write_multi_speaker_panel  # noqa: E402


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
        self.assertEqual("2.1", record["schema_version"])
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

    def _count_artifact_reads(self, page_number=1):
        """Return one validation's issues with a read count per bound artifact."""
        reads = {}
        real_read = page_quality._read_and_digest

        def counted_read(path):
            key = Path(path).resolve()
            reads[key] = reads.get(key, 0) + 1
            return real_read(path)

        def counted_digest(path):
            return counted_read(path)[1]

        # `_read_and_digest()` is the only digest primitive the module has today.
        # `sha256_file` is patched in as well so a regression that reintroduces a
        # second hashing pass through the shared helper is counted here instead of
        # escaping the assertion unnoticed.
        with (
            patch.object(page_quality, "_read_and_digest", counted_read),
            patch.object(page_quality, "sha256_file", counted_digest, create=True),
        ):
            issues = validate_page_quality(self.project, page_number)
        root = self.project.resolve()
        return issues, {
            path.relative_to(root).as_posix(): count for path, count in reads.items()
        }

    def test_one_validation_reads_each_bound_artifact_exactly_once(self):
        write_page_quality_record(self.project, 1, self._build_record())

        issues, reads = self._count_artifact_reads()

        self.assertEqual((), issues)
        # Counted rather than inspected: the recorded-path passes and the
        # re-derived pass both need these bytes, and the page raster is the
        # expensive one. Any reintroduced second read fails this outright.
        self.assertEqual(
            {
                "cache/composition.json": 1,
                "pages/page-001.png": 1,
                "panels/p01-01/lettering.json": 1,
                "panels/p01-01/normalization.json": 1,
                "panels/p01-02/lettering.json": 1,
                "panels/p01-02/normalization.json": 1,
                "panels/p01-03/lettering.json": 1,
                "panels/p01-03/normalization.json": 1,
                "plan/storyboard.json": 1,
            },
            reads,
        )

    def test_missing_and_changed_bound_artifacts_report_separately(self):
        write_page_quality_record(self.project, 1, self._build_record())
        page = self.project / "pages/page-001.png"
        payload = page.read_bytes()

        page.unlink()
        missing = validate_page_quality(self.project, 1)
        self.assertEqual(
            ["page-quality-stale: bound artifact is missing"],
            [
                issue.message for issue in missing
                if issue.field == "bindings.page_sha256"
            ],
            missing,
        )

        # A page whose bytes changed is a different verdict from a page that is
        # gone, and the re-derived pass adds its own. Sharing one read of the
        # raster must not merge them.
        page.write_bytes(payload + b"changed")
        changed = validate_page_quality(self.project, 1)
        self.assertEqual(
            [
                "page-quality-stale: bound artifact hash does not match",
                "page-quality-stale: bound value does not match current artifacts",
            ],
            sorted(
                issue.message for issue in changed
                if issue.field == "bindings.page_sha256"
            ),
            changed,
        )

    def test_current_but_wrong_binding_is_caught_by_the_re_derived_pass(self):
        decoy = self.project / "pages/page-002.png"
        Image.new("RGB", (12, 9), "white").save(decoy)
        record = self._build_record()
        record["bindings"]["page_path"] = "pages/page-002.png"
        record["bindings"]["page_sha256"] = hashlib.sha256(
            decoy.read_bytes()
        ).hexdigest()
        write_page_quality_record(self.project, 1, record)

        issues, reads = self._count_artifact_reads()

        # The recorded path resolves and its digest matches the bytes there, so
        # the recorded-path pass is satisfied. Only re-deriving the binding from
        # the storyboard reports a binding that is well-formed but wrong.
        self.assertEqual(
            ["page-quality-stale: bound value does not match current artifacts"],
            [issue.message for issue in issues if issue.field == "bindings.page_sha256"],
            issues,
        )
        self.assertIn("bindings.page_path", {issue.field for issue in issues})
        # Two different rasters stand behind one field, and each is read once:
        # memoizing on the resolved path shares reads without merging passes.
        self.assertEqual(1, reads["pages/page-001.png"])
        self.assertEqual(1, reads["pages/page-002.png"])

    def test_recorded_panel_set_disagreeing_with_the_storyboard_is_reported(self):
        undeclared = self.project / "panels/p01-99"
        undeclared.mkdir()
        lettering = undeclared / "lettering.json"
        shutil.copyfile(self.project / "panels/p01-03/lettering.json", lettering)
        record = self._build_record()
        recorded = record["bindings"]["lettering_sha256s"][:2]
        recorded.append("p01-99:" + hashlib.sha256(lettering.read_bytes()).hexdigest())
        record["bindings"]["lettering_sha256s"] = recorded
        write_page_quality_record(self.project, 1, record)

        issues = validate_page_quality(self.project, 1)

        # Every recorded hash is current, so the traversal over the record's own
        # panel IDs agrees with disk. The disagreement exists only against the
        # panel set the storyboard declares, which the other traversal walks.
        self.assertEqual(
            ["page-quality-stale: bound value does not match current artifacts"],
            [
                issue.message for issue in issues
                if issue.field == "bindings.lettering_sha256s"
            ],
            issues,
        )

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

    def test_publish_uses_one_snapshot_when_page_changes_after_checks(self):
        page_path = self.project / "pages/page-001.png"
        old_payload = page_path.read_bytes()
        old_digest = hashlib.sha256(old_payload).hexdigest()
        with Image.open(page_path) as image:
            old_size = image.size

        real_checks = page_quality._deterministic_checks
        real_write = page_quality._write_page_quality_record_locked
        held_handles = []

        def replace_page_after_checks(context):
            held = ProjectLock(self.project)._held_locks().get(self.project.resolve())
            held_handles.append(held[0] if held is not None else None)
            checks = real_checks(context)
            Image.new("RGB", (17, 13), "red").save(page_path)
            return checks

        def observe_write(project_dir, page_number, record):
            held = ProjectLock(self.project)._held_locks().get(self.project.resolve())
            held_handles.append(held[0] if held is not None else None)
            return real_write(project_dir, page_number, record)

        with (
            patch.object(
                page_quality,
                "_deterministic_checks",
                side_effect=replace_page_after_checks,
            ),
            patch.object(
                page_quality,
                "_write_page_quality_record_locked",
                side_effect=observe_write,
            ),
        ):
            record_path = publish_page_quality_record(
                self.project,
                1,
                reviewer_checks(self.project),
                reviewer="fixture-reviewer",
                reviewed_at="2026-08-14T01:02:03Z",
            )

        record = json.loads(record_path.read_text("utf-8"))
        current_digest = hashlib.sha256(page_path.read_bytes()).hexdigest()
        self.assertEqual(
            old_size,
            (
                record["bindings"]["page_width"],
                record["bindings"]["page_height"],
            ),
        )
        self.assertEqual(old_digest, record["bindings"]["page_sha256"])
        self.assertNotEqual(current_digest, record["bindings"]["page_sha256"])
        self.assertEqual(2, len(held_handles))
        self.assertIsNotNone(held_handles[0])
        self.assertIs(held_handles[0], held_handles[1])

    def test_page_quality_operations_are_reentrant_under_project_lock(self):
        with ProjectLock(self.project, timeout=1.0):
            record = self._build_record()
            write_page_quality_record(self.project, 1, record)
            self.assertEqual((), validate_page_quality(self.project, 1))
            self.assertIsNotNone(
                ProjectLock(self.project)._held_locks().get(self.project.resolve())
            )

    def test_advisory_validation_does_not_acquire_project_lock(self):
        write_page_quality_record(self.project, 1, self._build_record())

        with patch.object(
            page_quality,
            "ProjectLock",
            side_effect=AssertionError("read-only validation must not lock"),
        ):
            self.assertEqual((), validate_page_quality(self.project, 1))


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
        self.assertEqual(13, len(paths))
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

    def test_lettered_sfx_is_not_judged_by_the_balloon_placement_rules(self):
        """A sound effect belongs over the action, which is where anchors live.

        `balloon-subject-obstruction` encodes a rule about speech: a balloon must
        not cover the mouth it speaks from. `balloon-crowding` encodes a rule about
        the reading path and tells the reviewer to shorten dialogue. A lettered SFX
        is neither, so the same geometry that fails as a balloon must not fail — or
        be counted — as an effect. `clipped-text`, `text-overlap`, and
        `reading-order` still judge it, because those apply to any drawn box.
        """
        # p01-02's dialogue anchors at [0.7, 0.55] of a 720x1064 panel, so this box
        # sits directly on the protected voice source. The storyboard is untouched,
        # which keeps that anchor protected: the only variable is the drawn box's
        # kind.
        def extra(kind):
            return {"panels": {"p01-02": {"append": [{
                "anchor": "middle-right",
                "attribution": None,
                "box": {"x": 454, "y": 545, "width": 120, "height": 90},
                "font_runs": [
                    {"font_id": "ComicNeue-Bold.ttf", "style": "bold", "text": "KRAK!"}
                ],
                "id": "p01-02-t02",
                "kind": kind,
                "reading_order": 2,
                "tail": None,
            }]}}}

        as_balloon = self._fresh_project()
        apply_balloon_layout(as_balloon, extra("caption"))
        as_sfx = self._fresh_project()
        apply_balloon_layout(as_sfx, extra("sfx"))

        balloon_checks = self._checks(as_balloon)
        sfx_checks = self._checks(as_sfx)

        # Identical geometry: only the kind differs.
        self.assertEqual("fail", balloon_checks["balloon-subject-obstruction"]["result"])
        self.assertEqual(
            ["p01-02-t02"],
            [
                region["item_id"]
                for region in balloon_checks["balloon-subject-obstruction"]["regions"]
            ],
        )
        self.assertEqual("pass", sfx_checks["balloon-subject-obstruction"]["result"])
        self.assertEqual([], sfx_checks["balloon-subject-obstruction"]["regions"])
        for region in sfx_checks["balloon-crowding"]["regions"]:
            self.assertNotIn(
                "p01-02-t02",
                [item for pair in region.get("tight_pairs", []) for item in pair["items"]],
            )
        # The kind-agnostic text checks still cover the effect.
        for check_id in ("clipped-text", "text-overlap", "reading-order"):
            self.assertEqual("pass", sfx_checks[check_id]["result"], check_id)

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

    def test_out_of_range_anchor_fails_even_when_the_tail_agrees_with_it(self):
        project = self._fresh_project()
        anchor = [2.0, 0.5]
        normalization = json.loads(
            (project / "panels/p01-02/normalization.json").read_text("utf-8")
        )
        width, height = normalization["clean"]["size"]
        geometry = json.loads(
            (project / "panels/p01-02/lettering.json").read_text("utf-8")
        )
        box = next(
            item["box"] for item in geometry["items"] if item["id"] == "p01-02-t01"
        )

        # Rebuild a tail that is fully self-consistent for the off-panel anchor:
        # attachment on the ellipse, tip aimed at the target, gap recomputed. Every
        # other tail rule therefore passes and only the range check can object.
        target = (round(anchor[0] * width), round(anchor[1] * height))
        centre_x = box["x"] + box["width"] / 2
        centre_y = box["y"] + box["height"] / 2
        radius_x, radius_y = box["width"] / 2, box["height"] / 2
        delta_x, delta_y = target[0] - centre_x, target[1] - centre_y
        normalized = math.sqrt((delta_x / radius_x) ** 2 + (delta_y / radius_y) ** 2)
        attachment = (
            centre_x + delta_x / normalized,
            centre_y + delta_y / normalized,
        )
        span = math.hypot(target[0] - attachment[0], target[1] - attachment[1])
        unit_x = (target[0] - attachment[0]) / span
        unit_y = (target[1] - attachment[1]) / span
        length = 86.4
        tip = (attachment[0] + unit_x * length, attachment[1] + unit_y * length)

        storyboard_path = project / "plan/storyboard.json"
        storyboard = json.loads(storyboard_path.read_text("utf-8"))
        for panel in storyboard["pages"][0]["panels"]:
            for text in panel["text"]:
                if text.get("kind") == "dialogue":
                    text["speaker_anchor"] = anchor
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )
        apply_balloon_layout(project, {
            "panels": {
                "p01-02": {
                    "replace": {
                        "p01-02-t01": {
                            "tail_fields": {
                                # The echo agrees, so the mismatch rule cannot fire.
                                "speaker_anchor": anchor,
                                "attachment": [round(v, 4) for v in attachment],
                                "tip": [round(v, 4) for v in tip],
                                "source_gap": round(span - length, 4),
                            }
                        }
                    }
                }
            }
        })
        check = self._checks(project)["bubble-tail-geometry"]

        self.assertEqual("fail", check["result"])
        self.assertEqual(
            [{
                "panel_id": "p01-02",
                "reason": "speaker-anchor-out-of-range",
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


class MultiSpeakerAttributionQualityTests(unittest.TestCase):
    """Page QA over a panel where two characters speak in the same frame."""

    @classmethod
    def setUpClass(cls):
        cls._base_directory = tempfile.TemporaryDirectory()
        cls.base = Path(cls._base_directory.name) / "two-speakers"
        shutil.copytree(FIXTURE, cls.base)
        cls.text_ids = write_multi_speaker_panel(cls.base)
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

    def _checks(self, project):
        record = build_page_quality_record(
            project,
            1,
            reviewer_checks(project),
            reviewer="fixture-reviewer",
            reviewed_at="2026-08-14T01:02:03Z",
        )
        return record, {check["id"]: check for check in record["checks"]}

    @staticmethod
    def _geometry_path(project):
        return project / "panels/p01-02/lettering.json"

    def test_correct_multi_character_panel_passes_without_an_override(self):
        project = self._fresh_project()

        record, checks = self._checks(project)

        self.assertEqual("accept", record["decision"])
        self.assertNotIn("override_reason", record)
        for check_id in DETERMINISTIC_PAGE_CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertEqual("pass", checks[check_id]["result"])
                self.assertEqual([], checks[check_id]["regions"])

    def test_each_balloon_is_attributed_to_the_character_that_speaks_it(self):
        project = self._fresh_project()
        geometry = json.loads(self._geometry_path(project).read_text("utf-8"))

        attribution = {
            item["id"]: item["attribution"] for item in geometry["items"]
        }

        self.assertEqual(
            {
                self.text_ids[0]: {
                    "authored_speaker": "mira",
                    "resolution": "declared",
                    "speaker": "mira",
                    "speaker_anchor": [0.78, 0.34],
                },
                self.text_ids[1]: {
                    "authored_speaker": "ren",
                    "resolution": "declared",
                    "speaker": "ren",
                    "speaker_anchor": [0.22, 0.62],
                },
            },
            attribution,
        )

    def test_swapping_the_two_speakers_is_detected_on_both_balloons(self):
        project = self._fresh_project()
        path = self._geometry_path(project)
        geometry = json.loads(path.read_text("utf-8"))
        items = {item["id"]: item for item in geometry["items"]}
        first, second = (items[text_id]["attribution"] for text_id in self.text_ids)
        # Swap only the identities. Both tails still attach correctly and still
        # point at the anchor the storyboard authored, so nothing but attribution
        # reveals that the balloons now credit the wrong characters.
        first["authored_speaker"], second["authored_speaker"] = (
            second["authored_speaker"], first["authored_speaker"],
        )
        first["speaker"], second["speaker"] = second["speaker"], first["speaker"]
        path.write_text(
            json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            "utf-8",
        )

        record, checks = self._checks(project)

        self.assertEqual("regenerate", record["decision"])
        self.assertEqual("fail", checks["bubble-tail-geometry"]["result"])
        self.assertEqual(
            [
                {"panel_id": "p01-02", "reason": "speaker-mismatch", "text_id": self.text_ids[0]},
                {"panel_id": "p01-02", "reason": "speaker-mismatch", "text_id": self.text_ids[1]},
            ],
            checks["bubble-tail-geometry"]["regions"],
        )

    def test_either_retained_identity_field_disagreeing_is_detected(self):
        # Corrupting one field while the other still matches must not pass. The
        # canonical `speaker` is the identity consumers read, so a record that
        # agrees only on the authored echo is not trustworthy.
        for field in ("speaker", "authored_speaker"):
            with self.subTest(field=field):
                project = self._fresh_project()
                path = self._geometry_path(project)
                geometry = json.loads(path.read_text("utf-8"))
                items = {item["id"]: item for item in geometry["items"]}
                attribution = items[self.text_ids[0]]["attribution"]
                self.assertEqual("mira", attribution[field])
                attribution[field] = "ren"
                path.write_text(
                    json.dumps(geometry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    "utf-8",
                )

                record, checks = self._checks(project)

                self.assertEqual("regenerate", record["decision"])
                self.assertEqual(
                    [{
                        "panel_id": "p01-02",
                        "reason": "speaker-mismatch",
                        "text_id": self.text_ids[0],
                    }],
                    checks["bubble-tail-geometry"]["regions"],
                )

    def test_lettering_provenance_stays_current_for_both_speakers(self):
        project = self._fresh_project()

        self.assertEqual((), validate_lettering_provenance(project, "p01-02"))


if __name__ == "__main__":
    unittest.main()
