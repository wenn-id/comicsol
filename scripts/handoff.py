"""Versioned, provider-neutral contracts for portable generation handoff.

This module defines data contracts and deterministic helpers only. It does not
select a provider, invoke an executor, read credentials, or perform network I/O.
Lifecycle commands and result intake belong to later work packages.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from .core_primitives import canonical_artifact_bytes, canonical_json_bytes
from .input_limits import MAX_JSON_BYTES, looks_like_secret, loads_bounded_json
from .lifecycle_contracts import ALL_STATUSES, CATEGORY
from .project_io import (
    MAX_READ_BYTES,
    ProjectLock,
    contained_project_path,
    normalized_project_relative_path,
    read_contained_bytes,
)

HANDOFF_CONTRACT_VERSION = "1.0"
BATCHES_PATH = "generation/batches.json"
HANDOFF_MANIFEST_PATH = "handoff/manifest.json"
LOCKED_SCOPE_FIXED_PATHS = (
    "generation/batches.json",
    "logs/reference-selection.json",
    "plan/character-bible.json",
    "plan/story-plan.json",
    "plan/storyboard.json",
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_PANEL_ID_PATTERN = re.compile(r"^p[0-9]{2}-[0-9]{2}$")
_ASPECT_RATIO_PATTERN = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_ATTEMPT_KINDS = frozenset({"initial", "visual_retry", "transient_repeat"})
_EXECUTOR_KINDS = frozenset({"native-tool", "external-tool"})
_JOB_STATUSES = frozenset({"missing", "ready", "completed", "failed", "stale"})


class HandoffContractError(ValueError):
    """Raised when a handoff object cannot satisfy its exact versioned schema."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(sorted(set(issues)))
        super().__init__("invalid handoff contract: " + "; ".join(self.issues))


class StaleLockedScopeError(HandoffContractError):
    """Raised when returned work no longer belongs to the locked project scope."""


def _object(
    value: object,
    *,
    allowed: set[str],
    required: set[str],
    field: str,
    issues: list[str],
) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        issues.append(f"{field or 'root'} must be an object")
        return None
    for key in sorted(set(value) - allowed):
        issues.append(f"{field + '.' if field else ''}{key}: unknown field")
    for key in sorted(required - set(value)):
        issues.append(f"{field + '.' if field else ''}{key}: required field is missing")
    return value


def _schema_version(value: object, field: str, issues: list[str]) -> None:
    if value != HANDOFF_CONTRACT_VERSION:
        issues.append(f"{field}: must equal {HANDOFF_CONTRACT_VERSION}")


