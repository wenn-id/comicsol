#!/usr/bin/env python3
"""Validate the immutable identity of a tag-triggered release.

The helper deliberately uses only the Python standard library.  GitHub API
responses and remote refs are supplied as files so every validation path can be
tested offline.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


_TAG_PATTERN = re.compile(r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?)\Z")
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_REF_PATTERN_LITERAL = re.compile(r"[A-Za-z0-9._/-]\Z")
_REQUIRED_TAG_RULES = frozenset({"creation", "update", "deletion", "required_signatures"})
_APPROVED_BYPASS_ACTOR = {
    "actor_type": "RepositoryRole",
    "actor_id": 5,
    "bypass_mode": "always",
}


class IdentityError(RuntimeError):
    """Raised when release identity evidence is absent or inconsistent."""


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise IdentityError(f"{label} must be a lowercase 40-character Git SHA")
    return value


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise IdentityError(detail)
    return result


def _resolve_commit(repository: Path, revision: str, label: str) -> str:
    result = _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return _require_sha(result.stdout.strip(), label)


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IdentityError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise IdentityError(f"{label} must contain a JSON object")
    return value


def validate_tag_and_version(tag: str, package_version: str) -> str:
    """Return the exact package version encoded by a canonical release tag."""
    match = _TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise IdentityError(f"invalid release tag: {tag}")
    version = match.group("version")
    if package_version != version:
        raise IdentityError(
            f"release tag version {version} does not match package version {package_version}"
        )
    return version


def validate_tag_api_metadata(
    metadata: Mapping[str, Any], *, tag: str, tag_object_sha: str, candidate_commit: str
) -> None:
    """Require GitHub to identify the captured annotated tag as signed and verified."""
    if metadata.get("tag") != tag:
        raise IdentityError("tag API embedded name does not match the release tag")
    if metadata.get("sha") != tag_object_sha:
        raise IdentityError("tag API object SHA does not match the captured tag object")
    target = metadata.get("object")
    if not isinstance(target, dict):
        raise IdentityError("tag API metadata is missing the tagged object")
    if target.get("type") != "commit" or target.get("sha") != candidate_commit:
        raise IdentityError("tag API target does not match the candidate commit")
    verification = metadata.get("verification")
    if not isinstance(verification, dict):
        raise IdentityError("tag API metadata is missing signature verification")
    if verification.get("verified") is not True:
        reason = verification.get("reason", "unknown")
        raise IdentityError(f"annotated tag signature is not verified: {reason}")
    if not isinstance(verification.get("signature"), str) or not verification["signature"].strip():
        raise IdentityError("verified annotated tag metadata is missing its signature")


def prepare_identity(
    *,
    repository: Path,
    tag: str,
    event_ref: str,
    event_sha: str,
    checkout_sha: str,
    package_version: str,
    main_ref: str,
    tag_api_metadata: Mapping[str, Any],
) -> dict[str, str]:
    """Validate and capture the complete release identity from local/API evidence."""
    version = validate_tag_and_version(tag, package_version)
    expected_ref = f"refs/tags/{tag}"
    if event_ref != expected_ref:
        raise IdentityError(f"event ref {event_ref} does not match {expected_ref}")

    repository = repository.resolve()
    tag_object_sha = _require_sha(
        _git(repository, "rev-parse", "--verify", expected_ref).stdout.strip(),
        "tag object SHA",
    )
    object_type = _git(repository, "cat-file", "-t", tag_object_sha).stdout.strip()
    if object_type != "tag":
        raise IdentityError("release ref must be an annotated tag object")
    candidate_commit = _resolve_commit(repository, expected_ref, "candidate commit")
    event_commit = _resolve_commit(repository, event_sha, "event commit")
    checkout_commit = _resolve_commit(repository, checkout_sha, "checkout commit")
    if event_commit != candidate_commit:
        raise IdentityError("event SHA does not identify the candidate commit")
    if checkout_commit != candidate_commit:
        raise IdentityError("checkout does not identify the candidate commit")

    main_sha = _resolve_commit(repository, main_ref, "protected main SHA")
    ancestry = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        candidate_commit,
        main_sha,
        check=False,
    )
    if ancestry.returncode == 1:
        raise IdentityError("candidate commit is not an ancestor of protected main")
    if ancestry.returncode != 0:
        detail = ancestry.stderr.strip() or "cannot verify protected-main ancestry"
        raise IdentityError(detail)

    validate_tag_api_metadata(
        tag_api_metadata,
        tag=tag,
        tag_object_sha=tag_object_sha,
        candidate_commit=candidate_commit,
    )
    return {
        "candidate_commit": candidate_commit,
        "main_sha": main_sha,
        "tag": tag,
        "tag_object_sha": tag_object_sha,
        "version": version,
    }


def _compile_ref_pattern(pattern: object) -> re.Pattern[str] | None:
    """Compile the supported GitHub pathname-aware ref-pattern subset.

    GitHub evaluates ruleset ref-name conditions with pathname semantics: a
    single ``*`` or ``?`` cannot match ``/``, while the recursive ``**/`` form
    can consume zero or more path components. Unsupported constructs are
    rejected rather than approximated.
    """
    if pattern == "~ALL":
        return None
    if not isinstance(pattern, str) or not pattern.startswith("refs/"):
        raise IdentityError("ruleset ref pattern must be ~ALL or start with refs/")
    if not pattern or pattern.endswith("/"):
        raise IdentityError(f"malformed ruleset ref pattern: {pattern!r}")

    expression: list[str] = ["\\A"]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                if index + 2 < len(pattern) and pattern[index + 2] == "*":
                    raise IdentityError(f"unsupported ruleset ref pattern: {pattern!r}")
                if index + 2 < len(pattern) and pattern[index + 2] == "/":
                    expression.append("(?:.*/)?")
                    index += 3
                    continue
                expression.append("[^/]*")
                index += 2
                continue
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        elif _REF_PATTERN_LITERAL.fullmatch(character) is not None:
            expression.append(re.escape(character))
        else:
            raise IdentityError(f"unsupported ruleset ref pattern: {pattern!r}")
        index += 1
    expression.append("\\Z")
    return re.compile("".join(expression))


def github_ref_pattern_matches(pattern: object, ref: str) -> bool:
    """Return whether a supported GitHub ruleset pattern matches ``ref``."""
    if not isinstance(ref, str) or not ref.startswith("refs/"):
        raise IdentityError("release ref must start with refs/")
    compiled = _compile_ref_pattern(pattern)
    return compiled is None or compiled.fullmatch(ref) is not None


def _condition_patterns(value: object, label: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise IdentityError(f"ruleset {label} patterns must be a non-empty list")
    for pattern in value:
        _compile_ref_pattern(pattern)
    return value


def validate_tag_rulesets(
    rulesets: Sequence[Mapping[str, Any]], *, release_ref: str
) -> dict[str, Any]:
    """Validate the effective active tag policy and return canonical evidence."""
    if not release_ref.startswith("refs/tags/"):
        raise IdentityError("release ruleset validation requires a tag ref")

    matching: list[tuple[int, set[str], list[Mapping[str, Any]]]] = []
    seen_ids: set[int] = set()
    for ruleset in rulesets:
        if not isinstance(ruleset, Mapping):
            raise IdentityError("ruleset evidence must contain JSON objects")
        if ruleset.get("target") != "tag" or ruleset.get("enforcement") != "active":
            continue
        ruleset_id = ruleset.get("id")
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int) or ruleset_id <= 0:
            raise IdentityError("active tag ruleset ID is invalid")
        if ruleset_id in seen_ids:
            raise IdentityError(f"duplicate active tag ruleset ID: {ruleset_id}")
        seen_ids.add(ruleset_id)

        conditions = ruleset.get("conditions")
        if not isinstance(conditions, Mapping):
            raise IdentityError(f"active tag ruleset {ruleset_id} conditions are invalid")
        ref_name = conditions.get("ref_name")
        if not isinstance(ref_name, Mapping):
            raise IdentityError(f"active tag ruleset {ruleset_id} ref_name condition is invalid")
        includes = _condition_patterns(ref_name.get("include"), "include")
        excludes_value = ref_name.get("exclude", [])
        if not isinstance(excludes_value, list):
            raise IdentityError("ruleset exclude patterns must be a list")
        for pattern in excludes_value:
            _compile_ref_pattern(pattern)
        included = any(github_ref_pattern_matches(pattern, release_ref) for pattern in includes)
        excluded = any(
            github_ref_pattern_matches(pattern, release_ref) for pattern in excludes_value
        )
        if not included or excluded:
            continue

        rules = ruleset.get("rules")
        if not isinstance(rules, list):
            raise IdentityError(f"matching tag ruleset {ruleset_id} rules are invalid")
        rule_types: set[str] = set()
        for rule in rules:
            if not isinstance(rule, Mapping) or not isinstance(rule.get("type"), str):
                raise IdentityError(f"matching tag ruleset {ruleset_id} contains a malformed rule")
            rule_types.add(rule["type"])

        bypass_actors = ruleset.get("bypass_actors", [])
        if not isinstance(bypass_actors, list):
            raise IdentityError(f"matching tag ruleset {ruleset_id} bypass actors are invalid")
        actors: list[Mapping[str, Any]] = []
        for actor in bypass_actors:
            if not isinstance(actor, Mapping):
                raise IdentityError(f"matching tag ruleset {ruleset_id} bypass actor is malformed")
            identity = {key: actor.get(key) for key in _APPROVED_BYPASS_ACTOR}
            if identity != _APPROVED_BYPASS_ACTOR:
                raise IdentityError(
                    f"matching tag ruleset {ruleset_id} has an unapproved bypass actor"
                )
            actors.append(identity)
        matching.append((ruleset_id, rule_types, actors))

    if not matching:
        raise IdentityError("no active tag ruleset matches the release ref")
    effective_rule_types = set().union(*(rule_types for _, rule_types, _ in matching))
    missing = sorted(_REQUIRED_TAG_RULES - effective_rule_types)
    if missing:
        raise IdentityError(f"effective tag rulesets are missing required rules: {missing}")

    for ruleset_id, rule_types, actors in matching:
        if "creation" in rule_types and actors != [_APPROVED_BYPASS_ACTOR]:
            raise IdentityError(
                f"matching creation-restriction ruleset {ruleset_id} must have exactly "
                "the approved repository-admin bypass actor"
            )
    return {
        "matched_ruleset_ids": sorted(ruleset_id for ruleset_id, _, _ in matching),
        "approved_bypass_actors": [dict(_APPROVED_BYPASS_ACTOR)],
    }


def parse_remote_refs(text: str, tag: str) -> tuple[str, str]:
    """Extract one direct and one peeled ref for ``tag`` from ls-remote output."""
    expected = f"refs/tags/{tag}"
    direct: list[str] = []
    peeled: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.split()
        if len(fields) != 2:
            raise IdentityError("remote tag refs contain a malformed line")
        sha, ref = fields
        _require_sha(sha, "remote ref SHA")
        if ref == expected:
            direct.append(sha)
        elif ref == f"{expected}^{{}}":
            peeled.append(sha)
        else:
            raise IdentityError(f"unexpected remote ref: {ref}")
    if len(direct) != 1:
        raise IdentityError("remote release tag must have exactly one direct ref")
    if len(peeled) != 1:
        raise IdentityError("remote release tag must have exactly one peeled annotated ref")
    return direct[0], peeled[0]


def validate_remote_refs(
    text: str, *, tag: str, tag_object_sha: str, candidate_commit: str
) -> dict[str, str]:
    """Reject deletion, replacement, movement, and lightweight remote tags."""
    tag_object_sha = _require_sha(tag_object_sha, "captured tag object SHA")
    candidate_commit = _require_sha(candidate_commit, "captured candidate commit")
    direct, peeled = parse_remote_refs(text, tag)
    if direct != tag_object_sha:
        raise IdentityError("remote tag object changed after identity capture")
    if peeled != candidate_commit:
        raise IdentityError("remote tag candidate commit changed after identity capture")
    return {"candidate_commit": peeled, "tag": tag, "tag_object_sha": direct}


def validate_release_metadata(
    metadata: Mapping[str, Any],
    remote_refs: str,
    *,
    tag: str,
    tag_object_sha: str,
    candidate_commit: str,
    require_immutable_prerelease: bool = False,
) -> dict[str, Any]:
    """Bind a GitHub Release tag to the captured direct and peeled remote refs."""
    if metadata.get("tag_name") != tag:
        raise IdentityError("release tag_name does not match the captured tag")
    result: dict[str, Any] = validate_remote_refs(
        remote_refs,
        tag=tag,
        tag_object_sha=tag_object_sha,
        candidate_commit=candidate_commit,
    )
    if require_immutable_prerelease:
        expected_state = {"draft": False, "prerelease": True, "immutable": True}
        actual_state = {key: metadata.get(key) for key in expected_state}
        if actual_state != expected_state:
            raise IdentityError(
                "qualification requires a published immutable prerelease "
                "(draft=false, prerelease=true, immutable=true)"
            )
        result["release_state"] = expected_state
    return result


def _load_rulesets(directory: Path) -> list[Mapping[str, Any]]:
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as error:
        raise IdentityError(f"cannot list ruleset evidence: {error}") from error
    if not paths:
        raise IdentityError("ruleset evidence directory contains no JSON files")
    return [_load_json(path, f"ruleset evidence {path.name}") for path in paths]


def _write_result(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="capture and validate initial identity")
    prepare.add_argument("--repository", type=Path, default=Path("."))
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--event-ref", required=True)
    prepare.add_argument("--event-sha", required=True)
    prepare.add_argument("--checkout-sha", required=True)
    prepare.add_argument("--package-version", required=True)
    prepare.add_argument("--main-ref", required=True)
    prepare.add_argument("--tag-api-json", type=Path, required=True)

    rulesets = subparsers.add_parser("rulesets", help="validate effective tag rulesets")
    rulesets.add_argument("--rulesets-dir", type=Path, required=True)
    rulesets.add_argument("--release-ref", required=True)

    remote = subparsers.add_parser("remote", help="validate a captured identity against refs")
    remote.add_argument("--refs-file", type=Path, required=True)
    remote.add_argument("--tag", required=True)
    remote.add_argument("--tag-object-sha", required=True)
    remote.add_argument("--candidate-commit", required=True)

    release = subparsers.add_parser("release", help="validate GitHub Release identity")
    release.add_argument("--release-json", type=Path, required=True)
    release.add_argument("--refs-file", type=Path, required=True)
    release.add_argument("--tag", required=True)
    release.add_argument("--tag-object-sha", required=True)
    release.add_argument("--candidate-commit", required=True)
    release.add_argument("--require-immutable-prerelease", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_identity(
                repository=args.repository,
                tag=args.tag,
                event_ref=args.event_ref,
                event_sha=args.event_sha,
                checkout_sha=args.checkout_sha,
                package_version=args.package_version,
                main_ref=args.main_ref,
                tag_api_metadata=_load_json(args.tag_api_json, "tag API metadata"),
            )
        elif args.command == "rulesets":
            result = validate_tag_rulesets(
                _load_rulesets(args.rulesets_dir),
                release_ref=args.release_ref,
            )
        elif args.command == "remote":
            try:
                refs = args.refs_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise IdentityError(f"cannot read remote refs: {error}") from error
            result = validate_remote_refs(
                refs,
                tag=args.tag,
                tag_object_sha=args.tag_object_sha,
                candidate_commit=args.candidate_commit,
            )
        else:
            try:
                refs = args.refs_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise IdentityError(f"cannot read remote refs: {error}") from error
            result = validate_release_metadata(
                _load_json(args.release_json, "release metadata"),
                refs,
                tag=args.tag,
                tag_object_sha=args.tag_object_sha,
                candidate_commit=args.candidate_commit,
                require_immutable_prerelease=args.require_immutable_prerelease,
            )
    except IdentityError as error:
        print(f"release identity rejected: {error}", file=sys.stderr)
        return 1
    _write_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
