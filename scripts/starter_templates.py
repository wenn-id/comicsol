"""Fixed, provider-neutral starter-project catalog and loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from .project_io import (
    MAX_SOURCE_BYTES,
    read_bytes_nofollow,
    read_json_nofollow,
    validate_source_bytes,
)

STARTER_VERSION = "v1"
STARTER_IDS = (
    "minimal-one-page",
    "dialogue-two-page",
    "action-focused",
)
STARTER_FILES = (
    "source/input.txt",
    "source/request.json",
    "plan/story-plan.json",
    "plan/character-bible.json",
    "plan/storyboard.json",
)


@dataclass(frozen=True, slots=True)
class StarterProject:
    """One validated starter bundle ready for atomic project materialization."""

    starter_id: str
    version: str
    source: bytes
    request: dict[str, object]
    story_plan: dict[str, object]
    character_bible: dict[str, object]
    storyboard: dict[str, object]
    page_count: int
    panel_ids: tuple[str, ...]


CATALOG: Mapping[str, tuple[str, str]] = {
    starter_id: (STARTER_VERSION, f"starters/{STARTER_VERSION}/{starter_id}")
    for starter_id in STARTER_IDS
}


def _first_issue(issues: list[Any]) -> str | None:
    if not issues:
        return None
    issue = issues[0]
    return f"{issue.path} {issue.field}: {issue.message}"


def _bundle_root(templates_root: Path, starter_id: str) -> Path:
    """Resolve only fixed catalog entries; user input never becomes a path."""
    if not isinstance(starter_id, str) or starter_id not in CATALOG:
        raise ValueError("starter must be one of " + ", ".join(STARTER_IDS))
    version, relative = CATALOG[starter_id]
    if version != STARTER_VERSION:
        raise ValueError("starter catalog version is unsupported")
    return Path(templates_root).absolute() / Path(relative)


def load_starter(
    templates_root: Path,
    starter_id: str,
    *,
    request_validator: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> StarterProject:
    """Load and validate one exact, path-safe starter bundle."""
    root = _bundle_root(templates_root, starter_id)
    expected = set(STARTER_FILES)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"starter bundle is missing or invalid: {starter_id}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = missing[0] if missing else extra[0]
        kind = "missing" if missing else "unexpected"
        raise ValueError(f"starter bundle {starter_id} has {kind} file: {detail}")

    source = read_bytes_nofollow(root / "source/input.txt", max_bytes=MAX_SOURCE_BYTES)
    validate_source_bytes(source)
    request = cast(dict[str, object], read_json_nofollow(root / "source/request.json"))
    if request_validator is not None:
        request = request_validator(request)
    story = cast(dict[str, object], read_json_nofollow(root / "plan/story-plan.json"))
    characters = cast(dict[str, object], read_json_nofollow(root / "plan/character-bible.json"))
    storyboard = cast(dict[str, object], read_json_nofollow(root / "plan/storyboard.json"))

    from .validate_project import (
        validate_character_bible,
        validate_story_plan,
        validate_storyboard,
    )

    for label, issues in (
        ("story plan", validate_story_plan(story)),
        ("character bible", validate_character_bible(characters)),
        ("storyboard", validate_storyboard(storyboard, story, characters)),
    ):
        problem = _first_issue(issues)
        if problem is not None:
            raise ValueError(f"starter {starter_id} has invalid {label}: {problem}")

    known_characters = {
        item.get("id") for item in characters.get("characters", []) if isinstance(item, dict)
    }
    for scene in story.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        for character_id in scene.get("characters", []):
            if character_id not in known_characters:
                raise ValueError(f"starter {starter_id} story plan references an unknown character")

    pages = storyboard["pages"]
    assert isinstance(pages, list)
    panel_ids = tuple(
        panel["id"]
        for page in pages
        if isinstance(page, dict)
        for panel in page.get("panels", [])
        if isinstance(panel, dict) and isinstance(panel.get("id"), str)
    )
    return StarterProject(
        starter_id=starter_id,
        version=STARTER_VERSION,
        source=source,
        request=request,
        story_plan=story,
        character_bible=characters,
        storyboard=storyboard,
        page_count=len(pages),
        panel_ids=panel_ids,
    )


def inventory_starters(
    templates_root: Path,
    *,
    request_validator: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> tuple[list[str], list[str]]:
    """Return valid fixed IDs and sanitized failures for doctor diagnostics."""
    available: list[str] = []
    invalid: list[str] = []
    for starter_id in STARTER_IDS:
        try:
            load_starter(
                templates_root,
                starter_id,
                request_validator=request_validator,
            )
        except Exception as error:
            invalid.append(f"{starter_id} ({type(error).__name__})")
        else:
            available.append(starter_id)
    return available, invalid
