import hashlib
import json
import os
import tempfile
import unittest
import zipfile
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
            version="2.0.0rc1", platform="linux", architecture="x86_64"
        )

    def test_identity_and_artifact_names_are_canonical(self):
        self.assertEqual("v2.0.0rc1", self.identity.tag)
        self.assertEqual(
            "comic-sol-2.0.0rc1-linux-x86_64.tar.gz",
            artifact_name(self.identity, "tar.gz"),
        )
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0-rc1", "linux", "x86_64")
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0rc1", "Linux", "amd64")

    def test_metadata_checksum_and_sbom_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            first = release / artifact_name(self.identity, "tar.gz")
            second = release / "comic-sol-2.0.0rc1-linux-x86_64.sbom.json"
            first.write_bytes(b"portable-runtime")
            second.write_text("{}\n", encoding="utf-8")

            metadata = write_release_metadata(release, self.identity, [first.name])
            checksums = write_checksums(release, [second, first])
            sbom = write_sbom(release, self.identity)

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
            self.assertEqual("2.0.0rc1", sbom_record["metadata"]["component"]["version"])

    def test_verifier_rejects_missing_or_tampered_artifact(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            artifact = release / artifact_name(self.identity, "tar.gz")
            artifact.write_bytes(b"original")
            write_release_metadata(release, self.identity, [artifact.name])
            write_sbom(release, self.identity)
            write_checksums(
                release,
                [artifact, release / "comic-sol-2.0.0rc1-linux-x86_64.sbom.json"],
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
        self.assertIn("tags: [ 'v2.0.0rc1' ]", workflow)
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, workflow)
        self.assertIn("scripts/build_portable.py", workflow)
        self.assertIn("scripts/portable_release_smoke.py", workflow)
        portable_smoke = (root / "scripts/portable_release_smoke.py").read_text(encoding="utf-8")
        self.assertIn("installed_mcp_smoke.py", portable_smoke)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("sbom", workflow.lower())
        self.assertIn("dist/*.whl", workflow)
        self.assertIn("dist/*.tar.gz", workflow)
        self.assertIn("installers/install.sh", workflow)
        self.assertIn("installers/install.ps1", workflow)
        self.assertIn("prerelease: true", workflow)
        self.assertIn("if: github.ref == 'refs/tags/v2.0.0rc1'", workflow)
        for line in workflow.splitlines():
            if "uses:" in line:
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
