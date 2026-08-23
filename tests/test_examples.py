"""Official example projects build, validate, and stay documented."""

import tempfile
import unittest
from pathlib import Path

from scripts.build_examples import build_example, discover_examples
from scripts.comic_sol import read_json
from scripts.validate_project import require_valid_project


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
RASTER_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".pdf"})


class ExampleContractTests(unittest.TestCase):
    def test_every_deterministic_example_commits_only_editable_inputs(self):
        for example in discover_examples():
            with self.subTest(example=example.name):
                tracked = [
                    path.relative_to(example).as_posix()
                    for path in example.rglob("*")
                    if path.is_file() and path.suffix.lower() in RASTER_SUFFIXES
                ]
                self.assertEqual(
                    [],
                    tracked,
                    "deterministic examples must not commit rasters or exports",
                )

    def test_every_example_is_listed_in_the_samples_index(self):
        index = (SAMPLES / "README.md").read_text(encoding="utf-8")
        for example in discover_examples():
            with self.subTest(example=example.name):
                self.assertIn(f"({example.name})", index)
                self.assertTrue(
                    (example / "README.md").is_file(),
                    "each example needs its own README",
                )

    def test_readme_links_the_examples_from_the_usage_flow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("samples/README.md", readme)
        for example in discover_examples():
            with self.subTest(example=example.name):
                self.assertIn(f"samples/{example.name}", readme)

    def test_contract_declares_the_storyboard_scope(self):
        for example in discover_examples():
            with self.subTest(example=example.name):
                contract = read_json(example / "example.json")
                storyboard = read_json(example / "plan/storyboard.json")
                pages = storyboard["pages"]
                panels = [panel for page in pages for panel in page["panels"]]
                self.assertEqual("deterministic", contract["evidence_mode"])
                self.assertEqual(example.name, contract["example_id"])
                self.assertEqual(len(pages), contract["page_count"])
                self.assertEqual(len(panels), contract["panel_count"])
                self.assertEqual(
                    sorted({page["layout"] for page in pages}),
                    sorted(set(contract["layouts"])),
                )


class ExampleBuildTests(unittest.TestCase):
    """Build every committed example once, then assert on the results."""

    @classmethod
    def setUpClass(cls):
        cls._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(cls._temporary_directory.name)
        cls.built = {example.name: build_example(example, root) for example in discover_examples()}

    @classmethod
    def tearDownClass(cls):
        cls._temporary_directory.cleanup()

    def test_each_example_reaches_a_validated_terminal_state(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                manifest = read_json(project / "project.json")
                self.assertEqual("COMPLETE_WITH_WARNINGS", manifest["status"])
                self.assertNotEqual([], manifest["warnings"])
                require_valid_project(project, "final")

    def test_no_panel_check_claims_a_visual_review_that_did_not_happen(self):
        """A placeholder build must not publish passing subjective panel QA.

        Only `text-free` and `technical` are decidable from a synthetic raster;
        the builder measures both. Every other panel check is a judgement about
        artwork this build does not have, so it has to be recorded as an
        unreviewed warning rather than a pass.
        """
        for name, project in self.built.items():
            for record_path in sorted((project / "qa/panels").glob("*.json")):
                with self.subTest(example=name, panel=record_path.stem):
                    record = read_json(record_path)
                    self.assertEqual("accept-warning", record["decision"])
                    results = {
                        check["id"]: (check["result"], check["severity"])
                        for check in record["checks"]
                    }
                    for check_id in ("text-free", "technical"):
                        self.assertEqual(("pass", "error"), results[check_id])
                    unreviewed = [
                        check
                        for check in record["checks"]
                        if check["id"] not in {"text-free", "technical"}
                    ]
                    self.assertEqual(5, len(unreviewed))
                    for check in unreviewed:
                        self.assertEqual("warning", check["result"])
                        self.assertEqual("warning", check["severity"])
                        self.assertTrue(
                            check["evidence"].startswith("Not reviewed"),
                            f"{check['id']} must say what was not reviewed",
                        )
                        self.assertIn(check["evidence"], record["unresolved_warnings"])

    def test_no_page_check_claims_an_artwork_review_that_did_not_happen(self):
        """Artwork-content page checks stay unreviewed on a placeholder build.

        Tail direction is exempt: page-QA construction re-derives the expected
        regions from the storyboard and placed geometry and rejects stale or
        incomplete evidence, so that pass is machine-earned.
        """
        for name, project in self.built.items():
            for record_path in sorted((project / "qa/pages").glob("*.json")):
                with self.subTest(example=name, page=record_path.stem):
                    record = read_json(record_path)
                    self.assertEqual("accept-warning", record["decision"])
                    results = {check["id"]: check["result"] for check in record["checks"]}
                    for check_id in (
                        "face-action-obstruction",
                        "accidental-text-watermark",
                    ):
                        self.assertEqual("warning", results[check_id])
                    self.assertEqual("pass", results["bubble-tail-direction"])

    def test_unreviewed_panel_warnings_reach_the_manifest_and_the_report(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                manifest_warnings = read_json(project / "project.json")["warnings"]
                report = (project / "qa/report.md").read_text(encoding="utf-8")
                record = read_json(sorted((project / "qa/panels").glob("*.json"))[0])
                for warning in record["unresolved_warnings"]:
                    self.assertIn(warning, manifest_warnings)
                    self.assertIn(warning, report)

    def test_each_example_exports_a_recorded_pdf_and_report(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                manifest = read_json(project / "project.json")
                for descriptor in ("pdf", "qa_report", "composition_cache"):
                    self.assertIn(descriptor, manifest["artifacts"])
                pdf = manifest["artifacts"]["pdf"]["path"]
                self.assertEqual(f"exports/{name}.pdf", pdf)
                self.assertTrue((project / pdf).is_file())
                self.assertTrue((project / "qa/report.md").is_file())

    def test_each_example_composes_every_declared_page(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                contract = read_json(SAMPLES / name / "example.json")
                pages = sorted((project / "pages").glob("page-*.png"))
                self.assertEqual(contract["page_count"], len(pages))
                for page_number in range(1, contract["page_count"] + 1):
                    self.assertTrue((project / f"qa/pages/page-{page_number:03d}.json").is_file())

    def test_each_example_labels_itself_as_deterministic_evidence(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                evidence = read_json(project / "qa/evidence.json")
                self.assertEqual("deterministic", evidence["mode"])
                self.assertFalse(evidence["proves_visual_quality"])
                report = (project / "qa/report.md").read_text(encoding="utf-8")
                self.assertIn("Mode: deterministic", report)

    def test_committed_plan_artifacts_are_recorded_verbatim(self):
        for name, project in self.built.items():
            with self.subTest(example=name):
                source = SAMPLES / name
                for relative in (
                    "plan/story-plan.json",
                    "plan/character-bible.json",
                    "plan/storyboard.json",
                    "source/input.txt",
                ):
                    self.assertEqual(
                        (source / relative).read_bytes(),
                        (project / relative).read_bytes(),
                        relative,
                    )


if __name__ == "__main__":
    unittest.main()
