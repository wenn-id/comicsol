import shlex
import unittest
from pathlib import Path

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
            len({
                artifact
                for definition in STAGE_REGISTRY
                for artifact in definition.artifacts
            }),
        )

    def test_composition_resume_command_is_executable_and_shell_safe(self):
        project_dir = Path("/tmp/comic sol/project")
        action = _next_resume_action(project_dir, "composition")
        self.assertIsInstance(action, dict)
        command = shlex.split(action["command"])
        self.assertIn("--all", command)
        self.assertEqual(str(project_dir), command[-1])
        self.assertEqual("scripts/compose_pages.py", command[-3].split("/")[-2] + "/" + command[-3].split("/")[-1])


if __name__ == "__main__":
    unittest.main()
