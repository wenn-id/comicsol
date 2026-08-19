#!/usr/bin/env python3
"""Render Comic Sol's structured QA artifacts as transparent Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from PIL import Image

from .comic_sol import (
    ProjectTransaction,
    atomic_write_bytes,
    canonical_artifact_bytes,
    read_json,
    sha256_file,
)
from .project_io import ProjectLock, contained_project_path, open_path_nofollow
from .quality_records import PANEL_CHECK_IDS
from .schema import read_project_manifest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates/qa-report.md.tmpl"
TOKEN_PATTERN = re.compile(r"\{\{[A-Z0-9_]+\}\}")
PAGE_PATTERN = re.compile(r"^page-[0-9]{3}\.png$")


@dataclass(frozen=True)
class QaSummary:
    pages: int
    panels: int
    generation_attempts: int
    regenerated_panels: int
    accepted_warnings: int
    hard_failures: int


def _attempts(record: dict[str, object]) -> int:
    bindings = record.get("bindings")
    value = (
        bindings.get("attempts", 0)
        if record.get("schema_version") == "2.0" and isinstance(bindings, dict)
        else record.get("attempts", 0)
    )
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _panel_id(record: dict[str, object]) -> str | None:
    field = "subject_id" if record.get("schema_version") == "2.0" else "panel_id"
    value = record.get(field)
    return value if isinstance(value, str) and value else None


def _has_error_failure(record: dict[str, object]) -> bool:
    checks = record.get("checks", [])
    return isinstance(checks, list) and any(
        isinstance(check, dict)
        and check.get("result") == "fail"
        and check.get("severity") == "error"
        for check in checks
    )


def summarize_qa(
    manifest: dict[str, object],
    panel_records: list[dict[str, object]],
) -> QaSummary:
    """Aggregate QA solely from panel records plus manifest production settings."""
    settings = manifest.get("settings", {})
    pages = settings.get("page_count", 0) if isinstance(settings, dict) else 0
    pages = pages if isinstance(pages, int) and not isinstance(pages, bool) and pages >= 0 else 0
    maximum_retries = settings.get("max_panel_retries", 2) if isinstance(settings, dict) else 2
    if not isinstance(maximum_retries, int) or isinstance(maximum_retries, bool) or maximum_retries < 0:
        maximum_retries = 2
    hard_categories = {"corrupt", "corrupt_image", "safety", "safety_refusal"}
    return QaSummary(
        pages=pages,
        panels=len(panel_records),
        generation_attempts=sum(_attempts(record) for record in panel_records),
        regenerated_panels=sum(_attempts(record) > 1 for record in panel_records),
        accepted_warnings=sum(
            record.get("decision") in {"accept-warning", "accept_with_warnings"}
            for record in panel_records
        ),
        hard_failures=sum(
            record.get("failure_category") in hard_categories
            or (
                _has_error_failure(record)
                and _attempts(record) >= maximum_retries + 1
            )
            for record in panel_records
        ),
    )


def _escape_table(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _load_records(project_dir: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    panel_dir = project_dir / "qa/panels"
    if not panel_dir.is_dir():
        return records
    for path in panel_dir.glob("*.json"):
        record = read_json(path)
        if _panel_id(record) is None:
            raise ValueError(f"panel record has no panel identity: {path}")
        records.append(record)
    records.sort(key=lambda record: _panel_id(record) or "")
    return records


def _load_page_records(project_dir: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    page_dir = project_dir / "qa/pages"
    if not page_dir.is_dir():
        return records
    for path in page_dir.glob("page-*.json"):
        record = read_json(path)
        if record.get("schema_version") == "2.0" and record.get("kind") == "page-qa":
            records.append(record)
        elif record.get("schema_version") == "1.0":
            record["quality-migration-required"] = True
            records.append(record)
    def sort_key(record: dict[str, object]) -> tuple[str, str]:
        """Order page-QA records without trusting legacy page field types."""
        if record.get("schema_version") == "2.0":
            subject_id = record.get("subject_id")
            return ("v2", subject_id if isinstance(subject_id, str) else "")
        page = record.get("page")
        return (
            "v1",
            f"page-{page:03d}" if isinstance(page, int) and not isinstance(page, bool)
            else f"page-{page}",
        )

    records.sort(key=sort_key)
    return records


def _final_status(manifest: dict[str, object]) -> object:
    """Project the terminal status an EXPORTED project is about to reach.

    Final validation requires this report and its descriptor to exist before
    the terminal transition, so the report can only ever be written while the
    project is still EXPORTED. From there the outcome is determined by whether
    any warning is unresolved.
    """
    status = manifest.get("status")
    if status != "EXPORTED":
        return status if status is not None else "unknown"
    warnings = manifest.get("warnings")
    return "COMPLETE_WITH_WARNINGS" if isinstance(warnings, list) and warnings else "COMPLETE"


def _project_summary(manifest: dict[str, object]) -> str:
    return "\n".join((
        f"- Project: {_escape_table(manifest.get('title', 'Untitled'))} (`{manifest.get('project_id', 'unknown')}`)",
        f"- Final status: **{_final_status(manifest)}**",
    ))


def _capability(manifest: dict[str, object]) -> str:
    capability = manifest.get("capability", {})
    if not isinstance(capability, dict):
        capability = {}
    supported = capability.get("supports_reference_images") is True
    lines = [
        f"- Status: {capability.get('status', 'unknown')}",
        f"- Capability: {capability.get('name') or 'none'}",
        f"- Reference images supported: {'yes' if supported else 'no'}",
        f"- Explicit dimensions supported: {'yes' if capability.get('supports_dimensions') is True else 'no'}",
    ]
    if not supported:
        lines.append("- Consistency: degraded consistency mode because reference images are unsupported; canonical text anchors were used instead.")
    lines.append(
        "- Privacy: external provider policies govern transmitted prompts and references; Comic Sol stores no provider credentials."
    )
    return "\n".join(lines)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _counts(summary: QaSummary) -> str:
    return "\n".join((
        f"- {_plural(summary.pages, 'page')}",
        f"- {_plural(summary.panels, 'panel')}",
        f"- {_plural(summary.generation_attempts, 'generation attempt')}",
        f"- {_plural(summary.regenerated_panels, 'regenerated panel')}",
        f"- {_plural(summary.accepted_warnings, 'accepted warning')}",
        f"- {_plural(summary.hard_failures, 'hard failure')}",
    ))


def _evidence_provenance(project_dir: Path) -> str:
    path = project_dir / "qa/evidence.json"
    if not path.is_file():
        return (
            "- Mode: unavailable\n"
            "- Scope: no explicit evidence provenance record was supplied."
        )
    record = read_json(path)
    mode = record.get("mode")
    if mode == "deterministic":
        return "\n".join((
            "- Mode: deterministic",
            f"- Scope: {record.get('scope', 'mechanics-only')}",
            "- Claim boundary: deterministic evidence proves mechanics only and "
            "does not prove live visual quality.",
        ))
    if mode != "live-visual":
        raise ValueError("qa/evidence.json has an unsupported evidence mode")

    required = (
        "retained_attempt", "attempt_sha256", "provider", "model",
        "reviewer_method",
    )
    if any(
        not isinstance(record.get(name), str) or not record[name]
        for name in required
    ):
        raise ValueError("qa/evidence.json live-visual provenance is incomplete")

    def joined(name: str) -> str:
        values = record.get(name)
        if not isinstance(values, list):
            return "none"
        return ", ".join(_escape_table(value) for value in values) or "none"

    return "\n".join((
        "- Mode: live-visual",
        f"- Scope: {_escape_table(record.get('scope', 'retained-attempt-visual-review'))}",
        f"- Provider/model: {_escape_table(record['provider'])} / {_escape_table(record['model'])}",
        f"- Retained attempt: `{_escape_table(record['retained_attempt'])}`",
        f"- Attempt SHA-256: `{_escape_table(record['attempt_sha256'])}`",
        f"- References: {joined('references')}",
        f"- Reviewer method: {_escape_table(record['reviewer_method'])}",
        f"- Known limitations: {joined('limitations')}",
    ))


def _panel_table(records: list[dict[str, object]]) -> str:
    headings = ("Panel", "Attempts", "Decision", *PANEL_CHECK_IDS, "Evidence")
    lines = [
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
    ]
    for record in records:
        checks = record.get("checks", [])
        check_map = {
            check.get("id"): check
            for check in checks
            if isinstance(check, dict) and isinstance(check.get("id"), str)
        } if isinstance(checks, list) else {}
        decision = str(record.get("decision", "unknown"))
        override = record.get("override_reason")
        if isinstance(override, str) and override:
            decision += f" (override: {override})"
        results = []
        for check_id in PANEL_CHECK_IDS:
            check = check_map.get(check_id, {})
            result = str(check.get("result", "missing"))
            if result == "fail" and check.get("severity") == "warning":
                result += " (warning)"
            results.append(result)
        evidence = "; ".join(
            f"{check_id}: {check_map[check_id].get('evidence', '')}"
            for check_id in PANEL_CHECK_IDS if check_id in check_map
        )
        cells = (
            _panel_id(record) or "unknown", _attempts(record), decision,
            *results, evidence,
        )
        lines.append("| " + " | ".join(_escape_table(cell) for cell in cells) + " |")
    return "\n".join(lines)


def _normalization_table(
    project_dir: Path,
    records: list[dict[str, object]],
) -> str:
    headings = ("Panel", "Mode", "Source", "Target")
    lines = [
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
    ]
    for record in records:
        panel_id = _panel_id(record) or "unknown"
        bindings = record.get("bindings")
        relative = (
            bindings.get("normalization_path")
            if isinstance(bindings, dict)
            else None
        )
        path = _contained_or_none(project_dir, relative)
        mode = source_size = target_size = "unavailable"
        if path is not None and path.is_file():
            try:
                normalization = read_json(path)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                normalization = {}
            source = normalization.get("source")
            operation = normalization.get("operation")
            target = normalization.get("target_size")
            if isinstance(operation, dict) and isinstance(operation.get("mode"), str):
                mode = operation["mode"]
            source_value = source.get("size") if isinstance(source, dict) else None
            if (
                isinstance(source_value, list)
                and len(source_value) == 2
                and all(isinstance(value, int) for value in source_value)
            ):
                source_size = f"{source_value[0]}×{source_value[1]}"
            if (
                isinstance(target, list)
                and len(target) == 2
                and all(isinstance(value, int) for value in target)
            ):
                target_size = f"{target[0]}×{target[1]}"
        lines.append(
            "| " + " | ".join(
                _escape_table(value)
                for value in (panel_id, mode, source_size, target_size)
            ) + " |"
        )
    return "\n".join(lines)


def _page_qa_table(records: list[dict[str, object]]) -> str:
    headings = ("Page", "Layout", "Check", "Result", "Method", "Reviewer")
    lines = [
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
    ]
    for record in records:
        if record.get("quality-migration-required"):
            lines.append(
                "| " + " | ".join(_escape_table(cell) for cell in (
                    f"page-{record.get('page', 'unknown')}",
                    "quality-migration-required", "migration-required", "missing",
                    "migration-required", "migration-required",
                )) + " |"
            )
            continue
        bindings = record.get("bindings")
        layout = bindings.get("layout_name", "unknown") if isinstance(bindings, dict) else "unknown"
        version = bindings.get("layout_version", "unknown") if isinstance(bindings, dict) else "unknown"
        checks = record.get("checks")
        if not isinstance(checks, list):
            checks = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            cells = (
                record.get("subject_id", "unknown"),
                f"{layout} v{version}",
                check.get("id", "unknown"),
                check.get("result", "missing"),
                check.get("method", "missing"),
                check.get("reviewer", "missing"),
            )
            lines.append(
                "| " + " | ".join(_escape_table(cell) for cell in cells) + " |"
            )
    if len(lines) == 2:
        lines.append(
            "| none | unavailable | unavailable | unavailable | unavailable | unavailable |"
        )
    return "\n".join(lines)


def _warnings(
    manifest: dict[str, object],
    records: list[dict[str, object]],
) -> str:
    warnings: dict[str, list[str]] = {}

    def add_warning(source: str, warning: str) -> None:
        sources = warnings.setdefault(warning, [])
        if source not in sources:
            sources.append(source)

    for record in records:
        panel_id = _panel_id(record) or "unknown"
        values = record.get("unresolved_warnings", [])
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value:
                    add_warning(panel_id, value)
    manifest_warnings = manifest.get("warnings", [])
    if isinstance(manifest_warnings, list):
        for value in manifest_warnings:
            if isinstance(value, str) and value:
                add_warning("project", value)
    if not warnings:
        return "No unresolved warnings."
    return "\n".join(
        f"- `{', '.join(sources)}`: {warning}"
        for warning, sources in warnings.items()
    )


def _relative(project_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _contained_or_none(project_dir: Path, relative: object) -> Path | None:
    """Resolve a manifest-supplied path, or None when it escapes the project.

    Manifest values are agent-authored, so an absolute path, a UNC path or a
    ``..`` sequence must never be read or hashed into the report.
    """
    if not isinstance(relative, str):
        return None
    try:
        return contained_project_path(project_dir, relative)
    except (ValueError, OSError):
        return None


def _pdf_readable(path: Path, expected_pages: int) -> bool:
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    if not payload.startswith(b"%PDF-") or not payload.rstrip().endswith(b"%%EOF"):
        return False
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", payload))
    return pages == expected_pages


def _integrity(
    project_dir: Path,
    manifest: dict[str, object],
    records: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    artifacts = manifest.get("artifacts", {})
    if isinstance(artifacts, dict):
        for name in sorted(artifacts):
            descriptor = artifacts[name]
            if not isinstance(descriptor, dict):
                lines.append(f"- `{name}`: invalid descriptor")
                continue
            relative = descriptor.get("path")
            expected = descriptor.get("sha256")
            path = _contained_or_none(project_dir, relative)
            if path is None:
                lines.append(f"- `{relative}` — outside the project boundary")
                continue
            exists = path.is_file()
            matches = exists and isinstance(expected, str) and sha256_file(path) == expected
            lines.append(
                f"- `{relative}` — exists: {'yes' if exists else 'no'}; hash: `{expected}`; hash matches: {'yes' if matches else 'no'}"
            )

    pages = sorted(
        path for path in (project_dir / "pages").glob("page-*.png")
        if PAGE_PATTERN.fullmatch(path.name)
    ) if (project_dir / "pages").is_dir() else []
    for page in pages:
        try:
            with open_path_nofollow(page) as stream, Image.open(stream) as image:
                image.load()
                dimensions = f"{image.width}×{image.height}"
                valid = image.format == "PNG" and image.mode == "RGB" and image.size == (1600, 2400)
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            dimensions, valid = "unreadable", False
        lines.append(
            f"- `{_relative(project_dir, page)}` — dimensions: {dimensions}; valid page: {'yes' if valid else 'no'}; sha256: `{sha256_file(page) if page.is_file() else 'missing'}`"
        )

    references = sorted({
        reference
        for record in records
        for reference in (
            record.get("generation", {}).get("reference_paths", [])
            if isinstance(record.get("generation"), dict) else []
        )
        if isinstance(reference, str)
    })
    for reference in references:
        path = _contained_or_none(project_dir, reference)
        valid = path is not None and path.is_file()
        lines.append(f"- Reference `{reference}` — valid: {'yes' if valid else 'no'}")

    project_id = manifest.get("project_id", "")
    pdf_descriptor = artifacts.get("pdf") if isinstance(artifacts, dict) else None
    pdf_relative = pdf_descriptor.get("path") if isinstance(pdf_descriptor, dict) else f"exports/{project_id}.pdf"
    pdf_path = _contained_or_none(project_dir, pdf_relative)
    readable = pdf_path is not None and _pdf_readable(pdf_path, len(pages))
    lines.append(
        f"- `{pdf_relative}` — PDF readable: {'yes' if readable else 'no'}; pages: {len(pages)}"
    )
    return "\n".join(lines) if lines else "No artifacts were recorded."


def _resume(project_dir: Path) -> str:
    """Summarize which artifacts a resume carried over versus rebuilt.

    ``resume_project`` emits ``artifact.reused`` for every preserved stage
    output; ``promote_attempt`` emits ``artifact.regenerated`` when it replaces
    an existing panel.
    """
    event_path = project_dir / "logs/events.jsonl"
    reused: set[str] = set()
    regenerated: set[str] = set()
    if event_path.is_file():
        for line in event_path.read_text("utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or not isinstance(event.get("details"), dict):
                continue
            artifact = event["details"].get("artifact_path")
            if not isinstance(artifact, str):
                continue
            name = str(event.get("event", ""))
            if name == "artifact.reused":
                reused.add(artifact)
            elif name == "artifact.regenerated":
                regenerated.add(artifact)
    return "\n".join((
        "- Reused: " + (", ".join(sorted(reused)) if reused else "none"),
        "- Regenerated: " + (", ".join(sorted(regenerated)) if regenerated else "none"),
    ))


def _render_report_locked(project_dir: Path, output_path: Path | None = None) -> Path:
    """Render structured QA sections and publish the default report atomically."""
    project_dir = Path(project_dir)
    manifest = read_project_manifest(project_dir / "project.json")
    records = _load_records(project_dir)
    page_records = _load_page_records(project_dir)
    summary = summarize_qa(manifest, records)
    template = TEMPLATE_PATH.read_text("utf-8")
    replacements = {
        "{{PROJECT_SUMMARY}}": _project_summary(manifest),
        "{{CAPABILITY}}": _capability(manifest),
        "{{COUNTS}}": _counts(summary),
        "{{EVIDENCE_PROVENANCE}}": _evidence_provenance(project_dir),
        "{{PANEL_TABLE}}": _panel_table(records),
        "{{NORMALIZATION_TABLE}}": _normalization_table(project_dir, records),
        "{{PAGE_QA_TABLE}}": _page_qa_table(page_records),
        "{{WARNINGS}}": _warnings(manifest, records),
        "{{INTEGRITY}}": _integrity(project_dir, manifest, records),
        "{{RESUME}}": _resume(project_dir),
    }
    if set(TOKEN_PATTERN.findall(template)) != set(replacements):
        raise ValueError("QA report template tokens do not match replacements")
    rendered = template
    for token, content in replacements.items():
        rendered = rendered.replace(token, content)
    report_bytes = (rendered.rstrip() + "\n").encode("utf-8")

    if output_path is not None:
        destination = Path(output_path)
        atomic_write_bytes(destination, report_bytes)
        return destination

    destination = project_dir / "qa/report.md"
    with ProjectTransaction(project_dir, "report-publish") as transaction:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
        artifacts["qa_report"] = {
            "path": "qa/report.md",
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
        }
        manifest["artifacts"] = artifacts
        transaction.stage_bytes("qa/report.md", report_bytes)
        transaction.stage_bytes("project.json", canonical_artifact_bytes(manifest))
    return destination


def render_report(project_dir: Path, output_path: Path | None = None) -> Path:
    """Render a report and descriptor from one locked project snapshot."""
    with ProjectLock(Path(project_dir)):
        return _render_report_locked(Path(project_dir), output_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="render_report.py")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        print(render_report(arguments.project_dir, arguments.output))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
