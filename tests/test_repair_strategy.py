"""Deterministic repair planning, safe fallback, and recorded provenance."""

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core_primitives import PANEL_CHECK_IDS
from scripts.repair_strategy import (
    AREA_SCOPE,
    EDITING_UNSUPPORTED,
    FULL_REGENERATION,
    NO_REPAIR,
    PANEL_WIDE_CHECK,
    PANEL_WIDE_CHECKS,
    REPAIR_PLAN_PATH,
    REPAIR_PLAN_SCHEMA_VERSION,
    SELECTIVE_REPAIR,
    STALE_BINDINGS,
    SUBJECT_SCOPE,
    UNLOCALIZED_EVIDENCE,
    RepairStrategyError,
    accepted_content_is_stale,
    main,
    panel_repair_plan,
    plan_and_write_repair_plan,
    project_repair_plan,
    repair_plan_block,
    validate_defect_regions,
    validate_repair_plan,
)


CHARACTER_TRAITS = (
    "face",
    "hair",
    "age-appearance",
    "clothing",
    "accessories",
    "proportions",
    "immutable-traits",
)


def trait_region(character_id, trait, *, result="pass", severity="error"):
    """Return one character-identity trait region in the CS-021 shape."""
    failing = result != "pass" or severity == "warning"
    return {
        "character_id": character_id,
        "evidence": f"{character_id} {trait} observed as flat grey",
        "expected": f"canonical {trait}",
        "repair_guidance": (
            f"Repair {character_id} {trait} to match canonical {trait}; "
            f"observed: flat grey"
            if failing
            else None
        ),
        "result": result,
        "severity": severity,
    }


def identity_check(*, characters=("mira", "ren"), failures=(), warnings=()):
    """Return a trait-level character-identity check with provenance."""
    regions = []
    for character_id in characters:
        for trait in CHARACTER_TRAITS:
            if (character_id, trait) in failures:
                regions.append(
                    trait_region(character_id, trait, result="fail", severity="error")
                )
            elif (character_id, trait) in warnings:
                regions.append(
                    trait_region(character_id, trait, result="warning", severity="warning")
                )
            else:
                regions.append(trait_region(character_id, trait))
    if failures:
        result, severity = "fail", "error"
    elif warnings:
        result, severity = "warning", "warning"
    else:
        result, severity = "pass", "error"
    return {
        "evidence": f"{len(regions)} trait checks reviewed against the identity pack",
        "id": "character-identity",
        "method": "bounded-visual-review",
        "provenance": {
            "characters": [
                {
                    "character_id": character_id,
                    "selected_references": [],
                    "source_fingerprint_sha256": "e" * 64,
                }
                for character_id in characters
            ],
            "identity_pack_path": "plan/character-identity-pack.json",
            "identity_pack_sha256": "a" * 64,
            "panel_id": "p01-01",
            "reference_plan_path": "logs/reference-selection.json",
            "reference_plan_sha256": "b" * 64,
        },
        "regions": regions,
        "result": result,
        "reviewer": "fixture-reviewer",
        "severity": severity,
    }


def defect_region(*, area=None, character_id=None, result="fail", severity="error"):
    """Return one bounded defect region for a localizable non-identity check."""
    located = area if area is not None else character_id
    failing = result != "pass" or severity == "warning"
    return {
        "area": area,
        "character_id": character_id,
        "evidence": f"stray watermark glyphs visible at {located}",
        "repair_guidance": (
            f"Remove the stray watermark at {located} and restore the artwork beneath it"
            if failing
            else None
        ),
        "result": result,
        "severity": severity,
    }


def check(check_id, *, result="pass", severity="error", regions=None):
    """Return one plain panel check in the schema-2.0 shape."""
    return {
        "evidence": f"Observed {check_id} against the current panel artifacts",
        "id": check_id,
        "method": "bounded-visual-review",
        "regions": [] if regions is None else regions,
        "result": result,
        "reviewer": "fixture-reviewer",
        "severity": severity,
    }


