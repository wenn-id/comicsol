#!/usr/bin/env python3
"""Replace one generated visual SFX with deterministic lettering, auditably.

An image model asked to bake `KRAK!` into artwork sometimes returns `KRRAK`,
sometimes returns it mirrored, sometimes returns it twice, and sometimes returns
nothing at all. Before this command the only remedy was to re-roll the whole
panel and hope, because nothing recorded that the effect had ever been the
model's to draw.

This module turns that remedy into a stated operation with four properties:

- **The storyboard stays the source of truth.** The effect is not patched into
  the raster; its `render_mode` is flipped to `deterministic-lettering` so the
  next lettering pass draws it from authored text under the pinned font policy.
  Every later stage then derives the same answer from the plan, which is what
  makes the fix reproducible instead of a one-off touch-up.
- **The evidence is preserved before anything changes.** The clean raster that
  carried the faulty effect — and the lettered raster derived from it, when one
  exists — are archived under `panels/{panel-id}/sfx-audit/` and bound by hash in
  `panels/{panel-id}/sfx-audit.json`. A repair that destroys the artifact it was
  diagnosing cannot be reviewed, and a reviewer disputing the diagnosis has to be
  able to look at what was actually rejected.
- **Only the stages that actually depend on the effect go stale.** The manifest's
  storyboard descriptor is re-bound to the edited plan, so planning and the
  storyboard stage keep their cache. Generation goes stale because the prompt
  material no longer contains the effect — the artwork must come back without it
  baked in — and lettering goes stale because it now has an item to draw. No
  unrelated stage is rebuilt.
- **The negatives are kept honest.** A panel that letters its own SFX must tell
  the image model not to draw one, or a regenerated raster keeps the baked effect
  underneath the drawn one. The prohibition is added when the panel does not
  already carry one, rather than left as a trap for the next generation pass.

The module performs no generation, calls no provider, and reads no clock beyond
the manifest timestamp and the event log, exactly as the provider boundary in
``AGENTS.md`` requires.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .comic_sol import (
    IDENTIFIER,
    canonical_event_record,
    read_json,
)
# The manifest timestamp format is owned by the lifecycle module; reusing it keeps
# `updated_at` identical to every other operation that touches `project.json`.
from .comic_sol import _utc_now as _manifest_timestamp
from .core_primitives import PANEL_ID_PATTERN, canonical_artifact_bytes
from .project_io import (
    ProjectTransaction,
    contained_project_path,
    read_contained_bytes,
)
from .schema import read_project_manifest
from .sfx_verification import (
    DETERMINISTIC_LETTERING,
    DETERMINISTIC_SFX_NEGATIVE,
    GENERATED_VISUAL,
    is_sfx,
    negatives_prohibit_generated_sfx,
    sfx_render_mode,
)


STORYBOARD_PATH = "plan/storyboard.json"
SFX_AUDIT_SCHEMA_VERSION = "1.0"
# Every raster the rejected effect appears in, archived in derivation order. The
# accepted raw attempt is copied rather than merely referenced: the regeneration
# this repair asks for overwrites `panels/raw/{panel-id}.png`, so a record that
# only stored its hash would name bytes no path resolves to.
ARCHIVED_KINDS = ("raw", "clean", "lettered")
# The project-wide identifier shape, re-checked here rather than assumed, because
# the text ID becomes part of an archive file name and a path segment is never
# taken on trust from a JSON document or a command line.
TEXT_ID_PATTERN = IDENTIFIER


def _audit_relative(panel_id: str) -> str:
    """Return the audit-record path for one panel."""
    return f"panels/{panel_id}/sfx-audit.json"


def _clean_relative(project_dir: Path, panel_id: str) -> str:
    """Return the clean raster path for either supported panel layout."""
    canonical = f"panels/{panel_id}/clean.png"
    if contained_project_path(project_dir, canonical).is_file():
        return canonical
    return f"panels/clean/{panel_id}.png"


def _read_audit(project_dir: Path, panel_id: str) -> dict[str, object]:
    """Read the existing audit record, or return an empty one."""
    relative = _audit_relative(panel_id)
    path = contained_project_path(project_dir, relative)
    if not path.is_file():
        return {
            "kind": "sfx-audit",
            "panel_id": panel_id,
            "replacements": [],
            "schema_version": SFX_AUDIT_SCHEMA_VERSION,
        }
    record = read_json(path)
    if (
        record.get("kind") != "sfx-audit"
        or record.get("panel_id") != panel_id
        or record.get("schema_version") != SFX_AUDIT_SCHEMA_VERSION
        or not isinstance(record.get("replacements"), list)
    ):
        raise ValueError(f"SFX audit record is not readable: {relative}")
    return record


def _archive_relative(panel_id: str, text_id: str, sequence: int, kind: str) -> str:
    """Return the archive path one preserved raster occupies."""
    return (
        f"panels/{panel_id}/sfx-audit/{text_id}.attempt-{sequence}.{kind}.png"
    )


def _free_sequence(project_dir: Path, panel_id: str, text_id: str) -> int:
    """Return the first archive sequence with no slot of any kind taken.

    The slot is probed rather than derived from the record length so a repair can
    never overwrite evidence, even if a previous run committed the archive and its
    record separately. Every kind is probed, not just the first one written, so a
    partially populated sequence is skipped whole.
    """
    number = 1
    while True:
        if not any(
            contained_project_path(
                project_dir, _archive_relative(panel_id, text_id, number, kind)
            ).exists()
            for kind in ARCHIVED_KINDS
        ):
            return number
        number += 1


def _locate(
    storyboard: dict[str, object], panel_id: str, text_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Return the storyboard panel and SFX item a replacement addresses."""
    pages = storyboard.get("pages")
    if not isinstance(pages, list):
        raise ValueError("storyboard pages must be an array")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("panels"), list):
            continue
        for panel in page["panels"]:
            if not isinstance(panel, dict) or panel.get("id") != panel_id:
                continue
            text_items = panel.get("text")
            if not isinstance(text_items, list):
                raise ValueError(f"storyboard panel {panel_id} text must be an array")
            for item in text_items:
                if not isinstance(item, dict) or item.get("id") != text_id:
                    continue
                if not is_sfx(item):
                    raise ValueError(
                        f"text item {text_id} is not SFX; only SFX has a render mode"
                    )
                mode = sfx_render_mode(item)
                if mode != GENERATED_VISUAL:
                    raise ValueError(
                        f"text item {text_id} is already {mode}; there is no "
                        "generated effect to replace"
                    )
                return panel, item
            raise ValueError(f"storyboard panel {panel_id} has no text item {text_id}")
    raise ValueError(f"storyboard has no panel {panel_id}")


