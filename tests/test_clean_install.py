import unittest
from pathlib import Path

from comic_sol_product.release import (
    FORBIDDEN_WHEEL_MEMBERS,
    REQUIRED_SDIST_SUFFIXES,
    REQUIRED_WHEEL_MEMBERS,
    validate_sdist_members,
    validate_wheel_members,
)


class DistributionContractTests(unittest.TestCase):
    def test_required_wheel_members_cover_runtime_and_skill(self):
        self.assertIn("comic_sol_product/engine/comic_sol.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/quality_records.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/normalize_panels.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/typography.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/layouts.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/page_quality.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/engine/pdf_quality.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/assets/fonts/ComicNeue-Regular.ttf", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/templates/manifest.json", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/skill/SKILL.md", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("comic_sol_product/skill/references/workflow.md", REQUIRED_WHEEL_MEMBERS)

    def test_distribution_validation_reports_every_missing_member(self):
        present = REQUIRED_WHEEL_MEMBERS - {
            "comic_sol_product/skill/SKILL.md",
            "comic_sol_product/skill/references/workflow.md",
        }
        with self.assertRaisesRegex(ValueError, "SKILL.md.*workflow.md"):
            validate_wheel_members(present)

    def test_distribution_validation_accepts_complete_archive(self):
        validate_wheel_members(REQUIRED_WHEEL_MEMBERS | {"extra.txt"})

    def test_distribution_rejects_build_only_scripts(self):
        with self.assertRaisesRegex(ValueError, "build-only"):
            validate_wheel_members(REQUIRED_WHEEL_MEMBERS | FORBIDDEN_WHEEL_MEMBERS)

    def test_sdist_contract_covers_source_skill_and_runtime_assets(self):
        members = {f"comic-sol-2.0.0.dev0{suffix}" for suffix in REQUIRED_SDIST_SUFFIXES}
        validate_sdist_members(members)
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            validate_sdist_members({name for name in members if not name.endswith("/SKILL.md")})


if __name__ == "__main__":
    unittest.main()
