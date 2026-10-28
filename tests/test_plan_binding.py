import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.comic_sol import (
    atomic_write_json,
    bind_plan_artifacts,
    init_project,
    main,
    read_json,
    record_stage,
    transition,
)
from scripts.project_io import sha256_file
from scripts.schema import UnsupportedSchemaVersionError
from scripts.validate_project import validate_project

ROOT = Path(__file__).resolve().parents[1]
PLAN_FIXTURE = ROOT / "tests/fixtures/valid-one-page"
PLAN_FILES = (
    "plan/story-plan.json",
    "plan/character-bible.json",
    "plan/storyboard.json",
)


class PlanBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def blank_project(self) -> Path:
        return init_project(
            self.root / "projects",
            "Blank Binding",
            b"A courier carries a small sun through the night city.",
            {"language": "en", "mode": "short_prompt"},
            page_count=1,
        )

    def copy_plan_files(self, project: Path, relatives=PLAN_FILES) -> None:
        for relative in relatives:
            shutil.copyfile(PLAN_FIXTURE / relative, project / relative)

    def test_blank_workflow_binds_descriptors_and_passes_storyboard_validation(self):
        project = self.blank_project()
        self.copy_plan_files(project, PLAN_FILES[:2])

        self.assertEqual([], validate_project(project, "plan"))
        record_stage(project, "planning")
        transition(project, "PLANNED")
        self.copy_plan_files(project, PLAN_FILES[2:])
        transition(project, "SCRIPTED")

        result = bind_plan_artifacts(project)
        manifest = read_json(project / "project.json")

        self.assertTrue(result["changed"])
        self.assertEqual(1, result["page_count"])
        self.assertEqual(3, result["panel_count"])
        self.assertEqual(1, manifest["settings"]["page_count"])
        self.assertEqual(3, manifest["settings"]["panel_count"])
        self.assertEqual(["p01-01", "p01-02", "p01-03"], manifest["panels"])
        self.assertEqual(
            {"story_plan", "character_bible", "storyboard"}, set(manifest["artifacts"])
        )
        for name, relative in zip(
            ("story_plan", "character_bible", "storyboard"), PLAN_FILES, strict=True
        ):
            self.assertEqual(relative, manifest["artifacts"][name]["path"])
            self.assertEqual(sha256_file(project / relative), manifest["artifacts"][name]["sha256"])
        self.assertEqual([], validate_project(project, "storyboard"))

        record_stage(project, "storyboard")
        transition(project, "STORYBOARDED")
        manifest_bytes = (project / "project.json").read_bytes()
        events_bytes = (project / "logs/events.jsonl").read_bytes()
        unchanged = bind_plan_artifacts(project)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(manifest_bytes, (project / "project.json").read_bytes())
        self.assertEqual(events_bytes, (project / "logs/events.jsonl").read_bytes())

    def test_missing_or_invalid_plan_files_leave_manifest_and_events_unchanged(self):
        project = self.blank_project()
        self.copy_plan_files(project, PLAN_FILES[:2])
        manifest_before = (project / "project.json").read_bytes()
        events_before = (project / "logs/events.jsonl").read_bytes()

        with self.assertRaises(FileNotFoundError):
            bind_plan_artifacts(project)
        self.assertEqual(manifest_before, (project / "project.json").read_bytes())
        self.assertEqual(events_before, (project / "logs/events.jsonl").read_bytes())

        self.copy_plan_files(project, PLAN_FILES[2:])
        atomic_write_json(project / "plan/storyboard.json", {"schema_version": "9.0"})
        with self.assertRaisesRegex(ValueError, "plan/storyboard.json"):
            bind_plan_artifacts(project)
        self.assertEqual(manifest_before, (project / "project.json").read_bytes())
        self.assertEqual(events_before, (project / "logs/events.jsonl").read_bytes())

    def test_scene_character_must_exist_in_the_character_bible(self):
        project = self.blank_project()
        self.copy_plan_files(project)
        story = read_json(project / "plan/story-plan.json")
        story["scenes"][0]["characters"] = ["unknown-character"]
        atomic_write_json(project / "plan/story-plan.json", story)
        manifest_before = (project / "project.json").read_bytes()
        events_before = (project / "logs/events.jsonl").read_bytes()

        with self.assertRaisesRegex(
            ValueError,
            r"scenes\[0\]\.characters\[0\].*must reference the character bible",
        ):
            bind_plan_artifacts(project)

        self.assertEqual(manifest_before, (project / "project.json").read_bytes())
        self.assertEqual(events_before, (project / "logs/events.jsonl").read_bytes())

    def test_unsupported_manifest_schema_rejects_binding_without_mutation(self):
        project = self.blank_project()
        self.copy_plan_files(project)
        manifest = read_json(project / "project.json")
        manifest["schema_version"] = "9.0"
        atomic_write_json(project / "project.json", manifest)
        files_before = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in project.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(UnsupportedSchemaVersionError):
            bind_plan_artifacts(project)

        files_after = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in project.rglob("*")
            if path.is_file()
        }
        self.assertEqual(files_before, files_after)

    def test_source_cli_exposes_bind_plan(self):
        project = self.blank_project()
        self.copy_plan_files(project)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["bind-plan", str(project)])

        self.assertEqual(0, code)
        self.assertIn("plan bindings updated", stdout.getvalue())
        self.assertEqual([], validate_project(project, "storyboard"))


if __name__ == "__main__":
    unittest.main()
