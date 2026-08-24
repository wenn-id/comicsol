import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import comic_sol
from scripts.comic_sol import init_project, read_json, sha256_file
from scripts.starter_templates import (
    CATALOG,
    STARTER_FILES,
    STARTER_IDS,
    STARTER_VERSION,
    inventory_starters,
    load_starter,
)
from scripts.validate_project import validate_project


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


class StarterTemplateTests(unittest.TestCase):
    def test_catalog_is_fixed_versioned_and_path_safe(self):
        self.assertEqual(
            ("minimal-one-page", "dialogue-two-page", "action-focused"),
            STARTER_IDS,
        )
        self.assertEqual(set(STARTER_IDS), set(CATALOG))
        for starter_id, (version, relative) in CATALOG.items():
            self.assertEqual(STARTER_VERSION, version)
            self.assertEqual(f"starters/v1/{starter_id}", relative)
            self.assertEqual(
                set(STARTER_FILES),
                {
                    path.relative_to(TEMPLATES / relative).as_posix()
                    for path in (TEMPLATES / relative).rglob("*")
                    if path.is_file()
                },
            )
        for invalid in ("../minimal-one-page", "/tmp/action-focused", "v1", "unknown"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "starter"):
                load_starter(TEMPLATES, invalid)

    def test_default_loader_and_inventory_reject_malformed_request_settings(self):
        cases = (
            ([], ValueError, "JSON object"),
            ({"language": "en", "mode": "unsupported"}, ValueError, "request mode"),
        )
        for request, error_type, message in cases:
            with self.subTest(request=request), tempfile.TemporaryDirectory() as raw:
                templates = Path(raw) / "templates"
                shutil.copytree(TEMPLATES, templates)
                request_path = templates / "starters/v1/minimal-one-page/source/request.json"
                request_path.write_text(
                    json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(error_type, message):
                    load_starter(templates, "minimal-one-page")
                available, invalid = inventory_starters(templates)
                self.assertNotIn("minimal-one-page", available)
                self.assertTrue(
                    any(item.startswith("minimal-one-page (") for item in invalid),
                    invalid,
                )

    def test_every_starter_initializes_as_a_standard_storyboarded_project(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "projects"
            for starter_id in STARTER_IDS:
                with self.subTest(starter=starter_id):
                    bundle = load_starter(TEMPLATES, starter_id)
                    project = init_project(
                        output,
                        f"Starter {starter_id}",
                        starter=starter_id,
                    )
                    manifest = read_json(project / "project.json")

                    self.assertEqual("1.1", manifest["schema_version"])
                    self.assertEqual(
                        {
                            "contract_version": "1.0",
                            "locked_scope_sha256": None,
                            "manifest_path": None,
                        },
                        manifest["handoff"],
                    )
                    self.assertEqual("STORYBOARDED", manifest["status"])
                    self.assertEqual(bundle.page_count, manifest["settings"]["page_count"])
                    self.assertEqual(len(bundle.panel_ids), manifest["settings"]["panel_count"])
                    self.assertEqual(list(bundle.panel_ids), manifest["panels"])
                    self.assertEqual(
                        {"story_plan", "character_bible", "storyboard"},
                        set(manifest["artifacts"]),
                    )
                    for name, relative in (
                        ("story_plan", "plan/story-plan.json"),
                        ("character_bible", "plan/character-bible.json"),
                        ("storyboard", "plan/storyboard.json"),
                    ):
                        descriptor = manifest["artifacts"][name]
                        self.assertEqual(relative, descriptor["path"])
                        self.assertEqual(sha256_file(project / relative), descriptor["sha256"])

                    cache = read_json(project / "logs/stage-cache.json")
                    self.assertEqual({"planning", "storyboard"}, set(cache["stages"]))
                    self.assertEqual([], validate_project(project, "storyboard"))
                    self.assertEqual(bundle.source, (project / "source/input.txt").read_bytes())
                    self.assertEqual(bundle.request, read_json(project / "source/request.json"))

                    panel_issues = validate_project(project, "panels")
                    self.assertTrue(panel_issues)
                    self.assertTrue(
                        any(issue.path.startswith("qa/panels/") for issue in panel_issues),
                        panel_issues,
                    )
                    self.assertEqual([], list((project / "panels/raw").iterdir()))
                    self.assertEqual([], list((project / "panels/clean").iterdir()))
                    self.assertEqual([], list((project / "qa/panels").iterdir()))
                    self.assertFalse((project / "qa/report.md").exists())

    def test_starters_have_distinct_format_characteristics(self):
        minimal = load_starter(TEMPLATES, "minimal-one-page")
        dialogue = load_starter(TEMPLATES, "dialogue-two-page")
        action = load_starter(TEMPLATES, "action-focused")

        self.assertEqual((1, 1), (minimal.page_count, len(minimal.panel_ids)))
        self.assertEqual("full-page", minimal.storyboard["pages"][0]["layout"])

        dialogue_panels = [
            panel for page in dialogue.storyboard["pages"] for panel in page["panels"]
        ]
        dialogue_items = [
            item
            for panel in dialogue_panels
            for item in panel["text"]
            if item["kind"] == "dialogue"
        ]
        self.assertEqual((2, 4), (dialogue.page_count, len(dialogue.panel_ids)))
        self.assertGreaterEqual(len(dialogue_items), 4)
        self.assertEqual({"mara", "sol"}, {item["speaker"] for item in dialogue_items})
        self.assertTrue(all("speaker_anchor" in item for item in dialogue_items))

        action_panels = [panel for page in action.storyboard["pages"] for panel in page["panels"]]
        action_sfx = [
            item for panel in action_panels for item in panel["text"] if item["kind"] == "sfx"
        ]
        self.assertEqual((2, 6), (action.page_count, len(action.panel_ids)))
        self.assertGreaterEqual(len(action_sfx), 3)
        self.assertTrue(all(item["render_mode"] == "generated-visual" for item in action_sfx))

    def test_starter_conflicts_fail_before_output_allocation(self):
        cases = (
            {"source": b"explicit"},
            {"request": {"mode": "short_prompt", "language": "en"}},
            {"page_count": 1},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as raw:
                output = Path(raw) / "new-output"
                with self.assertRaisesRegex(ValueError, "cannot be combined"):
                    init_project(
                        output,
                        "Conflict",
                        starter="minimal-one-page",
                        **arguments,
                    )
                self.assertFalse(output.exists())

    def test_source_cli_initializes_a_starter(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "projects"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = comic_sol.main(
                    [
                        "init",
                        "--output-root",
                        str(output),
                        "--title",
                        "Source Starter",
                        "--starter",
                        "dialogue-two-page",
                    ]
                )
            project = Path(stdout.getvalue().strip())
            manifest = read_json(project / "project.json")
        self.assertEqual(0, code)
        self.assertEqual("STORYBOARDED", manifest["status"])
        self.assertEqual(2, manifest["settings"]["page_count"])

    def test_starter_plan_write_failure_leaves_no_partial_project(self):
        real_write = comic_sol.atomic_write_json

        def fail_on_story(path, value):
            real_write(path, value)
            if Path(path).name == "story-plan.json":
                raise OSError("injected starter write failure")

        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "projects"
            with mock.patch.object(comic_sol, "atomic_write_json", side_effect=fail_on_story):
                with self.assertRaisesRegex(OSError, "starter write failure"):
                    init_project(
                        output,
                        "Atomic Starter",
                        starter="minimal-one-page",
                    )
            self.assertFalse((output / "atomic-starter").exists())
            self.assertEqual([], list(output.glob(".comic-sol-init-*.tmp")))

    def test_bundle_json_is_canonical(self):
        for starter_id in STARTER_IDS:
            bundle = TEMPLATES / "starters" / STARTER_VERSION / starter_id
            for relative in STARTER_FILES:
                path = bundle / relative
                if path.suffix != ".json":
                    continue
                raw = path.read_bytes()
                value = json.loads(raw)
                expected = (
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                self.assertEqual(expected, raw, path)


if __name__ == "__main__":
    unittest.main()
