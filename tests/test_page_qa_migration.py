import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

from scripts.comic_sol import atomic_write_json, sha256_file  # noqa: E402
from scripts.compose_pages import compose_all_pages  # noqa: E402
from scripts.letter_panels import letter_project  # noqa: E402
from scripts.page_quality import (  # noqa: E402
    CURRENT_PAGE_QA_SCHEMA_VERSION,
    DETERMINISTIC_PAGE_CHECK_IDS,
    PAGE_BINDING_FIELDS,
    PAGE_CHECK_IDS,
    PAGE_QA_MIGRATION_SOURCES,
    PAGE_QA_MIGRATIONS,
    SUBJECTIVE_PAGE_CHECK_IDS,
    PageQualityMigrationError,
    migrate_page_quality_record,
    validate_page_quality,
)
from scripts.project_io import ProjectTransaction  # noqa: E402
from scripts.schema import UnsupportedSchemaVersionError  # noqa: E402
from scripts.validate_project import validate_project  # noqa: E402
from tests.support import bounded_tail_regions  # noqa: E402


FIXTURE = ROOT / "tests/fixtures/valid-one-page"
LEGACY_RECORD = ROOT / "tests/fixtures/page-qa-2.0/page-001.json"
RECORD_RELATIVE = "qa/pages/page-001.json"
SUPERSEDED_EVIDENCE = "Superseded schema-2.0 engine measurement"


def panel_ids(storyboard):
    """Return the storyboard panel IDs of page one, in authored order."""
    return [panel["id"] for panel in storyboard["pages"][0]["panels"]]


def legacy_page_record(project):
    """Load the committed schema-2.0 fixture and rebind its rendered provenance.

    The fixture records `null` for the three digests and the balloon tail tip
    that only exist once the project is lettered and composed. Everything else,
    including the old check order and the reviewer evidence, is committed. See
    `tests/fixtures/page-qa-2.0/README.md`.
    """
    record = json.loads(LEGACY_RECORD.read_text("utf-8"))
    storyboard = json.loads((project / "plan/storyboard.json").read_text("utf-8"))
    bindings = record["bindings"]
    bindings["page_sha256"] = sha256_file(project / "pages/page-001.png")
    bindings["composition_cache_sha256"] = sha256_file(project / "cache/composition.json")
    bindings["lettering_sha256s"] = [
        panel_id + ":" + sha256_file(project / f"panels/{panel_id}/lettering.json")
        for panel_id in panel_ids(storyboard)
    ]

    tips = {
        (region["panel_id"], region["text_id"]): region["tip"]
        for region in bounded_tail_regions(project, 1)
    }
    tail = next(
        check for check in record["checks"] if check["id"] == "bubble-tail-direction"
    )
    for region in tail["regions"]:
        key = (region["panel_id"], region["text_id"])
        if key not in tips:
            raise AssertionError(f"fixture region {key} is not in the storyboard")
        region["tip"] = tips[key]
    return record


class PageQaMigrationRegistryTests(unittest.TestCase):
    def test_page_qa_migration_contract_is_explicit_and_readable(self):
        self.assertEqual("2.1", CURRENT_PAGE_QA_SCHEMA_VERSION)
        # Keyed by (source, target) like PROJECT_MIGRATIONS, so a version without
        # a reviewed hook cannot be silently accepted.
        self.assertEqual(
            {("2.0", CURRENT_PAGE_QA_SCHEMA_VERSION)}, set(PAGE_QA_MIGRATIONS)
        )
        self.assertEqual({"2.0"}, set(PAGE_QA_MIGRATION_SOURCES))

    def test_shipped_template_starts_at_the_current_version(self):
        for relative in (
            "templates/page-qa.json",
            "skills/comic-sol/templates/page-qa.json",
        ):
            template = json.loads((ROOT / relative).read_text("utf-8"))
            self.assertEqual(
                CURRENT_PAGE_QA_SCHEMA_VERSION, template["schema_version"], relative
            )


class PageQaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        shutil.copytree(FIXTURE, self.project)
        letter_project(self.project)
        compose_all_pages(self.project)
        self.record_path = self.project / RECORD_RELATIVE
        atomic_write_json(self.record_path, legacy_page_record(self.project))
        self.before = self.record_path.read_bytes()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_committed_pre_change_record_migrates_to_a_valid_current_record(self):
        migrated = migrate_page_quality_record(self.project, 1)

        self.assertEqual(CURRENT_PAGE_QA_SCHEMA_VERSION, migrated["schema_version"])
        self.assertEqual("page-qa", migrated["kind"])
        self.assertEqual("page-001", migrated["subject_id"])
        self.assertEqual(
            list(PAGE_CHECK_IDS), [check["id"] for check in migrated["checks"]]
        )
        self.assertEqual(PAGE_BINDING_FIELDS, set(migrated["bindings"]))
        # The published bytes are the migrated record, in canonical artifact form.
        self.assertEqual(
            (json.dumps(migrated, ensure_ascii=False, indent=2, sort_keys=True)
             + "\n").encode("utf-8"),
            self.record_path.read_bytes(),
        )
        self.assertEqual((), validate_page_quality(self.project, 1))

    def test_migration_re_derives_deterministic_checks_and_added_binding(self):
        migrated = migrate_page_quality_record(self.project, 1)

        checks = {check["id"]: check for check in migrated["checks"]}
        self.assertEqual(7, len(DETERMINISTIC_PAGE_CHECK_IDS))
        for check_id in DETERMINISTIC_PAGE_CHECK_IDS:
            check = checks[check_id]
            self.assertEqual("comic-sol", check["reviewer"], check_id)
            self.assertEqual("deterministic-geometry-v1", check["method"], check_id)
            # A copied measurement would still carry the old evidence sentence.
            self.assertNotIn(SUPERSEDED_EVIDENCE, check["evidence"], check_id)
        self.assertNotIn(SUPERSEDED_EVIDENCE, json.dumps(migrated))

        storyboard = json.loads((self.project / "plan/storyboard.json").read_text("utf-8"))
        self.assertEqual(
            [
                panel_id + ":" + sha256_file(
                    self.project / f"panels/{panel_id}/normalization.json"
                )
                for panel_id in panel_ids(storyboard)
            ],
            migrated["bindings"]["normalization_sha256s"],
        )

    def test_migration_preserves_the_bounded_review_it_can_still_trust(self):
        legacy = json.loads(self.before.decode("utf-8"))
        legacy_checks = {check["id"]: check for check in legacy["checks"]}

        migrated = migrate_page_quality_record(self.project, 1)

        migrated_checks = {check["id"]: check for check in migrated["checks"]}
        for check_id in SUBJECTIVE_PAGE_CHECK_IDS:
            self.assertEqual(legacy_checks[check_id], migrated_checks[check_id], check_id)
        # The reviewer and the timestamp are the record of who looked and when.
        self.assertEqual(legacy["review"], migrated["review"])
        self.assertEqual("legacy-page-reviewer", migrated["review"]["reviewer"])
        self.assertEqual("2026-08-14T01:02:03Z", migrated["review"]["reviewed_at"])

    def test_migration_refuses_a_record_whose_page_no_longer_matches(self):
        page = self.project / "pages/page-001.png"
        page.write_bytes(page.read_bytes() + b"repainted")

        with self.assertRaisesRegex(
            PageQualityMigrationError, r"page-quality-stale: bindings\.page_sha256"
        ):
            migrate_page_quality_record(self.project, 1)

        self.assertEqual(self.before, self.record_path.read_bytes())

    def test_record_without_a_registered_migration_path_fails_closed(self):
        unmigratable = json.loads(self.before.decode("utf-8"))
        unmigratable["schema_version"] = "1.5"
        atomic_write_json(self.record_path, unmigratable)
        before = self.record_path.read_bytes()

        with self.assertRaisesRegex(
            UnsupportedSchemaVersionError, "page QA schema 1.5 is unsupported"
        ):
            migrate_page_quality_record(self.project, 1)

        self.assertEqual(before, self.record_path.read_bytes())

    def test_interrupted_migration_leaves_the_project_unchanged(self):
        with mock.patch.object(
            ProjectTransaction, "commit", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                migrate_page_quality_record(self.project, 1)
        self.assertEqual(self.before, self.record_path.read_bytes())

        ProjectTransaction.recover(self.project)
        self.assertEqual(self.before, self.record_path.read_bytes())
        self.assertEqual("2.0", json.loads(
            self.record_path.read_text("utf-8"))["schema_version"])

    def test_migrating_a_current_record_publishes_nothing(self):
        migrate_page_quality_record(self.project, 1)
        published = self.record_path.read_bytes()

        again = migrate_page_quality_record(self.project, 1)

        self.assertEqual(CURRENT_PAGE_QA_SCHEMA_VERSION, again["schema_version"])
        self.assertEqual(published, self.record_path.read_bytes())

    def test_pre_change_record_is_reported_as_migration_required_not_malformed(self):
        issues = [
            issue for issue in validate_project(self.project, "export-ready")
            if issue.path == RECORD_RELATIVE
        ]

        self.assertEqual(
            [(
                "schema_version",
                "quality-migration-required: schema 2.0 page QA must be migrated "
                f"to {CURRENT_PAGE_QA_SCHEMA_VERSION}",
            )],
            [(issue.field, issue.message) for issue in issues],
        )
        # The misleading verdict this change removes: a record that predates a
        # check-set change is not a reviewer who supplied the wrong check IDs.
        self.assertNotIn(
            "quality-check-ids", " ".join(issue.message for issue in issues)
        )

    def test_non_string_version_is_reported_without_raising(self):
        corrupt = json.loads(self.before.decode("utf-8"))
        corrupt["schema_version"] = {"unexpected": "object"}
        atomic_write_json(self.record_path, corrupt)

        issues = [
            issue for issue in validate_project(self.project, "export-ready")
            if issue.path == RECORD_RELATIVE
        ]

        self.assertTrue(any(
            "quality-migration-required" in issue.message for issue in issues
        ), issues)

    def test_migrated_record_satisfies_the_export_ready_gate(self):
        migrate_page_quality_record(self.project, 1)

        issues = [
            issue for issue in validate_project(self.project, "export-ready")
            if issue.path == RECORD_RELATIVE
        ]

        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
