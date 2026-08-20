import hashlib
import json
import os
import re
import tempfile
import unittest
import zipfile
import uuid
from pathlib import Path

from comic_sol_product.distribution import (
    ReleaseIdentity,
    artifact_name,
    verify_release_directory,
    validate_sbom_schema,
    write_checksums,
    write_release_metadata,
    write_sbom,
)
from comic_sol_product.portable import (
    REQUIRED_RUNTIME_SUFFIXES,
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
            {"bom-ref": "mcp==2.0.0", "name": "mcp", "purl": "pkg:pypi/mcp@2.0.0", "type": "library", "version": "2.0.0"},
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

    def test_plugin_manifest_has_public_legal_urls(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (root / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "https://github.com/wenn-id/comicsol/blob/main/PRIVACY.md",
            manifest["interface"]["privacyPolicyURL"],
        )
        self.assertEqual(
            "https://github.com/wenn-id/comicsol/blob/main/TERMS.md",
            manifest["interface"]["termsOfServiceURL"],
        )

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
            self.assertEqual("sigstore", metadata_record["signature_status"])
            self.assertEqual("SHA256SUMS.sigstore.json", metadata_record["signature_file"])
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
            refs = [item["bom-ref"] for item in sbom_record["components"]]
            self.assertNotIn(sbom_record["metadata"]["component"]["bom-ref"], refs)

    def test_finalized_sbom_matches_cyclonedx_schema(self):
        try:
            import cyclonedx  # noqa: F401
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("CycloneDX JSON validation dependencies are not installed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            artifact = release / artifact_name(self.identity, "zip")
            artifact.write_bytes(b"portable-runtime")
            environment = self._write_environment_sbom(release)
            validate_sbom_schema(
                write_sbom(release, self.identity, environment, artifact.name)
            )

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

    def test_portable_archive_contains_required_runtime(self):
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
            with zipfile.ZipFile(archive) as reader:
                members = set(reader.namelist())
                executable_mode = (
                    reader.getinfo("comic-sol/comic-sol").external_attr >> 16
                )
            validate_runtime_members(members)
            if os.name != "nt":
                self.assertEqual(0o755, executable_mode & 0o777)

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
        self.assertIn("mcp==2.0.0", dockerfile + (root / "requirements/locks/runtime-linux-x86_64.txt").read_text(encoding="utf-8"))
        self.assertNotIn("mcp==1.28.1", dockerfile)
        self.assertIn("/data", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn('["comic-sol", "doctor"', dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("/data", compose)

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("inputs:", workflow)
        self.assertIn("tag:", workflow)
        self.assertIn("tags: [ 'v*' ]", workflow)
        self.assertIn("name: Prepare release", workflow)
        self.assertIn('git rev-parse "${TAG}^{commit}"', workflow)
        self.assertIn("ref: ${{ inputs.tag || github.ref_name }}", workflow)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", workflow)
        self.assertGreaterEqual(workflow.count("ref: ${{ needs.prepare.outputs.sha }}"), 4)
        self.assertIn("needs: [prepare, native, container, source]", workflow)
        self.assertNotIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("requirements/locks/release-${{ matrix.platform }}-x86_64.txt", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("DOCKER_BASE_DIGEST", workflow)
        self.assertIn("python:3.11.15-slim@sha256:", dockerfile)
        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", dockerfile)

        self.assertNotIn("refs/tags/v2.0.0rc4", workflow)
        for runner in ("ubuntu-latest", "macos-26-intel", "windows-latest"):
            self.assertIn(runner, workflow)
        self.assertIn("scripts/build_portable.py", workflow)
        self.assertIn("build-environment.sbom.json", workflow)
        self.assertIn("--environment", workflow)
        self.assertIn("cyclonedx-bom==7.3.1", workflow + (root / "requirements/locks/release-linux-x86_64.txt").read_text(encoding="utf-8"))
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
        self.assertIn("Verify tag matches package version", workflow)
        self.assertIn("comic-sol:${{ needs.prepare.outputs.version }}", workflow)
        self.assertIn("actions/attest-build-provenance@", workflow)
        self.assertIn("sigstore/cosign-installer@", workflow)
        self.assertIn("SHA256SUMS.sigstore.json", workflow)
        self.assertIn("--bundle", workflow)
        self.assertIn("mcp==2.0.0", workflow + (root / "requirements/locks/release-linux-x86_64.txt").read_text(encoding="utf-8"))
        for installer in (root / "installers/install.sh", root / "installers/install.ps1"):
            installer_text = installer.read_text(encoding="utf-8")
            self.assertIn("sigstore", installer_text.lower())
            self.assertIn("SHA256SUMS.sigstore.json", installer_text)
            self.assertIn("signature verification", installer_text.lower())
        self.assertNotIn("mcp==1.28.1", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertNotIn("v2.0.0rc4", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc4", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc1", workflow)
        for line in workflow.splitlines():
            if "uses:" in line:
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_release_locks_are_hashed_and_complete_for_every_target(self):
        root = Path(__file__).resolve().parents[1]
        for platform in ("linux", "macos", "windows"):
            for kind in ("base", "runtime", "release"):
                lock = (root / "requirements/locks" / f"{kind}-{platform}-x86_64.txt").read_text(encoding="utf-8")
                self.assertNotRegex(lock, r"(?m)^\s*--(?:index-url|extra-index-url|find-links)\b")
                lines = lock.splitlines()
                blocks = []
                current = None
                for line in lines:
                    if re.match(r"^[A-Za-z0-9_.-]+(?:\[[^]]+\])?==\S+", line):
                        if current:
                            blocks.append(current)
                        current = [line]
                    elif current is not None:
                        current.append(line)
                if current:
                    blocks.append(current)
                self.assertTrue(blocks)
                for block in blocks:
                    self.assertRegex("\n".join(block), r"--hash=sha256:[0-9a-f]{64}")
                self.assertIn("pillow==12.3.0", lock.lower())
                if kind != "base":
                    self.assertIn("mcp==2.0.0", lock.lower())

    def test_dead_quality_and_lifecycle_surfaces_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "comic_sol_product/install_lifecycle.py").exists())
        self.assertFalse((root / "tests/test_install_lifecycle.py").exists())
        self.assertFalse((root / "tests/test_quality_records.py").exists())
        self.assertFalse((root / "docs/superpowers").exists())
        self.assertFalse((root / ".superpowers/sdd").exists())
        quality = (root / "scripts/quality_records.py").read_text(encoding="utf-8")
        for symbol in ("QualityCheck", "QualityBinding", "quality_record_hash", "read_quality_record", "migrate_quality_record"):
            self.assertNotIn(symbol, quality)

    def test_font_cmap_has_one_runtime_implementation(self):
        root = Path(__file__).resolve().parents[1]
        cmap = (root / "scripts/font_cmap.py").read_text(encoding="utf-8")
        self.assertIn("def unicode_cmap_subtables", cmap)
        self.assertIn("def cmap_glyph_id", cmap)
        for module in ("scripts/letter_panels.py", "scripts/typography.py"):
            text = (root / module).read_text(encoding="utf-8")
            self.assertNotIn("def _unicode_cmap_subtables", text)
            self.assertNotIn("def _cmap_glyph_id", text)


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
