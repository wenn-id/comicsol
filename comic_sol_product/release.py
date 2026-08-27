"""Distribution acceptance contracts for Comic Sol release artifacts."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.parser import HeaderParser
from pathlib import Path
from typing import Iterable


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


def _canonical_skill_files(canonical_root: Path | None = None) -> dict[str, bytes]:
    """Read the one synchronized bundle that every artifact must carry verbatim."""
    root = canonical_root or CANONICAL_SKILL_ROOT
    if not root.is_dir():
        raise ValueError(f"canonical Skill bundle is missing: {root.name}")
    payload: dict[str, bytes] = {}
    for source in sorted(root.rglob("*")):
        if source.is_symlink():
            raise ValueError("canonical Skill bundle contains a link")
        if source.is_file():
            payload[source.relative_to(root).as_posix()] = source.read_bytes()
    if "SKILL.md" not in payload:
        raise ValueError("canonical Skill bundle is missing SKILL.md")
    return payload


def canonical_skill_members(canonical_root: Path | None = None) -> frozenset[str]:
    """Return every wheel member name for the complete synchronized bundle."""
    return frozenset(
        f"{PACKAGED_SKILL_PREFIX}{relative}" for relative in _canonical_skill_files(canonical_root)
    )


def _skill_inventory() -> frozenset[str]:
    """Inventory the whole bundle from a checkout; fall back when installed."""
    if CANONICAL_SKILL_ROOT.is_dir():
        return canonical_skill_members()
    return frozenset()


REQUIRED_WHEEL_MEMBERS = frozenset(
    {
        "comic_sol_product/engine/comic_sol.py",
        "comic_sol_product/engine/dogfood_report.py",
        "comic_sol_product/engine/handoff.py",
        "comic_sol_product/engine/handoff_archive.py",
        "comic_sol_product/engine/quality_records.py",
        "comic_sol_product/engine/normalize_panels.py",
        "comic_sol_product/engine/typography.py",
        "comic_sol_product/engine/layouts.py",
        "comic_sol_product/engine/page_quality.py",
        "comic_sol_product/engine/pdf_quality.py",
        "comic_sol_product/engine/quality_sample.py",
        "comic_sol_product/engine/starter_templates.py",
        "comic_sol_product/assets/fonts/ComicNeue-Regular.ttf",
        "comic_sol_product/assets/fonts/ComicNeue-Bold.ttf",
        "comic_sol_product/templates/manifest.json",
        "comic_sol_product/skill/SKILL.md",
        "comic_sol_product/skill/references/workflow.md",
        "comic_sol_product/skill/references/visual-qa.md",
        "comic_sol_product/skill/references/starter-templates.md",
    }
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
        "/scripts/comic_sol.py",
        "/scripts/dogfood_report.py",
        "/scripts/handoff.py",
        "/scripts/handoff_archive.py",
        "/scripts/starter_templates.py",
        "/assets/fonts/ComicNeue-Regular.ttf",
        "/templates/manifest.json",
        "/references/workflow.md",
        "/references/starter-templates.md",
        "/comic_sol_product/cli.py",
    }
    | {
        f"/templates/starters/v1/{starter_id}/{relative}"
        for starter_id in _PACKAGED_STARTER_IDS
        for relative in _PACKAGED_STARTER_FILES
    }
)

FORBIDDEN_WHEEL_MEMBERS = frozenset(
    {
        "comic_sol_product/engine/assemble_release.py",
        "comic_sol_product/engine/benchmark.py",
        "comic_sol_product/engine/benchmark_summary.py",
        "comic_sol_product/engine/build_portable.py",
        "comic_sol_product/engine/clean_install_smoke.py",
        "comic_sol_product/engine/installed_mcp_smoke.py",
        "comic_sol_product/engine/portable_release_smoke.py",
        "comic_sol_product/engine/test_quality_matrix.py",
        "comic_sol_product/engine/support.py",
        "comic_sol_product/engine/quality-matrix/README.md",
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
    """Prove the wheel carries exactly the synchronized bundle, byte for byte."""
    canonical = _canonical_skill_files(canonical_root)
    packaged: dict[str, bytes] = {}
    for name in archive.namelist():
        if name.startswith(PACKAGED_SKILL_PREFIX) and not name.endswith("/"):
            packaged[name[len(PACKAGED_SKILL_PREFIX) :]] = archive.read(name)
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
        if not separator or not relative:
            continue
        stream = archive.extractfile(member)
        if stream is None:
            continue
        packaged[relative] = stream.read()
    _compare_payload(packaged, canonical, kind="sdist")


def validate_wheel_members(members: Iterable[str]) -> None:
    member_set = set(members)
    missing = sorted(REQUIRED_WHEEL_MEMBERS - member_set)
    if missing:
        raise ValueError("wheel is missing required members: " + ", ".join(missing))
    forbidden = sorted(FORBIDDEN_WHEEL_MEMBERS & member_set)
    if forbidden:
        raise ValueError("wheel contains build-only members: " + ", ".join(forbidden))


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