def panel_record(
    *,
    panel_id="p01-01",
    decision="accept",
    identity=None,
    overrides=None,
    unresolved_warnings=None,
):
    """Return one schema-2.0 panel QA record with the seven required checks."""
    faults = dict(overrides or {})
    checks = []
    for check_id in PANEL_CHECK_IDS:
        if check_id == "character-identity":
            checks.append(identity if identity is not None else identity_check())
        else:
            checks.append(check(check_id, **faults.get(check_id, {})))
    return {
        "bindings": {
            "clean_height": 1136,
            "clean_path": f"panels/{panel_id}/clean.png",
            "clean_sha256": "c" * 64,
            "clean_width": 736,
            "normalization_path": f"panels/{panel_id}/normalization.json",
            "normalization_sha256": "d" * 64,
            "raw_height": 1136,
            "raw_path": f"panels/raw/{panel_id}.png",
            "raw_sha256": "b" * 64,
            "raw_width": 736,
        },
        "checks": checks,
        "decision": decision,
        "kind": "panel-qa",
        "review": {
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-08-17T00:00:00Z",
        },
        "schema_version": "2.0",
        "subject_id": panel_id,
        "unresolved_warnings": list(unresolved_warnings or []),
    }


def failing_record(**kwargs):
    """Return a record whose review requires a new attempt."""
    return panel_record(decision="regenerate", **kwargs)


