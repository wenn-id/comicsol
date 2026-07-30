import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from quality_records import (  # noqa: E402
    QualityBinding,
    QualityCheck,
    migrate_quality_record,
    quality_record_hash,
    read_quality_record,
    validate_quality_checks,
)


PANEL_CHECK_IDS = (
    "character-identity",
    "anatomy",
    "action",
    "composition",
    "continuity",
    "text-free",
    "technical",
)
PAGE_CHECK_IDS = (
    "clipped-text",
    "text-overlap",
    "face-action-obstruction",
    "bubble-tail-direction",
    "reading-order",
    "accidental-text-watermark",
    "layout-border-integrity",
)


def passing_checks(ids=PANEL_CHECK_IDS):
    return [
        {
            "id": check_id,
            "result": "pass",
            "severity": "error",
            "evidence": f"Observed {check_id} against the bounded subject",
            "method": "agent-review",
            "reviewer": "fixture-reviewer",
            "regions": [],
        }
        for check_id in ids
    ]


class QualityRecordContractTests(unittest.TestCase):
    def test_typed_values_are_immutable_and_normalized(self):
        check = QualityCheck(
            id="anatomy",
            result="pass",
            severity="error",
            evidence=" Limbs are anatomically coherent. ",
            method="agent-review",
            reviewer="fixture-reviewer",
            regions=(),
        )
        binding = QualityBinding(name="raw_sha256", value="a" * 64)
        self.assertEqual("Limbs are anatomically coherent.", check.evidence)
        self.assertEqual("raw_sha256", binding.name)
        with self.assertRaises((AttributeError, TypeError)):
            check.result = "fail"

    def test_exact_check_ids_and_structured_values_are_required(self):
        self.assertEqual((), validate_quality_checks(passing_checks(), PANEL_CHECK_IDS))

        missing = passing_checks()[:-1]
        self.assertIn("quality-check-ids", validate_quality_checks(missing, PANEL_CHECK_IDS))

        duplicate = passing_checks() + [passing_checks()[0]]
        self.assertIn("quality-check-ids", validate_quality_checks(duplicate, PANEL_CHECK_IDS))

        unknown = passing_checks()
        unknown[-1]["id"] = "unknown"
        self.assertIn("quality-check-ids", validate_quality_checks(unknown, PANEL_CHECK_IDS))

        malformed = passing_checks()
        malformed[0]["result"] = "maybe"
        malformed[1]["severity"] = "cosmetic"
        malformed[2]["regions"] = "whole image"
        issues = validate_quality_checks(malformed, PANEL_CHECK_IDS)
        self.assertIn("quality-check-result", issues)
        self.assertIn("quality-check-severity", issues)
        self.assertIn("quality-check-regions", issues)

    def test_generic_or_identical_evidence_is_rejected_after_normalization(self):
        for generic in ("verified", " LOOKS   GOOD ", "ok", "PASS"):
            checks = passing_checks()
            checks[0]["evidence"] = generic
            self.assertIn(
                "quality-evidence-generic",
                validate_quality_checks(checks, PANEL_CHECK_IDS),
            )

        checks = passing_checks()
        for check in checks:
            check["evidence"] = " Same observation\nfor every check "
        issues = validate_quality_checks(checks, PANEL_CHECK_IDS)
        self.assertIn("quality-evidence-generic", issues)

    def test_page_checks_use_the_same_contract(self):
        self.assertEqual((), validate_quality_checks(passing_checks(PAGE_CHECK_IDS), PAGE_CHECK_IDS))

    def test_canonical_hash_covers_all_record_fields(self):
        record = {
            "schema_version": "2.0",
            "kind": "panel-qa",
            "subject_id": "p01-01",
            "bindings": {"raw_sha256": "a" * 64},
            "checks": passing_checks(),
        }
        reordered = {
            "checks": record["checks"],
            "bindings": record["bindings"],
            "subject_id": record["subject_id"],
            "kind": record["kind"],
            "schema_version": record["schema_version"],
        }
        self.assertEqual(quality_record_hash(record), quality_record_hash(reordered))
        changed = deepcopy(record)
        changed["checks"][0]["evidence"] += " Changed."
        self.assertNotEqual(quality_record_hash(record), quality_record_hash(changed))

    def test_schema_one_record_remains_readable_without_claiming_rc2_compliance(self):
        legacy = {
            "schema_version": "1.0",
            "panel_id": "p01-01",
            "checks": [{"id": check_id, "result": "pass", "severity": "error", "evidence": "verified"}
                       for check_id in PANEL_CHECK_IDS],
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "record.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = read_quality_record(path, "panel-qa")
        self.assertEqual(legacy, loaded)

    def test_schema_two_kind_mismatch_and_private_paths_are_rejected(self):
        record = {
            "schema_version": "2.0",
            "kind": "page-qa",
            "subject_id": "page-001",
            "bindings": {"page_path": "/home/private/page.png"},
            "checks": passing_checks(PAGE_CHECK_IDS),
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "record.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "kind"):
                read_quality_record(path, "panel-qa")
            with self.assertRaisesRegex(ValueError, "private absolute path"):
                read_quality_record(path, "page-qa")

    def test_migration_derives_only_supplied_facts_and_does_not_mutate_source(self):
        legacy = {
            "schema_version": "1.0",
            "panel_id": "p01-01",
            "checks": [{"id": check_id, "result": "pass", "severity": "error", "evidence": "verified"}
                       for check_id in PANEL_CHECK_IDS],
        }
        before = deepcopy(legacy)
        migrated = migrate_quality_record(
            legacy,
            "panel-qa",
            {
                "raw_sha256": "a" * 64,
                "raw_width": 1200,
                "raw_height": 800,
            },
        )
        self.assertEqual(before, legacy)
        self.assertEqual("2.0", migrated["schema_version"])
        self.assertEqual("panel-qa", migrated["kind"])
        self.assertEqual("p01-01", migrated["subject_id"])
        self.assertEqual("a" * 64, migrated["bindings"]["raw_sha256"])
        self.assertEqual("migration-required", migrated["review"]["method"])
        self.assertEqual("", migrated["review"]["reviewer"])
        self.assertEqual("regenerate", migrated["decision"])
        self.assertIn("quality-migration-required", migrated["unresolved_warnings"])
        self.assertTrue(all(check["result"] == "fail" for check in migrated["checks"]))
        self.assertTrue(all(check["evidence"] == "" for check in migrated["checks"]))

    def test_migration_rejects_unknown_derived_fields_without_mutating_source(self):
        legacy = {"schema_version": "1.0", "panel_id": "p01-01", "checks": []}
        before = deepcopy(legacy)
        with self.assertRaisesRegex(ValueError, "recomputed field"):
            migrate_quality_record(legacy, "panel-qa", {"reviewer": "invented"})
        self.assertEqual(before, legacy)


if __name__ == "__main__":
    unittest.main()
