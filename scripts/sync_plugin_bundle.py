from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "skills/comic-sol"
HOST_SPECIFIC_REFERENCES = {"capability-detection.md", "image-provider-setup.md"}


def synchronized_paths() -> list[Path]:
    paths = [Path("SKILL.md")]
    paths.extend(
        path.relative_to(ROOT)
        for path in sorted((ROOT / "references").glob("*.md"))
        if path.name not in HOST_SPECIFIC_REFERENCES
    )
    for directory in ("templates", "assets/fonts"):
        paths.extend(
            path.relative_to(ROOT)
            for path in sorted((ROOT / directory).iterdir())
            if path.is_file()
        )
    paths.extend(
        Path("scripts") / path.name
        for path in sorted((BUNDLE / "scripts").iterdir())
        if path.is_file()
    )
    return paths


def destination(relative: Path) -> Path:
    return BUNDLE / relative


def check() -> list[Path]:
    return [
        relative
        for relative in synchronized_paths()
        if not destination(relative).is_file()
        or (ROOT / relative).read_bytes() != destination(relative).read_bytes()
    ]


def sync() -> None:
    for relative in synchronized_paths():
        target = destination(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.check:
        drift = check()
        if drift:
            print("plugin bundle drift: " + ", ".join(path.as_posix() for path in drift))
            return 1
        return 0
    sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