class DefectClassificationTests(unittest.TestCase):
    def test_a_faulted_trait_repairs_only_its_own_subject(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(SELECTIVE_REPAIR, plan.strategy)
        self.assertIsNone(plan.fallback_reason)
        self.assertEqual(
            [(SUBJECT_SCOPE, "ren")],
            [(target.scope, target.target) for target in plan.targets],
        )
        self.assertEqual(1, plan.targets[0].rank)
        self.assertIn("Repair ren hair", plan.targets[0].guidance[0])
        self.assertEqual(("mira",), plan.unaffected_subjects)
        self.assertTrue(plan.preserves_accepted_content)

    def test_every_faulted_trait_of_one_subject_shares_one_target(self):
        record = failing_record(
            identity=identity_check(failures={("ren", "hair"), ("ren", "clothing")})
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(1, len(plan.targets))
        self.assertEqual(2, len(plan.targets[0].guidance))
        self.assertEqual(2, len(plan.defects))

    def test_two_faulted_subjects_produce_two_ranked_targets(self):
        record = failing_record(
            identity=identity_check(failures={("mira", "face"), ("ren", "hair")})
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(
            [(1, "mira"), (2, "ren")],
            [(target.rank, target.target) for target in plan.targets],
        )
        self.assertEqual((), plan.unaffected_subjects)

    def test_a_bounded_area_defect_is_repaired_by_area(self):
        record = failing_record(
            overrides={
                "text-free": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [defect_region(area="bottom-right")],
                }
            }
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(SELECTIVE_REPAIR, plan.strategy)
        self.assertEqual(
            [(AREA_SCOPE, "bottom-right")],
            [(target.scope, target.target) for target in plan.targets],
        )

    def test_a_named_subject_localizes_a_non_identity_check(self):
        record = failing_record(
            overrides={
                "anatomy": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [defect_region(character_id="mira")],
                }
            }
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(SELECTIVE_REPAIR, plan.strategy)
        self.assertEqual(
            [(SUBJECT_SCOPE, "mira")],
            [(target.scope, target.target) for target in plan.targets],
        )

    def test_subject_and_area_defects_are_repaired_together(self):
        record = failing_record(
            identity=identity_check(failures={("mira", "face")}),
            overrides={
                "text-free": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [defect_region(area="top-left")],
                }
            },
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(SELECTIVE_REPAIR, plan.strategy)
        self.assertEqual(
            [(SUBJECT_SCOPE, "mira"), (AREA_SCOPE, "top-left")],
            [(target.scope, target.target) for target in plan.targets],
        )

    def test_a_passing_region_is_not_a_defect(self):
        record = failing_record(
            overrides={
                "text-free": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [
                        defect_region(area="top-left", result="pass"),
                        defect_region(area="bottom-right"),
                    ],
                }
            }
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(
            [(AREA_SCOPE, "bottom-right")],
            [(target.scope, target.target) for target in plan.targets],
        )

    def test_a_warning_region_is_still_repairable(self):
        record = failing_record(
            identity=identity_check(
                failures={("mira", "face")}, warnings={("mira", "hair")}
            )
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(SELECTIVE_REPAIR, plan.strategy)
        self.assertEqual(2, len(plan.defects))
        self.assertEqual(2, len(plan.targets[0].guidance))


class FallbackTests(unittest.TestCase):
    def test_every_panel_wide_check_forces_full_regeneration(self):
        for check_id in sorted(PANEL_WIDE_CHECKS):
            with self.subTest(check=check_id):
                record = failing_record(
                    overrides={check_id: {"result": "fail", "severity": "error"}}
                )

                plan = panel_repair_plan(record, localized_edit_supported=True)

                self.assertEqual(FULL_REGENERATION, plan.strategy)
                self.assertEqual(PANEL_WIDE_CHECK, plan.fallback_reason)
                self.assertEqual((), plan.targets)
                self.assertFalse(plan.preserves_accepted_content)

    def test_a_panel_wide_check_outweighs_a_localized_defect(self):
        record = failing_record(
            identity=identity_check(failures={("mira", "face")}),
            overrides={"composition": {"result": "fail", "severity": "error"}},
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(FULL_REGENERATION, plan.strategy)
        self.assertEqual(PANEL_WIDE_CHECK, plan.fallback_reason)
        # The localized defect is still recorded, so the correction clause the
        # regeneration carries names the subject the review actually faulted.
        self.assertEqual(
            {"character-identity", "composition"},
            {defect.check_id for defect in plan.defects},
        )

    def test_an_identity_check_without_trait_regions_cannot_be_localized(self):
        record = failing_record(
            identity=check("character-identity", result="fail", severity="error")
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(FULL_REGENERATION, plan.strategy)
        self.assertEqual(UNLOCALIZED_EVIDENCE, plan.fallback_reason)
        self.assertEqual((), plan.unaffected_subjects)

    def test_a_failing_parent_whose_traits_all_pass_cannot_be_localized(self):
        identity = identity_check()
        identity.update({"result": "fail", "severity": "error"})
        record = failing_record(identity=identity)

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(FULL_REGENERATION, plan.strategy)
        self.assertEqual(UNLOCALIZED_EVIDENCE, plan.fallback_reason)

    def test_a_localizable_check_without_regions_cannot_be_localized(self):
        for check_id in ("anatomy", "text-free"):
            with self.subTest(check=check_id):
                record = failing_record(
                    overrides={check_id: {"result": "fail", "severity": "error"}}
                )

                plan = panel_repair_plan(record, localized_edit_supported=True)

                self.assertEqual(FULL_REGENERATION, plan.strategy)
                self.assertEqual(UNLOCALIZED_EVIDENCE, plan.fallback_reason)

    def test_a_missing_editing_capability_falls_back_safely(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        plan = panel_repair_plan(record, localized_edit_supported=False)

        self.assertEqual(FULL_REGENERATION, plan.strategy)
        self.assertEqual(EDITING_UNSUPPORTED, plan.fallback_reason)
        self.assertEqual((), plan.targets)
        self.assertFalse(plan.localized_edit_supported)

    def test_unverifiable_accepted_content_outranks_every_other_reason(self):
        record = failing_record(
            identity=identity_check(failures={("ren", "hair")}),
            overrides={"technical": {"result": "fail", "severity": "error"}},
        )

        plan = panel_repair_plan(
            record, localized_edit_supported=False, accepted_content_stale=True
        )

        self.assertEqual(FULL_REGENERATION, plan.strategy)
        self.assertEqual(STALE_BINDINGS, plan.fallback_reason)

    def test_a_missing_capability_outranks_an_unlocalized_defect(self):
        record = failing_record(
            overrides={"anatomy": {"result": "fail", "severity": "error"}}
        )

        plan = panel_repair_plan(record, localized_edit_supported=False)

        self.assertEqual(EDITING_UNSUPPORTED, plan.fallback_reason)
        self.assertEqual(
            [UNLOCALIZED_EVIDENCE],
            [defect.fallback_reason for defect in plan.defects],
        )


class AcceptedPanelTests(unittest.TestCase):
    def test_an_accepted_panel_plans_no_repair(self):
        plan = panel_repair_plan(panel_record(), localized_edit_supported=True)

        self.assertEqual(NO_REPAIR, plan.strategy)
        self.assertIsNone(plan.fallback_reason)
        self.assertEqual((), plan.targets)
        self.assertEqual((), plan.defects)
        self.assertEqual(PANEL_CHECK_IDS, plan.unaffected_checks)

    def test_an_accepted_warning_records_the_warning_without_repairing_it(self):
        record = panel_record(
            decision="accept-warning",
            identity=identity_check(warnings={("mira", "hair")}),
            unresolved_warnings=["Repair mira hair to match canonical hair"],
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(NO_REPAIR, plan.strategy)
        self.assertEqual((), plan.targets)
        self.assertEqual(1, len(plan.defects))
        self.assertEqual(SUBJECT_SCOPE, plan.defects[0].scope)

    def test_the_plan_binds_the_accepted_raster_it_preserves(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual("panels/raw/p01-01.png", plan.accepted_raw_path)
        self.assertEqual("b" * 64, plan.accepted_raw_sha256)


class AccountingTests(unittest.TestCase):
    def test_every_non_passing_check_is_accounted_for(self):
        record = failing_record(
            identity=identity_check(failures={("mira", "face")}),
            overrides={
                "action": {"result": "fail", "severity": "error"},
                "continuity": {"result": "warning", "severity": "warning"},
            },
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(
            {"character-identity", "action", "continuity"},
            {defect.check_id for defect in plan.defects},
        )
        self.assertEqual(
            ("anatomy", "composition", "text-free", "technical"),
            plan.unaffected_checks,
        )

    def test_defects_follow_the_normative_check_order(self):
        record = failing_record(
            identity=identity_check(failures={("mira", "face")}),
            overrides={
                "technical": {"result": "fail", "severity": "error"},
                "anatomy": {"result": "fail", "severity": "error"},
            },
        )

        plan = panel_repair_plan(record, localized_edit_supported=True)

        self.assertEqual(
            ["character-identity", "anatomy", "technical"],
            [defect.check_id for defect in plan.defects],
        )

    def test_a_panel_requiring_regeneration_must_name_a_defect(self):
        with self.assertRaisesRegex(RepairStrategyError, "non-passing check"):
            panel_repair_plan(failing_record(), localized_edit_supported=True)


class UntrustedInputTests(unittest.TestCase):
    def test_schema_one_records_are_refused(self):
        record = failing_record()
        record["schema_version"] = "1.0"

        with self.assertRaisesRegex(RepairStrategyError, "schema-2.0"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_reordered_checks_are_refused(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))
        record["checks"].reverse()

        with self.assertRaisesRegex(RepairStrategyError, "normative order"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_a_region_naming_both_a_subject_and_an_area_is_refused(self):
        record = failing_record(
            overrides={
                "text-free": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [defect_region(area="top-left", character_id="mira")],
                }
            }
        )

        with self.assertRaisesRegex(RepairStrategyError, "exactly one"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_a_region_naming_neither_a_subject_nor_an_area_is_refused(self):
        record = failing_record(
            overrides={
                "text-free": {"result": "fail", "severity": "error", "regions": [defect_region()]}
            }
        )

        with self.assertRaisesRegex(RepairStrategyError, "exactly one"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_an_unknown_area_is_refused(self):
        record = failing_record(
            overrides={
                "text-free": {
                    "result": "fail",
                    "severity": "error",
                    "regions": [defect_region(area="dead-centre")],
                }
            }
        )

        with self.assertRaisesRegex(RepairStrategyError, "anchor area"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_generic_region_evidence_is_refused(self):
        region = defect_region(area="top-left")
        region["evidence"] = "ok"
        record = failing_record(
            overrides={
                "text-free": {"result": "fail", "severity": "error", "regions": [region]}
            }
        )

        with self.assertRaisesRegex(RepairStrategyError, "specific evidence"):
            panel_repair_plan(record, localized_edit_supported=True)

    def test_a_faulted_region_without_guidance_is_refused(self):
        region = defect_region(area="top-left")
        region["repair_guidance"] = None
        record = failing_record(
            overrides={
                "text-free": {"result": "fail", "severity": "error", "regions": [region]}
            }
        )

        with self.assertRaisesRegex(RepairStrategyError, "repair guidance"):
            panel_repair_plan(record, localized_edit_supported=True)


class DefectRegionValidationTests(unittest.TestCase):
    def test_an_empty_region_list_stays_valid(self):
        for check_id in PANEL_CHECK_IDS:
            with self.subTest(check=check_id):
                self.assertEqual((), validate_defect_regions(check(check_id)))

    def test_trait_regions_are_left_to_the_character_quality_gate(self):
        self.assertEqual((), validate_defect_regions(identity_check()))

    def test_a_panel_wide_check_may_not_carry_bounded_regions(self):
        for check_id in sorted(PANEL_WIDE_CHECKS):
            with self.subTest(check=check_id):
                faulted = check(
                    check_id,
                    result="fail",
                    severity="error",
                    regions=[defect_region(area="top-left")],
                )

                self.assertEqual(("repair-region-scope",), validate_defect_regions(faulted))

    def test_a_valid_bounded_region_passes(self):
        faulted = check(
            "text-free",
            result="fail",
            severity="error",
            regions=[defect_region(area="top-left"), defect_region(character_id="mira")],
        )

        self.assertEqual((), validate_defect_regions(faulted))

    def test_a_repeated_region_is_reported(self):
        faulted = check(
            "text-free",
            result="fail",
            severity="error",
            regions=[defect_region(area="top-left"), defect_region(area="top-left")],
        )

        self.assertEqual(("repair-region-duplicate",), validate_defect_regions(faulted))

    def test_structural_and_evidence_faults_are_reported(self):
        cases = {
            "repair-region-structure": {"area": "top-left"},
            "repair-region-result": dict(defect_region(area="top-left"), result="maybe"),
            "repair-region-severity": dict(defect_region(area="top-left"), severity="info"),
            "repair-region-evidence": dict(defect_region(area="top-left"), evidence=" "),
            "repair-region-guidance": dict(
                defect_region(area="top-left", result="pass"),
                repair_guidance="unnecessary guidance",
            ),
        }
        for category, region in cases.items():
            with self.subTest(category=category):
                faulted = check(
                    "text-free", result="fail", severity="error", regions=[region]
                )

                self.assertIn(category, validate_defect_regions(faulted))

    def test_a_non_list_region_collection_is_reported(self):
        faulted = check("text-free", result="fail", severity="error")
        faulted["regions"] = {}

        self.assertEqual(("repair-region-structure",), validate_defect_regions(faulted))


class RenderingTests(unittest.TestCase):
    def test_a_selective_block_names_targets_and_preserved_content(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        block = repair_plan_block(
            panel_repair_plan(record, localized_edit_supported=True)
        )

        self.assertIn(f"REPAIR PLAN (repair-plan {REPAIR_PLAN_SCHEMA_VERSION})", block)
        self.assertIn("- strategy: selective-repair", block)
        self.assertIn("1. subject ren", block)
        self.assertIn("subjects: mira", block)
        self.assertIn("leaving all other pixels unchanged", block)

    def test_a_fallback_block_states_its_reason(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        block = repair_plan_block(
            panel_repair_plan(record, localized_edit_supported=False)
        )

        self.assertIn(f"- strategy: full-regeneration ({EDITING_UNSUPPORTED})", block)
        self.assertIn("regenerate the whole panel", block)

    def test_a_rendered_block_names_no_provider(self):
        record = failing_record(identity=identity_check(failures={("ren", "hair")}))

        block = repair_plan_block(
            panel_repair_plan(record, localized_edit_supported=True)
        )

        lowered = block.casefold()
        for forbidden in ("http", "api", "token", "model", "endpoint", "key="):
            self.assertNotIn(forbidden, lowered)


class RepairPlanProjectHarness(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.project = Path(self._temporary.name)
        (self.project / "plan").mkdir(parents=True)
        (self.project / "qa/panels").mkdir(parents=True)
        (self.project / "panels/raw").mkdir(parents=True)
        (self.project / "logs").mkdir(parents=True)
        self._write(
            "plan/storyboard.json",
            {
                "pages": [
                    {
                        "layout": "two-horizontal",
                        "number": 1,
                        "panels": [{"id": "p01-01"}, {"id": "p01-02"}],
                    }
                ],
                "schema_version": "1.0",
            },
        )

    def _write(self, relative, document):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
        return path

    def _digest(self, payload):
        return hashlib.sha256(payload).hexdigest()

    def _publish_record(self, record):
        """Write one QA record and the artifacts its bindings claim."""
        panel_id = record["subject_id"]
        raw = f"panels/raw/{panel_id}.png".encode("utf-8") + b"-raw"
        clean = raw + b"-clean"
        normalization = {"mode": "exact", "panel_id": panel_id}
        (self.project / f"panels/{panel_id}").mkdir(parents=True, exist_ok=True)
        (self.project / f"panels/raw/{panel_id}.png").write_bytes(raw)
        (self.project / f"panels/{panel_id}/clean.png").write_bytes(clean)
        normalization_path = self._write(
            f"panels/{panel_id}/normalization.json", normalization
        )
        record = deepcopy(record)
        record["bindings"].update(
            {
                "raw_sha256": self._digest(raw),
                "clean_sha256": self._digest(clean),
                "normalization_sha256": self._digest(normalization_path.read_bytes()),
            }
        )
        self._write(f"qa/panels/{panel_id}.json", record)
        return record


class AcceptedContentTests(RepairPlanProjectHarness):
    def test_matching_artifacts_are_not_stale(self):
        record = self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )

        self.assertFalse(accepted_content_is_stale(self.project, record))

    def test_changed_accepted_bytes_are_stale(self):
        record = self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        (self.project / "panels/raw/p01-01.png").write_bytes(b"different bytes")

        self.assertTrue(accepted_content_is_stale(self.project, record))

    def test_a_missing_bound_artifact_is_stale(self):
        record = self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        (self.project / "panels/p01-01/clean.png").unlink()

        self.assertTrue(accepted_content_is_stale(self.project, record))

    def test_stale_accepted_content_falls_back_in_the_project_plan(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        (self.project / "panels/raw/p01-01.png").write_bytes(b"different bytes")

        document = project_repair_plan(self.project, localized_edit_supported=True)

        self.assertEqual(FULL_REGENERATION, document["panels"][0]["strategy"])
        self.assertEqual(STALE_BINDINGS, document["panels"][0]["fallback_reason"])


class PersistenceTests(RepairPlanProjectHarness):
    def test_the_plan_publishes_a_canonical_artifact_in_storyboard_order(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_record(panel_record(panel_id="p01-02"))

        path = plan_and_write_repair_plan(self.project, localized_edit_supported=True)

        self.assertEqual(self.project / REPAIR_PLAN_PATH, path)
        payload = path.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        document = json.loads(payload)
        self.assertEqual(REPAIR_PLAN_SCHEMA_VERSION, document["schema_version"])
        self.assertEqual(
            ["p01-01", "p01-02"], [entry["panel_id"] for entry in document["panels"]]
        )
        self.assertEqual(SELECTIVE_REPAIR, document["panels"][0]["strategy"])
        self.assertEqual(NO_REPAIR, document["panels"][1]["strategy"])

    def test_an_unreviewed_panel_carries_no_plan(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )

        document = project_repair_plan(self.project, localized_edit_supported=True)

        self.assertEqual(["p01-01"], [entry["panel_id"] for entry in document["panels"]])

    def test_a_resume_rewrites_byte_identical_content(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        path = plan_and_write_repair_plan(self.project, localized_edit_supported=True)
        first = path.read_bytes()

        plan_and_write_repair_plan(self.project, localized_edit_supported=True)

        self.assertEqual(first, path.read_bytes())

    def test_an_unreadable_review_fails_before_publication(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        (self.project / "qa/panels/p01-01.json").write_text("not json", encoding="utf-8")

        with self.assertRaises(RepairStrategyError):
            plan_and_write_repair_plan(self.project, localized_edit_supported=True)

        self.assertFalse((self.project / REPAIR_PLAN_PATH).exists())

    def test_a_repeated_storyboard_panel_fails_before_publication(self):
        self._write(
            "plan/storyboard.json",
            {
                "pages": [
                    {
                        "layout": "two-horizontal",
                        "number": 1,
                        "panels": [{"id": "p01-01"}, {"id": "p01-01"}],
                    }
                ],
                "schema_version": "1.0",
            },
        )

        with self.assertRaisesRegex(RepairStrategyError, "repeat a panel id"):
            plan_and_write_repair_plan(self.project, localized_edit_supported=True)

        self.assertFalse((self.project / REPAIR_PLAN_PATH).exists())


class RepairPlanValidationTests(RepairPlanProjectHarness):
    def _publish_plan(self, *, localized_edit_supported=True):
        return plan_and_write_repair_plan(
            self.project, localized_edit_supported=localized_edit_supported
        )

    def test_a_freshly_published_plan_is_valid(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()

        self.assertEqual((), validate_repair_plan(self.project))

    def test_a_succeeded_repair_leaves_its_entry_as_history(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        # The repair worked, so the panel is accepted again. The entry now
        # describes something that already happened rather than a stale claim.
        self._publish_record(panel_record())

        self.assertEqual((), validate_repair_plan(self.project))

    def test_a_plan_is_stale_after_a_new_review(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        self._publish_record(
            failing_record(identity=identity_check(failures={("mira", "face")}))
        )

        self.assertIn("repair-plan-stale", validate_repair_plan(self.project))

    def test_a_plan_is_stale_after_the_accepted_bytes_change(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        (self.project / "panels/raw/p01-01.png").write_bytes(b"different bytes")

        self.assertIn("repair-plan-stale", validate_repair_plan(self.project))

    def test_a_plan_for_an_unknown_panel_is_reported(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        document = json.loads((self.project / REPAIR_PLAN_PATH).read_text("utf-8"))
        document["panels"][0]["panel_id"] = "p09-09"
        self._write(REPAIR_PLAN_PATH, document)

        self.assertIn("repair-plan-panel-unknown", validate_repair_plan(self.project))

    def test_a_plan_with_a_missing_review_is_reported(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        (self.project / "qa/panels/p01-01.json").unlink()

        self.assertIn("repair-plan-record-missing", validate_repair_plan(self.project))

    def test_a_repeated_or_reordered_entry_is_reported(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_record(
            failing_record(
                panel_id="p01-02", identity=identity_check(failures={("mira", "face")})
            )
        )
        self._publish_plan()
        document = json.loads((self.project / REPAIR_PLAN_PATH).read_text("utf-8"))
        document["panels"].reverse()
        self._write(REPAIR_PLAN_PATH, document)

        self.assertIn("repair-plan-panel-order", validate_repair_plan(self.project))

        document["panels"].append(deepcopy(document["panels"][0]))
        self._write(REPAIR_PLAN_PATH, document)

        self.assertIn("repair-plan-panel-duplicate", validate_repair_plan(self.project))

    def test_an_unknown_schema_version_is_reported(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan()
        document = json.loads((self.project / REPAIR_PLAN_PATH).read_text("utf-8"))
        document["schema_version"] = "9.0"
        self._write(REPAIR_PLAN_PATH, document)

        self.assertEqual(
            ("repair-plan-schema-version",), validate_repair_plan(self.project)
        )

    def test_a_malformed_document_is_reported(self):
        self._write(REPAIR_PLAN_PATH, {"panels": []})

        self.assertEqual(("repair-plan-structure",), validate_repair_plan(self.project))

    def test_a_recorded_capability_flag_is_replanned_as_recorded(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )
        self._publish_plan(localized_edit_supported=False)

        self.assertEqual((), validate_repair_plan(self.project))
        document = json.loads((self.project / REPAIR_PLAN_PATH).read_text("utf-8"))
        self.assertEqual(FULL_REGENERATION, document["panels"][0]["strategy"])
        self.assertEqual(EDITING_UNSUPPORTED, document["panels"][0]["fallback_reason"])


class CommandLineTests(RepairPlanProjectHarness):
    def test_plan_and_panel_exit_zero_on_a_reviewed_project(self):
        self._publish_record(
            failing_record(identity=identity_check(failures={("ren", "hair")}))
        )

        self.assertEqual(0, main([str(self.project), "--plan", "--localized-edit"]))
        self.assertEqual(0, main([str(self.project), "--panel", "p01-01"]))
        self.assertTrue((self.project / REPAIR_PLAN_PATH).is_file())

    def test_an_unreviewed_panel_exits_nonzero(self):
        self.assertEqual(1, main([str(self.project), "--panel", "p01-02"]))

    def test_a_missing_storyboard_exits_nonzero(self):
        (self.project / "plan/storyboard.json").unlink()

        self.assertEqual(1, main([str(self.project), "--plan"]))
        self.assertFalse((self.project / REPAIR_PLAN_PATH).exists())


if __name__ == "__main__":
    unittest.main()
