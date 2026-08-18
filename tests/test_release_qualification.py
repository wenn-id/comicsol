import json
import unittest
from pathlib import Path
import tempfile

from scripts.release_qualification import aggregate_summaries
from scripts.release_qualification import verify_payload_checksums


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-qualification.yml"
SCRIPT = ROOT / "scripts" / "release_qualification.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DOCS = ROOT / "docs" / "install.md"


class ReleaseQualificationContractTests(unittest.TestCase):
    def test_aggregate_summaries_produces_release_ready_only_when_all_p0_gates_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for platform in ("linux", "macos", "windows", "wsl"):
                (root / f"summary-{platform}.json").write_text(
                    json.dumps(
                        {
                            "platform": platform,
                            "status": "passed",
                            "checks": ["version", "doctor", "lifecycle", "uninstall-preservation"],
                            "exceptions": [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            output = root / "release-summary.json"
            result = aggregate_summaries(root, output)
            self.assertEqual("RELEASE READY", result["decision"])
            self.assertEqual("passed", result["status"])
            self.assertEqual(4, result["platform_count"])
            self.assertTrue(output.is_file())

            broken = json.loads((root / "summary-windows.json").read_text(encoding="utf-8"))
            broken["status"] = "failed"
            broken["exceptions"] = ["known broken artifact"]
            (root / "summary-windows.json").write_text(json.dumps(broken) + "\n", encoding="utf-8")
            blocked = aggregate_summaries(root, output)
            self.assertEqual("RELEASE BLOCKED", blocked["decision"])
            self.assertEqual("failed", blocked["status"])
            self.assertIn("windows", blocked["failed_platforms"])

    def test_verify_payload_checksums_covers_every_published_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = []
            for name, content in (
                ("runtime.zip", b"runtime"),
                ("install.sh", b"#!/bin/sh\n"),
                ("runtime.metadata.json", b"{}\n"),
                ("runtime.sbom.json", b"{}\n"),
            ):
                path = root / name
                path.write_bytes(content)
                payloads.append(path)
            manifest = root / "SHA256SUMS"
            lines = []
            import hashlib

            for path in payloads:
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
            lines.append(f"{'0' * 64}  other-platform.zip")
            manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(4, verify_payload_checksums(manifest, payloads))
            (root / "runtime.sbom.json").write_bytes(b"tampered\n")
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                verify_payload_checksums(manifest, payloads)

    def test_release_qualification_workflow_runs_source_p0_gates_and_aggregates(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for token in (
            "source:",
            "SHA256SUMS",
            "unittest discover",
            "test_golden_pipeline",
            "test_resume",
            "test_lifecycle_failures",
            "pip_audit",
            "Run pip-audit security gate",
            "RELEASE READY",
            "RELEASE BLOCKED",
            "release-qualification-summary",
        ):
            self.assertIn(token, workflow)

    def test_release_qualification_script_exists_with_required_interfaces(self):
        self.assertTrue(SCRIPT.is_file())
        source = SCRIPT.read_text(encoding="utf-8")
        for token in (
            "--archive",
            "--platform",
            "--sha256",
            "--installer",
            "--summary",
            "--version",
            "doctor",
            "init",
            "validate",
            "uninstall",
            "env=env",
            "HOME",
        ):
            self.assertIn(token, source)

    def test_release_publish_workflow_keeps_provenance_attestation_gate(self):
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("attest-build-provenance", workflow)
        self.assertIn("bundles/**/*.zip", workflow)
        self.assertIn("bundles/**/*.whl", workflow)
        self.assertIn("bundles/**/*.tar.gz", workflow)

    def test_release_qualification_workflow_uses_release_asset_not_checkout_build(self):
        self.assertTrue(WORKFLOW.is_file())
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for token in (
            "workflow_dispatch",
            "inputs:",
            "tag:",
            "gh release download",
            "linux",
            "macos",
            "windows",
            "wsl",
            "release_qualification.py",
            "qualification-summary",
            "if-no-files-found: error",
        ):
            self.assertIn(token, workflow)
        self.assertNotIn("python -m build", workflow)
        self.assertIn("actions/checkout", workflow)

    def test_workflow_records_platform_specific_exceptions_in_summary(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Platform-specific exceptions", workflow)
        self.assertIn("WSL2", workflow)
        self.assertIn("not available", workflow)
        self.assertIn("upload-artifact", workflow)
        self.assertIn("$qualificationRoot", workflow)
        self.assertIn("qualification/summary-wsl.json", workflow)

    def test_install_docs_describe_release_qualification_and_wsl(self):
        docs = DOCS.read_text(encoding="utf-8")
        self.assertIn("release qualification", docs.lower())
        self.assertIn("WSL2", docs)
        self.assertIn("intended release artifact", docs)
        self.assertIn("comic-sol --version", docs)
        self.assertIn("comic-sol doctor", docs)
        self.assertIn("user projects", docs.lower())


if __name__ == "__main__":
    unittest.main()
