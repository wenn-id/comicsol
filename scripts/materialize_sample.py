from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "scripts"

from .core_primitives import PANEL_ID_PATTERN
from .project_io import contained_project_path, open_contained, read_contained_bytes


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = ROOT / "samples/sunlight-courier"
PANEL_ID = PANEL_ID_PATTERN


def materialize_sample(project: Path = DEFAULT_SAMPLE) -> tuple[Path, ...]:
    project = Path(project).resolve(strict=True)
    manifest = json.loads(read_contained_bytes(project, "project.json"))
    if not isinstance(manifest, dict):
        raise ValueError("project.json must contain an object")
    panels = manifest.get("panels")
    if (
        not isinstance(panels, list)
        or not panels
        or any(
            not isinstance(panel_id, str)
            or PANEL_ID.fullmatch(panel_id) is None
            for panel_id in panels
        )
        or len(set(panels)) != len(panels)
    ):
        raise ValueError(
            "project.json panels must contain unique canonical panel IDs"
        )
    sources = tuple(
        (panel_id, f"panels/{panel_id}/clean.png")
        for panel_id in panels
    )
    payloads: dict[str, bytes] = {}
    for panel_id, source_relative in sources:
        try:
            source = contained_project_path(
                project, source_relative, must_exist=True
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"missing canonical panel: {panel_id}")
        if not source.is_file():
            raise FileNotFoundError(f"missing canonical panel: {panel_id}")
        payloads[panel_id] = read_contained_bytes(project, source_relative)
    planned_outputs = tuple(
        (panel_id, relative, contained_project_path(project, relative))
        for panel_id in panels
        for relative in (
            f"panels/raw/{panel_id}.png",
            f"panels/clean/{panel_id}.png",
        )
    )
    for relative in ("panels/raw", "panels/clean"):
        contained_project_path(project, relative).mkdir(parents=True, exist_ok=True)
    for panel_id, relative, _destination in planned_outputs:
        with open_contained(
            project,
            relative,
            flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o666,
        ) as stream:
            stream.write(payloads[panel_id])
    return tuple(destination for _panel_id, _relative, destination in planned_outputs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", type=Path, default=DEFAULT_SAMPLE)
    arguments = parser.parse_args(argv)
    try:
        outputs = materialize_sample(arguments.project)
    except (OSError, ValueError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(f"materialized {len(outputs)} sample panel copies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
