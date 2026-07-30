#!/usr/bin/env python3
"""Local-only evidence labeling for Comic Sol quality samples.

This module never invokes an image provider. Live visual evidence is accepted only
for an already retained attempt with explicit provenance supplied by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from project_io import contained_project_path, durable_atomic_write

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EvidenceModeError(ValueError):
    """Raised when evidence provenance is incomplete or unsafe."""


def build_evidence_record(
    mode: str,
    *,
    retained_attempt: str | None = None,
    attempt_sha256: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    references: Sequence[str] = (),
    reviewer_method: str | None = None,
    limitations: Sequence[str] = (),
) -> dict[str, object]:
    """Build an explicitly labeled deterministic or retained live evidence record."""
    if mode == "deterministic":
        return {
            "mode": "deterministic",
            "scope": "mechanics-only",
            "proves_visual_quality": False,
        }
    if mode != "live-visual":
        raise EvidenceModeError("mode must be deterministic or live-visual")
    if not retained_attempt:
        raise EvidenceModeError("live-visual mode requires a retained attempt")
    if not provider or not model:
        raise EvidenceModeError("live-visual mode requires provider and model")
    if not isinstance(attempt_sha256, str) or not SHA256_PATTERN.fullmatch(
        attempt_sha256
    ):
        raise EvidenceModeError("live-visual mode requires retained attempt sha256")
    if not reviewer_method:
        raise EvidenceModeError("live-visual mode requires reviewer method")
    return {
        "mode": "live-visual",
        "scope": "retained-attempt-visual-review",
        "proves_visual_quality": True,
        "retained_attempt": retained_attempt,
        "attempt_sha256": attempt_sha256,
        "provider": provider,
        "model": model,
        "references": list(references),
        "reviewer_method": reviewer_method,
        "limitations": list(limitations),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record local-only Comic Sol quality evidence provenance"
    )
    parser.add_argument("project_dir", type=Path)
    parser.add_argument(
        "--mode", required=True, choices=("deterministic", "live-visual")
    )
    parser.add_argument("--retained-attempt")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--reviewer-method")
    parser.add_argument("--limitation", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    project_dir = arguments.project_dir.resolve()
    try:
        attempt_hash = None
        if arguments.mode == "live-visual":
            if not arguments.retained_attempt:
                raise EvidenceModeError(
                    "live-visual mode requires a retained attempt"
                )
            attempt = contained_project_path(
                project_dir, arguments.retained_attempt, must_exist=True
            )
            if not attempt.is_file():
                raise EvidenceModeError("retained attempt must be a local file")
            attempt_hash = hashlib.sha256(attempt.read_bytes()).hexdigest()
        record = build_evidence_record(
            arguments.mode,
            retained_attempt=arguments.retained_attempt,
            attempt_sha256=attempt_hash,
            provider=arguments.provider,
            model=arguments.model,
            references=arguments.reference,
            reviewer_method=arguments.reviewer_method,
            limitations=arguments.limitation,
        )
        payload = (
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        durable_atomic_write(project_dir / "qa/evidence.json", payload)
        return 0
    except (EvidenceModeError, OSError, ValueError) as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
