import json
import tempfile
import unittest
from pathlib import Path

from scripts.character_identity import (
    IDENTITY_PACK_PATH,
    derive_and_write_identity_pack,
    derive_identity_pack,
)
from scripts.reference_strategy import (
    CANONICAL_ANCHOR,
    CLOSE_UP,
    DUPLICATE_PATH,
    FULL_BODY,
    IDENTITY_SUPPLEMENT,
    PROFILE,
    REFERENCE_BUDGET,
    REFERENCE_PLAN_PATH,
    REFERENCE_PLAN_SCHEMA_VERSION,
    REFERENCES_UNSUPPORTED,
    SCENE_SPECIFIC,
    SHOT_ALIGNED,
    THREE_QUARTER,
    UNCLASSIFIED,
    ReferenceStrategyError,
    classify_shot,
    main,
    panel_reference_plan,
    plan_and_write_reference_plan,
    project_reference_plan,
    reference_plan_block,
)


def character(character_id, name, *, silhouette, face, hair, invariants):
    return {
        "age_band": "young-adult",
        "id": character_id,
        "motivation": "keep the ledger true",
        "name": name,
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
                "Mira",
                silhouette="short compact build",
                face="round face with wide dark eyes",
                hair="chin-length black bob",
                invariants=["amber scarf", "circular brass bag clasp"],
            ),
            character(
                "ren",
                "Ren",
                silhouette="tall and angular",
                face="long face with a faint squint",
                hair="sandy hair tied at the nape",
                invariants=["brass loupe on the forehead", "ink-stained finger"],
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
                        "shot": "close shot across the bench, camera level with the page",
                    },
                    {
                        "characters": ["mira"],
                        "id": "p01-02",
                        "order": 2,
                        "shot": "wide establishing shot from the doorway",
                    },
                ],
            },
            {
                "layout": "full-page",
                "number": 2,
                "panels": [
                    {
                        "characters": ["mira"],
                        "id": "p02-01",
                        "order": 1,
                        "shot": "profile shot at frame left",
                    }
                ],
            },
        ],
        "schema_version": "1.0",
    }


def solo_storyboard():
    """Return the same storyboard with one character in every panel.

    Ranking within one character is only observable when the panel does not also
    interleave a second character's canonical anchor ahead of it.
    """
    board = storyboard()
    board["pages"][0]["panels"][0]["characters"] = ["mira"]
    return board


def with_views(pack, character_id, *views):
    """Append authored reference views to one pack entry, as an author would."""
    entry = next(item for item in pack["characters"] if item["id"] == character_id)
    entry["reference_views"] = entry["reference_views"] + [
        {"path": f"references/characters/{character_id}-{view}.png", "view": view}
        for view in views
    ]
    return pack


def views_of(plan):
    return [(item.character_id, item.view, item.reason) for item in plan.selected]


def omissions_of(plan):
    return [(item.character_id, item.view, item.reason) for item in plan.omitted]


class ShotClassificationTests(unittest.TestCase):
    def test_documented_shot_classes_are_recognized(self):
        cases = {
            "close shot across the bench": (CLOSE_UP, "close shot"),
            "extreme close-up on the ledger": (CLOSE_UP, "extreme close"),
            "profile shot at frame left": (PROFILE, "profile"),
            "over the shoulder as she reads": (THREE_QUARTER, "over the shoulder"),
            "cinematic medium-wide manga panel": (THREE_QUARTER, "medium-wide"),
            "full-body hero framing": (FULL_BODY, "full-body"),
            "wide establishing shot from the doorway": (FULL_BODY, "wide establishing"),
        }

        for shot, expected in cases.items():
            with self.subTest(shot=shot):
                self.assertEqual(expected, classify_shot(shot))

    def test_an_unrecognized_shot_is_reported_rather_than_guessed(self):
        self.assertEqual((UNCLASSIFIED, None), classify_shot("camera drifts inward"))
        self.assertEqual((UNCLASSIFIED, None), classify_shot(None))

    def test_the_earliest_cue_decides_a_mixed_description(self):
        # Both descriptions name two framings; the one they open with is the shot.
        self.assertEqual(
            (THREE_QUARTER, "medium wide"),
            classify_shot("medium wide shot along the canal"),
        )
        self.assertEqual(
            (FULL_BODY, "wide shot"),
            classify_shot("wide shot, then a close-up insert"),
        )


