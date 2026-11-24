"""Exercise the installed Agent Skill end to end with reviewed fixture inputs."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib


REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests/fixtures/valid-one-page/panels/raw/p01-01.png"
REVIEW = {"method": "offline-fixture", "reviewer": "fixture-reviewer",
          "reviewed_at": "2026-08-18T00:00:00Z"}


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raster_fixture(width, height):
    def chunk(name, payload):
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress((b"\0" + bytes((29, 57, 80)) * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class SkillWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="comic-sol-skill-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "projects"
        self.home = self.root / "home"
        self.home.mkdir()
        self.exe = Path(os.environ["COMIC_SOL_NATIVE_TEST_EXE"]).resolve(strict=True)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "PYTHONHOME"}}
        self.env.update(
            HOME=str(self.home), USERPROFILE=str(self.home), CODEX_HOME=str(self.home / ".codex"),
            PATH=str(self.root / "empty-path"), COMIC_SOL_PYTHON=str(self.root / "missing-python"),
            COMIC_SOL_ENGINE_DIR=str(self.root / "missing-engine"),
        )

    def invoke(self, *args, expected=0, payload=None, executable=None):
        result = subprocess.run(
            [executable or self.exe, "--json", *map(str, args)], input=payload, cwd=self.root,
            capture_output=True, text=True, env=self.env, timeout=70,
        )
        self.assertEqual(result.returncode, expected, (args, result.stdout, result.stderr))
        return json.loads(result.stdout)

    def install(self, executable=None):
        return self.invoke("skill-install", "--target", "codex", "--scope", "user",
                           executable=executable)["data"]

    def test_install_workflow_to_verified_pdf(self):
        skill = Path(self.install()["destination"])
        self.assertFalse(any(path.suffix in {".py", ".pyc", ".pyo"} for path in skill.rglob("*")))
        self.assertIn("comic-sol skill --help", (skill / "SKILL.md").read_text("utf-8"))
        self.assertIn("comic-sol skill character-quality",
                      (skill / "references/workflow.md").read_text("utf-8"))
        health = self.invoke("doctor", "--output-root", self.work)["data"]
        self.assertTrue(health["healthy"])
        data = self.invoke(
            "init", "--output-root", self.work, "--title", "Skill Smoke",
            "--starter", "minimal-one-page", "--image-capability-status", "available",
            "--image-capability-name", "agent-image-generation",
        )["data"]
        project = self.work / data["project_id"]

        def step(command, *args, **kwargs):
            return self.invoke("skill", command, project, *args, **kwargs)["data"]

        panel = read(project / "plan/storyboard.json")["pages"][0]["panels"][0]
        character = read(project / "plan/character-bible.json")["characters"][0]
        reference = project / character["reference_path"]
        reference.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, reference)
        step("character-identity", "--derive")
        step("character-identity", "--check")
        step("reference-strategy", "--plan", "--budget", 1)
        step("transition", "REFERENCES_READY")
        panel_id = panel["id"]
        prompt = project / f"prompts/panels/{panel_id}.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        identity = step("character-identity", "--panel", panel_id)
        selection = step("reference-strategy", "--panel", panel_id, "--budget", 1)
        prompt.write_text(identity + "\n" + selection + "\n", "utf-8")
        attempt = "panels/attempts/offline-fixture.png"
        (project / attempt).parent.mkdir(parents=True, exist_ok=True)
        (project / attempt).write_bytes(raster_fixture(panel["rect"]["width"] // 2,
                                                        panel["rect"]["height"] // 2))
        step("record-attempt", panel_id, "initial", attempt)
        raw = step("promote-attempt", panel_id, attempt)
        clean = step("normalize-panel", "--panel", panel_id, "--source", raw,
                     "--width", panel["rect"]["width"], "--height", panel["rect"]["height"],
                     "--mode", "fit")
        normalization = f"panels/{panel_id}/normalization.json"
        stats = read(project / normalization)
        template = read(skill / "templates/panel-record.json")
        template.update(subject_id=panel_id, decision="accept", review=REVIEW, unresolved_warnings=[])
        template["bindings"].update(
            raw_path=raw, raw_sha256=digest(project / raw),
            raw_width=stats["source"]["size"][0], raw_height=stats["source"]["size"][1],
            clean_path=clean, clean_sha256=digest(project / clean),
            clean_width=stats["clean"]["size"][0], clean_height=stats["clean"]["size"][1],
            normalization_path=normalization, normalization_sha256=digest(project / normalization),
        )
        for check in template["checks"]:
            check.update(result="pass", method=REVIEW["method"], reviewer=REVIEW["reviewer"],
                         evidence=f"Offline fixture provides the {check['id']} test observation")
        write(project / f"qa/panels/{panel_id}.json", template)
        context = step("character-quality", "--context", panel_id)
        assessments = [
            {"character_id": person["character_id"], "trait": trait["trait"], "result": "pass",
             "severity": "error", "evidence": f"Offline fixture observation for {trait['trait']}"}
            for person in context["characters"] for trait in person["traits"]
        ]
        step("character-quality", "--record", panel_id, "--method", REVIEW["method"],
             "--reviewer", REVIEW["reviewer"], payload=json.dumps(assessments))
        step("repair-strategy", "--plan")
        step("quality-sample", "--mode", "deterministic")
        self.invoke("validate", project, "--stage", "panels")
        step("record-stage", "generation")
        step("transition", "PANELS_READY")
        step("transition", "QA_READY")
        first = self.invoke("finalize", project, expected=2)
        self.assertEqual(first["error"]["code"], "CS-QA-001")
        self.assertEqual(read(project / "project.json")["status"], "COMPOSED")
        self.assertFalse(list((project / "exports").glob("*.pdf")))

        self.write_page_review(skill, project, panel_id)
        result = self.invoke("finalize", project)["data"]
        self.assertEqual(result["status"], "COMPLETE")
        self.invoke("validate", project, "--stage", "final")
        verification = read(project / "exports/pdf-verification.json")
        self.assertEqual(verification["pdf_sha256"], digest(project / result["pdf"]))
        self.assertIn("mechanics-only", (project / result["report"]).read_text("utf-8"))

    def write_page_review(self, skill, project, panel_id):
        template = read(skill / "templates/page-qa.json")
        page = "pages/page-001.png"
        cache = "cache/composition.json"
        board = "plan/storyboard.json"
        lettering = project / f"panels/{panel_id}/lettering.json"
        normalization = project / f"panels/{panel_id}/normalization.json"
        placements = read(lettering)["items"]
        self.assertEqual([("caption", 1)], [(item["kind"], item["reading_order"]) for item in placements])
        box = placements[0]["box"]
        width, height = read(normalization)["clean"]["size"]
        self.assertTrue(0 <= box["x"] < box["x"] + box["width"] <= width)
        self.assertTrue(0 <= box["y"] < box["y"] + box["height"] <= height)
        template["bindings"].update(
            composition_cache_sha256=digest(project / cache),
            layout_name=read(project / cache)["pages"][0]["layout"]["name"], layout_version="1",
            lettering_sha256s=[f"{panel_id}:{digest(lettering)}"],
            normalization_sha256s=[f"{panel_id}:{digest(normalization)}"],
            page_height=2400, page_width=1600, page_sha256=digest(project / page),
            storyboard_sha256=digest(project / board),
        )
        visual = {"face-action-obstruction", "bubble-tail-direction", "accidental-text-watermark"}
        for check in template["checks"]:
            is_visual = check["id"] in visual
            check.update(
                evidence=f"Mechanics-only page fixture supplies {check['id']} observation",
                result="pass", severity="info" if check["id"] == "balloon-crowding" else "error",
                method=REVIEW["method"] if is_visual else "deterministic-geometry-v1",
                reviewer=REVIEW["reviewer"] if is_visual else "comic-sol",
                regions=[{"scope": "page"}] if is_visual and check["id"] != "bubble-tail-direction" else [],
            )
        template.update(decision="accept", unresolved_warnings=[],
                        review={**REVIEW, "method": "deterministic-plus-bounded-visual-review"})
        write(project / "qa/pages/page-001.json", template)

    def test_upgrade_removes_only_old_managed_scripts(self):
        previous = os.environ.get("COMIC_SOL_PREVIOUS_NATIVE_EXE")
        if previous:
            old = Path(previous).resolve(strict=True)
            installed = Path(self.install(executable=old)["destination"])
        else:
            installed = Path(self.install()["destination"])
            old_script = installed / "scripts/comic_sol.py"
            old_script.parent.mkdir()
            shutil.copyfile(REPO / "scripts/comic_sol.py", old_script)
            marker_file = installed / ".comic-sol-install.json"
            marker = read(marker_file)
            marker["managed_paths"]["scripts/comic_sol.py"] = digest(old_script)
            combined = hashlib.sha256()
            for name, value in sorted(marker["managed_paths"].items()):
                combined.update(name.encode("utf-8") + b"\0" + bytes.fromhex(value))
            marker["bundle_digest"] = combined.hexdigest()
            write(marker_file, marker)
        self.assertTrue((installed / "scripts/comic_sol.py").is_file())
        upgrade = self.install()
        self.assertEqual(upgrade["status"], "upgraded")
        self.assertFalse(any(installed.rglob("*.py")))
        unchanged = self.install()
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(upgrade["bundle_digest"], unchanged["bundle_digest"])

    def test_unavailable_image_capability_preserves_prepared_handoff(self):
        initialized = self.invoke("init", "--output-root", self.work, "--title", "Handoff Smoke",
                                  "--starter", "minimal-one-page", "--image-capability-status", "unavailable")
        project = self.work / initialized["data"]["project_id"]
        character = read(project / "plan/character-bible.json")["characters"][0]
        panel = read(project / "plan/storyboard.json")["pages"][0]["panels"][0]["id"]
        for relative, content in (
            (f"prompts/references/{character['id']}.txt", "Identity reference in a neutral pose."),
            (f"prompts/panels/{panel}.txt", "Protect the tender green shoot in a rooftop planter."),
        ):
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, "utf-8")
        self.invoke("skill", "character-identity", project, "--derive", expected=2)
        self.invoke("skill", "character-identity", project, "--derive", "--allow-missing-references")
        self.invoke("skill", "character-identity", project, "--check", expected=2)
        self.invoke("handoff", "prepare", project)
        self.invoke("skill", "transition", project, "BLOCKED", "--warning",
                    "image-capability-unavailable")
        state = self.invoke("status", project)["data"]
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["blocked_reason"], "image-capability-unavailable")
        prepared = self.invoke("handoff", "inspect", project)["data"]
        self.assertTrue(any(job["status"] == "ready" for job in prepared["jobs"]))

    def test_repair_plan_refuses_nonfile_review(self):
        initialized = self.invoke("init", "--output-root", self.work, "--title", "Review Smoke",
                                  "--starter", "minimal-one-page")
        project = self.work / initialized["data"]["project_id"]
        (project / "qa/panels/p01-01.json").mkdir(parents=True)
        self.invoke("skill", "repair-strategy", project, "--plan", expected=2)
        self.assertFalse((project / "logs/repair-plan.json").exists())


if __name__ == "__main__":
    unittest.main()
