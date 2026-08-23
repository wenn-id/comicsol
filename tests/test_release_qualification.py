import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
import tempfile
from unittest import mock

from comic_sol_product import __version__ as _V

from scripts.release_evidence import build_evidence
from scripts.release_evidence import write_evidence
from scripts.release_qualification import aggregate_summaries
from scripts.release_qualification import exercise_injected_rollback
from scripts.release_qualification import executable_path
from scripts.release_qualification import install
from scripts.release_qualification import install_command
from scripts.release_qualification import qualify
from scripts.release_qualification import snapshot_tree
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
            required_targets = tuple(platform_architectures.items())
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
            result = aggregate_summaries(root, output, required_targets=required_targets)
            self.assertEqual("RELEASE READY", result["decision"])
            self.assertEqual("passed", result["status"])
            self.assertEqual(5, result["platform_count"])
            self.assertEqual([], result["architecture_mismatches"])
            self.assertTrue(output.is_file())

            macos_summary = root / "summary-macos.json"
            mismatched = json.loads(macos_summary.read_text(encoding="utf-8"))
            mismatched["architecture"] = "x86_64"
            macos_summary.write_text(json.dumps(mismatched) + "\n", encoding="utf-8")
            blocked = aggregate_summaries(root, output, required_targets=required_targets)
            self.assertEqual("RELEASE BLOCKED", blocked["decision"])
            self.assertIn("macos", blocked["failed_platforms"])
            self.assertEqual(["macos"], blocked["architecture_mismatches"])
            self.assertIn("expected architecture 'arm64'", blocked["exceptions"][0])
            mismatched["architecture"] = "arm64"
            macos_summary.write_text(json.dumps(mismatched) + "\n", encoding="utf-8")

            broken = json.loads((root / "summary-windows.json").read_text(encoding="utf-8"))
            broken["status"] = "failed"
            broken["exceptions"] = ["known broken artifact"]
            (root / "summary-windows.json").write_text(json.dumps(broken) + "\n", encoding="utf-8")
            blocked = aggregate_summaries(root, output, required_targets=required_targets)
            self.assertEqual("RELEASE BLOCKED", blocked["decision"])
            self.assertEqual("failed", blocked["status"])
            self.assertIn("windows", blocked["failed_platforms"])

            # A platform exception (for example an unavailable WSL2 runner) is
            # recorded as blocked/exception; it must never count as a pass.
            recovered = json.loads((root / "summary-windows.json").read_text(encoding="utf-8"))
            recovered["status"] = "passed"
            (root / "summary-windows.json").write_text(
                json.dumps(recovered) + "\n", encoding="utf-8"
            )
            exception = json.loads((root / "summary-wsl.json").read_text(encoding="utf-8"))
            exception["status"] = "exception"
            exception["exceptions"] = ["WSL2 not available on this runner"]
            (root / "summary-wsl.json").write_text(json.dumps(exception) + "\n", encoding="utf-8")
            blocked = aggregate_summaries(root, output, required_targets=required_targets)
            self.assertEqual("RELEASE BLOCKED", blocked["decision"])
            self.assertIn("wsl", blocked["failed_platforms"])
            self.assertEqual("exception", blocked["summaries"]["wsl"]["status"])

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

    def test_verify_payload_checksums_with_candidate_identity_files_present(self):
        """The source-job filter must exclude the identity files from payloads.

        SHA256SUMS is generated before candidate-identity.json exists, so the
        manifest deliberately has no entries for the identity files, its
        sidecar, or itself. Passing them as payloads would fail the coverage
        check, so the workflow filters exactly these four names out and binds
        them by digest in the dedicated candidate-identity step instead.
        """
        import hashlib

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payloads = []
            for name, content in (
                ("comic-sol-1.0.0-py3-none-any.whl", b"wheel"),
                ("comic-sol-1.0.0.tar.gz", b"sdist"),
                ("comic-sol-1.0.0-linux-x86_64.container.tar", b"image"),
                ("install.sh", b"#!/bin/sh\n"),
                ("install.ps1", b"pwsh\n"),
            ):
                path = root / name
                path.write_bytes(content)
                payloads.append(path)
            manifest = root / "SHA256SUMS"
            manifest.write_text(
                "".join(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                    for path in payloads
                ),
                encoding="utf-8",
            )
            for name in (
                "SHA256SUMS.sigstore.json",
                "candidate-identity.json",
                "candidate-identity.json.sha256",
            ):
                (root / name).write_bytes(b"supporting evidence\n")

            excluded = {
                "SHA256SUMS",
                "SHA256SUMS.sigstore.json",
                "candidate-identity.json",
                "candidate-identity.json.sha256",
            }
            filtered = [path for path in sorted(root.iterdir()) if path.name not in excluded]
            self.assertEqual(5, verify_payload_checksums(manifest, filtered))

            everything = [path for path in sorted(root.iterdir()) if path.name != "SHA256SUMS"]
            with self.assertRaisesRegex(RuntimeError, "payload coverage mismatch"):
                verify_payload_checksums(manifest, everything)

    def test_verify_payload_checksums_rejects_duplicate_global_manifest_names(self):
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
            with self.assertRaisesRegex(RuntimeError, "duplicate SHA256SUMS entry"):
                verify_payload_checksums(manifest, [payload])

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
                binary = executable_path(kwargs["install_root"])
                binary.parent.mkdir(parents=True, exist_ok=True)
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
                binary = executable_path(kwargs["install_root"])
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
                mock.patch("scripts.release_qualification.exercise_injected_rollback"),
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
            self.assertIn("upgrade-reinstall", result["checks"])
            self.assertIn("injected-rollback", result["checks"])

    def test_qualify_rejects_wrong_executable_version_with_correct_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            files = _write_qualification_files(root, platform="linux", architecture="x86_64")

            def fake_install(**kwargs):
                binary = executable_path(kwargs["install_root"])
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

    def test_install_command_builds_direct_argv_per_platform(self):
        arguments = {
            "installer": Path("installers/install.sh"),
            "archive": Path("runtime.zip"),
            "digest": "0" * 64,
            "checksums": Path("SHA256SUMS"),
            "signature": Path("SHA256SUMS.sigstore.json"),
            "install_root": Path("install root with spaces"),
        }
        posix = install_command(platform_name="linux", **arguments)
        self.assertEqual(
            [
                "sh",
                str(Path("installers/install.sh")),
                "--archive",
                str(Path("runtime.zip")),
                "--sha256",
                "0" * 64,
                "--checksums",
                str(Path("SHA256SUMS")),
                "--signature",
                str(Path("SHA256SUMS.sigstore.json")),
                "--install-root",
                str(Path("install root with spaces")),
            ],
            posix,
        )
        windows = install_command(platform_name="windows", **arguments)
        self.assertEqual("pwsh", windows[0])
        self.assertEqual(["-NoProfile", "-File"], windows[1:3])
        self.assertNotIn("bash -lc", " ".join(windows))
        with self.assertRaisesRegex(ValueError, "unsupported qualification platform"):
            install_command(platform_name="plan9", **arguments)

    def test_snapshot_tree_fingerprints_every_file_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "a").write_bytes(b"one")
            (root / "nested").mkdir()
            (root / "nested" / "b").write_bytes(b"two")
            fingerprint = snapshot_tree(root)
            self.assertEqual(
                {
                    "a": (3, hashlib.sha256(b"one").hexdigest()),
                    "nested/b": (3, hashlib.sha256(b"two").hexdigest()),
                },
                fingerprint,
            )
            # Same size, different content: still a different fingerprint.
            (root / "nested" / "b").write_bytes(b"tow")
            self.assertNotEqual(fingerprint, snapshot_tree(root))

    @unittest.skipUnless(os.name != "nt", "POSIX installer qualification test")
    def test_exercise_injected_rollback_restores_runtime_byte_for_byte(self):
        missing = sorted(
            name
            for name in ("sh", "perl", "sha256sum", "unzip", "mv", "mktemp")
            if shutil.which(name) is None
        )
        if missing:
            self.skipTest(f"missing required host tools: {', '.join(missing)}")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "runtime.zip"
            executable = (
                "#!/bin/sh\n"
                'case "$1" in\n'
                f"  --version) printf 'comic-sol {_V}\\n' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ).encode("utf-8")
            member = zipfile.ZipInfo("comic-sol/comic-sol")
            member.create_system = 3
            member.external_attr = 0o100755 << 16
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(member, executable)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            checksums = root / "SHA256SUMS"
            signature = root / "SHA256SUMS.sigstore.json"
            checksums.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            signature.write_text('{"verification":"test-fixture"}\n', encoding="utf-8")
            cosign = root / "cosign"
            cosign.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                'if [ "${1-}" != verify-blob ] || [ "${2-}" != --bundle ] || '
                '[ "${4-}" != --certificate-identity-regexp ] || '
                '[ "${6-}" != --certificate-oidc-issuer ]; then exit 90; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            cosign.chmod(0o755)
            home = root / "home"
            home.mkdir()
            env = dict(os.environ)
            env["COMIC_SOL_COSIGN"] = str(cosign)
            env["HOME"] = str(home)
            arguments = {
                "installer": ROOT / "installers/install.sh",
                "archive": archive,
                "digest": digest,
                "checksums": checksums,
                "signature": signature,
            }

            install(
                install_root=root / "runtime", cwd=root, env=env, platform_name="linux", **arguments
            )
            snapshot = snapshot_tree(root / "runtime")
            self.assertTrue((root / "runtime" / "versions" / _V / "comic-sol").is_file())

            exercise_injected_rollback(
                install_root=root / "runtime", cwd=root, env=env, platform_name="linux", **arguments
            )

            self.assertEqual(snapshot, snapshot_tree(root / "runtime"))

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

    def test_release_publish_workflow_attests_every_signed_manifest_payload(self):
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("attest-build-provenance", workflow)
        self.assertIn("subject-checksums: bundles/SHA256SUMS", workflow)
        self.assertNotIn("subject-path:", workflow)

    def test_release_qualification_workflow_uses_release_asset_not_checkout_build(self):
        self.assertTrue(WORKFLOW.is_file())
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for token in (
            "workflow_dispatch",
            "workflow_call",
            "inputs:",
            "tag:",
            "candidate_sha:",
            "ref: ${{ inputs.candidate_sha }}",
            'git rev-parse "${RELEASE_TAG}^{commit}"',
            "gh release download",
            "gh attestation verify",
            "*.whl",
            "*.tar.gz",
            "*.container.tar",
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
        self.assertIn("ARCH: x86_64", workflow)

    def test_wsl_qualification_uses_a_static_script_with_argv_and_env_handoff(self):
        """WSL qualification must never interpolate values into a bash command.

        The dispatch script is static, every dynamic value crosses the WSL
        boundary as one direct argv element via `wsl.exe --exec`, and cosign
        crosses through the WSLENV environment handoff. The release tag is
        full-matched before anything is dispatched into WSL.
        """
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("bash -lc", workflow)
        self.assertIn("refusing WSL qualification: release tag", workflow)
        self.assertIn("-notmatch '^v[0-9]+\\.[0-9]+\\.[0-9]+(rc[0-9]+)?$'", workflow)
        self.assertIn("wsl-qualify.sh", workflow)
        self.assertIn("& wsl.exe --exec bash", workflow)
        self.assertIn('$env:WSLENV = "COMIC_SOL_COSIGN/up"', workflow)
        self.assertIn("$dispatchScript = @'", workflow)
        self.assertIn('exec python3 "$harness"', workflow)
        self.assertIn('--summary "$qualification_root/summary-wsl.json"', workflow)
        # The static script re-validates its own argv before executing.
        self.assertIn("unsafe version argument", workflow)

    def test_workflow_records_platform_specific_exceptions_in_summary(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Platform-specific exceptions", workflow)
        self.assertIn("WSL2", workflow)
        self.assertIn("not available", workflow)
        self.assertIn("$summary = [ordered]@{", workflow)
        self.assertIn("architecture = $env:ARCH", workflow)
        self.assertIn("exceptions = @($reason)", workflow)
        self.assertIn(
            "$summary | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 qualification/summary-wsl.json",
            workflow,
        )
        self.assertNotIn("$reason.Replace", workflow)
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


class ReleaseOrchestrationContractTests(unittest.TestCase):
    def _artifact(self, artifact_id, name):
        return {
            "id": artifact_id,
            "name": name,
            "digest": "sha256:" + format(artifact_id, "064x"),
            "url": f"https://api.github.com/repos/wenn-id/comicsol/actions/artifacts/{artifact_id}",
            "archive_download_url": (
                f"https://api.github.com/repos/wenn-id/comicsol/actions/artifacts/{artifact_id}/zip"
            ),
        }

    def _candidate(self):
        return {
            "schema_version": 1,
            "state": "candidate",
            "tag": "v9.9.9rc1",
            "version": "9.9.9rc1",
            "candidate_commit": "a" * 40,
            "checksum_manifest": {"name": "SHA256SUMS", "sha256": "b" * 64},
            "payloads": [
                {"name": "comic-sol-9.9.9rc1-linux-x86_64.zip", "sha256": "c" * 64},
                {"name": "comic-sol-9.9.9rc1-linux-x86_64.sbom.json", "sha256": "d" * 64},
            ],
            "actions_artifacts": [self._artifact(1, "benchmark-results")],
        }

    def _qualification(self):
        return {
            "status": "passed",
            "decision": "RELEASE READY",
            "candidate": {
                "tag": "v9.9.9rc1",
                "commit_sha": "a" * 40,
                "checksum_manifest_sha256": "b" * 64,
            },
            "summaries": {"linux": {"status": "passed", "exceptions": []}},
        }

    def _arguments(self):
        return {
            "candidate_identity": self._candidate(),
            "qualification": self._qualification(),
            "benchmark": {
                "status": "passed",
                "decision": "NO REGRESSION",
                "candidate_sha": "a" * 40,
            },
            "qualification_sha256": "e" * 64,
            "benchmark_sha256": "f" * 64,
            "actions_artifacts": [
                self._artifact(1, "benchmark-results"),
                self._artifact(2, "candidate-identity"),
                self._artifact(3, "release-qualification-summary"),
            ],
            "deployment": {
                "id": 42,
                "sha": "a" * 40,
                "ref": "refs/tags/v9.9.9rc1",
                "environment": "release-production",
                "api_url": "https://api.github.com/repos/wenn-id/comicsol/deployments/42",
                "html_audit_url": "https://github.com/wenn-id/comicsol/actions/runs/1234/job/7",
            },
            "required_reviewers": [{"type": "User", "id": 7, "login": "eligible-maintainer"}],
            "repository": "wenn-id/comicsol",
            "run_id": "1234",
            "run_url": "https://github.com/wenn-id/comicsol/actions/runs/1234",
            "environment": "release-production",
            "trigger_actor": "workflow-trigger",
        }

    def test_disposable_candidate_evidence_drill_is_deterministic(self):
        arguments = self._arguments()
        first = build_evidence(**arguments)
        second = build_evidence(**arguments)
        self.assertEqual(first, second)
        self.assertEqual("promotion-ready", first["state"])
        self.assertEqual("e" * 64, first["qualification"]["summary_sha256"])
        self.assertEqual("f" * 64, first["gates"]["benchmark"]["summary_sha256"])
        self.assertEqual(
            ["eligible-maintainer"],
            [item["login"] for item in first["promotion"]["required_reviewers"]],
        )
        self.assertIsNone(first["promotion"]["actual_reviewer"])
        self.assertNotIn("promotion_actor", first["promotion"])
        self.assertEqual(
            {item["name"] for item in first["supply_chain"]["payloads"]},
            set(first["supply_chain"]["attestations"]["subjects"]),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_evidence(first, root / "evidence.json", root / "evidence.md")
            expected = (root / "evidence.json").read_bytes()
            write_evidence(second, root / "evidence-2.json", root / "evidence-2.md")
            self.assertEqual(expected, (root / "evidence-2.json").read_bytes())
            self.assertIn("Deployment audit", (root / "evidence.md").read_text(encoding="utf-8"))

    def test_evidence_binding_rejects_failed_or_mismatched_gate_identity(self):
        arguments = self._arguments()
        arguments["qualification"]["status"] = "failed"
        with self.assertRaisesRegex(RuntimeError, "qualification evidence"):
            build_evidence(**arguments)

        arguments = self._arguments()
        arguments["benchmark"]["decision"] = "REGRESSION"
        with self.assertRaisesRegex(RuntimeError, "benchmark evidence"):
            build_evidence(**arguments)

        arguments = self._arguments()
        arguments["qualification"]["candidate"]["commit_sha"] = "0" * 40
        with self.assertRaisesRegex(RuntimeError, "another candidate"):
            build_evidence(**arguments)

        arguments = self._arguments()
        arguments["benchmark"]["candidate_sha"] = "0" * 40
        with self.assertRaisesRegex(RuntimeError, "another candidate"):
            build_evidence(**arguments)

        arguments = self._arguments()
        arguments["deployment"]["environment"] = "staging"
        with self.assertRaisesRegex(RuntimeError, "deployment evidence"):
            build_evidence(**arguments)

    def test_release_trigger_and_codeql_are_bound_to_exact_github_identity(self):
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        codeql = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
        trigger = release.split("permissions:", 1)[0]
        prepare = release.split("\n  prepare:\n", 1)[1].split("\n  full-tests:\n", 1)[0]
        self.assertNotIn("workflow_dispatch", trigger)
        self.assertNotIn("inputs.tag", release)
        self.assertIn("tags: [ 'v*' ]", trigger)
        self.assertIn("group: release-${{ github.ref }}", trigger)
        self.assertIn("cancel-in-progress: false", trigger)
        self.assertIn("Verify active release-tag immutability rules", prepare)
        self.assertIn('{"update", "deletion"} <= rule_types', prepare)
        self.assertIn("refs/tags/{os.environ['RELEASE_TAG']}", prepare)
        self.assertIn('test "$TRIGGER_REF" = "refs/tags/$TAG"', prepare)
        self.assertIn('git rev-parse "${TRIGGER_SHA}^{commit}"', prepare)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SHA"', prepare)
        self.assertIn(
            "candidate_ref: ${{ format('refs/tags/{0}', needs.prepare.outputs.tag) }}", release
        )
        self.assertIn("ref: ${{ inputs.candidate_ref || github.ref }}", codeql)
        self.assertIn("sha: ${{ inputs.candidate_sha || github.sha }}", codeql)

    def test_candidate_publication_is_draft_verified_immutable_and_fail_closed(self):
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        candidate = release.split("\n  candidate:\n", 1)[1].split("\n  qualification:\n", 1)[0]
        self.assertIn("actions: read", candidate)
        self.assertIn("subject-checksums: bundles/SHA256SUMS", candidate)
        self.assertIn("release ${RELEASE_TAG} already exists", candidate)
        self.assertIn("cleanup_failed_candidate", candidate)
        self.assertIn('release_id="$(jq -er \'.id\' "$created_release")"', candidate)
        self.assertIn("mapfile -t matching_ids", candidate)
        self.assertIn("comic-sol-release-owner:${GITHUB_RUN_ID}:${GITHUB_RUN_ATTEMPT}", candidate)
        self.assertIn("contains($marker)", candidate)
        self.assertNotIn("draft candidate lookup was not unique", candidate)
        self.assertIn("find bundles -type f ! -name SHA256SUMS -print0", candidate)
        self.assertIn(
            'gh release upload "$RELEASE_TAG" bundles/SHA256SUMS --repo "$GITHUB_REPOSITORY"',
            candidate,
        )
        self.assertNotIn("--clobber", candidate)
        positions = [
            candidate.index("-F draft=true"),
            candidate.index("gh release upload"),
            candidate.index("draft-verification"),
            candidate.index("-F draft=false -F prerelease=true"),
            candidate.index("repository release immutability is required"),
            candidate.index("trap - EXIT"),
        ]
        self.assertEqual(sorted(positions), positions)
        self.assertEqual(3, candidate.count("git ls-remote --tags origin"))
        self.assertIn('"refs/tags/${RELEASE_TAG}^{}"', candidate)
        self.assertIn(".immutable != true", candidate)

    def test_qualification_uses_strict_provenance_and_bound_outputs(self):
        qualification = WORKFLOW.read_text(encoding="utf-8")
        benchmark = (ROOT / ".github/workflows/benchmark.yml").read_text(encoding="utf-8")
        strict_flags = (
            "--signer-workflow wenn-id/comicsol/.github/workflows/release.yml",
            '--source-digest "$CANDIDATE_SHA"',
            '--source-ref "refs/tags/$RELEASE_TAG"',
            "--deny-self-hosted-runners",
        )
        for flag in strict_flags:
            self.assertGreaterEqual(qualification.count(flag), 2)
        for workflow in (qualification, benchmark):
            self.assertIn("decision:", workflow)
            self.assertIn("summary_sha256:", workflow)
        self.assertIn('summary["candidate"]', qualification)
        self.assertIn('record["candidate_sha"]', benchmark)

    def test_promotion_attests_evidence_and_closes_final_mutation_boundary(self):
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        promotion = release.split("\n  promote:\n", 1)[1]
        for token in (
            "EXPECTED_BENCHMARK_SHA256",
            "EXPECTED_QUALIFICATION_SHA256",
            "evidence-actions-artifacts.json",
            "deployment-identity.json",
            "required-reviewers.json",
            "html_audit_url",
            "actions: read",
            "attestations: write",
            "id-token: write",
            "name: release-evidence-${{ github.run_attempt }}",
            "subject-checksums: release-evidence.sha256",
            '"run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])',
            "deployment, status = max(",
            'key=lambda item: (item[1]["id"], item[0]["id"])',
        ):
            self.assertIn(token, promotion)
        self.assertNotIn("rollback_promotion", promotion)
        self.assertNotIn("--clobber", promotion)
        final_step = promotion.split(
            "- name: Revalidate immutable release and record promotion as final mutation", 1
        )[1]
        positions = [
            final_step.index("sha256sum -c release-evidence.sha256"),
            final_step.index("releases/tags/${RELEASE_TAG}"),
            final_step.index("git ls-remote --tags origin"),
            final_step.index("promoted-release-notes.md"),
            final_step.index('boundary_release="$(gh api'),
            final_step.rindex("--method PATCH"),
        ]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('"refs/tags/${RELEASE_TAG}^{}"', final_step)
        self.assertGreaterEqual(final_step.count(".immutable"), 2)
        self.assertIn("production approved", final_step)
        self.assertIn("--raw-field", final_step)
        self.assertNotIn("-F prerelease=", final_step)
        self.assertNotIn("gh release upload", final_step)
        self.assertNotIn("--method DELETE", final_step)
        self.assertTrue(final_step.rstrip().endswith(">/dev/null"))

    def test_release_workflow_remains_one_exact_sha_no_rebuild_dag(self):
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        tests = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        codeql = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
        benchmark = (ROOT / ".github/workflows/benchmark.yml").read_text(encoding="utf-8")
        for token in (
            "uses: ./.github/workflows/tests.yml",
            "uses: ./.github/workflows/codeql.yml",
            "uses: ./.github/workflows/benchmark.yml",
            "uses: ./.github/workflows/release-qualification.yml",
            "candidate_sha: ${{ needs.prepare.outputs.sha }}",
            "name: release-production",
            "candidate-identity.json",
            "release-evidence.json",
            "required_reviewers",
            "prevent_self_review",
        ):
            self.assertIn(token, release)
        self.assertNotIn("--clobber", release)
        promotion = release.split("\n  promote:\n", 1)[1]
        for forbidden in (
            "python -m build",
            "docker build",
            "build_portable.py",
            "assemble_release.py",
            "cosign sign-blob",
        ):
            self.assertNotIn(forbidden, promotion)
        self.assertEqual(1, promotion.count("attest-build-provenance"))
        for workflow in (tests, codeql, benchmark):
            self.assertIn("workflow_call:", workflow)
            self.assertIn("candidate_sha:", workflow)
            self.assertIn("inputs.candidate_sha", workflow)


if __name__ == "__main__":
    unittest.main()
