"""Files supplied to the packaged Skill from their single runtime copy."""

SKILL_SHARED_FILES = frozenset(
    {
        "assets/fonts/ComicNeue-Bold.ttf",
        "assets/fonts/ComicNeue-Regular.ttf",
        "assets/fonts/NotoSans-Regular.ttf",
        "assets/fonts/OFL-ComicNeue.txt",
        "assets/fonts/OFL-NotoSans.txt",
        "scripts/character_identity.py",
        "scripts/character_quality.py",
        "scripts/comic_sol.py",
        "scripts/command_service.py",
        "scripts/compose_pages.py",
        "scripts/core_primitives.py",
        "scripts/dogfood_report.py",
        "scripts/export_pdf.py",
        "scripts/font_cmap.py",
        "scripts/font_coverage.py",
        "scripts/handoff.py",
        "scripts/handoff_archive.py",
        "scripts/input_limits.py",
        "scripts/layouts.py",
        "scripts/letter_panels.py",
        "scripts/lifecycle_contracts.py",
        "scripts/normalize_panels.py",
        "scripts/page_quality.py",
        "scripts/pdf_quality.py",
        "scripts/project_io.py",
        "scripts/quality_records.py",
        "scripts/quality_sample.py",
        "scripts/raster_limits.py",
        "scripts/reference_strategy.py",
        "scripts/render_report.py",
        "scripts/repair_strategy.py",
        "scripts/schema.py",
        "scripts/sfx_repair.py",
        "scripts/sfx_verification.py",
        "scripts/stage_registry.py",
        "scripts/starter_templates.py",
        "scripts/typography.py",
        "scripts/validate_project.py",
        "scripts/version.py",
    }
)


def shared_package_path(relative: str) -> str:
    """Return the package member that supplies one shared Skill file."""
    if relative not in SKILL_SHARED_FILES:
        raise ValueError(f"unrecognized shared Skill member: {relative}")
    if relative.startswith("scripts/"):
        return "engine/" + relative.removeprefix("scripts/")
    if relative.startswith("assets/fonts/"):
        return relative
    raise ValueError(f"unrecognized shared Skill member: {relative}")
