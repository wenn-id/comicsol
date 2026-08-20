import contextlib
import importlib
import io
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from scripts.character_identity import derive_and_write_identity_pack, derive_identity_pack
from scripts.comic_sol import _accepted_panel_problem, record_override
from scripts.core_primitives import PANEL_CHECK_IDS, canonical_artifact_bytes
from scripts.reference_strategy import plan_and_write_reference_plan, project_reference_plan
from scripts.validate_project import validate_panel_record, validate_project


ROOT = Path(__file__).resolve().parents[1]


TRAITS = (
    "face",
    "hair",
    "age-appearance",
    "clothing",
    "accessories",
    "proportions",
    "immutable-traits",
)


def character(character_id, *, face, hair, silhouette, invariants):
    return {
        "age_band": "young-adult",
        "id": character_id,
        "motivation": "protect the route",
        "name": character_id.title(),
        "personality": ["precise"],
        "pronouns": "they/them",
        "reference_path": f"references/characters/{character_id}.png",
        "role": "courier",
        "speech": "short practical sentences",
        "visual_fingerprint": {
            "avoid": ["logos", "generated text"],
            "face": face,
            "hair": hair,
            "invariants": list(invariants),
            "palette": ["charcoal", "cream", "amber"],
            "signature_props": ["rectangular courier case"],
            "silhouette": silhouette,
            "wardrobe": "cream courier jacket and dark trousers",
        },
    }


def character_bible():
    return {
        "characters": [
            character(
                "mira",
                face="round face with wide dark eyes",
                hair="chin-length black bob",
                silhouette="short compact build",
                invariants=("amber scarf", "circular brass bag clasp"),
            ),
            character(
                "ren",
                face="long face with a faint squint",
                hair="sandy hair tied at the nape",
                silhouette="tall and angular",
                invariants=("brass loupe", "ink-stained finger"),
            ),
        ],
        "schema_version": "1.0",
    }


def storyboard():
    return {
        "pages": [
            {
                "layout": "two-horizontal",
                "number": 1,
                "panels": [
                    {
                        "characters": ["ren", "mira"],
                        "id": "p01-01",
                        "order": 1,
                        "shot": "close shot across the bench",
                    }
                ],
            }
        ],
        "schema_version": "1.0",
    }


def context_for_mira(module):
    board = storyboard()
    board["pages"][0]["panels"][0]["characters"] = ["mira"]
    bible = character_bible()
    pack = derive_identity_pack(bible)
    return module.character_consistency_context(
        pack,
        bible,
        project_reference_plan(pack, board),
        "p01-01",
        storyboard=board,
    )


def passing_assessments():
    return [
        {
            "character_id": "mira",
            "evidence": f"mira {trait} matches the canonical identity",
            "result": "pass",
            "severity": "error",
            "trait": trait,
        }
        for trait in TRAITS
    ]


def panel_record():
    return {
        "checks": [
            {
                "evidence": f"{check_id} inspected against the authored panel",
                "id": check_id,
                "method": "bounded-visual-review",
                "regions": [],
                "result": "pass",
                "reviewer": "qa-agent",
                "severity": "error",
            }
            for check_id in PANEL_CHECK_IDS
        ],
        "decision": "accept",
        "unresolved_warnings": [],
    }


