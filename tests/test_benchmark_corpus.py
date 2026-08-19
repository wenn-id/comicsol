"""Benchmark corpus coverage: every scenario is a valid, small, documented project."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.comic_sol import read_json
from scripts.validate_project import (
    LAYOUTS,
    ProjectValidationError,
    require_valid_project,
)
from tests.benchmark_corpus import (
    BENCHMARK_SCENARIOS,
    benchmark_metadata,
    build_benchmark_project,
    build_storyboard,
)


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION = ROOT / "docs/benchmark-corpus.md"

# One benchmark project per capability the corpus has to exercise.
REQUIRED_SCENARIOS = {
    "dialogue-heavy",
    "action-sequence",
    "two-character",
    "multi-character",
    "silent-manga",
    "night-low-light",
    "long-dialogue",
    "complex-background",
    "four-page-story",
}
# Fixtures stay text-only: a raster would immediately break these budgets.
MAX_PROJECT_FILE_BYTES = 32 * 1024
MAX_CORPUS_BYTES = 512 * 1024

# The night scenario claims a single dark key source. Prose cannot prove a render,
# but it can prove that exactly one key source is named and that no competing
# daylight source was authored alongside it.
NIGHT_KEY_SOURCES = ("lamp", "lantern")
NIGHT_COMPETING_SOURCES = (
    "sun",
    "daylight",
    "fluorescent",
    "floodlight",
    "bulb",
    "headlight",
    "dawn",
    "window",
)
FOUR_PAGE_LAYOUTS = [
    "full-page",
    "two-horizontal",
    "three-horizontal",
    "two-top-hero-bottom",
]


def _panels(scenario):
    """Return every resolved panel of one benchmark scenario in reading order."""
    storyboard = build_storyboard(BENCHMARK_SCENARIOS[scenario]["pages"])
    return [panel for page in storyboard["pages"] for panel in page["panels"]]


def _text_items(scenario, kind=None):
    """Return the scenario's text items in reading order, optionally filtered by kind."""
    return [
        item
        for panel in _panels(scenario)
        for item in panel["text"]
        if kind is None or item["kind"] == kind
    ]


def _panel_speakers(panel):
    """Return the dialogue speakers of one panel in authored order."""
    return [item["speaker"] for item in panel["text"] if item["kind"] == "dialogue"]


class BenchmarkCorpusCoverageTests(unittest.TestCase):
    def test_corpus_covers_every_required_capability(self):
        self.assertEqual(REQUIRED_SCENARIOS, set(BENCHMARK_SCENARIOS))

    def test_metadata_states_the_stressed_capability(self):
        for scenario in sorted(BENCHMARK_SCENARIOS):
            with self.subTest(scenario=scenario):
                metadata = benchmark_metadata(scenario)
                self.assertEqual(scenario, metadata["scenario"])
                self.assertTrue(metadata["local_only"])
                self.assertEqual("structural", metadata["evidence_mode"])
                self.assertGreaterEqual(len(metadata["capability"].split()), 8)
                self.assertTrue(metadata["capability"].endswith("."))
                self.assertGreaterEqual(len(metadata["stresses"]), 1)
                for tag in metadata["stresses"]:
                    self.assertIn(":", tag, tag)
                panels = _panels(scenario)
                self.assertEqual(
                    [panel["id"] for panel in panels], metadata["expected"]["panels"]
                )
                self.assertEqual(len(panels), metadata["expected"]["panel_count"])

    def test_corpus_exercises_every_registered_layout(self):
        used = {
            page["layout"]
            for specification in BENCHMARK_SCENARIOS.values()
            for page in build_storyboard(specification["pages"])["pages"]
        }
        self.assertEqual(LAYOUTS, used)

    def test_documentation_describes_every_scenario_and_capability_tag(self):
        documentation = DOCUMENTATION.read_text(encoding="utf-8")
        for scenario in sorted(BENCHMARK_SCENARIOS):
            with self.subTest(scenario=scenario):
                self.assertIn(f"`{scenario}`", documentation)
                for tag in benchmark_metadata(scenario)["stresses"]:
                    self.assertIn(tag, documentation)