def replace_generated_sfx(
    project_dir: Path,
    panel_id: str,
    text_id: str,
    *,
    reason: str,
) -> dict[str, object]:
    """Route one generated SFX item to deterministic lettering, preserving evidence."""
    if not isinstance(panel_id, str) or PANEL_ID_PATTERN.fullmatch(panel_id) is None:
        raise ValueError("invalid panel ID")
    if not isinstance(text_id, str) or TEXT_ID_PATTERN.fullmatch(text_id) is None:
        raise ValueError("invalid text ID")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("replacement reason must not be empty")
    reason = reason.strip()
    project_dir = Path(project_dir).resolve(strict=True)

    with ProjectTransaction(project_dir, "replace-generated-sfx") as transaction:
        manifest_path = contained_project_path(
            project_dir, "project.json", must_exist=True
        )
        manifest = read_project_manifest(manifest_path, normalize_legacy=False)
        if manifest.get("status") == "BLOCKED":
            raise ValueError(
                "cannot repair a BLOCKED project; resume it before replacing SFX"
            )
        storyboard = read_json(
            contained_project_path(project_dir, STORYBOARD_PATH, must_exist=True)
        )
        panel, item = _locate(storyboard, panel_id, text_id)

        # Evidence first: a repair that has already edited the plan cannot prove
        # what the rejected artwork looked like.
        clean_relative = _clean_relative(project_dir, panel_id)
        clean_path = contained_project_path(project_dir, clean_relative)
        if not clean_path.is_file():
            raise ValueError(
                f"panel {panel_id} has no clean raster to audit at {clean_relative}; "
                "author the effect as deterministic lettering instead of repairing it"
            )
        sequence = _free_sequence(project_dir, panel_id, text_id)
        sources = {
            "raw": f"panels/raw/{panel_id}.png",
            "clean": clean_relative,
            "lettered": f"panels/{panel_id}/lettered.png",
        }
        archived: list[dict[str, str]] = []
        for kind in ARCHIVED_KINDS:
            source_relative = sources[kind]
            source = contained_project_path(project_dir, source_relative)
            if not source.is_file():
                continue
            payload = read_contained_bytes(project_dir, source_relative)
            archive_relative = _archive_relative(panel_id, text_id, sequence, kind)
            transaction.stage_bytes(archive_relative, payload)
            archived.append({
                "kind": kind,
                "path": archive_relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "source_path": source_relative,
            })

        # The plan edit itself, in place, so item order and every other authored
        # field survive untouched.
        item["render_mode"] = DETERMINISTIC_LETTERING
        negatives = panel.get("negative")
        negative_added = False
        if not isinstance(negatives, list):
            raise ValueError(f"storyboard panel {panel_id} negative must be an array")
        # The prohibition every panel already carries permits the authored effect,
        # which is the one now being lettered, so it does not cover this case.
        if not negatives_prohibit_generated_sfx(panel):
            negatives.append(DETERMINISTIC_SFX_NEGATIVE)
            negative_added = True

        storyboard_payload = canonical_artifact_bytes(storyboard)
        transaction.stage_bytes(STORYBOARD_PATH, storyboard_payload)
        storyboard_sha256 = hashlib.sha256(storyboard_payload).hexdigest()

        record = _read_audit(project_dir, panel_id)
        replacements = list(record["replacements"])  # type: ignore[arg-type]
        entry: dict[str, object] = {
            "archived": archived,
            "content": item.get("content"),
            "from_render_mode": GENERATED_VISUAL,
            "negative_added": DETERMINISTIC_SFX_NEGATIVE if negative_added else None,
            "reason": reason,
            "sequence": sequence,
            "text_id": text_id,
            "to_render_mode": DETERMINISTIC_LETTERING,
        }
        replacements.append(entry)
        record["replacements"] = replacements
        transaction.stage_bytes(
            _audit_relative(panel_id), canonical_artifact_bytes(record)
        )

        # Re-bind the storyboard descriptor so the edit reaches consumers through
        # stage input keys instead of faulting the storyboard stage's own artifact.
        # This is refused rather than skipped when it cannot be applied: returning
        # success while leaving the manifest hash disagreeing with the plan would
        # hand back a project that every later validation reports as broken, for a
        # reason the command that broke it never mentioned.
        artifacts = manifest.get("artifacts")
        descriptor = artifacts.get("storyboard") if isinstance(artifacts, dict) else None
        if not isinstance(descriptor, dict) or descriptor.get("path") != STORYBOARD_PATH:
            raise ValueError(
                "manifest has no storyboard artifact descriptor bound to "
                f"{STORYBOARD_PATH}; record the storyboard stage before repairing SFX"
            )
        descriptor["sha256"] = storyboard_sha256
        manifest["updated_at"] = _manifest_timestamp()
        transaction.stage_bytes("project.json", canonical_artifact_bytes(manifest))

        events = canonical_event_record(
            "sfx.replacement-recorded",
            {
                "attempt": sequence,
                "from": GENERATED_VISUAL,
                "panel_id": panel_id,
                "text_id": text_id,
                "to": DETERMINISTIC_LETTERING,
            },
        )
        events += canonical_event_record(
            "artifact.regenerated",
            {"artifact_path": STORYBOARD_PATH, "reused": False},
        )
        transaction.append_bytes(
            "logs/events.jsonl", events, repair_torn_jsonl=True
        )

    return {
        "archived": archived,
        "audit_path": _audit_relative(panel_id),
        "negative_added": negative_added,
        # Stated in the result because the next step is conditional and the
        # condition is a visual judgement this command cannot make. When the model
        # simply omitted the effect, re-lettering is the whole fix. When it drew a
        # faulty one, that ink is still in the clean raster and the panel has to be
        # regenerated — and `promote_attempt` refuses to replace an accepted raster
        # until the panel's QA record asks for a repair, so the review has to be
        # written first. Leaving that unsaid would strand the operator at a refusal.
        "next_action": (
            f"Re-letter the project. If the rejected effect is still visible in "
            f"panels/{panel_id}/clean.png, re-review qa/panels/{panel_id}.json to "
            f"`regenerate` before promoting a new raster."
        ),
        "panel_id": panel_id,
        "render_mode": DETERMINISTIC_LETTERING,
        "sequence": sequence,
        "text_id": text_id,
    }


class _SfxRepairArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"invalid invocation: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SfxRepairArgumentParser(prog="sfx_repair.py")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--panel", required=True, dest="panel_id")
    parser.add_argument("--text-id", required=True, dest="text_id")
    parser.add_argument(
        "--reason",
        required=True,
        help="Why the generated effect was rejected, retained in the audit record.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Replace one generated SFX with deterministic lettering from the CLI."""
    try:
        arguments = _build_parser().parse_args(argv)
        result = replace_generated_sfx(
            arguments.project_dir,
            arguments.panel_id,
            arguments.text_id,
            reason=arguments.reason,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
