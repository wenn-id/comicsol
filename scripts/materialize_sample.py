from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = ROOT / "samples/sunlight-courier"
PANEL_ID = re.compile(r"p\d{2}-\d{2}")


def materialize_sample(project: Path = DEFAULT_SAMPLE) -> tuple[Path, ...]:
    project = Path(project).resolve(strict=True)
    manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
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
        (panel_id, project / f"panels/{panel_id}/clean.png")
        for panel_id in panels
    )
    for panel_id, source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"missing canonical panel: {panel_id}")
    outputs: list[Path] = []
    for panel_id, source in sources:
        for relative in (
            f"panels/raw/{panel_id}.png",
            f"panels/clean/{panel_id}.png",
        ):
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            outputs.append(destination)
    return tuple(outputs)


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
