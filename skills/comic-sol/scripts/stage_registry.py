"""Single source of truth for Comic Sol resume-stage metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class StageDefinition:
    """Lifecycle metadata shared by resume, invalidation, and CLI surfaces."""

    name: str
    invalidation_status: str
    completion_status: str
    next_action: str
    runner: str | None = None
    material_kind: str = "none"
    output_kind: str = "none"
    stale_action: Literal["regenerate", "rerun"] = "rerun"
    artifacts: tuple[str, ...] = ()


STAGE_REGISTRY: tuple[StageDefinition, ...] = (
    StageDefinition(
        name="planning",
        invalidation_status="INIT",
        completion_status="SCRIPTED",
        next_action="agent",
        runner="agent",
        material_kind="planning",
        output_kind="planning",
        artifacts=("story_plan", "character_bible"),
    ),
    StageDefinition(
        name="storyboard",
        invalidation_status="SCRIPTED",
        completion_status="STORYBOARDED",
        next_action="agent",
        runner="agent",
        material_kind="storyboard",
        output_kind="storyboard",
        artifacts=("storyboard",),
    ),
    StageDefinition(
        name="generation",
        invalidation_status="REFERENCES_READY",
        completion_status="QA_READY",
        next_action="agent",
        runner="agent",
        material_kind="generation",
        output_kind="generation",
        stale_action="regenerate",
    ),
    StageDefinition(
        name="lettering",
        invalidation_status="QA_READY",
        completion_status="LETTERED",
        next_action="command",
        runner="scripts/letter_panels.py",
        material_kind="lettering",
        output_kind="lettering",
    ),
    StageDefinition(
        name="composition",
        invalidation_status="LETTERED",
        completion_status="COMPOSED",
        next_action="command",
        runner="scripts/compose_pages.py",
        material_kind="composition",
        output_kind="composition",
        artifacts=("composition_cache",),
    ),
    StageDefinition(
        name="export",
        invalidation_status="COMPOSED",
        completion_status="EXPORTED",
        next_action="command",
        runner="scripts/export_pdf.py",
        material_kind="export",
        output_kind="export",
        artifacts=("qa_report", "pdf", "pdf_verification"),
    ),
)

RESUME_STAGES = tuple(definition.name for definition in STAGE_REGISTRY)
STAGE_INVALIDATION_STATUS = {
    definition.name: definition.invalidation_status for definition in STAGE_REGISTRY
}
STAGE_COMPLETION_STATUS = {
    definition.name: definition.completion_status for definition in STAGE_REGISTRY
}
ARTIFACT_STAGE = {
    artifact: definition.name for definition in STAGE_REGISTRY for artifact in definition.artifacts
}

_DEFINITIONS_BY_NAME = {definition.name: definition for definition in STAGE_REGISTRY}


def get_stage(name: str) -> StageDefinition:
    """Return the registered stage definition or raise a clear error."""
    try:
        return _DEFINITIONS_BY_NAME[name]
    except KeyError as error:
        raise ValueError(f"unknown resume stage: {name}") from error
