import shlex
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import scripts.comic_sol as comic_sol
from scripts.comic_sol import _next_resume_action
from scripts.stage_registry import (
    ARTIFACT_STAGE,
    RESUME_STAGES,
    STAGE_COMPLETION_STATUS,
    STAGE_INVALIDATION_STATUS,
    STAGE_REGISTRY,
)


class StageRegistryTests(unittest.TestCase):
    def test_registry_is_the_complete_ordered_stage_authority(self):
        self.assertEqual(
            RESUME_STAGES,
            tuple(definition.name for definition in STAGE_REGISTRY),
        )
        self.assertEqual(set(RESUME_STAGES), set(STAGE_INVALIDATION_STATUS))
        self.assertEqual(set(RESUME_STAGES), set(STAGE_COMPLETION_STATUS))
        for definition in STAGE_REGISTRY:
            self.assertTrue(definition.name)
            self.assertTrue(definition.invalidation_status)
            self.assertTrue(definition.completion_status)
            self.assertIn(definition.next_action, {"agent", "command"})
            self.assertTrue(definition.material_kind)
            self.assertTrue(definition.output_kind)
            self.assertIn(definition.stale_action, {"regenerate", "rerun"})

    def test_artifacts_have_one_registered_owner(self):
        self.assertEqual(
            {
                "story_plan": "planning",
                "character_bible": "planning",
                "storyboard": "storyboard",
                "composition_cache": "composition",
                "qa_report": "export",
                "pdf": "export",
                "pdf_verification": "export",
            },
            ARTIFACT_STAGE,
        )
        self.assertEqual(
            len(ARTIFACT_STAGE),
            len({artifact for definition in STAGE_REGISTRY for artifact in definition.artifacts}),
        )

    def test_resume_commands_use_installed_engine_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine = Path(temporary_directory) / "Comic Sol" / "comic_sol_product" / "engine"
            engine.mkdir(parents=True)
            project_dir = Path(temporary_directory) / "project"

            runners = {
                "lettering": "letter_panels.py",
                "composition": "compose_pages.py",
                "export": "export_pdf.py",
            }
            for stage, runner_name in runners.items():
                with self.subTest(stage=stage):
                    engine_script = engine / runner_name
                    engine_script.write_text("# installed engine\n", encoding="utf-8")
                    with patch.object(comic_sol, "__file__", str(engine / "comic_sol.py")):
                        action = _next_resume_action(project_dir, stage)

                    command = cast(dict[str, str], action)["command"]
                    self.assertIn(shlex.quote(str(engine_script)), command)
                    self.assertIn(shlex.quote(str(project_dir)), command)
                    if stage == "composition":
                        self.assertIn("--all", command)
                    self.assertNotIn(str(comic_sol.ROOT / "scripts" / runner_name), command)


if __name__ == "__main__":
    unittest.main()