class CharacterQualityContextTests(unittest.TestCase):
    def test_context_exposes_every_expected_trait_for_each_panel_character(self):
        try:
            module = importlib.import_module("scripts.character_quality")
        except ModuleNotFoundError:
            self.fail("character quality context is not implemented")
        pack = derive_identity_pack(character_bible())
        reference_plan = project_reference_plan(pack, storyboard())

        context = module.character_consistency_context(
            pack,
            character_bible(),
            reference_plan,
            "p01-01",
            storyboard=storyboard(),
        )

        self.assertEqual("p01-01", context["panel_id"])
        self.assertEqual(["mira", "ren"], [item["character_id"] for item in context["characters"]])
        mira = context["characters"][0]
        self.assertEqual(list(TRAITS), [item["trait"] for item in mira["traits"]])
        self.assertEqual(
            [
                "round face with wide dark eyes",
                "chin-length black bob",
                "young-adult",
                "cream courier jacket and dark trousers",
                ["rectangular courier case"],
                {"build": "short compact build", "notes": []},
                ["amber scarf", "circular brass bag clasp"],
            ],
            [item["expected"] for item in mira["traits"]],
        )
        self.assertEqual(
            ["references/characters/mira.png"],
            [item["path"] for item in mira["selected_references"]],
        )
        self.assertRegex(context["identity_pack_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(context["reference_plan_sha256"], r"^[0-9a-f]{64}$")

    def test_context_rejects_an_identity_pack_from_another_bible(self):
        module = importlib.import_module("scripts.character_quality")
        bible = character_bible()
        pack = derive_identity_pack(bible)
        plan = project_reference_plan(pack, storyboard())
        bible["characters"][0]["visual_fingerprint"]["hair"] = "silver braid"

        with self.assertRaisesRegex(module.CharacterQualityError, "character bible"):
            module.character_consistency_context(
                pack, bible, plan, "p01-01", storyboard=storyboard()
            )

    def test_context_rejects_a_reference_plan_that_drops_an_on_panel_character(self):
        module = importlib.import_module("scripts.character_quality")
        bible = character_bible()
        pack = derive_identity_pack(bible)
        board = storyboard()
        plan = project_reference_plan(pack, board)
        plan["panels"][0]["characters"] = ["mira"]
        plan["panels"][0]["selected"] = [
            item for item in plan["panels"][0]["selected"] if item["character_id"] == "mira"
        ]

        with self.assertRaisesRegex(module.CharacterQualityError, "storyboard"):
            module.character_consistency_context(
                pack,
                bible,
                plan,
                "p01-01",
                storyboard=board,
            )

    def test_context_rejects_a_selected_reference_not_derived_from_the_identity_pack(self):
        module = importlib.import_module("scripts.character_quality")
        bible = character_bible()
        pack = derive_identity_pack(bible)
        board = storyboard()
        plan = project_reference_plan(pack, board)
        plan["panels"][0]["selected"][0]["path"] = "../outside.png"

        with self.assertRaisesRegex(module.CharacterQualityError, "reference plan"):
            module.character_consistency_context(
                pack,
                bible,
                plan,
                "p01-01",
                storyboard=board,
            )


class CharacterIdentityCheckTests(unittest.TestCase):
    def test_warning_trait_is_explainable_without_becoming_a_hard_failure(self):
        module = importlib.import_module("scripts.character_quality")
        context = context_for_mira(module)
        assessments = passing_assessments()
        assessments[1].update(
            {
                "evidence": "the bob is slightly longer than the jaw line",
                "result": "warning",
                "severity": "warning",
            }
        )
        builder = getattr(module, "build_character_identity_check", None)
        self.assertIsNotNone(builder, "character-identity check builder is not implemented")

        check = builder(
            context,
            assessments,
            method="bounded-visual-review",
            reviewer="qa-agent",
        )

        self.assertEqual("character-identity", check["id"])
        self.assertEqual("warning", check["result"])
        self.assertEqual("warning", check["severity"])
        self.assertEqual(list(TRAITS), [item["trait"] for item in check["regions"]])
        hair = check["regions"][1]
        self.assertEqual("mira", hair["character_id"])
        self.assertEqual("chin-length black bob", hair["expected"])
        self.assertIn("mira", hair["repair_guidance"])
        self.assertIn("hair", hair["repair_guidance"])
        self.assertIn("chin-length black bob", hair["repair_guidance"])
        self.assertEqual("p01-01", check["provenance"]["panel_id"])
        self.assertEqual(
            ["references/characters/mira.png"],
            [item["path"] for item in check["provenance"]["characters"][0]["selected_references"]],
        )
        serialized = json.dumps(check, sort_keys=True).casefold()
        for provider_term in ("openai", "gemini", "claude", "endpoint", "api_key"):
            self.assertNotIn(provider_term, serialized)

    def test_error_level_trait_failure_is_a_hard_failure(self):
        module = importlib.import_module("scripts.character_quality")
        context = context_for_mira(module)
        assessments = passing_assessments()
        assessments[0].update(
            {
                "evidence": "face shape and eye spacing belong to another character",
                "result": "fail",
                "severity": "error",
            }
        )
        builder = getattr(module, "build_character_identity_check", None)
        self.assertIsNotNone(builder, "character-identity check builder is not implemented")

        check = builder(
            context,
            assessments,
            method="human-review",
            reviewer="editor",
        )

        self.assertEqual("fail", check["result"])
        self.assertEqual("error", check["severity"])
        self.assertIn("1 hard failure", check["evidence"])

    def test_incomplete_or_foreign_assessments_fail_closed(self):
        module = importlib.import_module("scripts.character_quality")
        context = context_for_mira(module)
        builder = module.build_character_identity_check
        missing = passing_assessments()[:-1]
        foreign = passing_assessments() + [
            {
                "character_id": "ren",
                "evidence": "ren face inspected",
                "result": "pass",
                "severity": "error",
                "trait": "face",
            }
        ]
        generic = passing_assessments()
        generic[0]["evidence"] = "ok"

        for assessments, message in (
            (missing, "missing assessment"),
            (foreign, "unexpected assessment"),
            (generic, "generic evidence"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(module.CharacterQualityError, message):
                    builder(
                        context,
                        assessments,
                        method="human-review",
                        reviewer="editor",
                    )


class PanelRecordIntegrationTests(unittest.TestCase):
    def test_warning_guidance_becomes_an_unresolved_panel_warning(self):
        module = importlib.import_module("scripts.character_quality")
        context = context_for_mira(module)
        assessments = passing_assessments()
        assessments[4].update(
            {
                "evidence": "the courier case is absent",
                "result": "warning",
                "severity": "warning",
            }
        )
        check = module.build_character_identity_check(
            context,
            assessments,
            method="human-review",
            reviewer="editor",
        )
        apply_check = getattr(module, "apply_character_identity_check", None)
        self.assertIsNotNone(apply_check, "panel QA integration is not implemented")

        updated = apply_check(panel_record(), check)

        self.assertEqual("accept-warning", updated["decision"])
        self.assertEqual(
            [check["regions"][4]["repair_guidance"]],
            updated["unresolved_warnings"],
        )
        self.assertEqual(check, updated["checks"][0])

    def test_hard_failure_requires_panel_regeneration(self):
        module = importlib.import_module("scripts.character_quality")
        context = context_for_mira(module)
        assessments = passing_assessments()
        assessments[5].update(
            {
                "evidence": "body proportions changed from compact to very tall",
                "result": "fail",
                "severity": "error",
            }
        )
        check = module.build_character_identity_check(
            context,
            assessments,
            method="human-review",
            reviewer="editor",
        )
        apply_check = getattr(module, "apply_character_identity_check", None)
        self.assertIsNotNone(apply_check, "panel QA integration is not implemented")

        updated = apply_check(panel_record(), check)

        self.assertEqual("regenerate", updated["decision"])
        self.assertEqual([], updated["unresolved_warnings"])


class CharacterQualityProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.project = Path(self._temporary.name) / "project"
        shutil.copytree(ROOT / "tests/fixtures/valid-one-page", self.project)
        derive_and_write_identity_pack(self.project)
        plan_and_write_reference_plan(self.project)
        self.module = importlib.import_module("scripts.character_quality")
        pack = json.loads((self.project / "plan/character-identity-pack.json").read_text("utf-8"))
        bible = json.loads((self.project / "plan/character-bible.json").read_text("utf-8"))
        plan = json.loads((self.project / "logs/reference-selection.json").read_text("utf-8"))
        board = json.loads((self.project / "plan/storyboard.json").read_text("utf-8"))
        context = self.module.character_consistency_context(
            pack, bible, plan, "p01-01", storyboard=board
        )
        check = self.module.build_character_identity_check(
            context,
            passing_assessments(),
            method="human-review",
            reviewer="editor",
        )
        qa_path = self.project / "qa/panels/p01-01.json"
        record = json.loads(qa_path.read_text("utf-8"))
        self.record = self.module.apply_character_identity_check(record, check)
        qa_path.write_bytes(canonical_artifact_bytes(self.record))

    def test_current_identity_and_reference_provenance_is_reusable(self):
        validator = getattr(self.module, "validate_character_quality_provenance", None)
        self.assertIsNotNone(validator, "character QA provenance validator is not implemented")

        self.assertEqual((), validator(self.project, self.record))
        self.assertIsNone(_accepted_panel_problem(self.project, self.record))

    def test_semantically_unchanged_json_formatting_keeps_the_review_reusable(self):
        for relative in (
            "plan/character-identity-pack.json",
            "logs/reference-selection.json",
        ):
            path = self.project / relative
            document = json.loads(path.read_text("utf-8"))
            path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

        self.assertEqual(
            (),
            self.module.validate_character_quality_provenance(self.project, self.record),
        )
        self.assertIsNone(_accepted_panel_problem(self.project, self.record))

    def test_changed_reference_plan_makes_the_accepted_review_stale(self):
        validator = getattr(self.module, "validate_character_quality_provenance", None)
        self.assertIsNotNone(validator, "character QA provenance validator is not implemented")
        plan_path = self.project / "logs/reference-selection.json"
        plan = json.loads(plan_path.read_text("utf-8"))
        plan["panels"][0]["reference_budget"] = 0
        plan_path.write_bytes(canonical_artifact_bytes(plan))

        issues = validator(self.project, self.record)

        self.assertTrue(any("reference plan" in issue for issue in issues), issues)
        self.assertIn("character consistency", _accepted_panel_problem(self.project, self.record))

    def test_malformed_reference_plan_is_reported_without_crashing_validation(self):
        plan_path = self.project / "logs/reference-selection.json"
        plan = json.loads(plan_path.read_text("utf-8"))
        plan["panels"][0]["characters"] = [[]]
        plan_path.write_bytes(canonical_artifact_bytes(plan))

        issues = self.module.validate_character_quality_provenance(self.project, self.record)

        self.assertTrue(any("reference plan" in issue for issue in issues), issues)
        project_issues = validate_project(self.project, "panels")
        self.assertTrue(
            any("character-consistency-provenance" in issue.field for issue in project_issues),
            project_issues,
        )

    def test_changed_identity_pack_makes_the_accepted_review_stale(self):
        validator = getattr(self.module, "validate_character_quality_provenance", None)
        self.assertIsNotNone(validator, "character QA provenance validator is not implemented")
        pack_path = self.project / "plan/character-identity-pack.json"
        pack = json.loads(pack_path.read_text("utf-8"))
        pack["characters"][0]["immutable_traits"]["hair"] = "waist-length silver hair"
        pack_path.write_bytes(canonical_artifact_bytes(pack))

        issues = validator(self.project, self.record)

        self.assertTrue(any("identity pack" in issue for issue in issues), issues)

    def test_character_bible_drift_makes_the_identity_review_stale(self):
        validator = self.module.validate_character_quality_provenance
        bible_path = self.project / "plan/character-bible.json"
        bible = json.loads(bible_path.read_text("utf-8"))
        bible["characters"][0]["visual_fingerprint"]["face"] = "square face"
        bible_path.write_bytes(canonical_artifact_bytes(bible))

        issues = validator(self.project, self.record)

        self.assertTrue(any("character bible" in issue for issue in issues), issues)

    def test_non_object_character_bible_is_reported_without_crashing_validation(self):
        bible_path = self.project / "plan/character-bible.json"
        bible_path.write_text("[]\n", encoding="utf-8")

        issues = self.module.validate_character_quality_provenance(self.project, self.record)

        self.assertTrue(any("character identity context" in issue for issue in issues), issues)
        project_issues = validate_project(self.project, "panels")
        self.assertTrue(
            any("character-consistency-provenance" in issue.field for issue in project_issues),
            project_issues,
        )

    def test_panel_schema_rejects_a_malformed_trait_record(self):
        broken = deepcopy(self.record)
        del broken["checks"][0]["regions"][0]["expected"]

        issues = validate_panel_record(broken)

        self.assertTrue(
            any("character-trait-structure" in issue.field for issue in issues),
            issues,
        )

    def test_panel_schema_rejects_rich_character_regions_without_provenance(self):
        broken = deepcopy(self.record)
        del broken["checks"][0]["provenance"]

        issues = validate_panel_record(broken)

        self.assertTrue(
            any("character-provenance-structure" in issue.field for issue in issues),
            issues,
        )

    def test_panel_schema_rejects_generic_trait_evidence(self):
        generic = deepcopy(self.record)
        generic["checks"][0]["regions"][0]["evidence"] = "ok"

        issues = validate_panel_record(generic)

        self.assertTrue(
            any("character-trait-evidence" in issue.field for issue in issues),
            issues,
        )

    def test_project_validation_reports_stale_character_provenance(self):
        plan_path = self.project / "logs/reference-selection.json"
        plan = json.loads(plan_path.read_text("utf-8"))
        plan["panels"][0]["shot_class"] = "unclassified"
        plan_path.write_bytes(canonical_artifact_bytes(plan))

        issues = validate_project(self.project, "panels")

        self.assertTrue(
            any("character-consistency-provenance" in issue.field for issue in issues),
            issues,
        )

    def test_record_review_publishes_a_canonical_resumable_panel_record(self):
        writer = getattr(self.module, "record_character_quality_review", None)
        self.assertIsNotNone(writer, "character QA review writer is not implemented")
        assessments = passing_assessments()
        assessments[3].update(
            {
                "evidence": "jacket color drifts slightly toward green",
                "result": "warning",
                "severity": "warning",
            }
        )

        path = writer(
            self.project,
            "p01-01",
            assessments,
            method="human-review",
            reviewer="editor",
        )

        record = json.loads(path.read_text("utf-8"))
        self.assertEqual(canonical_artifact_bytes(record), path.read_bytes())
        self.assertEqual("accept-warning", record["decision"])
        self.assertEqual(
            (), self.module.validate_character_quality_provenance(self.project, record)
        )
        self.assertEqual([], validate_panel_record(record))

    def test_record_review_reads_project_state_only_after_acquiring_the_lock(self):
        real_transaction = self.module.ProjectTransaction
        real_read = self.module._read_document

        class GuardedTransaction(real_transaction):
            active = False

            def __enter__(self):
                entered = super().__enter__()
                type(self).active = True
                return entered

            def __exit__(self, exc_type, exc, traceback):
                try:
                    return super().__exit__(exc_type, exc, traceback)
                finally:
                    type(self).active = False

        def guarded_read(*args, **kwargs):
            self.assertTrue(GuardedTransaction.active, "project state was read before lock")
            return real_read(*args, **kwargs)

        with (
            mock.patch.object(self.module, "ProjectTransaction", GuardedTransaction),
            mock.patch.object(self.module, "_read_document", side_effect=guarded_read),
        ):
            self.module.record_character_quality_review(
                self.project,
                "p01-01",
                passing_assessments(),
                method="human-review",
                reviewer="editor",
            )

    def test_context_command_emits_provider_neutral_review_input(self):
        command = getattr(self.module, "main", None)
        self.assertIsNotNone(command, "character QA command is not implemented")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = command([str(self.project), "--context", "p01-01"])

        self.assertEqual(0, code)
        context = json.loads(output.getvalue())
        self.assertEqual("p01-01", context["panel_id"])
        self.assertEqual(
            list(TRAITS), [item["trait"] for item in context["characters"][0]["traits"]]
        )

    def test_record_command_consumes_normalized_assessments_from_stdin(self):
        command = getattr(self.module, "main", None)
        self.assertIsNotNone(command, "character QA command is not implemented")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = command(
                [
                    str(self.project),
                    "--record",
                    "p01-01",
                    "--method",
                    "human-review",
                    "--reviewer",
                    "editor",
                ],
                input_stream=io.StringIO(json.dumps(passing_assessments())),
            )

        self.assertEqual(0, code)
        path = Path(output.getvalue().strip())
        self.assertEqual(self.project / "qa/panels/p01-01.json", path)
        self.assertEqual("accept", json.loads(path.read_text("utf-8"))["decision"])

    def test_user_override_downgrades_the_actionable_trait_failure(self):
        assessments = passing_assessments()
        assessments[0].update(
            {
                "evidence": "face shape belongs to another character",
                "result": "fail",
                "severity": "error",
            }
        )
        self.module.record_character_quality_review(
            self.project,
            "p01-01",
            assessments,
            method="human-review",
            reviewer="editor",
        )

        record_override(self.project, "p01-01", "user accepts the visible face drift")

        record = json.loads((self.project / "qa/panels/p01-01.json").read_text("utf-8"))
        character_check = record["checks"][0]
        self.assertEqual(
            ("fail", "warning"), (character_check["result"], character_check["severity"])
        )
        self.assertEqual("warning", character_check["regions"][0]["severity"])
        self.assertEqual([], validate_panel_record(record))
        self.assertIsNone(_accepted_panel_problem(self.project, record))

    def test_unrelated_override_preserves_a_warning_character_check(self):
        assessments = passing_assessments()
        assessments[0].update(
            {
                "evidence": "face shape has a minor but recognizable drift",
                "result": "fail",
                "severity": "warning",
            }
        )
        self.module.record_character_quality_review(
            self.project,
            "p01-01",
            assessments,
            method="human-review",
            reviewer="editor",
        )
        qa_path = self.project / "qa/panels/p01-01.json"
        record = json.loads(qa_path.read_text("utf-8"))
        record["checks"][1].update({"result": "fail", "severity": "error"})
        record["decision"] = "regenerate"
        qa_path.write_bytes(canonical_artifact_bytes(record))

        record_override(self.project, "p01-01", "user accepts the anatomy drift")

        updated = json.loads(qa_path.read_text("utf-8"))
        character_check = updated["checks"][0]
        self.assertEqual(
            ("warning", "warning"),
            (character_check["result"], character_check["severity"]),
        )
        self.assertEqual([], validate_panel_record(updated))

    def test_overridden_panel_must_be_regenerated_before_a_new_review(self):
        assessments = passing_assessments()
        assessments[0].update(
            {
                "evidence": "face shape belongs to another character",
                "result": "fail",
                "severity": "error",
            }
        )
        self.module.record_character_quality_review(
            self.project,
            "p01-01",
            assessments,
            method="human-review",
            reviewer="editor",
        )
        record_override(self.project, "p01-01", "user accepts the visible face drift")
        qa_path = self.project / "qa/panels/p01-01.json"
        before = qa_path.read_bytes()

        with self.assertRaisesRegex(self.module.CharacterQualityError, "regenerated"):
            self.module.record_character_quality_review(
                self.project,
                "p01-01",
                passing_assessments(),
                method="human-review",
                reviewer="editor",
            )

        self.assertEqual(before, qa_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