class SelectionRuleTests(unittest.TestCase):
    def setUp(self):
        self.pack = derive_identity_pack(character_bible())
        self.storyboard = storyboard()

    def test_canonical_view_leads_every_shot_class(self):
        with_views(self.pack, "mira", "close-up", "full-body", "profile")

        for panel_id in ("p01-01", "p01-02", "p02-01"):
            with self.subTest(panel=panel_id):
                plan = panel_reference_plan(self.pack, self.storyboard, panel_id)

                self.assertEqual("mira", plan.selected[0].character_id)
                self.assertEqual("canonical", plan.selected[0].view)
                self.assertEqual(CANONICAL_ANCHOR, plan.selected[0].reason)
                self.assertEqual(1, plan.selected[0].rank)

    def test_shot_aligned_view_follows_the_canonical_anchor(self):
        with_views(self.pack, "mira", "close-up", "full-body", "profile")
        board = solo_storyboard()

        aligned = {
            "p01-01": "close-up",
            "p01-02": "full-body",
            "p02-01": "profile",
        }
        for panel_id, view in aligned.items():
            with self.subTest(panel=panel_id):
                plan = panel_reference_plan(self.pack, board, panel_id)

                self.assertEqual("canonical", plan.selected[0].view)
                self.assertEqual(view, plan.selected[1].view)
                self.assertEqual(SHOT_ALIGNED, plan.selected[1].reason)

    def test_scene_specific_views_never_precede_identity_views(self):
        with_views(self.pack, "mira", "scene-market", "profile")

        plan = panel_reference_plan(self.pack, self.storyboard, "p02-01")

        self.assertEqual(
            [
                ("mira", "canonical", CANONICAL_ANCHOR),
                ("mira", "profile", SHOT_ALIGNED),
                ("mira", "scene-market", SCENE_SPECIFIC),
            ],
            views_of(plan),
        )

    def test_remaining_identity_views_are_supplements_not_alignments(self):
        with_views(self.pack, "mira", "close-up")

        plan = panel_reference_plan(self.pack, self.storyboard, "p01-02")

        self.assertEqual(
            [
                ("mira", "canonical", CANONICAL_ANCHOR),
                ("mira", "close-up", IDENTITY_SUPPLEMENT),
            ],
            views_of(plan),
        )

    def test_an_unclassified_shot_claims_no_alignment(self):
        with_views(self.pack, "mira", "close-up", "profile")
        board = storyboard()
        board["pages"][1]["panels"][0]["shot"] = "camera drifts inward"

        plan = panel_reference_plan(self.pack, board, "p02-01")

        self.assertEqual(UNCLASSIFIED, plan.shot_class)
        self.assertIsNone(plan.shot_cue)
        self.assertNotIn(SHOT_ALIGNED, [item.reason for item in plan.selected])

    def test_selection_order_follows_the_pack_not_the_panel(self):
        plan = panel_reference_plan(self.pack, self.storyboard, "p01-01")

        self.assertEqual(
            ["ren", "mira"], self.storyboard["pages"][0]["panels"][0]["characters"]
        )
        self.assertEqual(("mira", "ren"), plan.character_ids)
        self.assertEqual(
            ("references/characters/mira.png", "references/characters/ren.png"),
            plan.attachment_paths,
        )


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.pack = derive_identity_pack(character_bible())
        self.storyboard = storyboard()

    def test_every_character_is_anchored_before_any_second_view(self):
        with_views(self.pack, "mira", "profile")
        with_views(self.pack, "ren", "profile")

        plan = panel_reference_plan(
            self.pack, self.storyboard, "p01-01", reference_budget=3
        )

        self.assertEqual(
            [
                ("mira", "canonical", CANONICAL_ANCHOR),
                ("ren", "canonical", CANONICAL_ANCHOR),
                ("mira", "profile", IDENTITY_SUPPLEMENT),
            ],
            views_of(plan),
        )
        self.assertEqual([("ren", "profile", REFERENCE_BUDGET)], omissions_of(plan))

    def test_a_budget_below_the_cast_size_is_recorded_not_hidden(self):
        plan = panel_reference_plan(
            self.pack, self.storyboard, "p01-01", reference_budget=1
        )

        self.assertEqual([("mira", "canonical", CANONICAL_ANCHOR)], views_of(plan))
        self.assertEqual([("ren", "canonical", REFERENCE_BUDGET)], omissions_of(plan))

    def test_a_repeated_path_is_attached_once_without_spending_the_budget(self):
        shared = "references/characters/mira.png"
        entry = next(item for item in self.pack["characters"] if item["id"] == "ren")
        entry["reference_views"] = [{"path": shared, "view": "canonical"}]
        with_views(self.pack, "mira", "profile")

        plan = panel_reference_plan(
            self.pack, self.storyboard, "p01-01", reference_budget=2
        )

        self.assertIn(("ren", "canonical", DUPLICATE_PATH), omissions_of(plan))
        # The duplicate did not consume the budget, so mira's second view still fits.
        self.assertEqual(
            [
                ("mira", "canonical", CANONICAL_ANCHOR),
                ("mira", "profile", IDENTITY_SUPPLEMENT),
            ],
            views_of(plan),
        )
        self.assertEqual(
            (shared, "references/characters/mira-profile.png"),
            plan.attachment_paths,
        )

    def test_a_zero_budget_records_every_reference_as_unsupported(self):
        plan = panel_reference_plan(
            self.pack, self.storyboard, "p01-01", reference_budget=0
        )

        self.assertEqual([], views_of(plan))
        self.assertEqual((), plan.attachment_paths)
        self.assertEqual(
            [
                ("mira", "canonical", REFERENCES_UNSUPPORTED),
                ("ren", "canonical", REFERENCES_UNSUPPORTED),
            ],
            omissions_of(plan),
        )

    def test_an_invalid_budget_fails_closed(self):
        for budget in (-1, True, 1.5, "2"):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ReferenceStrategyError, "reference budget"):
                    panel_reference_plan(
                        self.pack, self.storyboard, "p01-01", reference_budget=budget
                    )


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.pack = with_views(
            derive_identity_pack(character_bible()), "mira", "profile"
        )
        self.storyboard = storyboard()

    def test_document_records_every_panel_in_storyboard_order(self):
        document = project_reference_plan(self.pack, self.storyboard, reference_budget=2)

        self.assertEqual(REFERENCE_PLAN_SCHEMA_VERSION, document["schema_version"])
        self.assertEqual(
            ["p01-01", "p01-02", "p02-01"],
            [panel["panel_id"] for panel in document["panels"]],
        )

    def test_panel_record_names_the_shot_budget_and_every_reason(self):
        document = project_reference_plan(self.pack, self.storyboard, reference_budget=2)
        record = document["panels"][0]

        self.assertEqual(
            {
                "characters",
                "omitted",
                "panel_id",
                "reference_budget",
                "selected",
                "shot_class",
                "shot_cue",
            },
            set(record),
        )
        self.assertEqual(CLOSE_UP, record["shot_class"])
        self.assertEqual("close shot", record["shot_cue"])
        self.assertEqual(2, record["reference_budget"])
        self.assertEqual(["mira", "ren"], record["characters"])
        self.assertEqual(
            [
                {
                    "character_id": "mira",
                    "path": "references/characters/mira.png",
                    "rank": 1,
                    "reason": CANONICAL_ANCHOR,
                    "view": "canonical",
                },
                {
                    "character_id": "ren",
                    "path": "references/characters/ren.png",
                    "rank": 2,
                    "reason": CANONICAL_ANCHOR,
                    "view": "canonical",
                },
            ],
            record["selected"],
        )
        self.assertEqual(
            [
                {
                    "character_id": "mira",
                    "path": "references/characters/mira-profile.png",
                    "reason": REFERENCE_BUDGET,
                    "view": "profile",
                }
            ],
            record["omitted"],
        )

    def test_an_unlimited_budget_is_recorded_as_null(self):
        document = project_reference_plan(self.pack, self.storyboard)

        self.assertIsNone(document["panels"][0]["reference_budget"])

    def test_planning_is_deterministic(self):
        first = project_reference_plan(self.pack, self.storyboard, reference_budget=2)
        second = project_reference_plan(
            with_views(derive_identity_pack(character_bible()), "mira", "profile"),
            storyboard(),
            reference_budget=2,
        )

        self.assertEqual(first, second)

    def test_unknown_panel_and_unknown_character_fail_closed(self):
        with self.assertRaisesRegex(ReferenceStrategyError, "no panel 'p09-09'"):
            panel_reference_plan(self.pack, self.storyboard, "p09-09")

        board = storyboard()
        board["pages"][1]["panels"][0]["characters"] = ["ghost"]
        with self.assertRaisesRegex(ReferenceStrategyError, "no entry for: ghost"):
            panel_reference_plan(self.pack, board, "p02-01")

    def test_a_panel_without_characters_plans_nothing(self):
        board = storyboard()
        board["pages"][1]["panels"][0]["characters"] = []

        plan = panel_reference_plan(self.pack, board, "p02-01")

        self.assertEqual((), plan.character_ids)
        self.assertEqual((), plan.selected)
        self.assertEqual((), plan.omitted)

    def test_block_is_deterministic_plain_text(self):
        plan = panel_reference_plan(
            self.pack, self.storyboard, "p01-01", reference_budget=2
        )

        block = reference_plan_block(plan)

        self.assertEqual(block, reference_plan_block(plan))
        self.assertEqual(
            "\n".join(
                [
                    "REFERENCE PLAN (reference-selection 1.0)",
                    "- panel: p01-01",
                    "- shot class: close-up (cue: close shot)",
                    "- reference budget: 2",
                    "- attach in this order:",
                    "  1. mira canonical=references/characters/mira.png "
                    "(canonical-anchor)",
                    "  2. ren canonical=references/characters/ren.png "
                    "(canonical-anchor)",
                    "- omitted:",
                    "  mira profile=references/characters/mira-profile.png "
                    "(reference-budget)",
                ]
            ),
            block,
        )

    def test_block_never_names_a_provider_or_credential(self):
        plan = panel_reference_plan(self.pack, self.storyboard, "p01-01")

        block = reference_plan_block(plan).casefold()

        for forbidden in ("api", "key", "token", "endpoint", "http", "model="):
            self.assertNotIn(forbidden, block)


