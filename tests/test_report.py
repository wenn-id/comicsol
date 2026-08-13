import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from comic_sol import atomic_write_json, canonical_json_bytes, read_json  # noqa: E402
from quality_records import PANEL_CHECK_IDS  # noqa: E402
from render_report import QaSummary, main, render_report, summarize_qa  # noqa: E402


def panel_record(panel_id, *, attempts=1, decision="accept", warning=None,
                 failing=False, override_reason=None):
    checks = [{
        "id": check_id,
        "result": "fail" if failing and check_id == "technical" else (
            "warning" if warning and check_id == "continuity" else "pass"
        ),
        "severity": "error" if failing or not warning else "warning",
        "evidence": "pipe | line\nnext" if check_id == "composition" else f"{check_id} checked",
    } for check_id in PANEL_CHECK_IDS]
    record = {
        "schema_version": "1.0", "panel_id": panel_id,
        "source_prompt_path": f"prompts/panels/{panel_id}.txt",
        "raw_path": f"panels/raw/{panel_id}.png",
        "clean_path": f"panels/clean/{panel_id}.png",
        "raw_sha256": "a" * 64,
        "dimensions": {"width": 512, "height": 768},
        "attempts": attempts,
        "generation": {
            "capability_name": "agent-image-generation",
            "reference_paths": ["references/characters/mira.png"],
            "completed_at": "2026-07-18T04:10:00Z",
        },
        "checks": checks, "decision": decision,
        "retry_reason": "technical repair required" if decision == "regenerate" else None,
        "unresolved_warnings": [warning] if warning else [],
    }
    if override_reason:
        for check in record["checks"]:
            if check["result"] == "fail" and check["severity"] == "error":
                check["severity"] = "warning"
        record["failure_category"] = "visual_qa"
        record["override_reason"] = override_reason
        record["unresolved_warnings"].append(override_reason)
    return record


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "qa-comic"
        for relative in (
            "qa/panels", "pages", "exports", "logs", "plan",
            "prompts/panels", "panels/raw", "panels/clean",
            "references/characters",
        ):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        self.records = [
            panel_record("p01-01"),
            panel_record("p01-02", decision="accept_with_warnings", warning="Minor prop drift is visible."),
            panel_record("p01-03", attempts=2, decision="regenerate", failing=True),
            panel_record("p01-04", attempts=2, decision="accept_with_warnings", failing=True, override_reason="User override: scarf hue differs."),
            panel_record("p01-05", attempts=3, decision="regenerate", failing=True),
        ]
        for record in reversed(self.records):
            atomic_write_json(
                self.project / f"qa/panels/{record['panel_id']}.json", record
            )
            (self.project / record["source_prompt_path"]).write_text("prompt\n", "utf-8")
            Image.new("RGB", (512, 768), "gray").save(self.project / record["raw_path"])
            Image.new("RGB", (512, 768), "gray").save(self.project / record["clean_path"])
        Image.new("RGB", (512, 768), "orange").save(
            self.project / "references/characters/mira.png"
        )
        page = self.project / "pages/page-001.png"
        Image.new("RGB", (1600, 2400), "white").save(page)
        pdf = self.project / "exports/qa-comic.pdf"
        Image.open(page).convert("RGB").save(
            pdf, "PDF", resolution=150.0, creationDate=False, modDate=False, title=False
        )
        story = self.project / "plan/story-plan.json"
        atomic_write_json(story, {"schema_version": "1.0", "title": "QA Comic"})

        manifest = read_json(ROOT / "templates/manifest.json")
        manifest.update({"project_id": "qa-comic", "title": "QA Comic", "status": "BLOCKED"})
        manifest["input"]["source_sha256"] = "b" * 64
        manifest["settings"].update({"page_count": 1, "panel_count": 5})
        manifest["panels"] = [record["panel_id"] for record in self.records]
        manifest["capability"].update({
            "status": "available", "name": "agent-image-generation",
            "supports_reference_images": False, "supports_dimensions": True,
            "detected_at": "2026-07-18T04:00:00Z",
        })
        manifest["artifacts"] = {
            "story_plan": {"path": "plan/story-plan.json", "sha256": hashlib.sha256(story.read_bytes()).hexdigest()},
            "pdf": {"path": "exports/qa-comic.pdf", "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()},
        }
        atomic_write_json(self.project / "project.json", manifest)
        events = [
            {"event": "artifact.reused", "details": {"artifact_path": "panels/raw/p01-01.png"}, "timestamp": "2026-07-18T05:00:00Z"},
            {"event": "artifact.regenerated", "details": {"artifact_path": "panels/raw/p01-03.png"}, "timestamp": "2026-07-18T05:01:00Z"},
        ]
        (self.project / "logs/events.jsonl").write_bytes(
            b"".join(canonical_json_bytes(event) + b"\n" for event in events)
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_summary_uses_panel_records_for_exact_aggregates(self):
        summary = summarize_qa(read_json(self.project / "project.json"), self.records)
        self.assertEqual(QaSummary(
            pages=1, panels=5, generation_attempts=9,
            regenerated_panels=3, accepted_warnings=2, hard_failures=1,
        ), summary)

    def test_safety_category_counts_as_hard_failure_without_failed_check(self):
        record = panel_record("p01-01", decision="regenerate")
        record["failure_category"] = "safety_refusal"

        summary = summarize_qa(read_json(self.project / "project.json"), [record])
        self.assertEqual(1, summary.hard_failures)

    def test_report_distinguishes_all_decisions_and_has_no_template_tokens(self):
        output = render_report(self.project)
        text = output.read_text("utf-8")
        for phrase in (
            "Project summary", "Capability", "Panel QA", "Unresolved warnings",
            "Artifact integrity", "Resume summary", "accept_with_warnings",
            "regenerate", "override", "BLOCKED",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("{{", text)
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_report_contains_checks_escaped_evidence_disclosures_and_integrity(self):
        text = render_report(self.project).read_text("utf-8")
        for check_id in PANEL_CHECK_IDS:
            self.assertIn(check_id, text)
        self.assertIn("pipe \\| line<br>next", text)
        self.assertIn("5 panels", text)
        self.assertIn("9 generation attempts", text)
        self.assertIn("3 regenerated panels", text)
        self.assertIn("2 accepted warnings", text)
        self.assertIn("1 hard failure", text)
        self.assertIn("Minor prop drift is visible.", text)
        self.assertIn("User override: scarf hue differs.", text)
        self.assertIn("fail (warning)", text)
        self.assertIn("reference images are unsupported", text)
        self.assertIn("degraded consistency mode", text)
        self.assertIn("provider policies govern transmitted prompts and references", text)
        self.assertIn("pages/page-001.png", text)
        self.assertIn("1600×2400", text)
        self.assertIn("PDF readable: yes", text)
        self.assertIn("hash matches: yes", text)
        self.assertIn("Reused: panels/raw/p01-01.png", text)
        self.assertIn("Regenerated: panels/raw/p01-03.png", text)

    def test_report_labels_deterministic_evidence_as_mechanics_only(self):
        atomic_write_json(
            self.project / "qa/evidence.json",
            {
                "mode": "deterministic",
                "scope": "mechanics-only",
                "proves_visual_quality": False,
            },
        )
        text = render_report(self.project).read_text("utf-8")
        self.assertIn("Evidence provenance", text)
        self.assertIn("deterministic", text)
        self.assertIn("mechanics-only", text)
        self.assertIn("does not prove live visual quality", text)

    def test_report_discloses_retained_live_visual_provenance(self):
        atomic_write_json(
            self.project / "qa/evidence.json",
            {
                "mode": "live-visual",
                "scope": "retained-attempt-visual-review",
                "proves_visual_quality": True,
                "retained_attempt": "panels/raw/p01-01/attempt-001.png",
                "attempt_sha256": "a" * 64,
                "provider": "local-test-provider",
                "model": "test-model-v1",
                "references": ["references/characters/mira.png"],
                "reviewer_method": "bounded-visual-review",
                "limitations": ["synthetic fixture"],
            },
        )
        text = render_report(self.project).read_text("utf-8")
        for phrase in (
            "live-visual",
            "local-test-provider",
            "test-model-v1",
            "attempt-001.png",
            "a" * 64,
            "references/characters/mira.png",
            "bounded-visual-review",
            "synthetic fixture",
        ):
            self.assertIn(phrase, text)

    def test_report_discloses_page_layout_and_check_method_identity(self):
        page_dir = self.project / "qa/pages"
        page_dir.mkdir(parents=True, exist_ok=True)
        checks = []
        for check_id in (
            "clipped-text", "text-overlap", "face-action-obstruction",
            "bubble-tail-direction", "reading-order",
            "accidental-text-watermark", "layout-border-integrity",
        ):
            deterministic = check_id in {
                "clipped-text", "text-overlap", "reading-order",
                "layout-border-integrity",
            }
            checks.append({
                "id": check_id,
                "result": "pass",
                "severity": "error",
                "evidence": f"Bounded evidence for {check_id}.",
                "method": (
                    "deterministic-geometry-v1"
                    if deterministic else "bounded-visual-review"
                ),
                "reviewer": "comic-sol" if deterministic else "fixture-reviewer",
                "regions": [{"scope": "page"}],
            })
        atomic_write_json(page_dir / "page-001.json", {
            "bindings": {
                "layout_name": "four-grid",
                "layout_version": "1",
                "page_path": "pages/page-001.png",
                "page_sha256": "a" * 64,
            },
            "checks": checks,
            "decision": "accept",
            "kind": "page-qa",
            "review": {
                "method": "deterministic-plus-bounded-visual-review",
                "reviewed_at": "fixture",
                "reviewer": "fixture-reviewer",
            },
            "schema_version": "2.0",
            "subject_id": "page-001",
            "unresolved_warnings": [],
        })

        text = render_report(self.project).read_text("utf-8")
        self.assertIn("Page QA", text)
        self.assertIn("four-grid", text)
        self.assertIn("deterministic-geometry-v1", text)
        self.assertIn("bounded-visual-review", text)
        self.assertIn("face-action-obstruction", text)

    def test_absent_warnings_use_exact_sentence(self):
        for path in (self.project / "qa/panels").glob("*.json"):
            record = read_json(path)
            record["decision"] = "accept"
            record["retry_reason"] = None
            record["unresolved_warnings"] = []
            record.pop("override_reason", None)
            for check in record["checks"]:
                check.update({"result": "pass", "severity": "error"})
            atomic_write_json(path, record)
        text = render_report(self.project).read_text("utf-8")
        self.assertIn("No unresolved warnings.", text)

    def test_manifest_only_warning_is_reported_once_with_project_source(self):
        for path in (self.project / "qa/panels").glob("*.json"):
            record = read_json(path)
            record["decision"] = "accept"
            record["retry_reason"] = None
            record["unresolved_warnings"] = []
            record.pop("override_reason", None)
            for check in record["checks"]:
                check.update({"result": "pass", "severity": "error"})
            atomic_write_json(path, record)
        manifest = read_json(self.project / "project.json")
        manifest["warnings"] = ["Page crop was accepted by the user."]
        atomic_write_json(self.project / "project.json", manifest)

        text = render_report(self.project).read_text("utf-8")
        self.assertIn("- `project`: Page crop was accepted by the user.", text)
        self.assertEqual(1, text.count("Page crop was accepted by the user."))
        self.assertNotIn("No unresolved warnings.", text)

    def test_duplicate_panel_and_manifest_warning_preserves_both_sources(self):
        warning = "Minor prop drift is visible."
        manifest = read_json(self.project / "project.json")
        manifest["warnings"] = [warning]
        atomic_write_json(self.project / "project.json", manifest)

        text = render_report(self.project).read_text("utf-8")
        self.assertIn(f"- `p01-02, project`: {warning}", text)
        self.assertEqual(1, text.count(warning))

    def test_custom_output_cli_atomic_write_and_unresolved_token_failure(self):
        custom = self.project / "deliverables/report.md"
        self.assertEqual(custom, render_report(self.project, custom))
        self.assertTrue(custom.is_file())
        cli_output = self.project / "deliverables/cli.md"
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, main([str(self.project), "--output", str(cli_output)]))
        self.assertTrue(cli_output.is_file())

        broken_template = self.project / "broken.tmpl"
        broken_template.write_text((ROOT / "templates/qa-report.md.tmpl").read_text("utf-8") + "\n{{UNKNOWN}}\n", "utf-8")
        before = custom.read_bytes()
        with mock.patch("render_report.TEMPLATE_PATH", broken_template):
            with self.assertRaisesRegex(ValueError, "template token"):
                render_report(self.project, custom)
        self.assertEqual(before, custom.read_bytes())


class ReportFixtureIntegrationTests(unittest.TestCase):
    def test_valid_fixture_report_has_no_unresolved_warnings(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            self.assertIn("No unresolved warnings", render_report(project).read_text("utf-8"))

    def test_report_discloses_normalization_without_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            shutil.copytree(ROOT / "tests/fixtures/valid-one-page", project)
            text = render_report(project).read_text("utf-8")

            self.assertIn("## Panel normalization", text)
            self.assertIn("| p01-01 | exact | 736×588 | 736×588 |", text)
            self.assertIn("| p01-02 | exact | 720×1064 | 720×1064 |", text)
            self.assertNotIn(str(project), text)
            self.assertNotIn(str(Path.home()), text)
            self.assertNotIn("prompts/panels", text)


if __name__ == "__main__":
    unittest.main()
