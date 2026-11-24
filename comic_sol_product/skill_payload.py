"""Files supplied to the packaged Skill from their single runtime copy."""

SKILL_SHARED_FILES = frozenset(
    {
        "assets/fonts/ComicNeue-Bold.ttf",
        "assets/fonts/ComicNeue-Regular.ttf",
        "assets/fonts/NotoSans-Regular.ttf",
        "assets/fonts/OFL-ComicNeue.txt",
        "assets/fonts/OFL-NotoSans.txt",
    }
)


def shared_package_path(relative: str) -> str:
    """Return the package member that supplies one shared Skill file."""
    if relative not in SKILL_SHARED_FILES:
        raise ValueError(f"unrecognized shared Skill member: {relative}")
    return relative
