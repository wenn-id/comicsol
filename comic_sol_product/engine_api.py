"""Stable engine symbols for external integrations such as Comic Sol Studio."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENGINE_EXPORTS: dict[str, tuple[str, str]] = {
    "ARCHIVE_SUFFIX": ("handoff_archive", "ARCHIVE_SUFFIX"),
    "HandoffResultError": ("handoff", "HandoffResultError"),
    "MAX_ENCODED_RASTER_BYTES": ("raster_limits", "MAX_ENCODED_RASTER_BYTES"),
    "MAX_SOURCE_BYTES": ("project_io", "MAX_SOURCE_BYTES"),
    "MAX_WARNING_CHARS": ("input_limits", "MAX_WARNING_CHARS"),
    "NormalizationSpec": ("normalize_panels", "NormalizationSpec"),
    "PANEL_CHECK_IDS": ("core_primitives", "PANEL_CHECK_IDS"),
    "PANEL_ID_PATTERN": ("core_primitives", "PANEL_ID_PATTERN"),
    "ProjectLock": ("project_io", "ProjectLock"),
    "ProjectTransaction": ("project_io", "ProjectTransaction"),
    "REFERENCE_PLAN_PATH": ("character_quality", "REFERENCE_PLAN_PATH"),
    "SUBJECTIVE_PAGE_CHECK_IDS": ("page_quality", "SUBJECTIVE_PAGE_CHECK_IDS"),
    "WARNING_LIMIT_MESSAGE": ("input_limits", "WARNING_LIMIT_MESSAGE"),
    "accepted_panel_problem": ("comic_sol", "_accepted_panel_problem"),
    "accept_handoff_result": ("comic_sol", "accept_handoff_result"),
    "attempt_id": ("handoff", "attempt_id"),
    "build_character_identity_check": ("character_quality", "build_character_identity_check"),
    "build_resume_plan": ("comic_sol", "build_resume_plan"),
    "canonical_artifact_bytes": ("core_primitives", "canonical_artifact_bytes"),
    "character_consistency_context": ("character_quality", "character_consistency_context"),
    "cleanup_owned_directory": ("project_io", "cleanup_owned_directory"),
    "compose_project": ("compose_pages", "compose_project"),
    "contained_project_path": ("project_io", "contained_project_path"),
    "export_handoff_archive": ("handoff_archive", "export_handoff_archive"),
    "finalize_project": ("comic_sol", "finalize_project"),
    "generation_job_sha256": ("handoff", "generation_job_sha256"),
    "guarded_export": ("export_pdf", "guarded_export"),
    "import_handoff_archive": ("handoff_archive", "import_handoff_archive"),
    "init_project": ("comic_sol", "init_project"),
    "inspect_handoff": ("comic_sol", "inspect_handoff"),
    "invalidate_from": ("comic_sol", "invalidate_from"),
    "invalidate_from_locked": ("comic_sol", "_invalidate_from_locked"),
    "is_excluded_project_path": ("handoff_archive", "_is_excluded_project_path"),
    "is_generated_sfx": ("comic_sol", "is_generated_sfx"),
    "iter_project_entries": ("handoff_archive", "_iter_project_entries"),
    "letter_project": ("letter_panels", "letter_project"),
    "loads_bounded_json": ("input_limits", "loads_bounded_json"),
    "plan_and_write_repair_plan": ("repair_strategy", "plan_and_write_repair_plan"),
    "prepare_handoff": ("comic_sol", "prepare_handoff"),
    "promote_attempt": ("comic_sol", "promote_attempt"),
    "publish_page_quality_record": ("page_quality", "publish_page_quality_record"),
    "read_contained_bytes": ("project_io", "read_contained_bytes"),
    "read_contained_json": ("project_io", "read_contained_json"),
    "read_project_manifest": ("schema", "read_project_manifest"),
    "record_stage": ("comic_sol", "record_stage"),
    "references_ready": ("comic_sol", "_references_ready"),
    "storyboard_panels": ("comic_sol", "_storyboard_panels"),
    "transition": ("comic_sol", "transition"),
    "utc_now": ("comic_sol", "_utc_now"),
    "validate_character_bible": ("validate_project", "validate_character_bible"),
    "validate_character_identity_check": (
        "character_quality",
        "validate_character_identity_check",
    ),
    "validate_character_quality_provenance": (
        "character_quality",
        "validate_character_quality_provenance",
    ),
    "validate_defect_regions": ("repair_strategy", "validate_defect_regions"),
    "validate_generation_receipt": ("handoff", "validate_generation_receipt"),
    "validate_handoff_raster": ("comic_sol", "_validate_handoff_raster"),
    "validate_identity_pack": ("character_identity", "validate_identity_pack"),
    "validate_narrative": ("input_limits", "validate_narrative"),
    "validate_page_quality": ("page_quality", "validate_page_quality"),
    "validate_panel_provenance": ("validate_project", "validate_panel_provenance"),
    "validate_panel_record": ("validate_project", "validate_panel_record"),
    "validate_project": ("validate_project", "validate_project"),
    "validate_quality_checks": ("quality_records", "validate_quality_checks"),
    "validate_story_plan": ("validate_project", "validate_story_plan"),
    "validate_storyboard": ("validate_project", "validate_storyboard"),
}


@dataclass(frozen=True)
class PreparedNormalization:
    """Preflighted panel artifacts that a caller can stage in its own transaction."""

    spec: Any
    clean_bytes: bytes
    record_bytes: bytes


def _engine_module(name: str) -> Any:
    from .cli import _load_engine_module

    return _load_engine_module(name)


def prepare_normalization(project_dir: Path, spec: Any) -> PreparedNormalization:
    """Prepare canonical panel bytes without publishing files to the project."""
    prepared = _engine_module("normalize_panels")._prepare(Path(project_dir), spec)
    return PreparedNormalization(prepared.spec, prepared.clean_bytes, prepared.record_bytes)


__all__ = tuple(sorted((*_ENGINE_EXPORTS, "PreparedNormalization", "prepare_normalization")))


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _ENGINE_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(_engine_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
