import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile
from unittest import mock

from comic_sol_product import __version__ as _V

from scripts.release_qualification import aggregate_summaries
from scripts.release_qualification import qualify
from scripts.release_qualification import validate_published_metadata
from scripts.release_qualification import verify_payload_checksums


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-qualification.yml"
SCRIPT = ROOT / "scripts" / "release_qualification.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DOCS = ROOT / "docs" / "install.md"


def _write_release_contract(
    root: Path, *, platform: str, architecture: str, version: str = _V
) -> tuple[Path, Path]:
    metadata = root / "runtime.metadata.json"
    sbom = root / "runtime.sbom.json"
    metadata.write_text(
        json.dumps(
            {
                "product": "comic-sol",
                "platform": platform,
                "architecture": architecture,
                "tag": f"v{version}",
                "version": version,
                "signature_file": "SHA256SUMS.sigstore.json",
                "signature_status": "sigstore",
                "artifacts": ["runtime.zip"],
            }
        ),
        encoding="utf-8",
    )
    components = [
        {"name": name, "purl": f"pkg:generic/{name}@1", "bom-ref": name}
        for name in ("pillow", "mcp", "pyinstaller", "python")
    ]
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "serialNumber": "urn:uuid:12345678-1234-5678-1234-567812345678",
                "metadata": {
                    "component": {
                        "name": "comic-sol",
                        "version": version,
                        "bom-ref": "root",
                    },
                    "properties": [
                        {"name": "comic-sol:release:artifact", "value": "runtime.zip"},
                        {"name": "comic-sol:release:platform", "value": platform},
                        {
                            "name": "comic-sol:release:architecture",
                            "value": architecture,
                        },
                    ],
                },
                "components": components,
                "dependencies": [
                    {"ref": "root", "dependsOn": [item["bom-ref"] for item in components]}
                ],
            }
        ),
        encoding="utf-8",
    )
    return metadata, sbom


def _write_qualification_files(root: Path, *, platform: str, architecture: str) -> dict[str, Path]:
    import hashlib

    archive = root / "runtime.zip"
    installer = root / "install.sh"
    signature = root / "SHA256SUMS.sigstore.json"
    archive.write_bytes(b"runtime archive")
    installer.write_bytes(b"#!/bin/sh\n")
    signature.write_text("{}\n", encoding="utf-8")
    metadata, sbom = _write_release_contract(root, platform=platform, architecture=architecture)
    checksums = root / "SHA256SUMS"
    payloads = (archive, installer, metadata, sbom)
    checksums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in payloads
        ),
        encoding="utf-8",
    )
    return {
        "archive": archive,
        "installer": installer,
        "signature": signature,
        "metadata": metadata,
        "sbom": sbom,
        "checksums": checksums,
    }


