import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from comic_sol import (  # noqa: E402
    ResumeAction,
    atomic_write_json,
    block_project,
    build_resume_plan,
    canonical_artifact_bytes,
    finalize_project,
    init_project,
    invalidate_from,
    main,
    promote_attempt,
    read_json,
    record_generation_attempt,
    record_override,
    record_stage,
    resume_project,
    sha256_file,
    stage_cache_key,
    transition,
)
from normalize_panels import normalize_panel  # noqa: E402
from validate_project import validate_panel_record  # noqa: E402


STAGES = ("planning", "storyboard", "generation", "lettering", "composition", "export")
FIXTURES = ROOT / "tests/fixtures"
FIXTURES = ROOT / "tests/fixtures"


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root,
            "Sunlight Courier",
            b"A courier carries the last light.",
            {"mode": "short_prompt", "language": "en"},
        )
        self._complete_project()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_json(self, relative, data):
        path = self.project / relative
        atomic_write_json(path, data)
        return path

    def _complete_project(self):
        story = {
            "schema_version": "1.0", "title": "Sunlight Courier",
            "scenes": [{"id": "hall", "characters": ["mira"]}],
        }
        characters = {
            "schema_version": "1.0",
            "characters": [{
                "id": "mira",
                "visual_fingerprint": {"invariants": ["amber scarf", "round clasp"]},
                "reference_path": "references/characters/mira.png",
            }],
        }
        storyboard = {
            "schema_version": "1.0",
            "pages": [{
                "number": 1, "layout": "full-page",
                "panels": [{
                    "id": "p01-01", "scene_id": "hall", "characters": ["mira"],
                    "rect": {"x": 64, "y": 64, "width": 1472, "height": 2272},
                    "text": [{"id": "p01-01-t01", "content": "One last delivery."}],
                }],
            }],
        }
        self._write_json("plan/story-plan.json", story)
        self._write_json("plan/character-bible.json", characters)
        self._write_json("plan/storyboard.json", storyboard)
        (self.project / "prompts/panels/p01-01.txt").write_text("panel prompt\n", "utf-8")
        (self.project / "panels/p01-01").mkdir(exist_ok=True)
        for relative, color in (
            ("references/characters/mira.png", "orange"),
            ("panels/raw/p01-01.png", "navy"),
            ("panels/clean/p01-01.png", "blue"),
            ("panels/p01-01/lettered.png", "white"),
            ("pages/page-001.png", "gray"),
        ):
            Image.new("RGB", (512, 512), color).save(self.project / relative)
        self._write_json("qa/panels/p01-01.json", self._accepted_panel_record())
        (self.project / "qa/report.md").write_text("# QA\n", "utf-8")
        (self.project / "exports/sunlight-courier.pdf").write_bytes(b"%PDF-1.4\nfixture\n")

        manifest = read_json(self.project / "project.json")
        manifest["status"] = "COMPLETE"
        manifest["panels"] = ["p01-01"]
        manifest["settings"].update({"page_count": 1, "panel_count": 1})
        manifest["artifacts"] = {
            "story_plan": self._descriptor("plan/story-plan.json"),
            "character_bible": self._descriptor("plan/character-bible.json"),
            "storyboard": self._descriptor("plan/storyboard.json"),
            "qa_report": self._descriptor("qa/report.md"),
            "pdf": self._descriptor("exports/sunlight-courier.pdf"),
        }
        atomic_write_json(self.project / "project.json", manifest)
        self._write_cache_snapshot()

    def _descriptor(self, relative):
        return {"path": relative, "sha256": sha256_file(self.project / relative)}

    def _stage_material(self, stage):
        manifest = read_json(self.project / "project.json")
        story = read_json(self.project / "plan/story-plan.json")
        characters = read_json(self.project / "plan/character-bible.json")
        storyboard = read_json(self.project / "plan/storyboard.json")
        panels = [panel for page in storyboard["pages"] for panel in page["panels"]]
        if stage == "planning":
            return [read_json(self.project / "source/request.json")], [self.project / "source/input.txt"]
        if stage == "storyboard":
            identities = [{"id": item["id"]} for item in characters["characters"]]
            return [story, identities], []
        if stage == "generation":
            visual_panels = []
            for panel in panels:
                item = deepcopy(panel)
                sfx_items = [
                    text_item
                    for text_item in item.get("text", [])
                    if text_item.get("kind") == "sfx"
                ]
                if sfx_items:
                    item["text"] = sfx_items
                else:
                    item.pop("text", None)
                visual_panels.append(item)
            dependencies = []
            actual_references = []
            for panel in panels:
                record = read_json(self.project / f"qa/panels/{panel['id']}.json")
                if record.get("schema_version") == "2.0":
                    references = []
                    source_prompt_path = f"prompts/panels/{panel['id']}.txt"
                else:
                    references = record["generation"]["reference_paths"]
                    source_prompt_path = record["source_prompt_path"]
                dependencies.append({
                    "panel_id": panel["id"],
                    "reference_paths": references,
                    "source_prompt_path": source_prompt_path,
                })
                actual_references.extend(references)
            relatives = [dependency["source_prompt_path"] for dependency in dependencies]
            relatives.extend(item["reference_path"] for item in characters["characters"])
            relatives.extend(actual_references)
            files = [self.project / relative for relative in dict.fromkeys(relatives)]
            return [visual_panels, characters, manifest["capability"], dependencies], files
        if stage == "lettering":
            record = read_json(self.project / "qa/panels/p01-01.json")
            clean_relative = (
                "panels/p01-01/clean.png"
                if record.get("schema_version") == "2.0"
                else "panels/clean/p01-01.png"
            )
            return [[panel["text"] for panel in panels]], [self.project / clean_relative]
        if stage == "composition":
            geometry = [{"number": page["number"], "layout": page["layout"], "panels": [p["rect"] for p in page["panels"]]} for page in storyboard["pages"]]
            return [geometry], [self.project / "panels/p01-01/lettered.png"]
        return [{"project_id": manifest["project_id"], "settings": manifest["settings"]}], [self.project / "pages/page-001.png", self.project / "qa/report.md"]

    def _write_cache_snapshot(self):
        manifest = read_json(self.project / "project.json")
        panel_record = read_json(self.project / "qa/panels/p01-01.json")
        clean_relative = (
            "panels/p01-01/clean.png"
            if panel_record.get("schema_version") == "2.0"
            else "panels/clean/p01-01.png"
        )
        outputs = {
            "planning": ["plan/story-plan.json", "plan/character-bible.json"],
            "storyboard": ["plan/storyboard.json"],
            "generation": ["panels/raw/p01-01.png", clean_relative],
            "lettering": ["panels/p01-01/lettered.png"],
            "composition": ["pages/page-001.png"],
            "export": ["qa/report.md", "exports/sunlight-courier.pdf"],
        }
        stages = {}
        for stage in STAGES:
            canonical_inputs, files = self._stage_material(stage)
            stages[stage] = {
                "key": stage_cache_key(stage, canonical_inputs, files, manifest["stage_versions"][stage]),
                "artifacts": {relative: sha256_file(self.project / relative) for relative in outputs[stage]},
            }
        self._write_json("logs/stage-cache.json", {"schema_version": "1.0", "stages": stages})

    def _run_finalize(self, project_dir=None):
        """Run finalization while isolating stages outside the lettering decision."""
        (self.project / "qa/pages").mkdir(parents=True, exist_ok=True)
        (self.project / "qa/pages/page-001.json").write_text("{}\n", "utf-8")
        with (
            patch("compose_pages.compose_project"),
            patch("page_quality.validate_page_quality", return_value=[]),
            patch("export_pdf.guarded_export"),
            patch("render_report.render_report"),
            patch("comic_sol.record_stage"),
            patch("comic_sol.transition"),
        ):
            return finalize_project(self.project if project_dir is None else project_dir)

    def test_cache_key_is_canonical_and_excludes_timestamps(self):
        first = stage_cache_key("planning", [{"updated_at": "one", "b": 2, "a": 1}], [], "1")
        second = stage_cache_key("planning", [{"a": 1, "b": 2, "updated_at": "two"}], [], "1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, stage_cache_key("planning", [{"a": 1, "b": 3}], [], "1"))

    def test_finalize_treats_regenerate_and_rerun_as_stale(self):
        for action in ("regenerate", "rerun"):
            with self.subTest(action=action):
                plan = [ResumeAction("lettering", action, "stage", "stale")]
                with (
                    patch("comic_sol.build_resume_plan", return_value=plan),
                    patch("letter_panels.letter_project") as letter,
                ):
                    self._run_finalize()
                letter.assert_called_once_with(self.project)

    def test_finalize_preserves_lexical_path_for_stale_stage_callback(self):
        """Finalization must not leak a platform-specific resolved path to callbacks."""
        lexical_project = self.project / ".." / self.project.name
        plan = [ResumeAction("lettering", "rerun", "stage", "stale")]
        with (
            patch("comic_sol.build_resume_plan", return_value=plan),
            patch("letter_panels.letter_project") as letter,
        ):
            self._run_finalize(lexical_project)
        letter.assert_called_once_with(lexical_project)

    def test_finalize_does_not_accept_empty_manifest_panels_vacuously(self):
        manifest = read_json(self.project / "project.json")
        manifest["panels"] = []
        atomic_write_json(self.project / "project.json", manifest)
        (self.project / "panels/p01-01/lettered.png").unlink(missing_ok=True)
        with (
            patch("comic_sol.build_resume_plan", return_value=[]),
            patch("letter_panels.letter_project") as letter,
        ):
            self._run_finalize()
        letter.assert_called_once_with(self.project)

    def test_finalize_preserves_lexical_path_for_empty_panel_callback(self):
        """Storyboard fallback must preserve the caller path passed to lettering."""
        manifest = read_json(self.project / "project.json")
        manifest["panels"] = []
        atomic_write_json(self.project / "project.json", manifest)
        (self.project / "panels/p01-01/lettered.png").unlink(missing_ok=True)
        lexical_project = self.project / ".." / self.project.name
        with (
            patch("comic_sol.build_resume_plan", return_value=[]),
            patch("letter_panels.letter_project") as letter,
        ):
            self._run_finalize(lexical_project)
        letter.assert_called_once_with(lexical_project)

    def test_stale_v1_lettering_cache_reruns_lettering_onward_only(self):
        canonical_inputs, files = self._stage_material("lettering")
        cache = read_json(self.project / "logs/stage-cache.json")
        cache["stages"]["lettering"]["key"] = stage_cache_key(
            "lettering", canonical_inputs, files, "1"
        )
        atomic_write_json(self.project / "logs/stage-cache.json", cache)

        manifest = read_json(self.project / "project.json")
        manifest["stage_versions"]["lettering"] = "2"
        atomic_write_json(self.project / "project.json", manifest)
        raw_before = (self.project / "panels/raw/p01-01.png").read_bytes()
        clean_before = (self.project / "panels/clean/p01-01.png").read_bytes()

        actions = build_resume_plan(self.project)

        stage_actions = [
            (action.stage, action.action)
            for action in actions
            if action.artifact == "stage"
        ]
        self.assertEqual(
            [
                ("planning", "reuse"),
                ("storyboard", "reuse"),
                ("generation", "reuse"),
                ("lettering", "rerun"),
                ("composition", "rerun"),
                ("export", "rerun"),
            ],
            stage_actions,
        )
        self.assertEqual(raw_before, (self.project / "panels/raw/p01-01.png").read_bytes())
        self.assertEqual(clean_before, (self.project / "panels/clean/p01-01.png").read_bytes())

    def test_v2_panel_quality_record_preserves_generation_cache_reuse(self):
        atomic_write_json(
            self.project / "qa/panels/p01-01.json", self._panel_record_v2()
        )
        self._write_cache_snapshot()
        cache = read_json(self.project / "logs/stage-cache.json")
        generation_artifacts = cache["stages"]["generation"]["artifacts"]
        self.assertIn("panels/p01-01/clean.png", generation_artifacts)
        self.assertTrue((self.project / "panels/clean/p01-01.png").is_file())

        actions = build_resume_plan(self.project)

        by_stage = {
            action.stage: (action.action, action.reason)
            for action in actions
            if action.artifact == "stage"
        }
        self.assertEqual(
            ("reuse", "cache key and artifacts match"),
            by_stage["generation"],
        )

        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/p01-01/clean.png"
        )
        generation = next(
            action for action in build_resume_plan(self.project)
            if action.stage == "generation" and action.artifact == "stage"
        )
        self.assertEqual("regenerate", generation.action, generation.reason)

    def test_v2_canonical_and_legacy_clean_artifacts_do_not_cross_fingerprint(self):
        atomic_write_json(
            self.project / "qa/panels/p01-01.json", self._panel_record_v2()
        )
        self._write_cache_snapshot()

        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/clean/p01-01.png"
        )
        generation = next(
            action for action in build_resume_plan(self.project)
            if action.stage == "generation" and action.artifact == "stage"
        )
        self.assertEqual("reuse", generation.action, generation.reason)

        Image.new("RGB", (512, 512), "green").save(
            self.project / "panels/p01-01/clean.png"
        )
        generation = next(
            action for action in build_resume_plan(self.project)
            if action.stage == "generation" and action.artifact == "stage"
        )
        self.assertEqual("regenerate", generation.action, generation.reason)

    def test_v2_canonical_clean_artifact_is_fingerprinted(self):
        atomic_write_json(
            self.project / "qa/panels/p01-01.json", self._panel_record_v2()
        )
        (self.project / "panels/clean/p01-01.png").unlink()
        self._write_cache_snapshot()
        baseline = build_resume_plan(self.project)
        baseline_generation = next(
            action for action in baseline
            if action.stage == "generation" and action.artifact == "stage"
        )
        self.assertEqual("reuse", baseline_generation.action, baseline_generation.reason)

        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/p01-01/clean.png"
        )

        actions = build_resume_plan(self.project)
        by_stage = {
            action.stage: action.action
            for action in actions
            if action.artifact == "stage"
        }
        self.assertEqual("regenerate", by_stage["generation"])
        self.assertTrue(
            all(by_stage[stage] == "rerun" for stage in ("lettering", "composition", "export"))
        )

    def test_v1_legacy_clean_artifact_remains_fingerprinted(self):
        baseline = build_resume_plan(self.project)
        baseline_generation = next(
            action for action in baseline
            if action.stage == "generation" and action.artifact == "stage"
        )
        self.assertEqual("reuse", baseline_generation.action, baseline_generation.reason)

        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/clean/p01-01.png"
        )

        actions = build_resume_plan(self.project)
        by_stage = {
            action.stage: action.action
            for action in actions
            if action.artifact == "stage"
        }
        self.assertEqual("regenerate", by_stage["generation"])
        self.assertTrue(
            all(by_stage[stage] == "rerun" for stage in ("lettering", "composition", "export"))
        )

    def test_noop_resume_does_not_write_any_file(self):
        before = {p.relative_to(self.project): (p.stat().st_mtime_ns, sha256_file(p)) for p in self.project.rglob("*") if p.is_file()}
        actions = build_resume_plan(self.project)
        after = {p.relative_to(self.project): (p.stat().st_mtime_ns, sha256_file(p)) for p in self.project.rglob("*") if p.is_file()}
        self.assertTrue(actions)
        self.assertTrue(all(action.action == "reuse" for action in actions), actions)
        self.assertEqual(before, after)

    def test_dialogue_change_invalidates_lettering_onward_only(self):
        storyboard = read_json(self.project / "plan/storyboard.json")
        storyboard["pages"][0]["panels"][0]["text"][0]["content"] = "The final delivery begins."
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"] = self._descriptor("plan/storyboard.json")
        atomic_write_json(self.project / "project.json", manifest)
        raw_hash = sha256_file(self.project / "panels/raw/p01-01.png")
        clean_hash = sha256_file(self.project / "panels/clean/p01-01.png")

        actions = build_resume_plan(self.project)

        by_stage = {action.stage: action.action for action in actions if action.artifact == "stage"}
        self.assertEqual("reuse", by_stage["generation"])
        self.assertEqual(["lettering", "composition", "export"], [stage for stage in STAGES if by_stage[stage] == "rerun"])
        self.assertEqual(raw_hash, sha256_file(self.project / "panels/raw/p01-01.png"))
        self.assertEqual(clean_hash, sha256_file(self.project / "panels/clean/p01-01.png"))

    def test_sfx_change_invalidates_generation_onward(self):
        storyboard = read_json(self.project / "plan/storyboard.json")
        storyboard["pages"][0]["panels"][0]["text"] = [{
            "id": "p01-01-sfx", "kind": "sfx", "content": "KRAK!",
        }]
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"] = self._descriptor("plan/storyboard.json")
        atomic_write_json(self.project / "project.json", manifest)
        self._write_cache_snapshot()
        baseline = build_resume_plan(self.project)
        self.assertTrue(all(action.action == "reuse" for action in baseline), baseline)
        raw_hash = sha256_file(self.project / "panels/raw/p01-01.png")
        clean_hash = sha256_file(self.project / "panels/clean/p01-01.png")

        storyboard["pages"][0]["panels"][0]["text"][0]["content"] = "BOOM!"
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"] = self._descriptor("plan/storyboard.json")
        atomic_write_json(self.project / "project.json", manifest)

        actions = build_resume_plan(self.project)

        by_stage = {action.stage: action.action for action in actions if action.artifact == "stage"}
        self.assertEqual("reuse", by_stage["storyboard"])
        self.assertEqual("regenerate", by_stage["generation"])
        self.assertTrue(all(by_stage[stage] == "rerun" for stage in ("lettering", "composition", "export")))
        self.assertEqual(raw_hash, sha256_file(self.project / "panels/raw/p01-01.png"))
        self.assertEqual(clean_hash, sha256_file(self.project / "panels/clean/p01-01.png"))

    def test_fingerprint_change_invalidates_generation_onward(self):
        characters = read_json(self.project / "plan/character-bible.json")
        characters["characters"][0]["visual_fingerprint"]["invariants"][0] = "crimson scarf"
        atomic_write_json(self.project / "plan/character-bible.json", characters)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["character_bible"] = self._descriptor("plan/character-bible.json")
        atomic_write_json(self.project / "project.json", manifest)

        actions = build_resume_plan(self.project)

        by_stage = {action.stage: action.action for action in actions if action.artifact == "stage"}
        self.assertEqual("reuse", by_stage["storyboard"])
        self.assertEqual("regenerate", by_stage["generation"])
        self.assertTrue(all(by_stage[stage] == "rerun" for stage in ("lettering", "composition", "export")))

    def test_missing_or_hash_mismatch_invalidates_earliest_owner(self):
        (self.project / "pages/page-001.png").unlink()
        actions = build_resume_plan(self.project)
        by_stage = {action.stage: action.action for action in actions if action.artifact == "stage"}
        self.assertEqual("rerun", by_stage["composition"])
        self.assertEqual("rerun", by_stage["export"])
        self.assertEqual("reuse", by_stage["lettering"])

        self._complete_project()
        (self.project / "plan/storyboard.json").write_text("changed", "utf-8")
        actions = build_resume_plan(self.project)
        by_stage = {action.stage: action.action for action in actions if action.artifact == "stage"}
        self.assertEqual("reuse", by_stage["planning"])
        self.assertEqual("rerun", by_stage["storyboard"])
        self.assertTrue(all(by_stage[stage] != "reuse" for stage in STAGES[2:]))

    def test_interrupted_tmp_is_reported_and_not_deleted(self):
        interrupted = self.project / "panels/raw/.p01-01.png.crash.tmp"
        interrupted.write_bytes(b"partial")
        actions = build_resume_plan(self.project)
        self.assertTrue(any(action.artifact == "panels/raw/.p01-01.png.crash.tmp" and "interrupted" in action.reason for action in actions), actions)
        self.assertEqual(b"partial", interrupted.read_bytes())

    def test_invalidate_removes_manifest_entries_but_preserves_files(self):
        storyboard_path = self.project / "plan/storyboard.json"
        before = storyboard_path.read_bytes()
        removed = invalidate_from(self.project, "storyboard")
        self.assertEqual(["storyboard", "qa_report", "pdf"], removed)
        self.assertEqual(before, storyboard_path.read_bytes())
        self.assertNotIn("storyboard", read_json(self.project / "project.json")["artifacts"])
        self.assertEqual(
            {"planning"},
            set(read_json(self.project / "logs/stage-cache.json")["stages"]),
        )
        transactions = self.project / "logs/transactions"
        self.assertEqual([], list(transactions.iterdir()) if transactions.exists() else [])

    def test_attempt_is_retained_until_verified_promotion(self):
        attempt = self.project / "panels/raw/p01-01.attempt-2.png"
        Image.new("RGB", (640, 960), "green").save(attempt)
        counts = record_generation_attempt(self.project, "p01-01", "visual_retry", attempt)
        self.assertEqual(1, counts["visual_retries"])
        self.assertTrue(attempt.is_file())

        destination = promote_attempt(self.project, "p01-01", attempt)
        self.assertEqual(self.project / "panels/raw/p01-01.png", destination)
        self.assertTrue(attempt.is_file())
        self.assertEqual(sha256_file(attempt), sha256_file(destination))

        broken = self.project / "panels/raw/p01-01.attempt-3.png"
        broken.write_bytes(b"not an image")
        before = sha256_file(destination)
        with self.assertRaisesRegex(ValueError, "readable raster"):
            promote_attempt(self.project, "p01-01", broken)
        self.assertEqual(before, sha256_file(destination))

    def test_promotion_rechecks_original_relative_path_before_verification(self):
        attempt = self.project / "panels/raw/p01-01.swap-before-verify.png"
        outside = self.root / "outside-valid.png"
        Image.new("RGB", (640, 960), "green").save(attempt)
        Image.new("RGB", (640, 960), "red").save(outside)
        real_resolver = __import__("comic_sol")._contained_project_path
        calls = 0

        def swap_after_preflight(project_dir, path):
            nonlocal calls
            result = real_resolver(project_dir, path)
            if Path(path) == attempt and calls == 0:
                calls += 1
                attempt.unlink()
                attempt.symlink_to(outside)
            return result

        try:
            probe = self.project / "symlink-probe"
            probe.symlink_to(outside)
            probe.unlink()
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        with patch("comic_sol._contained_project_path", side_effect=swap_after_preflight):
            with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
                promote_attempt(self.project, "p01-01", attempt)

    def test_promotion_rechecks_original_relative_path_before_read(self):
        attempt = self.project / "panels/raw/p01-01.swap-before-read.png"
        outside = self.root / "outside-valid.png"
        Image.new("RGB", (640, 960), "green").save(attempt)
        Image.new("RGB", (640, 960), "red").save(outside)
        from comic_sol import _verify_raster

        def verify_then_swap(path):
            result = _verify_raster(path)
            attempt.unlink()
            attempt.symlink_to(outside)
            return result

        try:
            probe = self.project / "symlink-probe"
            probe.symlink_to(outside)
            probe.unlink()
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        with patch("comic_sol._verify_raster", side_effect=verify_then_swap):
            with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
                promote_attempt(self.project, "p01-01", attempt)

    def test_cli_attempt_path_rejection_matches_shared_semantics(self):
        for command in ("record-attempt", "promote-attempt"):
            arguments = [command, os.fspath(self.project), "p01-01"]
            if command == "record-attempt":
                arguments.append("initial")
            arguments.append("C:outside.png")
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(arguments))

    def _generation_state(self, project=None):
        project = project or self.project
        return tuple(
            path.read_bytes() if path.exists() else None
            for path in (
                project / "logs/generation-counters.json",
                project / "logs/events.jsonl",
            )
        )

    def _attempt(self, name, size=(512, 512), project=None):
        project = project or self.project
        path = project / f"panels/raw/{name}.png"
        Image.new("RGB", size, "green").save(path)
        return path

    def _assert_attempt_rejected_without_mutation(
        self, panel_id, kind, attempt, message, project=None
    ):
        project = project or self.project
        before = self._generation_state(project)
        with self.assertRaisesRegex(ValueError, message):
            record_generation_attempt(project, panel_id, kind, attempt)
        self.assertEqual(before, self._generation_state(project))

    def test_second_initial_is_rejected_without_mutation(self):
        first = self._attempt("p01-01.initial-1")
        counts = record_generation_attempt(self.project, "p01-01", "initial", first)
        self.assertEqual(1, counts["initial"])
        self.assertEqual(0, counts["global_extra_calls"])
        second = self._attempt("p01-01.initial-2")
        self._assert_attempt_rejected_without_mutation(
            "p01-01", "initial", second, "one initial attempt"
        )

    def test_second_transient_repeat_is_rejected_without_mutation(self):
        first = self._attempt("p01-01.transient-1")
        counts = record_generation_attempt(
            self.project, "p01-01", "transient_repeat", first
        )
        self.assertEqual(1, counts["transient_repeats"])
        self.assertEqual(1, counts["global_extra_calls"])
        second = self._attempt("p01-01.transient-2")
        self._assert_attempt_rejected_without_mutation(
            "p01-01", "transient_repeat", second, "one transient repeat"
        )

    def test_third_visual_retry_is_rejected_without_mutation(self):
        for number in (1, 2):
            counts = record_generation_attempt(
                self.project,
                "p01-01",
                "visual_retry",
                self._attempt(f"p01-01.visual-{number}"),
            )
        self.assertEqual(2, counts["visual_retries"])
        self.assertEqual(2, counts["global_extra_calls"])
        self._assert_attempt_rejected_without_mutation(
            "p01-01",
            "visual_retry",
            self._attempt("p01-01.visual-3"),
            "two visual retries",
        )

    def test_corrupt_raster_is_rejected_without_mutation(self):
        attempt = self.project / "panels/raw/p01-01.corrupt.png"
        attempt.write_bytes(b"not an image")
        self._assert_attempt_rejected_without_mutation(
            "p01-01", "initial", attempt, "readable raster"
        )

    def test_small_raster_is_rejected_without_mutation(self):
        for size in ((511, 512), (512, 511)):
            with self.subTest(size=size):
                self._assert_attempt_rejected_without_mutation(
                    "p01-01",
                    "initial",
                    self._attempt(f"p01-01.small-{size[0]}x{size[1]}", size),
                    "at least 512px",
                )

    def test_ninth_global_extra_is_rejected_and_initials_are_excluded(self):
        project = init_project(
            self.root,
            "Budget",
            b"Story",
            {"mode": "short_prompt", "language": "en"},
        )
        for number in range(1, 9):
            panel_id = f"p{number:02d}-01"
            counts = record_generation_attempt(
                project,
                panel_id,
                "initial",
                self._attempt(f"{panel_id}.initial", project=project),
            )
        self.assertEqual(0, counts["global_extra_calls"])
        for number in (1, 2):
            counts = record_generation_attempt(
                project,
                "p01-01",
                "visual_retry",
                self._attempt(f"p01-01.visual-{number}", project=project),
            )
        for number in range(2, 8):
            panel_id = f"p{number:02d}-01"
            counts = record_generation_attempt(
                project,
                panel_id,
                "transient_repeat",
                self._attempt(f"{panel_id}.transient", project=project),
            )
        self.assertEqual(8, counts["global_extra_calls"])
        self.assertEqual(1, counts["transient_repeats"])
        self._assert_attempt_rejected_without_mutation(
            "p08-01",
            "transient_repeat",
            self._attempt("p08-01.transient", project=project),
            "eight extra calls",
            project,
        )

    def test_successful_attempt_appends_sanitized_event(self):
        attempt = self._attempt("p01-01.initial")
        record_generation_attempt(self.project, "p01-01", "initial", attempt)
        event = json.loads(
            (self.project / "logs/events.jsonl").read_text("utf-8").splitlines()[-1]
        )
        self.assertEqual("generation.attempt-recorded", event["event"])
        self.assertEqual(
            {
                "attempt_path": "panels/raw/p01-01.initial.png",
                "kind": "initial",
                "panel_id": "p01-01",
            },
            event["details"],
        )

    def _failing_panel_record(self, category):
        check_ids = (
            "character-identity", "anatomy", "action", "composition",
            "continuity", "text-free", "technical",
        )
        return {
            "schema_version": "1.0",
            "panel_id": "p01-01",
            "source_prompt_path": "prompts/panels/p01-01.txt",
            "raw_path": "panels/raw/p01-01.png",
            "clean_path": "panels/clean/p01-01.png",
            "raw_sha256": sha256_file(self.project / "panels/raw/p01-01.png"),
            "dimensions": {"height": 512, "width": 512},
            "attempts": 3,
            "generation": {
                "capability_name": "test-image",
                "completed_at": "2026-07-20T00:00:00Z",
                "reference_paths": [],
            },
            "checks": [
                {
                    "id": check_id,
                    "result": "fail" if check_id == "anatomy" else "pass",
                    "severity": "error",
                    "evidence": "hand anatomy is broken" if check_id == "anatomy" else "ok",
                }
                for check_id in check_ids
            ],
            "decision": "regenerate",
            "retry_reason": "anatomy repair required",
            "unresolved_warnings": [],
            "failure_category": category,
            "override_reason": None,
        }

    def _accepted_panel_record(self, reference_paths=None):
        record = self._failing_panel_record(None)
        for check in record["checks"]:
            check.update({"result": "pass", "severity": "error", "evidence": "ok"})
        record.update({"attempts": 1, "decision": "accept", "retry_reason": None})
        record["generation"]["reference_paths"] = (
            ["references/characters/mira.png"]
            if reference_paths is None else reference_paths
        )
        return record

    def _quality_checks(self):
        check_ids = (
            "character-identity", "anatomy", "action", "composition",
            "continuity", "text-free", "technical",
        )
        return [{
            "id": check_id,
            "result": "pass",
            "severity": "error",
            "evidence": f"Observed {check_id} against current panel artifacts",
            "method": "bounded-visual-review",
            "reviewer": "fixture-reviewer",
            "regions": [],
        } for check_id in check_ids]

    def _panel_record_v2(self, panel_id="p01-01"):
        raw = self.project / f"panels/raw/{panel_id}.png"
        clean = self.project / f"panels/{panel_id}/clean.png"
        normalization = self.project / f"panels/{panel_id}/normalization.json"
        normalize_panel(
            self.project, panel_id, f"panels/raw/{panel_id}.png", (512, 512), "exact"
        )
        with Image.open(raw) as image:
            raw_width, raw_height = image.size
        with Image.open(clean) as image:
            clean_width, clean_height = image.size
        return {
            "schema_version": "2.0",
            "kind": "panel-qa",
            "subject_id": panel_id,
            "bindings": {
                "raw_path": f"panels/raw/{panel_id}.png",
                "raw_sha256": sha256_file(raw),
                "raw_width": raw_width,
                "raw_height": raw_height,
                "clean_path": f"panels/{panel_id}/clean.png",
                "clean_sha256": sha256_file(clean),
                "clean_width": clean_width,
                "clean_height": clean_height,
                "normalization_path": f"panels/{panel_id}/normalization.json",
                "normalization_sha256": sha256_file(normalization),
            },
            "checks": self._quality_checks(),
            "review": {
                "method": "bounded-visual-review",
                "reviewer": "fixture-reviewer",
                "reviewed_at": "2026-08-14T00:00:00Z",
            },
            "decision": "accept",
            "unresolved_warnings": [],
        }

    def test_resume_matches_valid_v2_record_by_subject_id(self):
        self._write_json("qa/panels/p01-01.json", self._panel_record_v2())
        self._write_cache_snapshot()

        actions = build_resume_plan(self.project)

        action = next(item for item in actions if item.artifact == "p01-01")
        self.assertEqual("reuse", action.action, action.reason)

    def test_corrupt_and_safety_failures_cannot_be_overridden(self):
        record_path = self.project / "qa/panels/p01-01.json"
        for category in ("corrupt_image", "safety_refusal"):
            atomic_write_json(record_path, self._failing_panel_record(category))
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, "cannot be overridden"):
                record_override(self.project, "p01-01", "accept the visual defect")

    def test_only_visual_qa_failures_can_be_overridden(self):
        record_path = self.project / "qa/panels/p01-01.json"
        for category in (None, "tool_quota", "transient_error"):
            atomic_write_json(record_path, self._failing_panel_record(category))
            with self.subTest(category=category), self.assertRaisesRegex(
                ValueError, "only non-safety visual QA errors"
            ):
                record_override(self.project, "p01-01", "accept the visual defect")

    def test_override_requires_an_error_level_failed_check(self):
        record_path = self.project / "qa/panels/p01-01.json"
        record = self._failing_panel_record("visual_qa")
        for check in record["checks"]:
            check.update({"result": "pass", "severity": "error"})
        record.update({"decision": "accept", "retry_reason": None})
        atomic_write_json(record_path, record)

        with self.assertRaisesRegex(ValueError, "error-level failed check"):
            record_override(self.project, "p01-01", "nothing failed")

    def test_override_rejects_mismatched_or_corrupt_recorded_artifacts(self):
        record_path = self.project / "qa/panels/p01-01.json"
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()

        record = self._failing_panel_record("visual_qa")
        record["raw_sha256"] = "0" * 64
        atomic_write_json(record_path, record)
        with self.assertRaisesRegex(ValueError, "corrupt images cannot be overridden"):
            record_override(self.project, "p01-01", "accept mismatched hash")
        self.assertEqual("regenerate", read_json(record_path)["decision"])
        self.assertEqual(original_manifest, manifest_path.read_bytes())

    def test_override_normalizes_pillow_safety_error_as_corrupt_image(self):
        record_path = self.project / "qa/panels/p01-01.json"
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        atomic_write_json(record_path, self._failing_panel_record("visual_qa"))

        with (
            patch(
                "comic_sol.Image.open",
                side_effect=Image.DecompressionBombError("unsafe dimensions"),
            ),
            self.assertRaisesRegex(ValueError, "corrupt images cannot be overridden"),
        ):
            record_override(self.project, "p01-01", "must remain blocked")

        record = self._failing_panel_record("visual_qa")
        atomic_write_json(record_path, record)
        (self.project / "panels/clean/p01-01.png").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "corrupt images cannot be overridden"):
            record_override(self.project, "p01-01", "accept corrupt clean image")
        self.assertEqual("regenerate", read_json(record_path)["decision"])
        self.assertEqual(original_manifest, manifest_path.read_bytes())

    def test_override_downgrades_failure_and_preserves_pipeline_status(self):
        record_path = self.project / "qa/panels/p01-01.json"
        atomic_write_json(record_path, self._failing_panel_record("visual_qa"))
        before = read_json(self.project / "project.json")["status"]

        record_override(self.project, "p01-01", "minor prop drift is acceptable")

        updated = read_json(record_path)
        self.assertEqual("accept_with_warnings", updated["decision"])
        self.assertIsNone(updated["retry_reason"])
        self.assertEqual("minor prop drift is acceptable", updated["override_reason"])
        self.assertIn("minor prop drift is acceptable", updated["unresolved_warnings"])
        anatomy = next(check for check in updated["checks"] if check["id"] == "anatomy")
        self.assertEqual({"result": "fail", "severity": "warning"}, {
            "result": anatomy["result"], "severity": anatomy["severity"],
        })
        self.assertEqual([], validate_panel_record(updated))

        manifest = read_json(self.project / "project.json")
        self.assertEqual(before, manifest["status"])
        self.assertIn("minor prop drift is acceptable", manifest["warnings"])

    def test_override_warning_selects_warning_terminal_at_final_transition(self):
        record_path = self.project / "qa/panels/p01-01.json"
        atomic_write_json(record_path, self._failing_panel_record("visual_qa"))
        manifest = read_json(self.project / "project.json")
        manifest["status"] = "QA_READY"
        atomic_write_json(self.project / "project.json", manifest)

        record_override(self.project, "p01-01", "minor prop drift is acceptable")
        for status in ("LETTERED", "COMPOSED", "EXPORTED"):
            transition(self.project, status)

        # This test isolates warning-target selection; final artifact gating is
        # covered by GuardedOperationTests.
        with patch("validate_project.require_valid_project"):
            completed = transition(self.project, "COMPLETE")
        self.assertEqual("COMPLETE_WITH_WARNINGS", completed["status"])

    def test_resume_plan_without_cache_marks_every_stage_stale(self):
        (self.project / "logs/stage-cache.json").unlink()
        actions = build_resume_plan(self.project)
        stage_actions = [action for action in actions if action.artifact == "stage"]
        self.assertEqual(6, len(stage_actions))
        self.assertFalse(any(action.action == "reuse" for action in stage_actions))
        self.assertEqual("stage cache entry is missing", stage_actions[0].reason)
        panel = next(action for action in actions if action.artifact == "p01-01")
        self.assertEqual("regenerate", panel.action)
        self.assertIn("cache", panel.reason)

    def test_invalid_cache_marks_every_stage_stale_without_raising(self):
        cache_path = self.project / "logs/stage-cache.json"
        invalid_payloads = (
            b"{broken",
            b"[]\n",
            b'{"schema_version":"2.0","stages":{}}\n',
            b'{"schema_version":"1.0","stages":{"planning":{"artifacts":[],"key":"bad"}}}\n',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                cache_path.write_bytes(payload)
                stage_actions = [
                    action
                    for action in build_resume_plan(self.project)
                    if action.artifact == "stage"
                ]
                self.assertEqual(6, len(stage_actions))
                self.assertFalse(any(action.action == "reuse" for action in stage_actions))
                self.assertIn("stage cache is invalid", stage_actions[0].reason)

    def test_record_stage_repairs_invalid_cache_with_canonical_atomic_json(self):
        cache_path = self.project / "logs/stage-cache.json"
        cache_path.write_bytes(b"{broken")

        recorded = record_stage(self.project, "planning")

        self.assertEqual("planning", recorded["stage"])
        cache = read_json(cache_path)
        self.assertEqual({"schema_version", "stages"}, set(cache))
        self.assertEqual({"planning"}, set(cache["stages"]))
        self.assertEqual(canonical_artifact_bytes(cache), cache_path.read_bytes())
        self.assertEqual([], list(cache_path.parent.glob(f".{cache_path.name}.*.tmp")))

    def test_record_stage_refuses_missing_expected_output(self):
        (self.project / "plan/story-plan.json").unlink()
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"].pop("story_plan")
        atomic_write_json(self.project / "project.json", manifest)

        with self.assertRaisesRegex(ValueError, "stage output is missing.*story-plan.json"):
            record_stage(self.project, "planning")

    def test_record_stage_refuses_stage_without_expected_artifacts(self):
        storyboard = read_json(self.project / "plan/storyboard.json")
        storyboard["pages"] = []
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)

        with self.assertRaisesRegex(ValueError, "storyboard has no panels"):
            record_stage(self.project, "generation")

    def test_changed_generated_output_hash_is_not_reused(self):
        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/p01-01/lettered.png"
        )

        lettering = next(
            action
            for action in build_resume_plan(self.project)
            if action.artifact == "stage" and action.stage == "lettering"
        )
        self.assertEqual("rerun", lettering.action)
        self.assertIn("artifact hash mismatch", lettering.reason)

    def test_manifest_update_cannot_mask_stale_cached_pdf(self):
        pdf_path = self.project / "exports/sunlight-courier.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nreplaced outside export stage\n")
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["pdf"] = self._descriptor(
            "exports/sunlight-courier.pdf"
        )
        atomic_write_json(self.project / "project.json", manifest)

        export = next(
            action
            for action in build_resume_plan(self.project)
            if action.artifact == "stage" and action.stage == "export"
        )
        self.assertEqual("rerun", export.action)
        self.assertIn("artifact hash mismatch", export.reason)

    def test_changed_accepted_panel_hash_is_not_reused(self):
        record = self._accepted_panel_record()
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)
        Image.new("RGB", (512, 512), "purple").save(
            self.project / "panels/raw/p01-01.png"
        )

        panel = next(
            action
            for action in build_resume_plan(self.project)
            if action.artifact == "p01-01"
        )
        self.assertEqual("regenerate", panel.action)
        self.assertIn("hash mismatch", panel.reason)

    def test_noncanonical_accepted_panel_paths_are_not_reused(self):
        alternate_raw = self.project / "panels/raw/alternate.png"
        alternate_clean = self.project / "panels/clean/alternate.png"
        shutil.copy2(self.project / "panels/raw/p01-01.png", alternate_raw)
        shutil.copy2(self.project / "panels/clean/p01-01.png", alternate_clean)
        record = self._accepted_panel_record()
        record.update({
            "raw_path": "panels/raw/alternate.png",
            "clean_path": "panels/clean/alternate.png",
            "raw_sha256": sha256_file(alternate_raw),
        })
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

        panel = next(
            action for action in build_resume_plan(self.project)
            if action.artifact == "p01-01"
        )
        self.assertEqual("regenerate", panel.action)
        self.assertIn("canonical", panel.reason)

    def test_generation_input_change_prevents_panel_reuse(self):
        storyboard = read_json(self.project / "plan/storyboard.json")
        storyboard["pages"][0]["panels"][0]["text"] = [{
            "id": "p01-01-sfx", "kind": "sfx", "content": "KRAK!",
        }]
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"] = self._descriptor("plan/storyboard.json")
        atomic_write_json(self.project / "project.json", manifest)
        record_stage(self.project, "generation")

        storyboard["pages"][0]["panels"][0]["text"][0]["content"] = "BOOM!"
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        manifest = read_json(self.project / "project.json")
        manifest["artifacts"]["storyboard"] = self._descriptor("plan/storyboard.json")
        atomic_write_json(self.project / "project.json", manifest)

        actions = build_resume_plan(self.project)
        panel = next(action for action in actions if action.artifact == "p01-01")
        self.assertEqual("regenerate", panel.action)
        self.assertIn("cache", panel.reason)

    def test_actual_scene_reference_change_invalidates_generation_and_panel(self):
        scene_path = self.project / "references/scenes/hall.png"
        Image.new("RGB", (512, 512), "green").save(scene_path)
        atomic_write_json(
            self.project / "qa/panels/p01-01.json",
            self._accepted_panel_record(["references/scenes/hall.png"]),
        )
        record_stage(self.project, "generation")
        baseline = build_resume_plan(self.project)
        self.assertEqual(
            "reuse",
            next(action for action in baseline if action.artifact == "p01-01").action,
        )

        Image.new("RGB", (512, 512), "purple").save(scene_path)
        actions = build_resume_plan(self.project)
        generation = next(
            action for action in actions
            if action.artifact == "stage" and action.stage == "generation"
        )
        panel = next(action for action in actions if action.artifact == "p01-01")
        self.assertEqual("regenerate", generation.action)
        self.assertEqual("regenerate", panel.action)

    def test_malformed_accepted_panel_record_is_not_reused(self):
        record = self._accepted_panel_record()
        record["checks"] = []
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

        panel = next(
            action for action in build_resume_plan(self.project)
            if action.artifact == "p01-01"
        )
        self.assertEqual("regenerate", panel.action)
        self.assertIn("invalid", panel.reason)

    def test_schema_invalid_override_metadata_is_not_reused(self):
        record = self._accepted_panel_record()
        record.update({
            "failure_category": "visual_qa",
            "override_reason": "not backed by a warning",
        })
        atomic_write_json(self.project / "qa/panels/p01-01.json", record)

        panel = next(
            action for action in build_resume_plan(self.project)
            if action.artifact == "p01-01"
        )
        self.assertEqual("regenerate", panel.action)
        self.assertIn("invalid", panel.reason)

    def test_record_stage_roundtrip_enables_honest_reuse(self):
        (self.project / "logs/stage-cache.json").unlink()
        for stage in STAGES:
            recorded = record_stage(self.project, stage)
            self.assertEqual(stage, recorded["stage"])
        cache = read_json(self.project / "logs/stage-cache.json")["stages"]
        self.assertEqual(
            {"panels/p01-01/lettered.png"},
            set(cache["lettering"]["artifacts"]),
        )
        self.assertEqual(
            {"pages/page-001.png"},
            set(cache["composition"]["artifacts"]),
        )
        stage_actions = [a for a in build_resume_plan(self.project) if a.artifact == "stage"]
        self.assertEqual(6, len(stage_actions))
        self.assertTrue(all(action.action == "reuse" for action in stage_actions))

        story = read_json(self.project / "plan/story-plan.json")
        story["title"] = "Changed title"
        atomic_write_json(self.project / "plan/story-plan.json", story)
        stage_actions = [a for a in build_resume_plan(self.project) if a.artifact == "stage"]
        self.assertEqual("rerun", stage_actions[0].action)
        self.assertFalse(any(action.action == "reuse" for action in stage_actions))

    def test_record_and_resume_accept_relative_project_path(self):
        with contextlib.chdir(self.root):
            relative_project = Path(self.project.name)
            for stage in STAGES:
                record_stage(relative_project, stage)
            stage_actions = [
                action
                for action in build_resume_plan(relative_project)
                if action.artifact == "stage"
            ]
        self.assertTrue(all(action.action == "reuse" for action in stage_actions))

    def test_resume_cli_commands_expose_interfaces(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["resume-plan", os.fspath(self.project), "--json"]))
        self.assertTrue(json.loads(output.getvalue()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(["invalidate", os.fspath(self.project), "export"]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(["record-stage", os.fspath(self.project), "export"]))


class ResumeFixtureIntegrationTests(unittest.TestCase):
    def test_interrupted_fixture_without_cache_regenerates_all_panels_honestly(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(FIXTURES / "interrupted-two-page", project)
            actions = build_resume_plan(project)
            panel_actions = {a.artifact: a.action for a in actions if a.artifact.startswith("p")}
            self.assertEqual("regenerate", panel_actions["p01-01"])
            self.assertEqual("regenerate", panel_actions["p01-02"])


class BlockedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = init_project(
            self.root,
            "Sunlight Courier",
            b"A courier carries the last light.",
            {"mode": "short_prompt", "language": "en"},
        )
        story = {
            "schema_version": "1.0", "title": "Sunlight Courier",
            "scenes": [{"id": "hall", "characters": ["mira"]}],
        }
        characters = {
            "schema_version": "1.0",
            "characters": [{
                "id": "mira",
                "visual_fingerprint": {"invariants": ["amber scarf", "round clasp"]},
                "reference_path": "references/characters/mira.png",
            }],
        }
        storyboard = {
            "schema_version": "1.0",
            "pages": [{
                "number": 1, "layout": "full-page",
                "panels": [{
                    "id": "p01-01", "scene_id": "hall", "characters": ["mira"],
                    "rect": {"x": 64, "y": 64, "width": 1472, "height": 2272},
                    "text": [{"id": "p01-01-t01", "content": "One last delivery."}],
                }],
            }],
        }
        atomic_write_json(self.project / "plan/story-plan.json", story)
        atomic_write_json(self.project / "plan/character-bible.json", characters)
        atomic_write_json(self.project / "plan/storyboard.json", storyboard)
        (self.project / "prompts/panels/p01-01.txt").write_text("panel prompt\n", "utf-8")
        (self.project / "panels/p01-01").mkdir(exist_ok=True)
        for relative, color in (
            ("references/characters/mira.png", "orange"),
            ("panels/raw/p01-01.png", "navy"),
            ("panels/clean/p01-01.png", "blue"),
            ("panels/p01-01/lettered.png", "white"),
            ("pages/page-001.png", "gray"),
        ):
            Image.new("RGB", (512, 512), color).save(self.project / relative)
        self._write_json("qa/panels/p01-01.json", {
            "schema_version": "1.0",
            "panel_id": "p01-01",
            "source_prompt_path": "prompts/panels/p01-01.txt",
            "raw_path": "panels/raw/p01-01.png",
            "clean_path": "panels/clean/p01-01.png",
            "raw_sha256": sha256_file(self.project / "panels/raw/p01-01.png"),
            "dimensions": {"height": 512, "width": 512},
            "attempts": 1,
            "generation": {
                "capability_name": "test-image",
                "completed_at": "2026-07-20T00:00:00Z",
                "reference_paths": ["references/characters/mira.png"],
            },
            "checks": [
                {"id": cid, "result": "pass", "severity": "error", "evidence": "ok"}
                for cid in ("character-identity", "anatomy", "action", "composition",
                             "continuity", "text-free", "technical")
            ],
            "decision": "accept",
            "retry_reason": None,
            "unresolved_warnings": [],
        })
        (self.project / "qa/report.md").write_text("# QA\n", "utf-8")
        (self.project / "exports/sunlight-courier.pdf").write_bytes(b"%PDF-1.4\nfixture\n")

        manifest = read_json(self.project / "project.json")
        manifest.update({
            "status": "STORYBOARDED",
            "panels": ["p01-01"],
            "artifacts": {
                "story_plan": {"path": "plan/story-plan.json",
                                "sha256": sha256_file(self.project / "plan/story-plan.json")},
                "character_bible": {"path": "plan/character-bible.json",
                                    "sha256": sha256_file(self.project / "plan/character-bible.json")},
                "storyboard": {"path": "plan/storyboard.json",
                               "sha256": sha256_file(self.project / "plan/storyboard.json")},
            },
        })
        manifest["settings"].update({"page_count": 1, "panel_count": 1})
        manifest["warnings"] = ["unrelated continuity warning"]
        manifest["capability"].update({
            "detected_at": "2026-07-23T00:00:00Z",
            "name": None,
            "status": "unavailable",
        })
        atomic_write_json(self.project / "project.json", manifest)

        cache = {"schema_version": "1.0", "stages": {}}
        from comic_sol import _resume_stage_material
        for stage in ("planning", "storyboard"):
            canonical_inputs, files = _resume_stage_material(self.project, stage, manifest)
            outputs = {"planning": ["plan/story-plan.json", "plan/character-bible.json"],
                        "storyboard": ["plan/storyboard.json"]}[stage]
            cache["stages"][stage] = {
                "key": stage_cache_key(stage, canonical_inputs, files, manifest["stage_versions"][stage]),
                "artifacts": {r: sha256_file(self.project / r) for r in outputs},
            }
        atomic_write_json(self.project / "logs/stage-cache.json", cache)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_json(self, relative, data):
        path = self.project / relative
        atomic_write_json(path, data)
        return path


    def test_blocked_project_resumes_without_losing_valid_artifacts(self):
        warning = "image capability unavailable"
        before = {
            relative: (self.project / relative).read_bytes()
            for relative in (
                "plan/story-plan.json",
                "plan/character-bible.json",
                "plan/storyboard.json",
            )
        }
        blocked = block_project(
            self.project, "image-capability-unavailable", warning
        )
        self.assertEqual("BLOCKED", blocked["status"])
        self.assertEqual("STORYBOARDED", blocked["blocked_from"])
        self.assertEqual("image-capability-unavailable", blocked["blocked_reason"])

        manifest = read_json(self.project / "project.json")
        manifest["capability"].update({
            "detected_at": "2026-07-23T00:01:00Z",
            "name": "restored-image-tool",
            "status": "available",
        })
        atomic_write_json(self.project / "project.json", manifest)

        result = resume_project(self.project)

        self.assertEqual("STORYBOARDED", result["status"])
        self.assertEqual(["planning", "storyboard"], result["preserved"])
        self.assertEqual(["generation", "lettering", "composition", "export"], result["invalidated"])
        self.assertEqual({"agent_required": "generation"}, result["next_action"])
        resumed = read_json(self.project / "project.json")
        self.assertIsNone(resumed["blocked_from"])
        self.assertIsNone(resumed["blocked_reason"])
        self.assertEqual(["unrelated continuity warning"], resumed["warnings"])
        for relative, payload in before.items():
            self.assertEqual(payload, (self.project / relative).read_bytes())

    def test_resume_cli_reports_actionable_json_and_human_output(self):
        block_project(
            self.project,
            "image-capability-unavailable",
            "image capability unavailable",
        )
        manifest = read_json(self.project / "project.json")
        manifest["capability"].update({
            "detected_at": "2026-07-23T00:01:00Z",
            "name": "restored-image-tool",
            "status": "available",
        })
        atomic_write_json(self.project / "project.json", manifest)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["resume", os.fspath(self.project), "--json"]))
        self.assertEqual(
            {"agent_required": "generation"},
            json.loads(output.getvalue())["next_action"],
        )

    def test_resume_cli_human_output(self):
        block_project(
            self.project,
            "image-capability-unavailable",
            "image capability unavailable",
        )
        manifest = read_json(self.project / "project.json")
        manifest["capability"].update({
            "detected_at": "2026-07-23T00:01:00Z",
            "name": "restored-image-tool",
            "status": "available",
        })
        atomic_write_json(self.project / "project.json", manifest)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["resume", os.fspath(self.project)]))
        self.assertIn("agent required: generation", output.getvalue())


if __name__ == "__main__":
    unittest.main()