class ReferencePlanProjectHarness(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.project = Path(self._temporary.name)
        (self.project / "plan").mkdir(parents=True)
        (self.project / "references/characters").mkdir(parents=True)
        (self.project / "plan/character-bible.json").write_text(
            json.dumps(character_bible(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.project / "plan/storyboard.json").write_text(
            json.dumps(storyboard(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for name in ("mira", "ren"):
            (self.project / f"references/characters/{name}.png").write_bytes(
                b"\x89PNG\r\n\x1a\n"
            )


class PersistenceTests(ReferencePlanProjectHarness):
    def test_plan_publishes_a_canonical_artifact(self):
        derive_and_write_identity_pack(self.project)

        path, issues = plan_and_write_reference_plan(self.project, reference_budget=4)

        self.assertEqual((), issues)
        self.assertEqual(self.project / REFERENCE_PLAN_PATH, path)
        payload = path.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        document = json.loads(payload)
        self.assertEqual(REFERENCE_PLAN_SCHEMA_VERSION, document["schema_version"])
        self.assertEqual(
            ["p01-01", "p01-02", "p02-01"],
            [panel["panel_id"] for panel in document["panels"]],
        )

    def test_resume_rewrites_byte_identical_content(self):
        derive_and_write_identity_pack(self.project)
        path, _ = plan_and_write_reference_plan(self.project, reference_budget=4)
        first = path.read_bytes()

        plan_and_write_reference_plan(self.project, reference_budget=4)

        self.assertEqual(first, path.read_bytes())

    def test_planning_refuses_a_missing_identity_pack(self):
        path, issues = plan_and_write_reference_plan(self.project)

        self.assertEqual(
            (f"{IDENTITY_PACK_PATH} is missing; derive it before generation",), issues
        )
        self.assertFalse(path.exists())

    def test_planning_refuses_a_pack_whose_reference_is_missing(self):
        derive_and_write_identity_pack(self.project)
        (self.project / "references/characters/mira.png").unlink()

        path, issues = plan_and_write_reference_plan(self.project)

        self.assertTrue(issues)
        self.assertFalse(path.exists())

    def test_corrupt_storyboard_fails_closed(self):
        derive_and_write_identity_pack(self.project)
        (self.project / "plan/storyboard.json").write_text(
            "{ not json", encoding="utf-8"
        )

        with self.assertRaisesRegex(ReferenceStrategyError, "is not valid JSON"):
            plan_and_write_reference_plan(self.project)


class CommandLineTests(ReferencePlanProjectHarness):
    def test_plan_and_panel_exit_zero_on_a_valid_project(self):
        derive_and_write_identity_pack(self.project)

        self.assertEqual(0, main([str(self.project), "--plan", "--budget", "3"]))
        self.assertEqual(0, main([str(self.project), "--panel", "p01-01"]))
        self.assertTrue((self.project / REFERENCE_PLAN_PATH).is_file())

    def test_plan_fails_before_the_identity_pack_exists(self):
        self.assertEqual(1, main([str(self.project), "--plan"]))
        self.assertEqual(1, main([str(self.project), "--panel", "p01-01"]))
        self.assertFalse((self.project / REFERENCE_PLAN_PATH).exists())

    def test_unknown_panel_exits_nonzero(self):
        derive_and_write_identity_pack(self.project)

        self.assertEqual(1, main([str(self.project), "--panel", "p09-09"]))

    def test_negative_budget_exits_nonzero(self):
        derive_and_write_identity_pack(self.project)

        self.assertEqual(1, main([str(self.project), "--plan", "--budget", "-1"]))
        self.assertFalse((self.project / REFERENCE_PLAN_PATH).exists())


if __name__ == "__main__":
    unittest.main()