class ReleaseQualificationContractTests(unittest.TestCase):
    def test_aggregate_summaries_produces_release_ready_only_when_all_p0_gates_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            platform_architectures = {
                "linux": "x86_64",
                "macos": "arm64",
                "windows": "x86_64",
                "wsl": "x86_64",
                "source": None,
            }
            required_platforms = tuple(platform_architectures)
            for platform, architecture in platform_architectures.items():
                record = {
                    "platform": platform,
                    "status": "passed",
                    "checks": ["version", "doctor", "lifecycle", "uninstall-preservation"],
                    "exceptions": [],
                }
                if architecture is not None:
                    record["architecture"] = architecture
                (root / f"summary-{platform}.json").write_text(
                    json.dumps(record) + "\n",
                    encoding="utf-8",
                )
            output = root / "release-summary.json"
            result = aggregate_summaries(root, output, required_platforms=required_platforms)
            self.assertEqual("RELEASE READY", result["decision"])
            self.assertEqual("passed", result["status"])
            self.assertEqual(5, result["platform_count"])
            self.assertTrue(output.is_file())

            broken = json.loads((root / "summary-windows.json").read_text(encoding="utf-8"))
            broken["status"] = "failed"
            broken["exceptions"] = ["known broken artifact"]
            (root / "summary-windows.json").write_text(json.dumps(broken) + "\n", encoding="utf-8")
            blocked = aggregate_summaries(root, output, required_platforms=required_platforms)
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
                        "architecture": "x86_64",
                        "tag": f"v{_V}",
                        "version": _V,
                        "signature_file": "SHA256SUMS.sigstore.json",
                        "signature_status": "sigstore",
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
                    metadata,
                    sbom,
                    artifact="runtime.zip",
                    platform="linux",
                    architecture="x86_64",
                    version=_V,
                )

            sbom_record["components"] = components
            sbom_record["dependencies"] = [{"ref": "root", "dependsOn": ["unknown"]}]
            sbom.write_text(json.dumps(sbom_record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unknown dependency"):
                validate_published_metadata(
                    metadata,
                    sbom,
                    artifact="runtime.zip",
                    platform="linux",
                    architecture="x86_64",
                    version=_V,
                )
            for field, value in (
                ("architecture", "arm64"),
                ("tag", "v2.0.0rc3"),
                ("version", "2.0.0rc3"),
            ):
                metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
                metadata_record[field] = value
                metadata.write_text(json.dumps(metadata_record), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, f"metadata mismatch: {field}"):
                    validate_published_metadata(
                        metadata,
                        sbom,
                        artifact="runtime.zip",
                        platform="linux",
                        architecture="x86_64",
                        version=_V,
                    )
                metadata_record[field] = {
                    "architecture": "x86_64",
                    "tag": f"v{_V}",
                    "version": _V,
                }[field]
                metadata.write_text(json.dumps(metadata_record), encoding="utf-8")

    def test_validate_published_metadata_accepts_macos_arm64(self):
        with tempfile.TemporaryDirectory() as raw:
            metadata, sbom = _write_release_contract(
                Path(raw), platform="macos", architecture="arm64"
            )
            validate_published_metadata(
                metadata,
                sbom,
                artifact="runtime.zip",
                platform="macos",
                architecture="arm64",
                version=_V,
            )

    def test_validate_published_metadata_rejects_architecture_mismatches(self):
        with tempfile.TemporaryDirectory() as raw:
            metadata, sbom = _write_release_contract(
                Path(raw), platform="macos", architecture="arm64"
            )
            metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
            metadata_record["architecture"] = "x86_64"
            metadata.write_text(json.dumps(metadata_record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "metadata mismatch: architecture"):
                validate_published_metadata(
                    metadata,
                    sbom,
                    artifact="runtime.zip",
                    platform="macos",
                    architecture="arm64",
                    version=_V,
                )

            metadata_record["architecture"] = "arm64"
            metadata.write_text(json.dumps(metadata_record), encoding="utf-8")
            sbom_record = json.loads(sbom.read_text(encoding="utf-8"))
            architecture_property = next(
                item
                for item in sbom_record["metadata"]["properties"]
                if item["name"] == "comic-sol:release:architecture"
            )
            architecture_property["value"] = "x86_64"
            sbom.write_text(json.dumps(sbom_record), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SBOM release properties are invalid"):
                validate_published_metadata(
                    metadata,
                    sbom,
                    artifact="runtime.zip",
                    platform="macos",
                    architecture="arm64",
                    version=_V,
                )

    def test_qualify_accepts_macos_arm64_and_records_normalized_version(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            files = _write_qualification_files(root, platform="macos", architecture="arm64")

            def fake_install(**kwargs):
                binary = kwargs["install_root"] / "bin" / "comic-sol"
                binary.parent.mkdir(parents=True)
                binary.write_text("fake executable\n", encoding="utf-8")

            def fake_json_command(_executable, arguments, **_kwargs):
                if arguments[0] == "doctor":
                    return {"data": {"healthy": True}}
                if arguments[0] == "init":
                    output_root = Path(arguments[arguments.index("--output-root") + 1])
                    project = output_root / "qualification-fixture"
                    project.mkdir(parents=True)
                    (project / "project.json").write_text('{"artifacts": {}}\n', encoding="utf-8")
                    return {"data": {"project_id": project.name}}
                if arguments[0] == "status":
                    return {"data": {"status": "INIT"}}
                if arguments[0] == "validate":
                    return {"data": []}
                raise AssertionError(f"unexpected command: {arguments!r}")

            def fake_uninstall(**kwargs):
                binary = kwargs["install_root"] / "bin" / "comic-sol"
                binary.unlink()
                binary.parent.rmdir()

            with (
                mock.patch("scripts.release_qualification.install", side_effect=fake_install),
                mock.patch("scripts.release_qualification.run", return_value=f"comic-sol {_V}"),
                mock.patch(
                    "scripts.release_qualification.json_command",
                    side_effect=fake_json_command,
                ),
                mock.patch("scripts.release_qualification.uninstall", side_effect=fake_uninstall),
            ):
                result = qualify(
                    platform_name="macos",
                    architecture="arm64",
                    archive=files["archive"],
                    installer=files["installer"],
                    checksums=files["checksums"],
                    signature=files["signature"],
                    summary=root / "summary-macos.json",
                    version=_V,
                    metadata=files["metadata"],
                    sbom=files["sbom"],
                )

            self.assertEqual("passed", result["status"])
            self.assertEqual("arm64", result["architecture"])
            self.assertEqual(_V, result["version"])
            self.assertNotEqual(f"comic-sol {_V}", result["version"])

    def test_qualify_rejects_wrong_executable_version_with_correct_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            files = _write_qualification_files(root, platform="linux", architecture="x86_64")

            def fake_install(**kwargs):
                binary = kwargs["install_root"] / "bin" / "comic-sol"
                binary.parent.mkdir(parents=True)
                binary.write_text("fake executable\n", encoding="utf-8")

            with (
                mock.patch("scripts.release_qualification.install", side_effect=fake_install),
                mock.patch(
                    "scripts.release_qualification.run",
                    return_value="comic-sol definitely-wrong",
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected installed version output"):
                    qualify(
                        platform_name="linux",
                        architecture="x86_64",
                        archive=files["archive"],
                        installer=files["installer"],
                        checksums=files["checksums"],
                        signature=files["signature"],
                        summary=root / "summary-linux.json",
                        version=_V,
                        metadata=files["metadata"],
                        sbom=files["sbom"],
                    )

    def test_artifact_qualification_requires_architecture_argument(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--archive",
                "runtime.zip",
                "--installer",
                "install.sh",
                "--sha256",
                "SHA256SUMS",
                "--signature",
                "SHA256SUMS.sigstore.json",
                "--platform",
                "linux",
                "--summary",
                "summary-linux.json",
                "--version",
                _V,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--architecture", result.stderr)

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
            "--architecture",
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
        self.assertIn('--architecture "$ARCH"', workflow)
        self.assertIn("--architecture '$architecture'", workflow)
        self.assertIn("ARCH: x86_64", workflow)

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
