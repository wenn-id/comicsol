"""Opt-in, deterministic, privacy-bounded dogfood report contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from .core_primitives import canonical_artifact_bytes
from .input_limits import MAX_JSON_BYTES, loads_bounded_json
from .project_io import (
    MAX_READ_BYTES,
    ProjectLock,
    contained_project_path,
    durable_external_write,
    read_bytes_nofollow,
    read_contained_bytes,
    read_contained_json,
)
from .schema import LEGACY_PROJECT_SCHEMA_VERSION, SUPPORTED_PROJECT_SCHEMA_VERSIONS
from .lifecycle_contracts import ALL_STATUSES
from .stage_registry import RESUME_STAGES


REPORT_KIND = "comic-sol-dogfood-report"
REPORT_SCHEMA_VERSION = "1.0"
MAX_MINUTES = 10_080
MAX_FAILED_RESUME_ATTEMPTS = 1_000
MAX_EVIDENCE_COUNT = 1_000_000
MAX_EVENT_LINES = 100_000
MAX_RECEIPTS = 10_000
STAGES = RESUME_STAGES
FRICTION_CATEGORIES = frozenset(
    {
        "installation",
        "setup",
        "first-project",
        "story-planning",
        "image-generation",
        "character-consistency",
        "handoff",
        "blocked-recovery",
        "resume",
        "lettering",
        "composition",
        "pdf-export",
        "filesystem",
        "documentation",
        "performance",
        "other",
    }
)
EXECUTOR_KINDS = frozenset({"native-tool", "external-tool", "handoff"})
HANDOFF_ROUTES = frozenset({"shared-folder", "archive"})
SAFE_BLOCKED_CATEGORIES = frozenset(
    {
        "artifact-missing",
        "executor-failure",
        "image-capability-unavailable",
        "input-invalid",
        "io-error",
        "permission-denied",
        "project-blocked",
        "provider-refusal",
        "quality-failure",
        "resource-limit",
        "validation-failure",
        "other",
    }
)
CAPABILITY_FLAGS = ("dimensions", "localized_edit", "reference_images")
CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
ALIAS = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
LIMITATIONS = (
    "Local opt-in report; no automatic upload.",
    "Derived only from persisted allowlisted project evidence.",
    "Successful resumes count committed project.resumed events; failed attempts are self-reported.",
    "Handoff route is omitted when persisted evidence cannot distinguish shared-folder from archive.",
)
ROOT_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "comic_sol_version",
        "project_schema_version",
        "derived",
        "creator",
        "consent",
        "limitations",
    }
)
DERIVED_FIELDS = frozenset(
    {
        "terminal_status",
        "page_count",
        "panel_count",
        "completed_stages",
        "generation_attempts",
        "retry_rate",
        "blocked_categories",
        "successful_resumes",
        "handoff_count",
        "handoff_routes",
        "handoff_completions",
        "executor_kinds",
        "executor_capabilities",
        "final_pdf_verified",
        "unresolved_warning_count",
        "manual_override_count",
    }
)
CREATOR_FIELDS = frozenset(
    {
        "setup_minutes",
        "first_project_minutes",
        "pdf_minutes",
        "manual_intervention",
        "would_use_again",
        "failed_resume_attempts",
        "friction_categories",
        "cohort_alias",
    }
)


class DogfoodReportError(ValueError):
    """Raised when local dogfood evidence violates its public contract."""


def _exact_object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DogfoodReportError(f"{name} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise DogfoodReportError(f"{name} fields are invalid: {'; '.join(details)}")
    return value


def _bounded_integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise DogfoodReportError(f"{name} must be an integer from 0 through {maximum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise DogfoodReportError(f"{name} must be a boolean")
    return value


def _optional_integer(value: object, name: str, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_integer(value, name, maximum)


def _optional_alias(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or ALIAS.fullmatch(value) is None:
        raise DogfoodReportError("cohort_alias must be a lowercase slug of at most 48 characters")
    return value


def _friction_categories(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise DogfoodReportError("friction_categories must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in FRICTION_CATEGORIES:
            raise DogfoodReportError("friction category is not in the bounded allowlist")
        if item not in result:
            result.append(item)
    if len(result) > len(FRICTION_CATEGORIES):
        raise DogfoodReportError("too many friction categories")
    return result


def validate_creator_inputs(
    *,
    setup_minutes: object,
    first_project_minutes: object,
    pdf_minutes: object,
    manual_intervention: object,
    would_use_again: object,
    failed_resume_attempts: object = None,
    friction_categories: object = (),
    cohort_alias: object = None,
) -> dict[str, object]:
    """Validate and normalize the complete self-reported allowlist."""
    return {
        "setup_minutes": _bounded_integer(setup_minutes, "setup_minutes", MAX_MINUTES),
        "first_project_minutes": _bounded_integer(
            first_project_minutes, "first_project_minutes", MAX_MINUTES
        ),
        "pdf_minutes": _bounded_integer(pdf_minutes, "pdf_minutes", MAX_MINUTES),
        "manual_intervention": _boolean(manual_intervention, "manual_intervention"),
        "would_use_again": _boolean(would_use_again, "would_use_again"),
        "failed_resume_attempts": _optional_integer(
            failed_resume_attempts,
            "failed_resume_attempts",
            MAX_FAILED_RESUME_ATTEMPTS,
        ),
        "friction_categories": _friction_categories(friction_categories),
        "cohort_alias": _optional_alias(cohort_alias),
    }


def _read_object(project_dir: Path, relative: str, *, optional: bool = False) -> dict[str, object]:
    try:
        value = read_contained_json(project_dir, relative)
    except FileNotFoundError:
        if optional:
            return {}
        raise DogfoodReportError(f"required dogfood evidence is missing: {relative}") from None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise DogfoodReportError(
            f"invalid bounded dogfood evidence: {Path(relative).name}"
        ) from error
    if not isinstance(value, dict):
        raise DogfoodReportError(f"dogfood evidence must be an object: {Path(relative).name}")
    return value


def _manifest(project_dir: Path) -> tuple[dict[str, object], str]:
    manifest = _read_object(project_dir, "project.json")
    version = manifest.get("schema_version", LEGACY_PROJECT_SCHEMA_VERSION)
    if version not in SUPPORTED_PROJECT_SCHEMA_VERSIONS:
        raise DogfoodReportError("project schema version is not readable by this release")
    # Validate the complete current-or-legacy manifest in memory. The validator
    # is read-only; reporting must never migrate or recover a project.
    from .validate_project import validate_manifest

    if validate_manifest(manifest):
        raise DogfoodReportError("project manifest evidence is invalid")
    return manifest, str(version)


def _manifest_counts(manifest: Mapping[str, object]) -> tuple[int, int]:
    settings = manifest.get("settings")
    if not isinstance(settings, dict):  # guarded by validate_manifest
        raise DogfoodReportError("project count evidence is invalid")
    page_count = settings.get("page_count")
    panel_count = settings.get("panel_count")
    return (
        _bounded_integer(page_count, "page count", MAX_EVIDENCE_COUNT),
        _bounded_integer(panel_count, "panel count", MAX_EVIDENCE_COUNT),
    )


def _generation_attempts(project_dir: Path) -> dict[str, int]:
    counters = _read_object(project_dir, "logs/generation-counters.json", optional=True)
    if not counters:
        return {"initial": 0, "retries": 0, "total": 0}
    if set(counters) != {"schema_version", "global_extra_calls", "panels"}:
        raise DogfoodReportError("generation counter evidence has invalid fields")
    if counters.get("schema_version") != "1.0":
        raise DogfoodReportError("generation counter schema is unsupported")
    panels = counters.get("panels")
    if not isinstance(panels, dict):
        raise DogfoodReportError("generation counter panels must be an object")
    if len(panels) > MAX_RECEIPTS:
        raise DogfoodReportError("generation counter collection exceeds the report bound")
    initial = 0
    retries = 0
    for counts in panels.values():
        if not isinstance(counts, dict) or set(counts) != {
            "initial",
            "transient_repeats",
            "visual_retries",
        }:
            raise DogfoodReportError("generation panel counters have invalid fields")
        initial += _bounded_integer(counts.get("initial"), "initial attempts", 1)
        retries += _bounded_integer(counts.get("transient_repeats"), "transient repeats", 1)
        retries += _bounded_integer(counts.get("visual_retries"), "visual retries", 2)
    global_extras = _bounded_integer(
        counters.get("global_extra_calls"), "global extra calls", MAX_EVIDENCE_COUNT
    )
    if global_extras != retries:
        raise DogfoodReportError("generation retry counters are inconsistent")
    total = initial + retries
    if total > MAX_EVIDENCE_COUNT:
        raise DogfoodReportError("generation attempt total exceeds the report bound")
    return {"initial": initial, "retries": retries, "total": total}


def _events(project_dir: Path) -> list[dict[str, object]]:
    try:
        payload = read_contained_bytes(project_dir, "logs/events.jsonl", max_bytes=MAX_READ_BYTES)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise DogfoodReportError("event evidence cannot be read safely") from error
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise DogfoodReportError("event evidence must be UTF-8") from error
    lines = text.splitlines()
    if len(lines) > MAX_EVENT_LINES:
        raise DogfoodReportError("event evidence exceeds the report line bound")
    events: list[dict[str, object]] = []
    for line in lines:
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise DogfoodReportError("event evidence contains invalid JSON") from error
        if not isinstance(value, dict):
            raise DogfoodReportError("event evidence must contain objects")
        events.append(value)
    return events


def _event_metrics(
    events: Sequence[Mapping[str, object]], manifest: Mapping[str, object]
) -> tuple[list[str], list[str], int, int, int, int]:
    blocked: set[str] = set()
    completed_stages: set[str] = set()
    successful_resumes = 0
    handoff_count = 0
    handoff_completions = 0
    override_count = 0

    def blocked_category(details: Mapping[str, object]) -> None:
        reason = details.get("blocked_reason")
        if isinstance(reason, str) and CATEGORY.fullmatch(reason):
            blocked.add(reason if reason in SAFE_BLOCKED_CATEGORIES else "other")

    for event in events:
        name = event.get("event")
        details = event.get("details")
        if not isinstance(name, str) or not isinstance(details, dict):
            continue
        if name == "project.transitioned" and str(details.get("to", "")).upper() == "BLOCKED":
            blocked_category(details)
        elif name == "project.resumed":
            if (
                str(details.get("from", "")).upper() == "BLOCKED"
                and details.get("to") in ALL_STATUSES
                and str(details.get("to", "")).upper() != "BLOCKED"
                and isinstance(details.get("blocked_reason"), str)
                and CATEGORY.fullmatch(str(details["blocked_reason"]))
            ):
                successful_resumes += 1
                blocked_category(details)
        elif name == "stage.recorded" and details.get("action") in STAGES:
            completed_stages.add(str(details["action"]))
        elif name == "handoff.prepared":
            handoff_count += _bounded_integer(
                details.get("count"), "prepared handoff count", MAX_EVIDENCE_COUNT
            )
        elif name == "handoff.result-accepted":
            handoff_completions += 1
        elif name == "panel.overridden":
            override_count += 1
    if manifest.get("status") == "BLOCKED":
        reason = manifest.get("blocked_reason")
        if isinstance(reason, str) and CATEGORY.fullmatch(reason):
            blocked.add(reason if reason in SAFE_BLOCKED_CATEGORIES else "other")
    for count, name in (
        (successful_resumes, "successful resume count"),
        (handoff_count, "handoff count"),
        (handoff_completions, "handoff completion count"),
        (override_count, "manual override count"),
    ):
        _bounded_integer(count, name, MAX_EVIDENCE_COUNT)
    return (
        sorted(blocked),
        [stage for stage in STAGES if stage in completed_stages],
        successful_resumes,
        handoff_count,
        handoff_completions,
        override_count,
    )


def _receipt_metrics(project_dir: Path) -> tuple[list[str], dict[str, bool]]:
    receipts_root = contained_project_path(project_dir, "generation/receipts")
    executor_kinds: set[str] = set()
    capabilities = {name: False for name in CAPABILITY_FLAGS}
    if not receipts_root.exists():
        return [], capabilities
    contained_project_path(project_dir, "generation/receipts", must_exist=True)
    entries = sorted(receipts_root.iterdir(), key=lambda path: path.name)
    if len(entries) > MAX_RECEIPTS:
        raise DogfoodReportError("generation receipt collection exceeds the report bound")

    from .handoff import (
        attempt_id,
        generation_job_sha256,
        validate_generation_job,
        validate_generation_receipt,
    )

    for path in entries:
        if path.suffix != ".json":
            continue
        relative = path.relative_to(project_dir).as_posix()
        receipt = _read_object(project_dir, relative)
        if validate_generation_receipt(receipt):
            raise DogfoodReportError("generation receipt evidence is invalid")
        receipt_id = receipt.get("attempt_id")
        job_id = receipt.get("job_id")
        if (
            not isinstance(receipt_id, str)
            or path.stem != receipt_id
            or not isinstance(job_id, str)
        ):
            raise DogfoodReportError("generation receipt identity binding is invalid")
        job_relative = f"generation/jobs/{job_id}.json"
        job = _read_object(project_dir, job_relative)
        if validate_generation_job(job) or job.get("job_id") != job_id:
            raise DogfoodReportError("generation receipt job binding is invalid")
        if receipt.get("job_sha256") != generation_job_sha256(job):
            raise DogfoodReportError("generation receipt job digest is stale")
        retry_limit = job.get("retry_limit")
        if not isinstance(retry_limit, int) or isinstance(retry_limit, bool):
            raise DogfoodReportError("generation receipt retry binding is invalid")
        expected_attempts = {
            attempt_id(job_id=job_id, attempt=ordinal) for ordinal in range(1, retry_limit + 2)
        }
        if receipt_id not in expected_attempts:
            raise DogfoodReportError("generation receipt attempt binding is invalid")
        if receipt.get("outcome") == "success":
            raster_path = receipt.get("raster_path")
            raster_sha256 = receipt.get("raster_sha256")
            if not isinstance(raster_path, str) or not isinstance(raster_sha256, str):
                raise DogfoodReportError("successful generation receipt is rasterless")
            try:
                raster = read_contained_bytes(project_dir, raster_path, max_bytes=MAX_READ_BYTES)
            except (OSError, ValueError) as error:
                raise DogfoodReportError(
                    "successful generation receipt raster is unreadable"
                ) from error
            if hashlib.sha256(raster).hexdigest() != raster_sha256:
                raise DogfoodReportError("successful generation receipt raster digest is stale")
        kind = receipt.get("executor_kind")
        assert isinstance(kind, str)
        executor_kinds.add(kind)
        flags = receipt.get("capabilities_used")
        assert isinstance(flags, dict)
        for name in CAPABILITY_FLAGS:
            if flags.get(name) is True:
                capabilities[name] = True
    return sorted(executor_kinds), capabilities


def _descriptor_bytes(
    project_dir: Path, descriptor: object, *, maximum: int = MAX_READ_BYTES
) -> tuple[bytes, str] | None:
    if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}:
        return None
    relative = descriptor.get("path")
    expected = descriptor.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return None
    try:
        payload = read_contained_bytes(project_dir, relative, max_bytes=maximum)
    except (OSError, ValueError):
        return None
    if hashlib.sha256(payload).hexdigest() != expected:
        return None
    return payload, expected


def _pdf_verified(project_dir: Path, manifest: Mapping[str, object]) -> bool:
    artifacts = manifest.get("artifacts")
    project_id = manifest.get("project_id")
    if not isinstance(artifacts, dict) or not isinstance(project_id, str):
        return False
    descriptor = artifacts.get("pdf")
    if not isinstance(descriptor, dict) or descriptor.get("path") != f"exports/{project_id}.pdf":
        return False
    pdf = _descriptor_bytes(project_dir, descriptor, maximum=MAX_READ_BYTES)
    if pdf is None or not pdf[0].startswith(b"%PDF-"):
        return False
    verification_descriptor = artifacts.get("pdf_verification")
    if (
        not isinstance(verification_descriptor, dict)
        or verification_descriptor.get("path") != "exports/pdf-verification.json"
    ):
        return False
    verification = _descriptor_bytes(project_dir, verification_descriptor)
    if verification is None:
        return False
    try:
        record = loads_bounded_json(verification[0], source="pdf-verification.json")
    except (ValueError, json.JSONDecodeError):
        return False
    settings = manifest.get("settings")
    page_count = settings.get("page_count") if isinstance(settings, dict) else None
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        return False
    from .validate_project import validate_pdf_verification

    return bool(
        isinstance(record, dict)
        and record.get("pdf_sha256") == pdf[1]
        and not validate_pdf_verification(project_dir, project_id, page_count)
    )


def _reject_incomplete_transactions(project_dir: Path) -> None:
    transactions = contained_project_path(project_dir, "logs/transactions")
    if not transactions.exists():
        return
    if not transactions.is_dir() or transactions.is_symlink():
        raise DogfoodReportError("project transaction evidence is invalid")
    try:
        entries = list(transactions.iterdir())
    except OSError as error:
        raise DogfoodReportError("project transaction evidence is unreadable") from error
    if any(entry.name.isdecimal() for entry in entries):
        raise DogfoodReportError(
            "project has an incomplete transaction; recover it before creating a report"
        )


def _derive_locked(root: Path) -> tuple[str, dict[str, object]]:
    manifest, project_schema_version = _manifest(root)
    page_count, panel_count = _manifest_counts(manifest)
    attempts = _generation_attempts(root)
    (
        blocked,
        completed_stages,
        resumes,
        handoffs,
        handoff_completions,
        overrides,
    ) = _event_metrics(_events(root), manifest)
    executor_kinds, executor_capabilities = _receipt_metrics(root)
    pdf_verified = _pdf_verified(root, manifest)
    warnings = manifest.get("warnings")
    if not isinstance(warnings, list):  # guarded by validate_manifest
        raise DogfoodReportError("project warning evidence is invalid")
    warning_count = len(warnings)
    status = manifest.get("status")
    if not isinstance(status, str) or status not in ALL_STATUSES:
        raise DogfoodReportError("project terminal status is not a supported lifecycle status")
    derived = {
        "terminal_status": status,
        "page_count": page_count,
        "panel_count": panel_count,
        "completed_stages": completed_stages,
        "generation_attempts": attempts,
        "retry_rate": {"numerator": attempts["retries"], "denominator": attempts["total"]},
        "blocked_categories": blocked,
        "successful_resumes": resumes,
        "handoff_count": handoffs,
        # Portable archives do not currently persist route evidence in the project.
        # Empty is honest; inferring shared-folder would misclassify imported archives.
        "handoff_routes": [],
        "handoff_completions": handoff_completions,
        "executor_kinds": executor_kinds,
        "executor_capabilities": executor_capabilities,
        "final_pdf_verified": pdf_verified,
        "unresolved_warning_count": _bounded_integer(
            warning_count, "unresolved warning count", MAX_EVIDENCE_COUNT
        ),
        "manual_override_count": overrides,
    }
    return project_schema_version, derived


def _derive(project_dir: Path) -> tuple[str, dict[str, object]]:
    supplied = Path(project_dir).expanduser().absolute()
    try:
        metadata = supplied.lstat()
        if supplied.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise DogfoodReportError("project directory must not be a symlink or reparse point")
        root = supplied.resolve(strict=True)
    except DogfoodReportError:
        raise
    except OSError as error:
        raise DogfoodReportError("project directory is not readable") from error
    if not root.is_dir():
        raise DogfoodReportError("project directory is not readable")
    try:
        with ProjectLock(root, timeout=0, read_only=True):
            _reject_incomplete_transactions(root)
            return _derive_locked(root)
    except TimeoutError as error:
        raise DogfoodReportError("project is busy; retry after the active operation") from error
    except DogfoodReportError:
        raise
    except (OSError, ValueError) as error:
        raise DogfoodReportError("project lock cannot be opened safely") from error


def derive_project_metrics(project_dir: Path) -> dict[str, object]:
    """Derive only allowlisted scalar/count/category evidence from persisted files."""
    return _derive(Path(project_dir))[1]


def build_report(
    project_dir: Path,
    *,
    comic_sol_version: str,
    creator_inputs: Mapping[str, object],
    consent_to_share: bool,
) -> dict[str, object]:
    """Build a report in memory without migration, locking, writes, network, or clock reads."""
    if not isinstance(comic_sol_version, str) or VERSION.fullmatch(comic_sol_version) is None:
        raise DogfoodReportError("comic_sol_version must be a bounded release identifier")
    if not isinstance(creator_inputs, Mapping):
        raise DogfoodReportError("creator inputs must be an object")
    unknown = set(creator_inputs) - CREATOR_FIELDS
    if unknown:
        raise DogfoodReportError("creator inputs contain unknown fields")
    try:
        creator = validate_creator_inputs(**creator_inputs)
    except TypeError as error:
        raise DogfoodReportError("creator inputs are incomplete") from error
    consent = _boolean(consent_to_share, "consent_to_share")
    project_schema_version, derived = _derive(Path(project_dir))
    report: dict[str, object] = {
        "kind": REPORT_KIND,
        "schema_version": REPORT_SCHEMA_VERSION,
        "comic_sol_version": comic_sol_version,
        "project_schema_version": project_schema_version,
        "derived": derived,
        "creator": creator,
        "consent": {"share_report": consent},
        "limitations": list(LIMITATIONS),
    }
    validate_report(report)
    return report


def _validate_string_list(
    value: object, name: str, *, allowed: frozenset[str] | None = None
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DogfoodReportError(f"{name} must be an array of bounded strings")
    if len(value) != len(set(value)) or value != sorted(value):
        raise DogfoodReportError(f"{name} must be sorted and de-duplicated")
    for item in value:
        if (allowed is not None and item not in allowed) or (
            allowed is None and CATEGORY.fullmatch(item) is None
        ):
            raise DogfoodReportError(f"{name} contains an unsupported category")
    return value


def validate_report(report: object, *, require_consent: bool = False) -> dict[str, object]:
    """Validate exact report schema 1.0 and reject every unknown/free-form field."""
    root = _exact_object(report, ROOT_FIELDS, "report")
    if root.get("kind") != REPORT_KIND or root.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise DogfoodReportError("dogfood report kind/schema must be comic-sol-dogfood-report 1.0")
    version = root.get("comic_sol_version")
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise DogfoodReportError("comic_sol_version is invalid")
    if root.get("project_schema_version") not in SUPPORTED_PROJECT_SCHEMA_VERSIONS:
        raise DogfoodReportError("project_schema_version is not readable")
    if root.get("limitations") != list(LIMITATIONS):
        raise DogfoodReportError("limitations must match the fixed privacy contract")

    creator = _exact_object(root.get("creator"), CREATOR_FIELDS, "creator")
    normalized_creator = validate_creator_inputs(**creator)
    if creator != normalized_creator:
        raise DogfoodReportError("creator fields must be canonical and de-duplicated")

    consent = _exact_object(root.get("consent"), frozenset({"share_report"}), "consent")
    share_report = _boolean(consent.get("share_report"), "share_report")
    if require_consent and not share_report:
        raise DogfoodReportError("explicit consent to share is required for a persisted report")

    derived = _exact_object(root.get("derived"), DERIVED_FIELDS, "derived")
    status = derived.get("terminal_status")
    if not isinstance(status, str) or status not in ALL_STATUSES:
        raise DogfoodReportError("terminal_status must be a supported lifecycle status")
    for name in (
        "page_count",
        "panel_count",
        "successful_resumes",
        "handoff_count",
        "handoff_completions",
        "unresolved_warning_count",
        "manual_override_count",
    ):
        _bounded_integer(derived.get(name), name, MAX_EVIDENCE_COUNT)
    handoff_completions = derived["handoff_completions"]
    handoff_count = derived["handoff_count"]
    assert isinstance(handoff_completions, int) and isinstance(handoff_count, int)
    if handoff_completions > handoff_count:
        raise DogfoodReportError("handoff completions cannot exceed prepared handoff units")
    completed = derived.get("completed_stages")
    if not isinstance(completed, list) or completed != [
        stage for stage in STAGES if stage in completed
    ]:
        raise DogfoodReportError("completed_stages must follow the canonical stage order")
    if len(completed) != len(set(completed)) or any(stage not in STAGES for stage in completed):
        raise DogfoodReportError("completed_stages contains an unsupported stage")
    _validate_string_list(
        derived.get("blocked_categories"),
        "blocked_categories",
        allowed=SAFE_BLOCKED_CATEGORIES,
    )
    _validate_string_list(derived.get("executor_kinds"), "executor_kinds", allowed=EXECUTOR_KINDS)
    _validate_string_list(derived.get("handoff_routes"), "handoff_routes", allowed=HANDOFF_ROUTES)
    capabilities = _exact_object(
        derived.get("executor_capabilities"), frozenset(CAPABILITY_FLAGS), "executor_capabilities"
    )
    for name in CAPABILITY_FLAGS:
        _boolean(capabilities.get(name), name)
    _boolean(derived.get("final_pdf_verified"), "final_pdf_verified")
    attempts = _exact_object(
        derived.get("generation_attempts"),
        frozenset({"initial", "retries", "total"}),
        "generation_attempts",
    )
    initial = _bounded_integer(attempts.get("initial"), "initial attempts", MAX_EVIDENCE_COUNT)
    retries = _bounded_integer(attempts.get("retries"), "retry attempts", MAX_EVIDENCE_COUNT)
    total = _bounded_integer(attempts.get("total"), "total attempts", MAX_EVIDENCE_COUNT)
    if initial + retries != total:
        raise DogfoodReportError("generation attempt totals are inconsistent")
    rate = _exact_object(
        derived.get("retry_rate"), frozenset({"numerator", "denominator"}), "retry_rate"
    )
    if rate != {"numerator": retries, "denominator": total}:
        raise DogfoodReportError("retry rate evidence is inconsistent")
    return root


def canonical_report_bytes(report: object) -> bytes:
    """Return canonical deterministic report bytes after strict validation."""
    return canonical_artifact_bytes(validate_report(report))


def render_preview(report: object) -> str:
    """Render every bounded report field for informed local review."""
    value = validate_report(report)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_report(output_path: Path, report: object, *, project_dir: Path) -> Path:
    """Atomically persist a consented canonical report outside the project tree."""
    value = validate_report(report, require_consent=True)
    payload = canonical_artifact_bytes(value)
    try:
        return durable_external_write(Path(output_path), payload, project_dir=Path(project_dir))
    except (OSError, ValueError) as error:
        raise DogfoodReportError(f"security-error: {error}") from error


def validate_report_file(path: Path, *, require_consent: bool = False) -> dict[str, object]:
    """Read and validate one bounded report without writing or touching a project."""
    try:
        absolute = Path(path).expanduser().absolute()
        payload = read_bytes_nofollow(absolute, max_bytes=MAX_JSON_BYTES)
        value = loads_bounded_json(payload, source=absolute.name)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, DogfoodReportError):
            raise
        raise DogfoodReportError("dogfood report file is invalid or unreadable") from error
    return validate_report(value, require_consent=require_consent)
