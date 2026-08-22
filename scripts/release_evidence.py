"""Create deterministic machine-readable and Markdown release promotion evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _read_object(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return record


def _read_array(path: Path) -> list[Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, list):
        raise RuntimeError(f"expected a JSON array: {path}")
    return record


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _artifact_identity(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RuntimeError("Actions artifact identity is invalid")
    artifact_id = record.get("id")
    name = record.get("name")
    url = record.get("url")
    archive_url = record.get("archive_download_url")
    if (
        not isinstance(artifact_id, int)
        or not isinstance(name, str)
        or not name
        or not isinstance(url, str)
        or not url.startswith("https://")
        or not isinstance(archive_url, str)
        or not archive_url.startswith("https://")
    ):
        raise RuntimeError("Actions artifact identity is invalid")
    result = {
        "id": artifact_id,
        "name": name,
        "api_url": url,
        "archive_download_url": archive_url,
    }
    digest = record.get("digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RuntimeError("Actions artifact digest is invalid")
        result["service_digest"] = digest
    return result


def build_evidence(
    *,
    candidate_identity: dict[str, Any],
    qualification: dict[str, Any],
    benchmark: dict[str, Any],
    qualification_sha256: str,
    benchmark_sha256: str,
    actions_artifacts: list[Any],
    deployment: dict[str, Any],
    required_reviewers: list[Any],
    repository: str,
    run_id: str,
    run_url: str,
    environment: str,
    trigger_actor: str,
) -> dict[str, Any]:
    """Build canonical promotion-ready evidence, failing closed on every identity."""
    if candidate_identity.get("state") != "candidate":
        raise RuntimeError("candidate identity is not in candidate state")
    if qualification.get("status") != "passed" or qualification.get("decision") != "RELEASE READY":
        raise RuntimeError("qualification evidence is not release-ready")
    if benchmark.get("status") != "passed" or benchmark.get("decision") != "NO REGRESSION":
        raise RuntimeError("benchmark evidence is not release-ready")
    if not _is_sha256(qualification_sha256) or not _is_sha256(benchmark_sha256):
        raise RuntimeError("summary digest identity is invalid")

    tag = candidate_identity.get("tag")
    commit_sha = candidate_identity.get("candidate_commit")
    manifest = candidate_identity.get("checksum_manifest")
    payloads = candidate_identity.get("payloads")
    if (
        not isinstance(tag, str)
        or not tag.startswith("v")
        or not isinstance(commit_sha, str)
        or len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise RuntimeError("candidate tag or commit identity is invalid")
    if not isinstance(manifest, dict) or not _is_sha256(manifest.get("sha256")):
        raise RuntimeError("candidate checksum identity is invalid")
    if not isinstance(payloads, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not _is_sha256(item.get("sha256"))
        for item in payloads
    ):
        raise RuntimeError("candidate payload identity is invalid")
    payload_names = [item["name"] for item in payloads]
    if len(payload_names) != len(set(payload_names)):
        raise RuntimeError("candidate payload names are not unique")

    qualification_candidate = qualification.get("candidate")
    expected_qualification_identity = {
        "tag": tag,
        "commit_sha": commit_sha,
        "checksum_manifest_sha256": manifest["sha256"],
    }
    if qualification_candidate != expected_qualification_identity:
        raise RuntimeError("qualification evidence belongs to another candidate")
    if benchmark.get("candidate_sha") != commit_sha:
        raise RuntimeError("benchmark evidence belongs to another candidate")

    candidate_artifacts = candidate_identity.get("actions_artifacts", [])
    if not isinstance(candidate_artifacts, list):
        raise RuntimeError("candidate Actions artifact identity is invalid")
    artifact_records: dict[int, dict[str, Any]] = {}
    for item in [*candidate_artifacts, *actions_artifacts]:
        artifact = _artifact_identity(item)
        existing = artifact_records.get(artifact["id"])
        if existing is not None and existing != artifact:
            raise RuntimeError("conflicting Actions artifact identity")
        artifact_records[artifact["id"]] = artifact
    artifact_names = {item["name"] for item in artifact_records.values()}
    required_artifacts = {
        "benchmark-results",
        "candidate-identity",
        "release-qualification-summary",
    }
    if not required_artifacts <= artifact_names:
        raise RuntimeError("required Actions artifact identity is missing")

    if (
        deployment.get("sha") != commit_sha
        or deployment.get("environment") != environment
        or not isinstance(deployment.get("id"), int)
        or not isinstance(deployment.get("api_url"), str)
        or not isinstance(deployment.get("html_audit_url"), str)
    ):
        raise RuntimeError("deployment evidence belongs to another candidate or environment")
    if not required_reviewers:
        raise RuntimeError("required reviewer identity is invalid")
    if any(not isinstance(reviewer, dict) for reviewer in required_reviewers):
        raise RuntimeError("required reviewer identity is invalid")

    release_url = f"https://github.com/{repository}/releases/tag/{quote(tag, safe='')}"
    download_base_url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}"
    payload_records = [
        {
            "name": item["name"],
            "sha256": item["sha256"],
            "url": f"{download_base_url}/{quote(item['name'], safe='')}",
        }
        for item in sorted(payloads, key=lambda item: item["name"])
    ]
    artifacts = sorted(artifact_records.values(), key=lambda item: (item["name"], item["id"]))
    return {
        "schema_version": 1,
        "state": "promotion-ready",
        "candidate": {
            "tag": tag,
            "commit_sha": commit_sha,
            "release_url": release_url,
            "workflow_run_id": run_id,
            "workflow_run_url": run_url,
            "actions_artifacts": artifacts,
        },
        "gates": {
            "full_tests_including_pip_audit": {"status": "passed", "run_url": run_url},
            "release_blocking_quality": {"status": "passed", "run_url": run_url},
            "codeql": {"status": "passed", "run_url": run_url},
            "benchmark": {
                "status": "passed",
                "decision": benchmark["decision"],
                "summary_sha256": benchmark_sha256,
                "artifact": next(item for item in artifacts if item["name"] == "benchmark-results"),
            },
        },
        "supply_chain": {
            "checksum_manifest": {
                "name": manifest.get("name"),
                "sha256": manifest["sha256"],
                "url": f"{download_base_url}/SHA256SUMS",
                "signature_url": f"{download_base_url}/SHA256SUMS.sigstore.json",
            },
            "payloads": payload_records,
            "sboms": [item for item in payload_records if item["name"].endswith(".sbom.json")],
            "attestations": {
                "subjects": [item["name"] for item in payload_records],
                "url": f"https://github.com/{repository}/attestations",
            },
        },
        "qualification": {
            "status": qualification["status"],
            "decision": qualification["decision"],
            "summary_sha256": qualification_sha256,
            "summaries": qualification.get("summaries", {}),
            "artifact": next(
                item for item in artifacts if item["name"] == "release-qualification-summary"
            ),
        },
        "promotion": {
            "environment": environment,
            "deployment": deployment,
            "required_reviewers": required_reviewers,
            "actual_reviewer": None,
            "workflow_trigger_actor": trigger_actor,
            "reviewer_identity_source": (
                "The configured eligible reviewers are recorded from Environment protection; "
                "the actual approver remains null because the workflow does not safely expose it."
            ),
        },
    }


def write_evidence(record: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    """Write canonical JSON and a compact human-readable companion."""
    json_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    candidate = record["candidate"]
    checksums = record["supply_chain"]["checksum_manifest"]
    promotion = record["promotion"]
    deployment = promotion["deployment"]
    lines = [
        "# Release promotion evidence",
        "",
        f"- State: **{record['state']}**",
        f"- Candidate: `{candidate['tag']}` at `{candidate['commit_sha']}`",
        f"- Orchestration run: {candidate['workflow_run_url']}",
        f"- Signed checksum manifest: `{checksums['sha256']}` ({checksums['url']})",
        f"- Benchmark: **{record['gates']['benchmark']['decision']}** "
        f"(`{record['gates']['benchmark']['summary_sha256']}`)",
        f"- Qualification: **{record['qualification']['decision']}** "
        f"(`{record['qualification']['summary_sha256']}`)",
        f"- Protected environment: `{promotion['environment']}`",
        f"- Deployment API: {deployment['api_url']}",
        f"- Deployment audit: {deployment['html_audit_url']}",
        f"- Configured required reviewers: {len(promotion['required_reviewers'])}",
        "- Actual environment reviewer: unavailable; no workflow actor is asserted as approver.",
        "",
        "## Supply-chain evidence",
        "",
        f"- Attestations: {record['supply_chain']['attestations']['url']}",
        f"- SBOM count: {len(record['supply_chain']['sboms'])}",
        f"- Payload count: {len(record['supply_chain']['payloads'])}",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-identity", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--qualification-sha256", required=True)
    parser.add_argument("--benchmark-delta", type=Path, required=True)
    parser.add_argument("--benchmark-sha256", required=True)
    parser.add_argument("--actions-artifacts", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--required-reviewers", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--trigger-actor", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    record = build_evidence(
        candidate_identity=_read_object(arguments.candidate_identity),
        qualification=_read_object(arguments.qualification),
        benchmark=_read_object(arguments.benchmark_delta),
        qualification_sha256=arguments.qualification_sha256,
        benchmark_sha256=arguments.benchmark_sha256,
        actions_artifacts=_read_array(arguments.actions_artifacts),
        deployment=_read_object(arguments.deployment),
        required_reviewers=_read_array(arguments.required_reviewers),
        repository=arguments.repository,
        run_id=arguments.run_id,
        run_url=arguments.run_url,
        environment=arguments.environment,
        trigger_actor=arguments.trigger_actor,
    )
    arguments.json_output.parent.mkdir(parents=True, exist_ok=True)
    arguments.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    write_evidence(record, arguments.json_output, arguments.markdown_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
