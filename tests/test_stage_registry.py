import unittest

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
        self.assertEqual(
            set(RESUME_STAGES),
            set(STAGE_INVALIDATION_STATUS),
        )
        self.assertEqual(
            set(RESUME_STAGES),
            set(STAGE_COMPLETION_STATUS),
        )
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
            ARTIFACT_STAGE,
            {
                artifact: definition.name
                for definition in STAGE_REGISTRY
                for artifact in definition.artifacts
            },
        )
        self.assertEqual(
            len(ARTIFACT_STAGE),
            len({artifact for definition in STAGE_REGISTRY for artifact in definition.artifacts}),
        )


if __name__ == "__main__":
    unittest.main()