def _identifier(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        issues.append(f"{field}: must be a stable lowercase identifier")


def _sha256(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        issues.append(f"{field}: must be a lowercase 64-character SHA-256")


def _relative_path(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, str) or not value:
        issues.append(f"{field}: must be a relative project path")
        return
    try:
        normalized_project_relative_path(value)
    except ValueError:
        issues.append(f"{field}: must be a normalized relative project path without traversal")


def _reference_raster_path(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, str):
        return
    parts = value.split("/")
    if (
        len(parts) != 3
        or parts[0] != "references"
        or parts[1] not in {"characters", "scenes"}
        or not parts[2].endswith(".png")
        or _IDENTIFIER_PATTERN.fullmatch(parts[2].removesuffix(".png")) is None
    ):
        issues.append(f"{field}: must name a canonical local character or scene PNG reference")


def _boolean(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, bool):
        issues.append(f"{field}: must be boolean")


def _safe_label(
    value: object,
    field: str,
    issues: list[str],
    *,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 128:
        issues.append(f"{field}: must be a non-empty string of at most 128 characters")
        return
    if any(ord(character) < 32 for character in value):
        issues.append(f"{field}: must not contain control characters")
    if looks_like_secret(value):
        issues.append(f"{field}: must not contain secrets or credentials")
    if value.startswith(("/", "\\", "~")) or _DRIVE_PATTERN.match(value):
        issues.append(f"{field}: must not contain a private absolute path")
    try:
        parsed = urlsplit(value)
    except ValueError:
        issues.append(f"{field}: must not contain a malformed URL")
        return
    if parsed.scheme:
        if parsed.username is not None or parsed.password is not None:
            issues.append(f"{field}: must not contain URL credentials")
        else:
            issues.append(f"{field}: must not contain an endpoint URL")


def _require_valid(issues: Sequence[str]) -> None:
    if issues:
        raise HandoffContractError(issues)


def _job_identity(job: Mapping[str, object]) -> dict[str, object]:
    return {
        key: job[key]
        for key in (
            "subject_kind",
            "subject_id",
            "prompt_path",
            "prompt_sha256",
            "references",
            "requested_dimensions",
            "requested_aspect_ratio",
            "attempt_kind",
            "retry_limit",
            "batch_id",
            "target_path",
        )
        if key in job
    }


def _derived_job_id(job: Mapping[str, object]) -> str:
    preimage = {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "job": _job_identity(job),
    }
    return hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()


def _retained_raster_path(value: object, field: str, issues: list[str]) -> None:
    if not isinstance(value, str):
        return
    parts = value.split("/")
    if len(parts) != 4 or parts[1] != "attempts":
        issues.append(f"{field}: must name a retained raster attempt")
        return
    namespace, _, subject_id, filename = parts
    if namespace == "panels":
        valid_subject = _PANEL_ID_PATTERN.fullmatch(subject_id) is not None
    elif namespace == "references":
        valid_subject = _IDENTIFIER_PATTERN.fullmatch(subject_id) is not None
    else:
        valid_subject = False
    filename_pattern = re.compile(
        rf"^(?:{'|'.join(map(re.escape, sorted(_ATTEMPT_KINDS)))})-"
        r"0*[1-9][0-9]*\.(?:png|jpg|webp)$"
    )
    if not valid_subject or filename_pattern.fullmatch(filename) is None:
        issues.append(f"{field}: must name a retained raster attempt")


def _attempt_target(root: Mapping[str, object], issues: list[str]) -> None:
    target = root.get("target_path")
    subject_kind = root.get("subject_kind")
    subject_id = root.get("subject_id")
    attempt_kind = root.get("attempt_kind")
    if not all(
        isinstance(value, str) for value in (target, subject_kind, subject_id, attempt_kind)
    ):
        return
    if subject_kind == "panel":
        prefix = f"panels/attempts/{subject_id}/"
    elif subject_kind == "reference":
        prefix = f"references/attempts/{subject_id}/"
    else:
        return
    filename = target.removeprefix(prefix)
    expected = re.compile(rf"^{re.escape(attempt_kind)}-0*[1-9][0-9]*\.(?:png|jpg|webp)$")
    if not target.startswith(prefix) or "/" in filename or expected.fullmatch(filename) is None:
        issues.append("target_path: must name the subject's retained raster attempt namespace")


def validate_generation_batches(value: object) -> list[str]:
    """Validate ``generation/batches.json`` against contract version 1.0."""
    issues: list[str] = []
    root = _object(
        value,
        allowed={"schema_version", "batches"},
        required={"schema_version", "batches"},
        field="",
        issues=issues,
    )
    if root is None:
        return sorted(issues)
    _schema_version(root.get("schema_version"), "schema_version", issues)
    batches = root.get("batches")
    if not isinstance(batches, list):
        issues.append("batches: must be an array")
        return sorted(issues)
    batch_ids: list[str] = []
    all_job_ids: list[str] = []
    for index, value_item in enumerate(batches):
        field = f"batches[{index}]"
        item = _object(
            value_item,
            allowed={"batch_id", "kind", "job_ids"},
            required={"batch_id", "kind", "job_ids"},
            field=field,
            issues=issues,
        )
        if item is None:
            continue
        _identifier(item.get("batch_id"), f"{field}.batch_id", issues)
        if isinstance(item.get("batch_id"), str):
            batch_ids.append(item["batch_id"])
        if item.get("kind") not in {"reference", "panel"}:
            issues.append(f"{field}.kind: must be reference or panel")
        job_ids = item.get("job_ids")
        if not isinstance(job_ids, list) or not job_ids:
            issues.append(f"{field}.job_ids: must be a non-empty array")
            continue
        for job_index, job_id in enumerate(job_ids):
            _sha256(job_id, f"{field}.job_ids[{job_index}]", issues)
            if isinstance(job_id, str):
                all_job_ids.append(job_id)
        if len(job_ids) != len(set(map(str, job_ids))):
            issues.append(f"{field}.job_ids: job IDs must be unique")
    if len(batch_ids) != len(set(batch_ids)):
        issues.append("batches: batch IDs must be unique")
    if len(all_job_ids) != len(set(all_job_ids)):
        issues.append("batches: a job ID may belong to only one batch")
    return sorted(issues)


def build_generation_batches(batches: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Build the canonical in-memory batch map without publishing it."""
    record: dict[str, object] = {
        "batches": [dict(batch) for batch in batches],
        "schema_version": HANDOFF_CONTRACT_VERSION,
    }
    _require_valid(validate_generation_batches(record))
    return record


def validate_generation_job(value: object) -> list[str]:
    """Validate one deterministic generation job contract."""
    issues: list[str] = []
    fields = {
        "schema_version",
        "job_id",
        "subject_kind",
        "subject_id",
        "prompt_path",
        "prompt_sha256",
        "references",
        "requested_dimensions",
        "requested_aspect_ratio",
        "attempt_kind",
        "retry_limit",
        "batch_id",
        "target_path",
    }
    root = _object(value, allowed=fields, required=fields, field="", issues=issues)
    if root is None:
        return sorted(issues)
    _schema_version(root.get("schema_version"), "schema_version", issues)
    _sha256(root.get("job_id"), "job_id", issues)
    kind = root.get("subject_kind")
    if kind not in {"reference", "panel"}:
        issues.append("subject_kind: must be reference or panel")
    subject_id = root.get("subject_id")
    if kind == "panel":
        if not isinstance(subject_id, str) or _PANEL_ID_PATTERN.fullmatch(subject_id) is None:
            issues.append("subject_id: panel jobs must use a pNN-NN panel ID")
    else:
        _identifier(subject_id, "subject_id", issues)
    prompt_path = root.get("prompt_path")
    _relative_path(prompt_path, "prompt_path", issues)
    if isinstance(prompt_path, str) and isinstance(subject_id, str):
        if kind == "panel":
            expected_prompt_path = f"prompts/panels/{subject_id}.txt"
        elif kind == "reference":
            expected_prompt_path = f"prompts/references/{subject_id}.txt"
        else:
            expected_prompt_path = None
        if expected_prompt_path is not None and prompt_path != expected_prompt_path:
            issues.append(f"prompt_path: must equal {expected_prompt_path}")
    _sha256(root.get("prompt_sha256"), "prompt_sha256", issues)
    references = root.get("references")
    reference_paths: list[str] = []
    if not isinstance(references, list):
        issues.append("references: must be an array")
    else:
        for index, value_item in enumerate(references):
            field = f"references[{index}]"
            item = _object(
                value_item,
                allowed={"path", "sha256"},
                required={"path", "sha256"},
                field=field,
                issues=issues,
            )
            if item is None:
                continue
            _relative_path(item.get("path"), f"{field}.path", issues)
            _reference_raster_path(item.get("path"), f"{field}.path", issues)
            if isinstance(item.get("path"), str):
                reference_paths.append(item["path"])
            _sha256(item.get("sha256"), f"{field}.sha256", issues)
        if len(reference_paths) != len(set(reference_paths)):
            issues.append("references: paths must be unique")
    dimensions = root.get("requested_dimensions")
    if dimensions is not None:
        item = _object(
            dimensions,
            allowed={"width", "height"},
            required={"width", "height"},
            field="requested_dimensions",
            issues=issues,
        )
        if item is not None:
            for name in ("width", "height"):
                number = item.get(name)
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    issues.append(f"requested_dimensions.{name}: must be a positive integer")
    aspect_ratio = root.get("requested_aspect_ratio")
    if aspect_ratio is not None and (
        not isinstance(aspect_ratio, str) or _ASPECT_RATIO_PATTERN.fullmatch(aspect_ratio) is None
    ):
        issues.append("requested_aspect_ratio: must be null or a positive W:H ratio")
    if (
        isinstance(dimensions, Mapping)
        and isinstance(aspect_ratio, str)
        and _ASPECT_RATIO_PATTERN.fullmatch(aspect_ratio) is not None
    ):
        width = dimensions.get("width")
        height = dimensions.get("height")
        if (
            not isinstance(width, bool)
            and isinstance(width, int)
            and width > 0
            and not isinstance(height, bool)
            and isinstance(height, int)
            and height > 0
        ):
            try:
                ratio_width, ratio_height = map(int, aspect_ratio.split(":"))
            except ValueError:
                issues.append("requested_aspect_ratio: components are too large")
            else:
                if width * ratio_height != height * ratio_width:
                    issues.append(
                        "requested_aspect_ratio: must match requested_dimensions width:height"
                    )
    if root.get("attempt_kind") not in _ATTEMPT_KINDS:
        issues.append("attempt_kind: unknown retained-attempt kind")
    retry_limit = root.get("retry_limit")
    if (
        isinstance(retry_limit, bool)
        or not isinstance(retry_limit, int)
        or not 0 <= retry_limit <= 2
    ):
        issues.append("retry_limit: must be an integer from 0 to 2")
    _identifier(root.get("batch_id"), "batch_id", issues)
    _relative_path(root.get("target_path"), "target_path", issues)
    _attempt_target(root, issues)
    if not issues and root.get("job_id") != _derived_job_id(root):
        issues.append("job_id: does not match canonical job inputs")
    return sorted(issues)


def build_generation_job(
    *,
    subject_kind: str,
    subject_id: str,
    prompt_path: str,
    prompt_sha256: str,
    references: Sequence[Mapping[str, object]],
    requested_dimensions: Mapping[str, object] | None,
    requested_aspect_ratio: str | None,
    attempt_kind: str,
    retry_limit: int,
    batch_id: str,
    target_path: str,
) -> dict[str, object]:
    """Build one job and derive its full SHA-256 ID from canonical inputs."""
    record: dict[str, object] = {
        "attempt_kind": attempt_kind,
        "batch_id": batch_id,
        "prompt_path": prompt_path,
        "prompt_sha256": prompt_sha256,
        "references": [dict(reference) for reference in references],
        "requested_aspect_ratio": requested_aspect_ratio,
        "requested_dimensions": (
            None if requested_dimensions is None else dict(requested_dimensions)
        ),
        "retry_limit": retry_limit,
        "schema_version": HANDOFF_CONTRACT_VERSION,
        "subject_id": subject_id,
        "subject_kind": subject_kind,
        "target_path": target_path,
    }
    record["job_id"] = _derived_job_id(record)
    _require_valid(validate_generation_job(record))
    return record


def generation_job_sha256(job: Mapping[str, object]) -> str:
    """Return the canonical artifact digest for a valid generation job."""
    _require_valid(validate_generation_job(job))
    return hashlib.sha256(canonical_artifact_bytes(job)).hexdigest()


def validate_generation_receipt(value: object) -> list[str]:
    """Validate one sanitized generation result receipt."""
    issues: list[str] = []
    fields = {
        "schema_version",
        "attempt_id",
        "job_id",
        "job_sha256",
        "raster_path",
        "raster_sha256",
        "executor_kind",
        "executor_id",
        "provider",
        "model",
        "capabilities_used",
        "outcome",
        "category",
    }
    root = _object(value, allowed=fields, required=fields, field="", issues=issues)
    if root is None:
        return sorted(issues)
    _schema_version(root.get("schema_version"), "schema_version", issues)
    _identifier(root.get("attempt_id"), "attempt_id", issues)
    _sha256(root.get("job_id"), "job_id", issues)
    _sha256(root.get("job_sha256"), "job_sha256", issues)
    _relative_path(root.get("raster_path"), "raster_path", issues)
    _retained_raster_path(root.get("raster_path"), "raster_path", issues)
    _sha256(root.get("raster_sha256"), "raster_sha256", issues)
    if root.get("executor_kind") not in _EXECUTOR_KINDS:
        issues.append("executor_kind: must be native-tool or external-tool")
    _identifier(root.get("executor_id"), "executor_id", issues)
    _safe_label(root.get("provider"), "provider", issues, nullable=True)
    _safe_label(root.get("model"), "model", issues, nullable=True)
    capabilities = _object(
        root.get("capabilities_used"),
        allowed={"reference_images", "dimensions", "localized_edit"},
        required={"reference_images", "dimensions", "localized_edit"},
        field="capabilities_used",
        issues=issues,
    )
    if capabilities is not None:
        for name in ("reference_images", "dimensions", "localized_edit"):
            _boolean(capabilities.get(name), f"capabilities_used.{name}", issues)
    if root.get("outcome") not in {"success", "failure"}:
        issues.append("outcome: must be success or failure")
    category = root.get("category")
    if not isinstance(category, str) or CATEGORY.fullmatch(category) is None:
        issues.append("category: must be a stable sanitized category")
    elif looks_like_secret(category):
        issues.append("category: must not contain secrets or credentials")
    return sorted(issues)


def build_generation_receipt(
    *,
    attempt_id: str,
    job_id: str,
    job_sha256: str,
    raster_path: str,
    raster_sha256: str,
    executor_kind: str,
    executor_id: str,
    provider: str | None,
    model: str | None,
    capabilities_used: Mapping[str, object],
    outcome: str,
    category: str,
) -> dict[str, object]:
    """Build a sanitized receipt without retaining or promoting a raster."""
    record: dict[str, object] = {
        "attempt_id": attempt_id,
        "capabilities_used": dict(capabilities_used),
        "category": category,
        "executor_id": executor_id,
        "executor_kind": executor_kind,
        "job_id": job_id,
        "job_sha256": job_sha256,
        "model": model,
        "outcome": outcome,
        "provider": provider,
        "raster_path": raster_path,
        "raster_sha256": raster_sha256,
        "schema_version": HANDOFF_CONTRACT_VERSION,
    }
    _require_valid(validate_generation_receipt(record))
    return record


def validate_handoff_manifest(value: object) -> list[str]:
    """Validate ``handoff/manifest.json`` against contract version 1.0."""
    issues: list[str] = []
    fields = {
        "schema_version",
        "project_schema_version",
        "project_id",
        "stage",
        "locked_scope_sha256",
        "batches",
        "jobs",
        "required_artifacts",
    }
    root = _object(value, allowed=fields, required=fields, field="", issues=issues)
    if root is None:
        return sorted(issues)
    _schema_version(root.get("schema_version"), "schema_version", issues)
    if root.get("project_schema_version") != "1.1":
        issues.append("project_schema_version: must equal 1.1")
    _identifier(root.get("project_id"), "project_id", issues)
    if root.get("stage") not in ALL_STATUSES:
        issues.append("stage: unknown lifecycle stage")
    _sha256(root.get("locked_scope_sha256"), "locked_scope_sha256", issues)
    batches = _object(
        root.get("batches"),
        allowed={"path", "sha256"},
        required={"path", "sha256"},
        field="batches",
        issues=issues,
    )
    if batches is not None:
        _relative_path(batches.get("path"), "batches.path", issues)
        if batches.get("path") != BATCHES_PATH:
            issues.append(f"batches.path: must equal {BATCHES_PATH}")
        _sha256(batches.get("sha256"), "batches.sha256", issues)
    jobs = root.get("jobs")
    job_ids: list[str] = []
    if not isinstance(jobs, list):
        issues.append("jobs: must be an array")
    else:
        for index, value_item in enumerate(jobs):
            field = f"jobs[{index}]"
            item = _object(
                value_item,
                allowed={"job_id", "path", "sha256", "status"},
                required={"job_id", "path", "sha256", "status"},
                field=field,
                issues=issues,
            )
            if item is None:
                continue
            _sha256(item.get("job_id"), f"{field}.job_id", issues)
            _relative_path(item.get("path"), f"{field}.path", issues)
            if isinstance(item.get("job_id"), str):
                job_ids.append(item["job_id"])
                expected_path = f"generation/jobs/{item['job_id']}.json"
                if item.get("path") != expected_path:
                    issues.append(f"{field}.path: must equal {expected_path}")
            _sha256(item.get("sha256"), f"{field}.sha256", issues)
            if item.get("status") not in _JOB_STATUSES:
                issues.append(f"{field}.status: unknown generation job status")
        if len(job_ids) != len(set(job_ids)):
            issues.append("jobs: job IDs must be unique")
    required_artifacts = root.get("required_artifacts")
    artifact_paths: list[str] = []
    if not isinstance(required_artifacts, list):
        issues.append("required_artifacts: must be an array")
    else:
        for index, value_item in enumerate(required_artifacts):
            field = f"required_artifacts[{index}]"
            item = _object(
                value_item,
                allowed={"path", "sha256"},
                required={"path", "sha256"},
                field=field,
                issues=issues,
            )
            if item is None:
                continue
            _relative_path(item.get("path"), f"{field}.path", issues)
            if isinstance(item.get("path"), str):
                artifact_paths.append(item["path"])
            _sha256(item.get("sha256"), f"{field}.sha256", issues)
        if len(artifact_paths) != len(set(artifact_paths)):
            issues.append("required_artifacts: paths must be unique")
    return sorted(issues)


def build_handoff_manifest(
    *,
    project_id: str,
    project_schema_version: str,
    stage: str,
    locked_scope_sha256: str,
    batches_path: str,
    batches_sha256: str,
    jobs: Sequence[Mapping[str, object]],
    required_artifacts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build a deterministic handoff manifest without publishing it."""
    record: dict[str, object] = {
        "batches": {"path": batches_path, "sha256": batches_sha256},
        "jobs": sorted((dict(job) for job in jobs), key=lambda item: str(item.get("job_id", ""))),
        "locked_scope_sha256": locked_scope_sha256,
        "project_id": project_id,
        "project_schema_version": project_schema_version,
        "required_artifacts": sorted(
            (dict(artifact) for artifact in required_artifacts),
            key=lambda item: str(item.get("path", "")),
        ),
        "schema_version": HANDOFF_CONTRACT_VERSION,
        "stage": stage,
    }
    _require_valid(validate_handoff_manifest(record))
    return record


def _canonical_scope_bytes(project_dir: Path, relative: str) -> bytes:
    payload = read_contained_bytes(
        project_dir,
        relative,
        max_bytes=MAX_JSON_BYTES if relative.endswith(".json") else MAX_READ_BYTES,
    )
    if relative.endswith(".json"):
        value = loads_bounded_json(payload, source=relative)
        return canonical_json_bytes(value)
    return payload


def _authoritative_prompt_paths(project_dir: Path) -> set[str]:
    prompts: set[str] = set()
    for relative_dir in ("prompts/references", "prompts/panels"):
        directory = contained_project_path(project_dir, relative_dir, must_exist=True)
        if not directory.is_dir():
            raise HandoffContractError([f"{relative_dir}: must be a prompt directory"])
        for entry in sorted(directory.iterdir(), key=lambda path: path.name):
            relative = f"{relative_dir}/{entry.name}"
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".txt":
                raise HandoffContractError(
                    [f"{relative}: generation prompts must be regular .txt files"]
                )
            contained_project_path(project_dir, relative, must_exist=True)
            prompts.add(relative)
    return prompts


def _authoritative_reference_paths(project_dir: Path) -> set[str]:
    payload = read_contained_bytes(
        project_dir,
        "logs/reference-selection.json",
        max_bytes=MAX_JSON_BYTES,
    )
    value = loads_bounded_json(payload, source="logs/reference-selection.json")
    if not isinstance(value, Mapping) or not isinstance(value.get("panels"), list):
        raise HandoffContractError(["logs/reference-selection.json: panels must be an array"])
    references: set[str] = set()
    issues: list[str] = []
    for panel_index, panel_value in enumerate(value["panels"]):
        field = f"logs/reference-selection.json.panels[{panel_index}]"
        if not isinstance(panel_value, Mapping) or not isinstance(
            panel_value.get("selected"), list
        ):
            issues.append(f"{field}.selected: must be an array")
            continue
        for selection_index, selection_value in enumerate(panel_value["selected"]):
            selection_field = f"{field}.selected[{selection_index}].path"
            if not isinstance(selection_value, Mapping):
                issues.append(f"{selection_field}: required field is missing")
                continue
            path = selection_value.get("path")
            _relative_path(path, selection_field, issues)
            if not isinstance(path, str) or not path.startswith("references/"):
                issues.append(f"{selection_field}: must be beneath references/")
                continue
            references.add(path)
    _require_valid(issues)
    for relative in sorted(references):
        contained_project_path(project_dir, relative, must_exist=True)
    return references


def _complete_scope_paths(
    project_dir: Path,
    prompt_paths: Sequence[str],
    reference_paths: Sequence[str],
) -> set[str]:
    provided_prompts: list[str] = []
    provided_references: list[str] = []
    for relative in prompt_paths:
        issues: list[str] = []
        _relative_path(relative, "prompt_paths", issues)
        if not isinstance(relative, str) or not relative.startswith("prompts/"):
            issues.append("prompt_paths: entries must be beneath prompts/")
        _require_valid(issues)
        provided_prompts.append(relative)
    for relative in reference_paths:
        issues = []
        _relative_path(relative, "reference_paths", issues)
        if not isinstance(relative, str) or not relative.startswith("references/"):
            issues.append("reference_paths: entries must be beneath references/")
        _require_valid(issues)
        provided_references.append(relative)

    authoritative_prompts = _authoritative_prompt_paths(project_dir)
    if (
        len(provided_prompts) != len(set(provided_prompts))
        or set(provided_prompts) != authoritative_prompts
    ):
        raise HandoffContractError(
            ["prompt_paths: must contain all generation prompts exactly once"]
        )
    authoritative_references = _authoritative_reference_paths(project_dir)
    if (
        len(provided_references) != len(set(provided_references))
        or set(provided_references) != authoritative_references
    ):
        raise HandoffContractError(
            ["reference_paths: must contain all selected references exactly once"]
        )
    return set(LOCKED_SCOPE_FIXED_PATHS) | authoritative_prompts | authoritative_references


def _locked_scope_sha256(
    project_dir: Path,
    prompt_paths: Sequence[str],
    reference_paths: Sequence[str],
) -> str:
    relative_paths = _complete_scope_paths(project_dir, prompt_paths, reference_paths)
    files = []
    for relative in sorted(relative_paths):
        content = _canonical_scope_bytes(project_dir, relative)
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    preimage = {"contract_version": HANDOFF_CONTRACT_VERSION, "files": files}
    return hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()


def locked_scope_sha256(
    project_dir: Path,
    *,
    prompt_paths: Sequence[str],
    reference_paths: Sequence[str],
) -> str:
    """Hash the complete canonical locked scope in project-relative path order."""
    project_dir = Path(project_dir)
    with ProjectLock(project_dir):
        return _locked_scope_sha256(project_dir, prompt_paths, reference_paths)


def assert_locked_scope(
    project_dir: Path,
    expected_sha256: str,
    *,
    prompt_paths: Sequence[str],
    reference_paths: Sequence[str],
) -> None:
    """Reject a result whose expected scope digest no longer matches the project."""
    issues: list[str] = []
    _sha256(expected_sha256, "locked_scope_sha256", issues)
    _require_valid(issues)
    project_dir = Path(project_dir)
    with ProjectLock(project_dir):
        actual = _locked_scope_sha256(project_dir, prompt_paths, reference_paths)
        if not hmac.compare_digest(actual, expected_sha256):
            raise StaleLockedScopeError(["locked_scope_sha256: stale project scope"])


def rank_executors(
    declarations: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return compatible executor declarations in deterministic policy order."""
    fields = {
        "capability_id",
        "executor_kind",
        "text_to_image",
        "local_raster",
        "supports_reference_images",
        "supports_dimensions",
        "supports_localized_edit",
    }
    validated: list[dict[str, object]] = []
    issues: list[str] = []
    for index, declaration in enumerate(declarations):
        field = f"declarations[{index}]"
        item = _object(
            declaration,
            allowed=fields,
            required=fields,
            field=field,
            issues=issues,
        )
        if item is None:
            continue
        _identifier(item.get("capability_id"), f"{field}.capability_id", issues)
        if item.get("executor_kind") not in _EXECUTOR_KINDS:
            issues.append(f"{field}.executor_kind: must be native-tool or external-tool")
        for name in (
            "text_to_image",
            "local_raster",
            "supports_reference_images",
            "supports_dimensions",
            "supports_localized_edit",
        ):
            _boolean(item.get(name), f"{field}.{name}", issues)
        validated.append(dict(item))
    _require_valid(issues)
    compatible = [
        item for item in validated if item["text_to_image"] is True and item["local_raster"] is True
    ]
    return sorted(
        compatible,
        key=lambda item: (
            0 if item["executor_kind"] == "native-tool" else 1,
            -int(item["supports_reference_images"] is True),
            -int(item["supports_dimensions"] is True),
            -int(item["supports_localized_edit"] is True),
            str(item["capability_id"]),
        ),
    )
