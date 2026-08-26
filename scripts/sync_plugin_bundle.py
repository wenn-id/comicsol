from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "skills/comic-sol"
HOST_SPECIFIC_REFERENCES = {"capability-detection.md", "image-provider-setup.md"}
SYNCHRONIZED_REFERENCES = (
    "creative-direction.md",
    "safety-ip.md",
    "schemas.md",
    "starter-templates.md",
    "visual-qa.md",
    "workflow.md",
)
BUNDLED_TEMPLATES = (
    "character-bible.json",
    "character-identity-pack.json",
    "manifest.json",
    "page-qa.json",
    "panel-record.json",
    "qa-report.md.tmpl",
    "story-plan.json",
    "storyboard.json",
)
STARTER_IDS = (
    "minimal-one-page",
    "dialogue-two-page",
    "action-focused",
)
STARTER_FILES = (
    "source/input.txt",
    "source/request.json",
    "plan/story-plan.json",
    "plan/character-bible.json",
    "plan/storyboard.json",
)
BUNDLED_FONTS = (
    "ComicNeue-Bold.ttf",
    "ComicNeue-Regular.ttf",
    "NotoSans-Regular.ttf",
    "OFL-ComicNeue.txt",
    "OFL-NotoSans.txt",
)
BUNDLED_SCRIPTS = (
    "__init__.py",
    "character_identity.py",
    "character_quality.py",
    "comic_sol.py",
    "compose_pages.py",
    "command_service.py",
    "core_primitives.py",
    "export_pdf.py",
    "font_cmap.py",
    "font_coverage.py",
    "handoff.py",
    "handoff_archive.py",
    "input_limits.py",
    "layouts.py",
    "lifecycle_contracts.py",
    "letter_panels.py",
    "normalize_panels.py",
    "page_quality.py",
    "pdf_quality.py",
    "project_io.py",
    "quality_records.py",
    "quality_sample.py",
    "raster_limits.py",
    "reference_strategy.py",
    "render_report.py",
    "repair_strategy.py",
    "schema.py",
    "sfx_repair.py",
    "sfx_verification.py",
    "stage_registry.py",
    "starter_templates.py",
    "typography.py",
    "validate_project.py",
)
MANAGED_DIRECTORIES = ("references", "templates", "assets/fonts", "scripts")


def synchronized_paths() -> list[Path]:
    return [
        Path("SKILL.md"),
        *(Path("references") / name for name in SYNCHRONIZED_REFERENCES),
        *(Path("templates") / name for name in BUNDLED_TEMPLATES),
        *(
            Path("templates/starters/v1") / starter_id / relative
            for starter_id in STARTER_IDS
            for relative in STARTER_FILES
        ),
        *(Path("assets/fonts") / name for name in BUNDLED_FONTS),
        *(Path("scripts") / name for name in BUNDLED_SCRIPTS),
    ]


def expected_bundle_paths() -> set[Path]:
    return set(synchronized_paths()) | {
        Path("references") / name for name in HOST_SPECIFIC_REFERENCES
    }


def actual_bundle_paths() -> set[Path]:
    paths = {Path("SKILL.md")} if (BUNDLE / "SKILL.md").is_file() else set()
    for directory in MANAGED_DIRECTORIES:
        root = BUNDLE / directory
        if root.is_dir():
            paths.update(
                path.relative_to(BUNDLE)
                for path in root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
            )
    return paths


def destination(relative: Path) -> Path:
    return BUNDLE / relative


def check() -> list[Path]:
    drift = {
        relative
        for relative in synchronized_paths()
        if not (ROOT / relative).is_file()
        or not destination(relative).is_file()
        or (ROOT / relative).read_bytes() != destination(relative).read_bytes()
    }
    drift.update(expected_bundle_paths() ^ actual_bundle_paths())
    return sorted(drift, key=Path.as_posix)


def sync() -> None:
    for relative in synchronized_paths():
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(f"canonical bundle source is missing: {relative.as_posix()}")
        target = destination(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for relative in sorted(actual_bundle_paths() - expected_bundle_paths(), key=Path.as_posix):
        destination(relative).unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.check:
        drift = check()
        if drift:
            print("plugin bundle drift: " + ", ".join(path.as_posix() for path in drift))
            return 1
        return 0
    sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
