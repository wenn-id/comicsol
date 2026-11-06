"""Distribution acceptance contracts for Comic Sol release artifacts."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.parser import HeaderParser
from pathlib import Path
from typing import Iterable

from .runtime_contract import RUNTIME_SCRIPTS
from .skill_payload import SKILL_SHARED_FILES, shared_package_path


_PACKAGED_STARTER_IDS = (
    "minimal-one-page",
    "dialogue-two-page",
    "action-focused",
)
_PACKAGED_STARTER_FILES = (
    "source/input.txt",
    "source/request.json",
    "plan/story-plan.json",
    "plan/character-bible.json",
    "plan/storyboard.json",
)

CANONICAL_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "comic-sol"
PACKAGED_SKILL_PREFIX = "comic_sol_product/skill/"
SDIST_SKILL_PREFIX = "skills/comic-sol/"


def _is_generated_bytecode(relative: str) -> bool:
    """Byte-compilation output is environment-generated, never packaged payload."""
    return "__pycache__" in Path(relative).parts or relative.endswith((".pyc", ".pyo"))


def _canonical_skill_files(canonical_root: Path | None = None) -> dict[str, bytes]:
    """Read the one synchronized bundle that every artifact must carry verbatim."""
    root = canonical_root or CANONICAL_SKILL_ROOT
    if not root.is_dir():
        raise ValueError(f"canonical Skill bundle is missing: {root.name}")
    payload: dict[str, bytes] = {}
    for source in sorted(root.rglob("*")):
        relative = source.relative_to(root).as_posix()
        if _is_generated_bytecode(relative):
            continue
        if source.is_symlink():
            raise ValueError("canonical Skill bundle contains a link")
        if source.is_file():
            payload[relative] = source.read_bytes()
    if "SKILL.md" not in payload:
        raise ValueError("canonical Skill bundle is missing SKILL.md")
    return payload


def canonical_skill_members(canonical_root: Path | None = None) -> frozenset[str]:
    """Return physical wheel members after subtracting byte-identical shared files."""
    root = canonical_root or CANONICAL_SKILL_ROOT
    repository_root = root.parents[1]
    canonical = _canonical_skill_files(root)
    return frozenset(
        f"{PACKAGED_SKILL_PREFIX}{relative}"
        for relative, payload in canonical.items()
        if relative not in SKILL_SHARED_FILES
        or not (repository_root / relative).is_file()
        or (repository_root / relative).read_bytes() != payload
    )


def _skill_inventory() -> frozenset[str]:
    """Inventory the whole bundle from a checkout; fall back when installed."""
    if CANONICAL_SKILL_ROOT.is_dir():
        return canonical_skill_members()
    return frozenset()


_ENGINE_MODULE_PREFIX = "comic_sol_product/engine/"
_RUNTIME_ENGINE_MEMBERS = frozenset(
    {f"{_ENGINE_MODULE_PREFIX}{name}" for name in RUNTIME_SCRIPTS}
    | {f"{_ENGINE_MODULE_PREFIX}__init__.py"}
)

REQUIRED_WHEEL_MEMBERS = frozenset(
    {
        "comic_sol_product/runtime_contract.py",
        "comic_sol_product/skill_payload.py",
        "comic_sol_product/engine_api.py",
        "comic_sol_product/assets/fonts/ComicNeue-Regular.ttf",
        "comic_sol_product/assets/fonts/ComicNeue-Bold.ttf",
        "comic_sol_product/assets/fonts/NotoSans-Regular.ttf",
        "comic_sol_product/assets/fonts/OFL-ComicNeue.txt",
        "comic_sol_product/assets/fonts/OFL-NotoSans.txt",
        "comic_sol_product/templates/manifest.json",
        "comic_sol_product/skill/SKILL.md",
        "comic_sol_product/skill/references/workflow.md",
        "comic_sol_product/skill/references/visual-qa.md",
        "comic_sol_product/skill/references/starter-templates.md",
    }
    | _RUNTIME_ENGINE_MEMBERS
    | {
        f"comic_sol_product/templates/starters/v1/{starter_id}/{relative}"
        for starter_id in _PACKAGED_STARTER_IDS
        for relative in _PACKAGED_STARTER_FILES
    }
    | _skill_inventory()
)

REQUIRED_SDIST_SUFFIXES = frozenset(
    {
        "/SKILL.md",
        "/comic_sol_product/runtime_contract.py",
        "/comic_sol_product/skill_payload.py",
        "/assets/fonts/ComicNeue-Regular.ttf",
        "/templates/manifest.json",
        "/references/workflow.md",
        "/references/starter-templates.md",
        "/comic_sol_product/cli.py",
    }
    | {f"/scripts/{name}" for name in RUNTIME_SCRIPTS}
    | {
        f"/templates/starters/v1/{starter_id}/{relative}"
        for starter_id in _PACKAGED_STARTER_IDS
        for relative in _PACKAGED_STARTER_FILES
    }
)

FORBIDDEN_WHEEL_DATA_MEMBERS = frozenset(
    {
        "comic_sol_product/engine/quality-matrix/README.md",
        "comic_sol_product/assets/comic-sol-logo.png",
        "comic_sol_product/assets/comic-sol-thumbnail.png",
    }
)

REQUIRED_PROJECT_URLS = frozenset(
    {
        "Homepage",
        "Repository",
        "Documentation",
        "Changelog",
        "Issue Tracker",
        "Security Policy",
    }
)

REQUIRED_METADATA_CLASSIFIER_PREFIXES = (
    "Development Status ::",
    "Intended Audience ::",
    "Operating System ::",
    "Programming Language :: Python ::",
    "Topic ::",
)


def _compare_payload(packaged: dict[str, bytes], canonical: dict[str, bytes], *, kind: str) -> None:
    if packaged == canonical:
        return
    missing = sorted(set(canonical) - set(packaged))
    extra = sorted(set(packaged) - set(canonical))
    differing = sorted(
        relative
        for relative in set(canonical) & set(packaged)
        if canonical[relative] != packaged[relative]
    )
    problems = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if extra:
        problems.append("unexpected: " + ", ".join(extra))
    if differing:
        problems.append("differing bytes: " + ", ".join(differing))
    raise ValueError(
        f"{kind} Skill payload does not match the synchronized bundle: " + "; ".join(problems)
    )


def validate_wheel_skill_payload(
    archive: zipfile.ZipFile, *, canonical_root: Path | None = None
) -> None:
    """Prove the wheel reconstructs the synchronized bundle from shared files."""
    canonical = _canonical_skill_files(canonical_root)
    packaged: dict[str, bytes] = {}
    names = archive.namelist()
    for name in names:
        if name.startswith(PACKAGED_SKILL_PREFIX) and not name.endswith("/"):
            relative = name[len(PACKAGED_SKILL_PREFIX) :]
            if _is_generated_bytecode(relative):
                continue
            if relative in packaged:
                raise ValueError(f"wheel Skill payload contains a duplicate member: {relative}")
            packaged[relative] = archive.read(name)
    for relative in sorted(SKILL_SHARED_FILES):
        source_member = f"comic_sol_product/{shared_package_path(relative)}"
        if names.count(source_member) != 1:
            raise ValueError(f"wheel must contain one shared Skill source: {source_member}")
        source_payload = archive.read(source_member)
        if relative in packaged:
            if packaged[relative] == source_payload:
                raise ValueError(f"wheel duplicates shared Skill payload: {relative}")
            continue
        packaged[relative] = source_payload
    _compare_payload(packaged, canonical, kind="wheel")


def validate_sdist_skill_payload(
    archive: tarfile.TarFile, *, canonical_root: Path | None = None
) -> None:
    """Prove the sdist carries exactly the synchronized bundle, byte for byte."""
    canonical = _canonical_skill_files(canonical_root)
    packaged: dict[str, bytes] = {}
    for member in archive.getmembers():
        if not member.isfile():
            continue
        _, separator, relative = member.name.partition(SDIST_SKILL_PREFIX)
        if not separator or not relative or _is_generated_bytecode(relative):
            continue
        stream = archive.extractfile(member)
        if stream is None:
            continue
        if relative in packaged:
            raise ValueError(f"sdist Skill payload contains a duplicate member: {relative}")
        packaged[relative] = stream.read()
    _compare_payload(packaged, canonical, kind="sdist")


def validate_wheel_members(members: Iterable[str]) -> None:
    """Validate required members and reject engine modules outside the runtime inventory."""
    member_set = set(members)
    missing = sorted(REQUIRED_WHEEL_MEMBERS - member_set)
    if missing:
        raise ValueError("wheel is missing required members: " + ", ".join(missing))
    engine_modules = {
        member
        for member in member_set
        if member.startswith(_ENGINE_MODULE_PREFIX) and member.endswith(".py")
    }
    unexpected = sorted(engine_modules - _RUNTIME_ENGINE_MEMBERS)
    if unexpected:
        raise ValueError(
            "wheel contains engine modules outside the runtime inventory: " + ", ".join(unexpected)
        )
    forbidden = sorted(FORBIDDEN_WHEEL_DATA_MEMBERS & member_set)
    if forbidden:
        raise ValueError("wheel contains build-only members: " + ", ".join(forbidden))
    non_runtime_assets = sorted(
        member
        for member in member_set
        if member.startswith("comic_sol_product/assets/")
        and not member.startswith("comic_sol_product/assets/fonts/")
    )
    if non_runtime_assets:
        raise ValueError("wheel contains non-runtime assets: " + ", ".join(non_runtime_assets))


def validate_wheel_metadata(metadata_text: str) -> None:
    """Fail closed when the wheel METADATA stops describing the project.

    Project URLs, classifiers, keywords, and maintainer fields are the
    discovery surface PyPI and package tooling read; a wheel that loses them
    still installs, so only this gate makes the loss visible.
    """

    message = HeaderParser().parsestr(metadata_text)
    problems: list[str] = []

    for field in ("Name", "Version", "Summary"):
        if not (message.get(field) or "").strip():
            problems.append(f"missing required field: {field}")

    requires_python = (message.get("Requires-Python") or "").strip()
    if not requires_python.startswith(">=3.11"):
        problems.append(f"Requires-Python must require 3.11+: {requires_python!r}")

    if not (message.get("Author") or "").strip() and not (message.get("Maintainer") or "").strip():
        problems.append("neither Author nor Maintainer is set")

    if not (message.get("Keywords") or "").strip():
        problems.append("missing Keywords")

    classifiers = message.get_all("Classifier") or []
    if not classifiers:
        problems.append("missing Classifiers")
    for prefix in REQUIRED_METADATA_CLASSIFIER_PREFIXES:
        if not any(classifier.startswith(prefix) for classifier in classifiers):
            problems.append(f"no {prefix.rstrip(' ::')} classifier")

    project_urls = [
        (entry.split(",", 1)[0].strip() if "," in entry else entry.strip())
        for entry in (message.get_all("Project-URL") or [])
    ]
    missing_urls = sorted(REQUIRED_PROJECT_URLS - set(project_urls))
    if missing_urls:
        problems.append("missing Project-URL entries: " + ", ".join(missing_urls))

    # The license is declared as a PEP 639 SPDX expression; a legacy license
    # classifier alongside it is deprecated metadata, not a second license.
    if not (message.get("License-Expression") or "").strip():
        problems.append("missing License-Expression")
    if any(classifier.startswith("License ::") for classifier in classifiers):
        problems.append("deprecated License :: classifier alongside License-Expression")

    if problems:
        raise ValueError("wheel METADATA failed validation: " + "; ".join(problems))


def wheel_metadata_member(members: Iterable[str]) -> str:
    member_set = set(members)
    candidates = sorted(
        name for name in member_set if name.endswith(".dist-info/METADATA") and name.count("/") == 1
    )
    if len(candidates) != 1:
        raise ValueError(
            "wheel must contain exactly one <package>.dist-info/METADATA member, found: "
            + ", ".join(candidates)
        )
    return candidates[0]


def validate_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        validate_wheel_members(archive.namelist())
        metadata_member = wheel_metadata_member(archive.namelist())
        validate_wheel_metadata(archive.read(metadata_member).decode("utf-8"))
        if CANONICAL_SKILL_ROOT.is_dir():
            validate_wheel_skill_payload(archive)


def validate_sdist_members(members: Iterable[str]) -> None:
    names = set(members)
    missing = sorted(
        suffix
        for suffix in REQUIRED_SDIST_SUFFIXES
        if not any(name.endswith(suffix) for name in names)
    )
    if missing:
        raise ValueError("sdist is missing required members: " + ", ".join(missing))


def validate_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        validate_sdist_members(archive.getnames())
        if CANONICAL_SKILL_ROOT.is_dir():
            validate_sdist_skill_payload(archive)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m comic_sol_product.release")
    parser.add_argument("artifacts", nargs="+", type=Path)
    arguments = parser.parse_args(argv)
    for artifact in arguments.artifacts:
        if artifact.suffix == ".whl":
            validate_wheel(artifact)
        elif artifact.name.endswith(".tar.gz"):
            validate_sdist(artifact)
        else:
            raise ValueError(f"unsupported distribution artifact: {artifact.name}")
        print(f"distribution-ok: {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