class BenchmarkScenarioShapeTests(unittest.TestCase):
    def test_dialogue_heavy_packs_three_text_items_into_every_panel(self):
        panels = _panels("dialogue-heavy")
        self.assertEqual(2, len(panels))
        for panel in panels:
            self.assertEqual(3, len(panel["text"]), panel["id"])
        self.assertEqual(5, len(_text_items("dialogue-heavy", "dialogue")))
        self.assertEqual(1, len(_text_items("dialogue-heavy", "caption")))
        # The capability is alternating speakers, so reading order is the claim.
        # A set comparison would also accept one character carrying every line.
        self.assertEqual(
            ["nadia", "bram", "nadia", "bram", "nadia"],
            [item["speaker"] for item in _text_items("dialogue-heavy", "dialogue")],
        )

    def test_action_scenario_is_sfx_led_and_dialogue_light(self):
        self.assertEqual(3, len(_panels("action-sequence")))
        self.assertGreaterEqual(len(_text_items("action-sequence", "sfx")), 3)
        self.assertLessEqual(len(_text_items("action-sequence", "dialogue")), 1)

    def test_two_character_scenario_gives_both_speakers_every_panel(self):
        specification = BENCHMARK_SCENARIOS["two-character"]
        self.assertEqual(2, len(specification["characters"]["characters"]))
        for panel in _panels("two-character"):
            speakers = _panel_speakers(panel)
            with self.subTest(panel=panel["id"]):
                self.assertEqual(2, len(panel["characters"]))
                # Two dialogue items whose speakers are the two characters means
                # the pair trades lines inside every panel, not across the scenario.
                self.assertEqual(2, len(speakers))
                self.assertEqual({"mei", "ari"}, set(speakers), speakers)

    def test_multi_character_scenario_frames_four_characters_at_once(self):
        specification = BENCHMARK_SCENARIOS["multi-character"]
        self.assertEqual(4, len(specification["characters"]["characters"]))
        panels = _panels("multi-character")
        self.assertEqual(1, len(panels))
        self.assertEqual(4, len(panels[0]["characters"]))

    def test_silent_manga_scenario_authors_no_text_at_all(self):
        panels = _panels("silent-manga")
        self.assertEqual(4, len(panels))
        self.assertEqual([], _text_items("silent-manga"))
        for panel in panels:
            self.assertIn("sound effects", panel["negative"])

    def test_night_scenario_names_exactly_one_dark_key_source(self):
        specification = BENCHMARK_SCENARIOS["night-low-light"]
        for scene in specification["story"]["scenes"]:
            self.assertEqual("night", scene["time"])
        for character in specification["characters"]["characters"]:
            palette = character["visual_fingerprint"]["palette"]
            with self.subTest(character=character["id"]):
                self.assertTrue(
                    any("black" in tone or "dim" in tone for tone in palette), palette
                )
        for panel in _panels("night-low-light"):
            lighting = panel["lighting"].lower()
            named = [source for source in NIGHT_KEY_SOURCES if source in lighting]
            with self.subTest(panel=panel["id"]):
                self.assertEqual(1, len(named), panel["lighting"])
                for competing in NIGHT_COMPETING_SOURCES:
                    self.assertNotIn(competing, lighting, panel["lighting"])

    def test_long_dialogue_scenario_approaches_the_word_ceiling(self):
        dialogue = _text_items("long-dialogue", "dialogue")
        self.assertEqual(1, len(dialogue))
        words = len(dialogue[0]["content"].split())
        self.assertGreaterEqual(words, 28)
        self.assertLessEqual(words, 32)
        for panel in _panels("long-dialogue"):
            total = sum(len(item["content"].split()) for item in panel["text"])
            self.assertLessEqual(total, 45)

    def test_complex_background_scenario_reserves_text_safe_space(self):
        panels = _panels("complex-background")
        self.assertEqual(3, len(panels))
        for panel in panels:
            self.assertIn("text", panel["composition"].lower())

    def test_four_page_scenario_numbers_four_pages_with_four_layouts(self):
        storyboard = build_storyboard(BENCHMARK_SCENARIOS["four-page-story"]["pages"])
        pages = storyboard["pages"]
        layouts = [page["layout"] for page in pages]
        self.assertEqual([1, 2, 3, 4], [page["number"] for page in pages])
        # The capability promises four different layouts, so assert the exact
        # sequence: a duplicated layout would otherwise pass unnoticed.
        self.assertEqual(FOUR_PAGE_LAYOUTS, layouts)
        self.assertEqual(4, len(set(layouts)), layouts)
        self.assertEqual(9, len(_panels("four-page-story")))


class BenchmarkProjectValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary_directory.name)
        cls.projects = {
            scenario: build_benchmark_project(cls.root, scenario)
            for scenario in sorted(BENCHMARK_SCENARIOS)
        }

    @classmethod
    def tearDownClass(cls):
        cls._temporary_directory.cleanup()

    def test_every_benchmark_project_validates_under_the_current_schema(self):
        for scenario, project in self.projects.items():
            with self.subTest(scenario=scenario):
                try:
                    require_valid_project(project, "storyboard")
                except ProjectValidationError as error:
                    self.fail(
                        f"{scenario} failed storyboard validation:\n"
                        + "\n".join(
                            f"{issue.path}:{issue.field}: {issue.message}"
                            for issue in error.issues
                        )
                    )

    def test_every_benchmark_project_is_plan_complete_and_self_consistent(self):
        for scenario, project in self.projects.items():
            with self.subTest(scenario=scenario):
                manifest = read_json(project / "project.json")
                metadata = benchmark_metadata(scenario)
                self.assertEqual("STORYBOARDED", manifest["status"])
                self.assertEqual(scenario, manifest["project_id"])
                self.assertEqual(metadata["title"], manifest["title"])
                self.assertEqual(metadata["expected"]["panels"], manifest["panels"])
                self.assertEqual(
                    metadata["expected"]["page_count"], manifest["settings"]["page_count"]
                )
                self.assertEqual(
                    metadata["expected"]["panel_count"], manifest["settings"]["panel_count"]
                )
                for name in ("character_bible", "story_plan", "storyboard"):
                    self.assertIn(name, manifest["artifacts"])

    def test_sidecar_metadata_accompanies_every_benchmark_project(self):
        for scenario in self.projects:
            with self.subTest(scenario=scenario):
                sidecar = self.root / f"{scenario}.benchmark.json"
                self.assertTrue(sidecar.is_file(), sidecar)
                recorded = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(benchmark_metadata(scenario), recorded)

    def test_corpus_stays_small_enough_for_practical_ci_evaluation(self):
        total = 0
        for scenario, project in self.projects.items():
            for path in project.rglob("*"):
                if not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                with self.subTest(scenario=scenario, artifact=path.name):
                    self.assertLessEqual(size, MAX_PROJECT_FILE_BYTES, path)
        self.assertLessEqual(total, MAX_CORPUS_BYTES)
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
