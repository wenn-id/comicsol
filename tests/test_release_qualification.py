import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile

from scripts.release_qualification import aggregate_summaries
from scripts.release_qualification import validate_published_metadata
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

    def test_verify_payload_checksums_accepts_duplicate_global_manifest_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = root / "install.sh"
            payload.write_bytes(b"selected installer\n")
            selected = __import__("hashlib").sha256(payload.read_bytes()).hexdigest()
            manifest = root / "SHA256SUMS"
            manifest.write_text(
                "\n".join(
                    [
                        f"{'0' * 64}  install.sh",
                        f"{selected}  install.sh",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(1, verify_payload_checksums(manifest, [payload]))

    def test_qualification_harness_help_runs_without_source_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            harness = root / "release_qualification.py"
            harness.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, str(harness), "--help"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

    def test_validate_published_metadata_rejects_malformed_sbom_types_and_references(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metadata = root / "runtime.metadata.json"
            sbom = root / "runtime.sbom.json"
            metadata.write_text(
                json.dumps(
                    {
                        "product": "comic-sol",
                        "platform": "linux",
                        "signature_status": "unsigned",
                        "artifacts": ["runtime.zip"],
                    }
                ),
                encoding="utf-8",
            )
            components = [
                {"name": name, "purl": f"pkg:generic/{name}@1", "bom-ref": name}
                for name in ("pillow", "mcp", "pyinstaller", "python")
            ]
            sbom_record = {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "serialNumber": "urn:uuid:12345678-1234-5678-1234-567812345678",
                "metadata": {
                    "component": {
                        "name": "comic-sol",
                        "version": __import__("comic_sol_product").__version__,
                        "bom-ref": "root",
                    },
                    "properties": [
                        {"name": "comic-sol:release:artifact", "value": "runtime.zip"},
                        {"name": "comic-sol:release:platform", "value": "linux"},
                        {"name": "comic-sol:release:architecture", "value": "x86_64"},
                    ],
                },
                "components": components,
                "dependencies": [
                    {"ref": "root", "dependsOn": [item["bom-ref"] for item in components]}
                ],
            }
            sbom.write_text(json.dumps(sbom_record), encoding="utf-8")
            sbom_record["components"] = {"pillow": "invalid"}
            sbom.write_text(json.dumps(sbom_record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "collection types"):
                validate_published_metadata(
                    metadata, sbom, artifact="runtime.zip", platform="linux", version="2.0.0rc4"
                )

            sbom_record["components"] = components
            sbom_record["dependencies"] = [{"ref": "root", "dependsOn": ["unknown"]}]
            sbom.write_text(json.dumps(sbom_record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unknown dependency"):
                validate_published_metadata(
                    metadata, sbom, artifact="runtime.zip", platform="linux", version="2.0.0rc4"
                )

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
