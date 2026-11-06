"""Canonical inventory of scripts included in the installed runtime."""

RUNTIME_SCRIPTS = frozenset(
    {
        "character_identity.py",
        "character_quality.py",
        "comic_sol.py",
        "command_service.py",
        "compose_pages.py",
        "core_primitives.py",
        "dogfood_report.py",
        "export_pdf.py",
        "font_cmap.py",
        "font_coverage.py",
        "handoff.py",
        "handoff_archive.py",
        "input_limits.py",
        "layouts.py",
        "letter_panels.py",
        "lifecycle_contracts.py",
        "mcp_server.py",
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
        "version.py",
    }
)

REQUIRED_RUNTIME_SCRIPTS = RUNTIME_SCRIPTS
