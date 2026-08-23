import json
import tempfile
import unittest
from pathlib import Path

from scripts.character_identity import (
    CANONICAL_VIEW,
    DERIVED_FINGERPRINT_FIELDS,
    IDENTITY_PACK_PATH,
    IDENTITY_PACK_SCHEMA_VERSION,
    IdentityPackError,
    check_identity_pack,
    derive_and_write_identity_pack,
    derive_identity_pack,
    fingerprint_digest,
    identity_prompt_block,
    identity_reference_paths,
    main,
    panel_identity_context,
    read_identity_pack,
    validate_identity_pack,
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
                invariants=["brass loupe on the forehead", "ink-stained right index finger"],
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
                    {"characters": ["ren", "mira"], "id": "p01-01", "order": 1},
                    {"characters": ["mira"], "id": "p01-02", "order": 2},
                ],
            }
        ],
        "schema_version": "1.0",
    }


class IdentityPackHarness(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.project = Path(self._temporary.name)
        (self.project / "plan").mkdir(parents=True)
        (self.project / "references/characters").mkdir(parents=True)
        self.bible = character_bible()
        self.write_bible(self.bible)
        self.write_storyboard(storyboard())
        for character_id in ("mira", "ren"):
            self.reference(character_id)

    def write_bible(self, bible):
        (self.project / "plan/character-bible.json").write_text(
            json.dumps(bible, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def write_storyboard(self, board):
        (self.project / "plan/storyboard.json").write_text(
            json.dumps(board, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def reference(self, name):
        path = self.project / f"references/characters/{name}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path


class DerivationTests(IdentityPackHarness):
    def test_derived_pack_captures_the_documented_identity_surface(self):
        pack = derive_identity_pack(self.bible)

        self.assertEqual(IDENTITY_PACK_SCHEMA_VERSION, pack["schema_version"])
        self.assertEqual(["mira", "ren"], [entry["id"] for entry in pack["characters"]])
        mira = pack["characters"][0]
        self.assertEqual(
            {"face", "hair", "invariants", "silhouette"}, set(mira["immutable_traits"])
        )
        self.assertEqual("short compact build", mira["immutable_traits"]["silhouette"])
        self.assertEqual(
            ["amber scarf", "circular brass bag clasp"],
            mira["immutable_traits"]["invariants"],
        )
        self.assertEqual("cream courier jacket and dark trousers", mira["wardrobe"]["base"])
        self.assertEqual(["rectangular courier case"], mira["wardrobe"]["accessories"])
        self.assertEqual(["charcoal", "cream", "amber"], mira["wardrobe"]["palette"])
        self.assertEqual("short compact build", mira["proportions"]["build"])
        self.assertEqual([], mira["proportions"]["notes"])
        self.assertEqual(
            [{"path": "references/characters/mira.png", "view": CANONICAL_VIEW}],
            mira["reference_views"],
        )
        self.assertEqual(
            fingerprint_digest(self.bible["characters"][0]["visual_fingerprint"]),
            mira["source_fingerprint_sha256"],
        )

    def test_derivation_is_deterministic_and_validates(self):
        first = derive_identity_pack(self.bible)
        second = derive_identity_pack(character_bible())

        self.assertEqual(first, second)
        self.assertEqual((), validate_identity_pack(first, character_bible=self.bible))

    def test_derivation_rejects_a_character_without_a_fingerprint(self):
        bible = character_bible()
        del bible["characters"][0]["visual_fingerprint"]

        with self.assertRaisesRegex(IdentityPackError, "visual_fingerprint"):
            derive_identity_pack(bible)


class ValidationTests(IdentityPackHarness):
    def test_valid_pack_reports_no_issues(self):
        pack = derive_identity_pack(self.bible)

        self.assertEqual(
            (),
            validate_identity_pack(pack, character_bible=self.bible, project_dir=self.project),
        )

    def test_unknown_and_missing_fields_are_reported(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["nickname"] = "scout"
        del pack["characters"][0]["avoid"]

        issues = validate_identity_pack(pack)

        self.assertTrue(any("unknown fields: nickname" in issue for issue in issues), issues)
        self.assertTrue(any("missing fields: avoid" in issue for issue in issues), issues)

    def test_unsupported_schema_version_is_rejected(self):
        pack = derive_identity_pack(self.bible)
        pack["schema_version"] = "2.0"

        issues = validate_identity_pack(pack)

        self.assertTrue(any("schema_version" in issue for issue in issues), issues)

    def test_invariants_stay_panel_checkable(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["immutable_traits"]["invariants"] = ["only one"]

        issues = validate_identity_pack(pack)

        self.assertTrue(any("at least 2 entries" in issue for issue in issues), issues)

    def test_missing_canonical_view_is_rejected(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["reference_views"] = [
            {"path": "references/characters/mira-profile.png", "view": "profile"}
        ]

        issues = validate_identity_pack(pack)

        self.assertTrue(any("'canonical' view" in issue for issue in issues), issues)

    def test_escaping_reference_path_is_rejected(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["reference_views"][0]["path"] = "../secrets/mira.png"

        issues = validate_identity_pack(pack, project_dir=self.project)

        self.assertTrue(any("POSIX relative project path" in issue for issue in issues), issues)

    def test_missing_reference_file_is_reported_before_generation(self):
        pack = derive_identity_pack(self.bible)
        (self.project / "references/characters/ren.png").unlink()

        issues = validate_identity_pack(pack, project_dir=self.project)

        self.assertEqual(
            (
                "character-identity-pack character 'ren' reference view file is "
                "missing: references/characters/ren.png",
            ),
            issues,
        )

    def test_edited_immutable_trait_must_match_the_bible_verbatim(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["immutable_traits"]["hair"] = "shaved head"

        issues = validate_identity_pack(pack, character_bible=self.bible)

        self.assertEqual(
            (
                "character-identity-pack character 'mira' immutable_traits.hair "
                "must match the bible verbatim",
            ),
            issues,
        )

    def test_edited_wardrobe_and_avoid_must_match_the_bible_verbatim(self):
        pack = derive_identity_pack(self.bible)
        mira = pack["characters"][0]
        mira["wardrobe"]["base"] = "black tactical armour"
        mira["wardrobe"]["accessories"] = ["knife"]
        mira["wardrobe"]["palette"] = ["neon pink"]
        mira["avoid"] = []

        issues = validate_identity_pack(pack, character_bible=self.bible)

        self.assertEqual(
            (
                "character-identity-pack character 'mira' avoid must match the bible verbatim",
                "character-identity-pack character 'mira' wardrobe.accessories must "
                "match the bible verbatim",
                "character-identity-pack character 'mira' wardrobe.base must match the "
                "bible verbatim",
                "character-identity-pack character 'mira' wardrobe.palette must match "
                "the bible verbatim",
            ),
            issues,
        )

    def test_every_derived_field_is_covered_by_the_bible_comparison(self):
        """A derived field must not be addable without cross-artifact enforcement."""
        derived = derive_identity_pack(self.bible)["characters"][0]
        enforced = {path.split(".")[0] for path, _ in DERIVED_FINGERPRINT_FIELDS}

        self.assertEqual(
            {"avoid", "immutable_traits", "wardrobe"},
            enforced,
        )
        # Everything else in an entry is either authored or the digest itself.
        self.assertEqual(
            {"id", "proportions", "reference_views", "source_fingerprint_sha256"},
            set(derived) - enforced,
        )

    def test_authored_proportions_are_not_forced_to_match_the_bible(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["proportions"]["build"] = "seven heads tall, long limbed"
        pack["characters"][0]["proportions"]["notes"] = ["hands slightly oversized"]

        self.assertEqual((), validate_identity_pack(pack, character_bible=self.bible))

    def test_authored_extra_reference_view_is_not_forced_to_match_the_bible(self):
        pack = derive_identity_pack(self.bible)
        pack["characters"][0]["reference_views"].append(
            {"path": "references/characters/mira-profile.png", "view": "profile"}
        )

        self.assertEqual((), validate_identity_pack(pack, character_bible=self.bible))

    def test_pack_built_against_an_older_fingerprint_is_reported_as_stale(self):
        pack = derive_identity_pack(self.bible)
        bible = character_bible()
        bible["characters"][0]["visual_fingerprint"]["palette"] = ["ash", "rust"]

        issues = validate_identity_pack(pack, character_bible=bible)

        # The digest reports that the bible moved; the field comparison names what moved.
        self.assertEqual(
            (
                "character-identity-pack character 'mira' is stale: re-derive it "
                "after the character bible fingerprint changed",
                "character-identity-pack character 'mira' wardrobe.palette must match "
                "the bible verbatim",
            ),
            issues,
        )

    def test_bible_character_missing_from_the_pack_is_reported(self):
        pack = derive_identity_pack(self.bible)
        del pack["characters"][1]

        issues = validate_identity_pack(pack, character_bible=self.bible)

        self.assertIn("character-identity-pack is missing bible character 'ren'", issues)


class PromptContextTests(IdentityPackHarness):
    def test_prompt_block_is_deterministic_plain_text(self):
        pack = derive_identity_pack(self.bible)

        first = identity_prompt_block(pack, ["mira"])
        second = identity_prompt_block(pack, ["mira"])

        self.assertEqual(first, second)
        self.assertEqual(
            "\n".join(
                [
                    "IDENTITY LOCK (character-identity-pack 1.0)",
                    "- mira",
                    "  immutable: short compact build; round face with wide dark eyes; "
                    "chin-length black bob",
                    "  invariants: amber scarf; circular brass bag clasp",
                    "  proportions: short compact build",
                    "  wardrobe: cream courier jacket and dark trousers",
                    "  accessories: rectangular courier case",
                    "  palette: charcoal; cream; amber",
                    "  avoid: logos; generated text",
                    "  reference views: canonical=references/characters/mira.png",
                ]
            ),
            first,
        )

    def test_prompt_block_order_follows_the_pack_not_the_panel(self):
        pack = derive_identity_pack(self.bible)

        block = identity_prompt_block(pack, ["ren", "mira"])

        self.assertLess(block.index("- mira"), block.index("- ren"))

    def test_unknown_character_fails_closed(self):
        pack = derive_identity_pack(self.bible)

        with self.assertRaisesRegex(IdentityPackError, "no entry for: ghost"):
            identity_prompt_block(pack, ["ghost"])

    def test_reference_paths_are_unique_and_ordered(self):
        pack = derive_identity_pack(self.bible)

        self.assertEqual(
            ("references/characters/mira.png", "references/characters/ren.png"),
            identity_reference_paths(pack),
        )

    def test_panel_context_binds_storyboard_characters_to_the_pack(self):
        pack = derive_identity_pack(self.bible)

        context = panel_identity_context(pack, storyboard(), "p01-01")

        self.assertEqual("p01-01", context.panel_id)
        self.assertEqual(("ren", "mira"), context.character_ids)
        self.assertEqual(
            ("references/characters/mira.png", "references/characters/ren.png"),
            context.reference_paths,
        )
        self.assertIn("- mira", context.prompt_block)
        self.assertIn("- ren", context.prompt_block)

    def test_panel_context_for_a_single_character_panel_excludes_others(self):
        pack = derive_identity_pack(self.bible)

        context = panel_identity_context(pack, storyboard(), "p01-02")

        self.assertEqual(("mira",), context.character_ids)
        self.assertNotIn("- ren", context.prompt_block)

    def test_unknown_panel_fails_closed(self):
        pack = derive_identity_pack(self.bible)

        with self.assertRaisesRegex(IdentityPackError, "no panel 'p09-09'"):
            panel_identity_context(pack, storyboard(), "p09-09")

    def test_prompt_block_never_names_a_provider_or_credential(self):
        pack = derive_identity_pack(self.bible)

        block = identity_prompt_block(pack).casefold()

        for forbidden in ("api", "key", "token", "endpoint", "http", "model="):
            self.assertNotIn(forbidden, block)


class PersistenceTests(IdentityPackHarness):
    def test_derive_publishes_a_canonical_artifact(self):
        path, issues = derive_and_write_identity_pack(self.project)

        self.assertEqual((), issues)
        self.assertEqual(self.project / IDENTITY_PACK_PATH, path)
        payload = path.read_bytes()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload, payload.decode("utf-8").encode("utf-8"))
        self.assertEqual(derive_identity_pack(self.bible), json.loads(payload))

    def test_resume_rewrites_byte_identical_content(self):
        path, _ = derive_and_write_identity_pack(self.project)
        first = path.read_bytes()

        derive_and_write_identity_pack(self.project)

        self.assertEqual(first, path.read_bytes())
        self.assertEqual((), check_identity_pack(self.project))

    def test_authored_views_and_notes_survive_re_derivation(self):
        derive_and_write_identity_pack(self.project)
        pack = json.loads((self.project / IDENTITY_PACK_PATH).read_text(encoding="utf-8"))
        pack["characters"][0]["reference_views"].append(
            {"path": "references/characters/mira-profile.png", "view": "profile"}
        )
        pack["characters"][0]["proportions"]["notes"] = ["seven heads tall"]
        (self.project / IDENTITY_PACK_PATH).write_text(
            json.dumps(pack, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.reference("mira-profile")

        _, issues = derive_and_write_identity_pack(self.project)

        self.assertEqual((), issues)
        stored = read_identity_pack(self.project)
        mira = stored["characters"][0]
        self.assertEqual(
            [
                {"path": "references/characters/mira.png", "view": "canonical"},
                {"path": "references/characters/mira-profile.png", "view": "profile"},
            ],
            mira["reference_views"],
        )
        self.assertEqual(["seven heads tall"], mira["proportions"]["notes"])

    def test_re_derivation_repairs_drift_from_the_bible(self):
        derive_and_write_identity_pack(self.project)
        bible = character_bible()
        bible["characters"][0]["visual_fingerprint"]["hair"] = "cropped silver hair"
        self.write_bible(bible)

        self.assertTrue(check_identity_pack(self.project))

        _, issues = derive_and_write_identity_pack(self.project)

        self.assertEqual((), issues)
        self.assertEqual((), check_identity_pack(self.project))
        self.assertEqual(
            "cropped silver hair",
            read_identity_pack(self.project)["characters"][0]["immutable_traits"]["hair"],
        )

    def test_tampered_persisted_wardrobe_is_rejected_by_check(self):
        derive_and_write_identity_pack(self.project)
        pack = json.loads((self.project / IDENTITY_PACK_PATH).read_text(encoding="utf-8"))
        pack["characters"][0]["wardrobe"]["base"] = "black tactical armour"
        (self.project / IDENTITY_PACK_PATH).write_text(
            json.dumps(pack, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        self.assertEqual(
            (
                "character-identity-pack character 'mira' wardrobe.base must match the "
                "bible verbatim",
            ),
            check_identity_pack(self.project),
        )
        self.assertEqual(1, main([str(self.project), "--panel", "p01-01"]))

    def test_corrupt_pack_fails_closed_instead_of_discarding_authored_content(self):
        derive_and_write_identity_pack(self.project)
        (self.project / IDENTITY_PACK_PATH).write_text("{ not json", encoding="utf-8")

        with self.assertRaisesRegex(IdentityPackError, "is not valid JSON"):
            derive_and_write_identity_pack(self.project)

        self.assertEqual(1, main([str(self.project), "--derive"]))

    def test_check_reports_a_missing_pack(self):
        self.assertEqual(
            (f"{IDENTITY_PACK_PATH} is missing; derive it before generation",),
            check_identity_pack(self.project),
        )

    def test_derive_refuses_to_publish_an_invalid_pack(self):
        (self.project / "references/characters/mira.png").unlink()

        _, issues = derive_and_write_identity_pack(self.project)

        self.assertTrue(issues)
        self.assertFalse((self.project / IDENTITY_PACK_PATH).exists())


class CommandLineTests(IdentityPackHarness):
    def test_derive_check_and_panel_exit_zero_on_a_valid_project(self):
        self.assertEqual(0, main([str(self.project), "--derive"]))
        self.assertEqual(0, main([str(self.project), "--check"]))
        self.assertEqual(0, main([str(self.project), "--panel", "p01-01"]))

    def test_check_fails_before_the_pack_exists(self):
        self.assertEqual(1, main([str(self.project), "--check"]))

    def test_panel_fails_closed_when_a_reference_is_missing(self):
        self.assertEqual(0, main([str(self.project), "--derive"]))
        (self.project / "references/characters/mira.png").unlink()

        self.assertEqual(1, main([str(self.project), "--panel", "p01-01"]))


if __name__ == "__main__":
    unittest.main()
