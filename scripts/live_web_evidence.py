"""Validate and summarize candidate-bound Web/Studio live evidence.

The deterministic offline suite cannot judge live or deployed execution.
This tool consumes a curated, local evidence bundle produced by an
authorized maintainer after a real deployment, a real ``document.modelContext``
session, a real local ComfyUI run, a real paid/live provider smoke, or a
real native portable release-asset smoke. It never calls a provider,
reads credentials, or persists prompts.

It is the publication gate for the evidence tracked in
``docs/web/live-evidence.md`` (issue #321) and the submission companion
``submission/webmcp/live-evidence.md``.

Usage
-----

::

    python -m scripts.live_web_evidence <bundle-root> \\
        [--candidate <40-hex-sha>]

``<bundle-root>`` is the directory containing ``manifest.json`` and any
media artifacts referenced from it. ``--candidate`` independently supplies
the trusted 40-character commit SHA, and the manifest's
``candidate.sha`` must match. The default candidate is the working-tree
``HEAD`` commit, looked up from the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.input_limits import looks_like_secret

SCHEMA_VERSION = "1.0"
KIND = "web-live-evidence"
SUPPORTED_ROW_KINDS = {
    "deployment",
    "agent-webmcp",
    "comfyui",
    "provider-smoke",
    "media",
    "release-asset-smoke",
}
SUPPORTED_MEDIA_KINDS = {"image", "narration"}
ALLOWED_MANIFEST_FIELDS = {
    "schema",
    "candidate",
    "authorization",
    "retention",
    "rows",
}
ALLOWED_CANDIDATE_FIELDS = {
    "sha",
    "engine_version",
    "recorded_before_execution",
}
ALLOWED_AUTHORIZATION_FIELDS = {
    "provider_or_host",
    "max_cost",
    "maintainer",
    "notes",
}
ALLOWED_RETENTION_FIELDS = {
    "location",
    "created_at",
}
ALLOWED_ROW_FIELDS = {
    "id",
    "kind",
    "date",
    "environment",
    "route",
    "provider",
    "model",
    "credential_mode",
    "step",
    "result",
    "cost",
    "artifact",
    "limitations",
}
ALLOWED_ENVIRONMENTS = {"local", "external", "hybrid", "offline"}
ALLOWED_RESULTS = {"pass", "fail", "skipped", "incomplete"}
ALLOWED_CREDENTIAL_MODES = {"hosted", "session-byok", "persisted-byok", "agent-handoff", "none"}


class EvidenceError(ValueError):
    """Raised when a Web/Studio live evidence bundle is incomplete or unsafe."""


def _is_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object, label: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvidenceError(f"{label} must be a non-empty string of at most {maximum} characters")
    if looks_like_secret(value):
        raise EvidenceError(f"{label} must not contain credentials or secrets")
    if any(character in value for character in ("\r", "\n", "|", "`")):
        raise EvidenceError(f"{label} must not contain Markdown control characters")
    return value.strip()


def _exact(record: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = set(record) - fields
    missing = fields - set(record)
    if unknown or missing:
        raise EvidenceError(
            f"{label} must contain exactly {sorted(fields)}; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{label} must be a JSON array")
    return value


def _bundle_path(root: Path, relative: object, label: str) -> Path:
    text = _text(relative, f"{label}.path", maximum=240)
    pure = PurePosixPath(text)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise EvidenceError(f"{label}.path must be a contained POSIX relative path")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceError(f"{label}.path must not traverse a symbolic link")
    path = root.joinpath(*pure.parts)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise EvidenceError(f"{label}.path escapes the evidence bundle") from error
    if not path.is_file():
        raise EvidenceError(f"{label}.path does not name a retained file")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso8601(value: object, label: str) -> str:
    text = _text(value, label, maximum=40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must include a timezone")
    return text


def _validate_candidate(value: object, *, expected_sha: str) -> dict[str, Any]:
    record = _mapping(value, "candidate")
    _exact(record, ALLOWED_CANDIDATE_FIELDS, "candidate")
    sha = record.get("sha")
    if not _is_digest(sha, 40):
        raise EvidenceError("candidate.sha must be a lowercase 40-character Git commit")
    if sha != expected_sha:
        raise EvidenceError("candidate.sha does not match the trusted expected commit")
    if record.get("recorded_before_execution") is not True:
        raise EvidenceError("candidate.recorded_before_execution must be true")
    engine_version = _text(record.get("engine_version"), "candidate.engine_version", maximum=80)
    return {"sha": str(sha), "engine_version": engine_version}


def _validate_authorization(value: object) -> dict[str, str]:
    record = _mapping(value, "authorization")
    if not record:
        raise EvidenceError(
            "authorization must be present (use provider_or_host='none' for offline-only manual evidence)"
        )
    _exact(record, ALLOWED_AUTHORIZATION_FIELDS, "authorization")
    provider_or_host = _text(
        record.get("provider_or_host"), "authorization.provider_or_host", maximum=120
    )
    max_cost = _text(record.get("max_cost"), "authorization.max_cost", maximum=40)
    maintainer = _text(record.get("maintainer"), "authorization.maintainer", maximum=80)
    notes = _text(record.get("notes"), "authorization.notes", maximum=240)
    return {
        "provider_or_host": provider_or_host,
        "max_cost": max_cost,
        "maintainer": maintainer,
        "notes": notes,
    }


def _validate_retention(value: object) -> dict[str, str]:
    record = _mapping(value, "retention")
    _exact(record, ALLOWED_RETENTION_FIELDS, "retention")
    location = _text(record.get("location"), "retention.location", maximum=240)
    pure = PurePosixPath(location)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise EvidenceError("retention.location must be a contained POSIX relative path")
    created_at = _iso8601(record.get("created_at"), "retention.created_at")
    return {"location": location, "created_at": created_at}


def _validate_artifact(root: Path, value: object, label: str) -> dict[str, Any]:
    record = _mapping(value, label)
    if set(record) != {"path", "sha256", "kind"}:
        raise EvidenceError(f"{label} must contain exactly path, sha256, kind")
    kind = record.get("kind")
    if kind not in SUPPORTED_MEDIA_KINDS:
        raise EvidenceError(f"{label}.kind must be one of {sorted(SUPPORTED_MEDIA_KINDS)}")
    path = _bundle_path(root, record.get("path"), label)
    expected = record.get("sha256")
    if not _is_digest(expected, 64):
        raise EvidenceError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    actual = _file_sha256(path)
    if actual != expected:
        raise EvidenceError(f"{label}.sha256 does not match the retained file")
    extra: dict[str, Any] = {}
    if kind == "image":
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError, DecompressionBombError) as error:
            raise EvidenceError(f"{label}.path must be a decodable image raster") from error
        if width < 64 or height < 64:
            raise EvidenceError(f"{label}.path must be at least 64x64 pixels")
        extra = {"width": width, "height": height}
    else:
        size = path.stat().st_size
        if size <= 0 or size > 2 * 1024 * 1024:
            raise EvidenceError(f"{label}.path narration must be in (0, 2 MiB]")
        extra = {"bytes": size}
    return {"kind": kind, "path": path.relative_to(root).as_posix(), "sha256": actual, **extra}


def _validate_row(root: Path, value: object, index: int) -> dict[str, Any]:
    label = f"rows[{index}]"
    record = _mapping(value, label)
    _exact(record, ALLOWED_ROW_FIELDS, label)
    row_id = _text(record.get("id"), f"{label}.id", maximum=80)
    kind = record.get("kind")
    if kind not in SUPPORTED_ROW_KINDS:
        raise EvidenceError(f"{label}.kind must be one of {sorted(SUPPORTED_ROW_KINDS)}")
    date = _text(record.get("date"), f"{label}.date", maximum=20)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise EvidenceError(f"{label}.date must be an ISO YYYY-MM-DD date")
    environment = record.get("environment")
    if environment not in ALLOWED_ENVIRONMENTS:
        raise EvidenceError(f"{label}.environment must be one of {sorted(ALLOWED_ENVIRONMENTS)}")
    route = _text(record.get("route"), f"{label}.route", maximum=80)
    provider = _text(record.get("provider"), f"{label}.provider", maximum=80)
    model = _text(record.get("model"), f"{label}.model", maximum=80)
    credential_mode = record.get("credential_mode")
    if credential_mode not in ALLOWED_CREDENTIAL_MODES:
        raise EvidenceError(
            f"{label}.credential_mode must be one of {sorted(ALLOWED_CREDENTIAL_MODES)}"
        )
    step = _text(record.get("step"), f"{label}.step", maximum=240)
    result = record.get("result")
    if result not in ALLOWED_RESULTS:
        raise EvidenceError(f"{label}.result must be one of {sorted(ALLOWED_RESULTS)}")
    cost = _text(record.get("cost"), f"{label}.cost", maximum=40)
    artifact_value = record.get("artifact")
    if artifact_value is None:
        artifact: dict[str, Any] | None = None
    else:
        artifact = _validate_artifact(root, artifact_value, f"{label}.artifact")
    limitations = _text(record.get("limitations"), f"{label}.limitations", maximum=240)
    return {
        "id": row_id,
        "kind": kind,
        "date": date,
        "environment": environment,
        "route": route,
        "provider": provider,
        "model": model,
        "credential_mode": credential_mode,
        "step": step,
        "result": result,
        "cost": cost,
        "artifact": artifact,
        "limitations": limitations,
    }


def _resolve_candidate(root: Path, override: str | None) -> str:
    if override is not None:
        if not _is_digest(override, 40):
            raise EvidenceError("--candidate must be a lowercase 40-character Git commit")
        return override
    head = root.joinpath(".git", "HEAD")
    if not head.is_file():
        raise EvidenceError("no --candidate supplied and bundle root is not a git working tree")
    ref = head.read_text(encoding="utf-8").strip()
    if not ref.startswith("ref:"):
        raise EvidenceError("detached HEAD; supply --candidate explicitly")
    ref_path = root.joinpath(".git", *ref[len("ref:") :].strip().split("/"))
    if not ref_path.is_file():
        raise EvidenceError(f"ref {ref!r} is missing; supply --candidate explicitly")
    sha = ref_path.read_text(encoding="utf-8").strip()
    if not _is_digest(sha, 40):
        raise EvidenceError(f"ref {ref!r} does not resolve to a 40-character commit")
    return sha


def validate_bundle(root: Path, candidate: str) -> dict[str, Any]:
    if not root.is_dir():
        raise EvidenceError(f"bundle root is not a directory: {root}")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise EvidenceError("manifest.json is missing at the bundle root")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = _mapping(payload, "manifest")
    _exact(record, ALLOWED_MANIFEST_FIELDS, "manifest")
    if record.get("schema") != f"{KIND}/{SCHEMA_VERSION}":
        raise EvidenceError(f"manifest.schema must be {KIND}/{SCHEMA_VERSION!r}")
    candidate_record = _validate_candidate(record.get("candidate"), expected_sha=candidate)
    authorization = _validate_authorization(record.get("authorization"))
    retention = _validate_retention(record.get("retention"))
    rows = _list(record.get("rows"), "manifest.rows")
    seen: set[str] = set()
    validated_rows: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = _validate_row(root, item, index)
        if row["id"] in seen:
            raise EvidenceError(f"rows[{index}].id must be unique within a bundle")
        seen.add(row["id"])
        validated_rows.append(row)
    return {
        "schema": record["schema"],
        "candidate": candidate_record,
        "authorization": authorization,
        "retention": retention,
        "rows": validated_rows,
    }


def render_markdown(summary: Mapping[str, Any]) -> str:
    candidate = summary["candidate"]
    authorization = summary["authorization"]
    retention = summary["retention"]
    rows = summary["rows"]
    out: list[str] = []
    out.append(f"# Web live evidence — candidate `{candidate['sha']}`")
    out.append("")
    out.append(f"- Engine version: `{candidate['engine_version']}`")
    out.append(f"- Maintainer: `{authorization['maintainer']}`")
    out.append(
        f"- Authorization: provider/host `{authorization['provider_or_host']}`, "
        f"max cost `{authorization['max_cost']}`"
    )
    out.append(f"- Retention: `{retention['location']}` (recorded at {retention['created_at']})")
    out.append(f"- Rows: {len(rows)}")
    if not rows:
        out.append("")
        out.append("No rows retained. This bundle passes the gate with an empty roster;")
        out.append("it does not, by itself, claim any live or deployed evidence.")
    else:
        out.append("")
        out.append("| Row | Kind | Date | Env | Result | Artifact |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for row in rows:
            artifact = row.get("artifact")
            artifact_cell = ""
            if artifact is not None:
                artifact_cell = f"`{artifact['path']}` ({artifact['sha256'][:12]}…)"
            out.append(
                f"| `{row['id']}` | {row['kind']} | {row['date']} | {row['environment']} | "
                f"{row['result']} | {artifact_cell} |"
            )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and summarize candidate-bound Web/Studio live evidence."
    )
    parser.add_argument("bundle_root", type=Path, help="path to the web-live-evidence bundle")
    parser.add_argument(
        "--candidate",
        help="trusted 40-character Git commit SHA (default: bundle root's HEAD)",
    )
    args = parser.parse_args(argv)

    root = args.bundle_root.resolve()
    try:
        candidate_sha = _resolve_candidate(root, args.candidate)
        summary = validate_bundle(root, candidate_sha)
    except EvidenceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    json_path = root / "summary.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    md_path = root / "summary.md"
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    print(f"OK: {len(summary['rows'])} row(s) validated; wrote summary.json and summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
