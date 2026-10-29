import tempfile
import unittest
from pathlib import Path

from PIL import Image

from comic_sol_product import engine_api


STUDIO_ENGINE_API_NAMES = {
    "ARCHIVE_SUFFIX",
    "HandoffResultError",
    "MAX_ENCODED_RASTER_BYTES",
    "MAX_SOURCE_BYTES",
    "MAX_WARNING_CHARS",
    "NormalizationSpec",
    "PANEL_CHECK_IDS",
    "PANEL_ID_PATTERN",
    "PreparedNormalization",
    "ProjectLock",
    "ProjectTransaction",
    "REFERENCE_PLAN_PATH",
    "SUBJECTIVE_PAGE_CHECK_IDS",
    "WARNING_LIMIT_MESSAGE",
    "accept_handoff_result",
    "accepted_panel_problem",
    "attempt_id",
    "build_character_identity_check",
    "build_resume_plan",
    "canonical_artifact_bytes",
    "character_consistency_context",
    "cleanup_owned_directory",
    "compose_project",
    "contained_project_path",
    "export_handoff_archive",
    "finalize_project",
    "generation_job_sha256",
    "guarded_export",
    "import_handoff_archive",
    "init_project",
    "inspect_handoff",
    "invalidate_from",
    "invalidate_from_locked",
    "is_excluded_project_path",
    "is_generated_sfx",
    "iter_project_entries",
    "letter_project",
    "loads_bounded_json",
    "plan_and_write_repair_plan",
    "prepare_handoff",
    "prepare_normalization",
    "promote_attempt",
    "publish_page_quality_record",
    "read_contained_bytes",
    "read_contained_json",
    "read_project_manifest",
    "record_stage",
    "references_ready",
    "storyboard_panels",
    "transition",
    "utc_now",
    "validate_character_bible",
    "validate_character_identity_check",
    "validate_character_quality_provenance",
    "validate_defect_regions",
    "validate_generation_receipt",
    "validate_handoff_raster",
    "validate_identity_pack",
    "validate_narrative",
    "validate_page_quality",
    "validate_panel_provenance",
    "validate_panel_record",
    "validate_project",
    "validate_quality_checks",
    "validate_story_plan",
    "validate_storyboard",
}


class StudioEngineAPIContractTests(unittest.TestCase):
    def test_public_names_match_the_studio_consumer_contract(self):
        self.assertEqual(STUDIO_ENGINE_API_NAMES, set(engine_api.__all__))
        self.assertNotIn("_load_engine_module", engine_api.__all__)
        for name in STUDIO_ENGINE_API_NAMES:
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(engine_api, name))

    def test_prepare_normalization_returns_stable_bytes_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            source = project / "source.png"
            Image.new("RGB", (8, 6), color=(32, 64, 96)).save(source, format="PNG")
            spec = engine_api.NormalizationSpec("p01-01", "source.png", (8, 6), "exact")

            prepared = engine_api.prepare_normalization(project, spec)

            self.assertIsInstance(prepared, engine_api.PreparedNormalization)
            self.assertEqual(spec, prepared.spec)
            self.assertTrue(prepared.clean_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertTrue(prepared.record_bytes)
            self.assertFalse((project / "panels").exists())


if __name__ == "__main__":
    unittest.main()
