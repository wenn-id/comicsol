"""Offline contract tests for the container runtime hardening work (#214).

These tests pin the declared hardening of ``Dockerfile``, ``compose.yaml``,
the release/qualification/test workflows, the user-facing documentation, and
the two container scripts. They are intentionally offline: the fail-closed
runtime assertions themselves live in ``scripts/container_runtime_audit.py``
and run wherever a Docker engine is available.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from comic_sol_product import __version__ as RELEASE_VERSION  # noqa: E402
from scripts.container_runtime_audit import (  # noqa: E402
    DEFAULT_PIDS_LIMIT,
    EXPECTED_GID,
    EXPECTED_UID,
    MCP_INITIALIZE_REQUEST,
    _probe_args,
)
from scripts.container_sbom import (  # noqa: E402
    container_sbom_name,
    container_tar_name,
    parse_base_image,
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class ComposeHardeningContractTests(unittest.TestCase):
    def test_compose_applies_the_full_hardening_set(self):
        compose = read("compose.yaml")
        for fragment in (
            "read_only: true",
            "network_mode: none",
            'user: "10001:10001"',
            "init: true",
            "cap_drop:",
            "- ALL",
            "pids: 64",
            "no-new-privileges:true",
            "- comic-sol-data:/data",
            "- /tmp",
        ):
            self.assertIn(fragment, compose)
        self.assertIsNone(re.search(r"^\s*-\s*seccomp", compose, re.MULTILINE))
        self.assertIn(f"comic-sol:{RELEASE_VERSION}", compose)

    def test_compose_privileged_escape_hatches_are_absent(self):
        compose = read("compose.yaml")
        for forbidden in ("privileged: true", "cap_add:", "ipc: host", "pid: host"):
            self.assertNotIn(forbidden, compose)


class DockerfileIdentityContractTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = read("Dockerfile")

    def test_single_digest_pinned_base_argument(self):
        self.assertIn("ARG PYTHON_BASE=", self.dockerfile)
        self.assertEqual(1, self.dockerfile.count("sha256:"))
        self.assertIn("python:3.11.15-slim@sha256:", self.dockerfile)
        self.assertIn("FROM ${PYTHON_BASE} AS builder", self.dockerfile)
        self.assertIn("FROM ${PYTHON_BASE}\n", self.dockerfile)

    def test_fixed_numeric_runtime_identity(self):
        self.assertIn("groupadd --gid 10001 comic-sol", self.dockerfile)
        self.assertIn("useradd --uid 10001 --gid 10001 --home-dir /home/comic-sol", self.dockerfile)
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertNotIn("USER comic-sol", self.dockerfile)
        self.assertNotIn("USER root", self.dockerfile)

    def test_healthcheck_and_volume_contract(self):
        self.assertIn('CMD ["comic-sol", "doctor", "--output-root", "/data"]', self.dockerfile)
        self.assertIn('VOLUME ["/data"]', self.dockerfile)
        self.assertIn('CMD ["mcp", "--root", "/data"]', self.dockerfile)


class WorkflowContractTests(unittest.TestCase):
    def test_release_workflow_has_no_base_digest_build_argument(self):
        workflow = read(".github/workflows/release.yml")
        self.assertNotIn("DOCKER_BASE_DIGEST", workflow)
        self.assertNotIn("--build-arg", workflow)
        self.assertIn("docker build -t comic-sol:${{ needs.prepare.outputs.version }} .", workflow)

    def test_release_container_job_audits_scans_and_generates_sbom(self):
        workflow = read(".github/workflows/release.yml")
        for fragment in (
            "scripts/container_runtime_audit.py",
            "--expect-version ${{ needs.prepare.outputs.version }}",
            "scripts/container_sbom.py",
            "--output container-assets/comic-sol-${{ needs.prepare.outputs.version }}"
            "-linux-x86_64.container.sbom.json",
            "pip_audit -r requirements/locks/runtime-linux-x86_64.txt",
            "docker save comic-sol:${{ needs.prepare.outputs.version }}",
            "mkdir -p container-assets",
        ):
            self.assertIn(fragment, workflow)

    def test_qualification_covers_container_hardening_and_sbom(self):
        workflow = read(".github/workflows/release-qualification.yml")
        for fragment in (
            "--pattern '*.container.sbom.json'",
            "qualification/*.container.sbom.json",
            "validate_sbom_schema",
            "scripts/container_runtime_audit.py",
            '--image "comic-sol:${version}"',
            '--expect-version "$version"',
            "container-hardening-audit",
            "container-sbom",
        ):
            self.assertIn(fragment, workflow)

    def test_pr_ci_audits_the_built_image_against_the_package_version(self):
        workflow = read(".github/workflows/tests.yml")
        self.assertIn("scripts/container_runtime_audit.py --image comic-sol:pr", workflow)
        self.assertIn(
            "--expect-version \"$(python -c 'from comic_sol_product import __version__; print(__version__)')\"",
            workflow,
        )


class ContainerScriptContractTests(unittest.TestCase):
    def test_expected_identity_constants(self):
        self.assertEqual(10001, EXPECTED_UID)
        self.assertEqual(10001, EXPECTED_GID)
        self.assertEqual(64, DEFAULT_PIDS_LIMIT)

    def test_probe_arguments_mirror_the_compose_hardening(self):
        arguments = _probe_args("docker", "comic-sol:test", DEFAULT_PIDS_LIMIT)
        joined = " ".join(arguments)
        for fragment in (
            "--rm",
            "--init",
            "--read-only",
            "--network none",
            "--cap-drop ALL",
            "--pids-limit 64",
            "--security-opt no-new-privileges",
            "--tmpfs /tmp",
        ):
            self.assertIn(fragment, joined)
        self.assertEqual("docker run --rm", " ".join(arguments[:3]))

    def test_mcp_initialize_request_is_a_valid_handshake(self):
        self.assertTrue(MCP_INITIALIZE_REQUEST.endswith("\n"))
        request = json.loads(MCP_INITIALIZE_REQUEST)
        self.assertEqual("2.0", request["jsonrpc"])
        self.assertEqual(1, request["id"])
        self.assertEqual("initialize", request["method"])
        self.assertIn("protocolVersion", request["params"])
        self.assertIn("clientInfo", request["params"])

    def test_container_payload_names_match_the_release_version(self):
        self.assertEqual(
            f"comic-sol-{RELEASE_VERSION}-linux-x86_64.container.sbom.json",
            container_sbom_name(RELEASE_VERSION),
        )
        self.assertEqual(
            f"comic-sol-{RELEASE_VERSION}-linux-x86_64.container.tar",
            container_tar_name(RELEASE_VERSION),
        )

    def test_base_image_parses_from_the_single_dockerfile_argument(self):
        base = parse_base_image()
        self.assertIn("python:3.11.15-slim@sha256:", base)
        with tempfile.TemporaryDirectory() as temporary:
            tampered = Path(temporary) / "Dockerfile"
            tampered.write_text("FROM python:3.11.15-slim\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                parse_base_image(tampered)
            tampered.write_text(
                'ARG PYTHON_BASE="python:3.11.15-slim@sha256:aaa"\n'
                'ARG PYTHON_BASE="python:3.11.15-slim@sha256:bbb"\n',
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                parse_base_image(tampered)


class ContainerDocumentationContractTests(unittest.TestCase):
    def test_install_documents_seccomp_policy_and_hardening(self):
        install = read("docs/install.md")
        for fragment in (
            "effective seccomp policy",
            "**default profile**",
            "seccomp=unconfined",
            "10001:10001",
            "--cap-drop ALL",
            "--pids-limit 64",
            "--read-only",
            "--network none",
            "comic-sol-<version>-linux-x86_64.container.sbom.json",
            "scripts/container_runtime_audit.py",
            "CapEff",
            "NoNewPrivs",
        ):
            self.assertIn(fragment, install)
        self.assertNotIn("comic-sol:2.0.0rc4", install)
        self.assertNotIn("comic-sol-2.0.0rc4-linux-x86_64.container.tar", install)

    def test_support_matrix_publishes_container_limitations(self):
        matrix = read("docs/support-matrix.md")
        for fragment in (
            "default seccomp profile",
            "seccomp=unconfined",
            "10001:10001",
            "read-only root filesystem",
            "no network",
            "all Linux capabilities",
            "64-process limit",
            "comic-sol-<version>-linux-x86_64.container.sbom.json",
        ):
            self.assertIn(fragment, matrix)

    def test_trust_chain_records_the_container_sbom_subject(self):
        chain = read("docs/releases/release-trust-chain.md")
        self.assertIn("| `comic-sol-X-linux-x86_64.container.sbom.json` |", chain)
        self.assertIn("container_runtime_audit.py", chain)
        self.assertIn("pip-audit", chain)
        self.assertNotIn("DOCKER_BASE_DIGEST", chain)


if __name__ == "__main__":
    unittest.main()
