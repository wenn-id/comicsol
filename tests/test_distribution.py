import hashlib
import json
import os
import tempfile
import unittest
import zipfile
import uuid
from pathlib import Path

from comic_sol_product.distribution import (
    ReleaseIdentity,
    artifact_name,
    verify_release_directory,
    write_checksums,
    write_release_metadata,
    write_sbom,
)
from comic_sol_product.portable import (
    REQUIRED_RUNTIME_SUFFIXES,
    safe_extract_zip,
    validate_runtime_members,
)


class NativeDistributionContractTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity(
            version="2.0.0rc4", platform="linux", architecture="x86_64"
        )

    def _write_environment_sbom(self, release: Path) -> Path:
        components = [
            {
                "bom-ref": "comic-sol==2.0.0rc4",
                "name": "comic-sol",
                "purl": "pkg:pypi/comic-sol@2.0.0rc4",
                "type": "application",
                "version": "2.0.0rc4",
            },
            {"bom-ref": "Pillow==12.3.0", "name": "Pillow", "purl": "pkg:pypi/pillow@12.3.0", "type": "library", "version": "12.3.0"},
            {"bom-ref": "mcp==1.28.1", "name": "mcp", "purl": "pkg:pypi/mcp@1.28.1", "type": "library", "version": "1.28.1"},
            {"bom-ref": "pyinstaller==6.15.0", "name": "pyinstaller", "purl": "pkg:pypi/pyinstaller@6.15.0", "type": "library", "version": "6.15.0"},
            {"bom-ref": "pkg:generic/python@3.11.9", "name": "Python", "purl": "pkg:generic/python@3.11.9", "type": "framework", "version": "3.11.9"},
        ]
        destination = release / "build-environment.sbom.json"
        destination.write_text(
            json.dumps(
                {
                    "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
                    "bomFormat": "CycloneDX",
                    "components": components,
                    "dependencies": [
                        {
                            "ref": item["bom-ref"],
                            "dependsOn": [
                                components[1]["bom-ref"],
                                components[2]["bom-ref"],
                                components[4]["bom-ref"],
                            ]
                            if item is components[0]
                            else [],
                        }
                        for item in components
                    ],
                    "metadata": {},
                    "specVersion": "1.6",
                    "version": 1,
                }
            ),
            encoding="utf-8",
        )
        return destination

    def test_identity_and_artifact_names_are_canonical(self):
        self.assertEqual("v2.0.0rc4", self.identity.tag)
        self.assertEqual(
            "comic-sol-2.0.0rc4-linux-x86_64.tar.gz",
            artifact_name(self.identity, "tar.gz"),
        )
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0-rc2", "linux", "x86_64")
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0rc4", "Linux", "amd64")

    def test_metadata_checksum_and_sbom_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            first = release / artifact_name(self.identity, "tar.gz")
            second = release / "comic-sol-2.0.0rc4-linux-x86_64.sbom.json"
            first.write_bytes(b"portable-runtime")
            second.write_text("{}\n", encoding="utf-8")
            environment = self._write_environment_sbom(release)

            metadata = write_release_metadata(release, self.identity, [first.name])
            checksums = write_checksums(release, [second, first])
            sbom = write_sbom(release, self.identity, environment, first.name)

            metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual("unsigned", metadata_record["signature_status"])
            self.assertEqual([first.name], metadata_record["artifacts"])
            self.assertNotIn(str(release), metadata.read_text(encoding="utf-8"))

            checksum_lines = checksums.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sorted(checksum_lines), checksum_lines)
            expected = hashlib.sha256(first.read_bytes()).hexdigest()
            self.assertIn(f"{expected}  {first.name}", checksum_lines)

            sbom_record = json.loads(sbom.read_text(encoding="utf-8"))
            self.assertEqual("CycloneDX", sbom_record["bomFormat"])
            self.assertEqual("1.6", sbom_record["specVersion"])
            self.assertEqual("comic-sol", sbom_record["metadata"]["component"]["name"])
            self.assertEqual("2.0.0rc4", sbom_record["metadata"]["component"]["version"])
            self.assertEqual("application", sbom_record["metadata"]["component"]["type"])
            self.assertEqual("pkg:pypi/comic-sol@2.0.0rc4", sbom_record["metadata"]["component"]["purl"])
            uuid.UUID(sbom_record["serialNumber"].removeprefix("urn:uuid:"))
            self.assertEqual(
                first.name,
                next(item["value"] for item in sbom_record["metadata"]["properties"] if item["name"] == "comic-sol:release:artifact"),
            )
            self.assertGreaterEqual(len(sbom_record["dependencies"]), 5)

    def test_verifier_rejects_missing_or_tampered_artifact(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            artifact = release / artifact_name(self.identity, "tar.gz")
            artifact.write_bytes(b"original")
            environment = self._write_environment_sbom(release)
            write_release_metadata(release, self.identity, [artifact.name])
            write_sbom(release, self.identity, environment, artifact.name)
            write_checksums(
                release,
                [artifact, release / "comic-sol-2.0.0rc4-linux-x86_64.sbom.json"],
            )
            verify_release_directory(release, self.identity)

            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_release_directory(release, self.identity)

            artifact.unlink()
            with self.assertRaisesRegex(ValueError, "missing artifact"):
                verify_release_directory(release, self.identity)

    def test_portable_runtime_requires_executable_skill_fonts_and_mcp(self):
        validate_runtime_members(REQUIRED_RUNTIME_SUFFIXES | {"comic-sol/comic-sol"})
        validate_runtime_members(REQUIRED_RUNTIME_SUFFIXES | {"comic-sol/comic-sol.exe"})
        with self.assertRaisesRegex(ValueError, "portable runtime is missing"):
            validate_runtime_members({"comic-sol/comic-sol"})

    def test_portable_archive_round_trip_preserves_required_runtime(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime"
            for member in REQUIRED_RUNTIME_SUFFIXES | {"comic-sol/comic-sol"}:
                relative = member.removeprefix("comic-sol/")
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"runtime")
                if relative == "comic-sol":
                    target.chmod(0o755)
            archive = create_portable_archive(runtime, root / "portable.zip")
            extracted = root / "extracted"
            safe_extract_zip(archive, extracted)
            members = {
                path.relative_to(extracted).as_posix()
                for path in extracted.rglob("*")
                if path.is_file()
            }
            validate_runtime_members(members)
            if os.name != "nt":
                self.assertTrue(os.access(extracted / "comic-sol/comic-sol", os.X_OK))

    def test_portable_zip_rejects_traversal_before_extracting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as writer:
                writer.writestr("../escape.txt", "escape")
                writer.writestr("comic-sol/comic-sol", "binary")
            destination = root / "output"
            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                safe_extract_zip(archive, destination)
            self.assertFalse(destination.exists())

    def test_pyinstaller_spec_freezes_console_entrypoint_and_resources(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "packaging/comic-sol.spec").read_text(encoding="utf-8")
        entrypoint = (root / "packaging/entrypoint.py").read_text(encoding="utf-8")
        self.assertIn("comic_sol_product", spec)
        self.assertIn("collect_all", spec)
        self.assertIn("not name.startswith('mcp.cli')", spec)
        self.assertIn("excludes=['pkg_resources', 'setuptools']", spec)
        self.assertIn("name='comic-sol'", spec)
        self.assertIn("comic_sol_product.cli", entrypoint)

    def test_container_and_release_workflow_are_hardened(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn("USER comic-sol", dockerfile)
        self.assertIn("/data", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn('["comic-sol", "doctor"', dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("/data", compose)

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("inputs:", workflow)
        self.assertIn("tag:", workflow)
        self.assertIn("tags: [ 'v*' ]", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc4", workflow)
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, workflow)
        self.assertIn("scripts/build_portable.py", workflow)
        self.assertIn("build-environment.sbom.json", workflow)
        self.assertIn("--environment", workflow)
        self.assertIn("cyclonedx-bom==7.3.1", workflow)
        self.assertIn("validate_sbom_schema", workflow)
        self.assertIn("scripts/portable_release_smoke.py", workflow)
        portable_smoke = (root / "scripts/portable_release_smoke.py").read_text(encoding="utf-8")
        self.assertIn("installed_mcp_smoke.py", portable_smoke)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("sbom", workflow.lower())
        self.assertIn("dist/*.whl", workflow)
        self.assertIn("dist/*.tar.gz", workflow)
        self.assertIn("installers/install.sh", workflow)
        self.assertIn("installers/install.ps1", workflow)
        self.assertIn("prerelease", workflow.lower())
        self.assertIn("packaging.version", workflow)
        self.assertIn("github.ref_name", workflow)
        self.assertIn("inputs.tag || github.ref_name", workflow)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("Verify tag matches package version", workflow)
        self.assertIn("comic-sol:${{ steps.identity.outputs.version }}", workflow)
        self.assertIn("actions/attest-build-provenance@", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertNotIn("v2.0.0rc4", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc4", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc1", workflow)
        for line in workflow.splitlines():
            if "uses:" in line:
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_version_sources_and_quality_runtime_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        package = (root / "comic_sol_product/__init__.py").read_text(encoding="utf-8")
        distribution = (root / "comic_sol_product/distribution.py").read_text(encoding="utf-8")
        assembler = (root / "scripts/assemble_release.py").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        release_contract = (root / "comic_sol_product/release.py").read_text(encoding="utf-8")
        version = (root / "comic_sol_product/version.py").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn("from .version import VERSION", package)
        self.assertIn('VERSION = "2.0.0rc4"', version)
        self.assertIn("__version__", distribution)
        self.assertIn("__version__", assembler)
        self.assertIn("comic-sol:2.0.0rc4", compose)
        for module in (
            "normalize_panels.py", "typography.py", "layouts.py", "page_quality.py",
            "pdf_quality.py", "quality_sample.py",
        ):
            self.assertIn(module, release_contract)


if __name__ == "__main__":
    unittest.main()
