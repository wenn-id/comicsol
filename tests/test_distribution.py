import hashlib
import json
import os
import re
import socket
import struct
import tarfile
import tempfile
import unittest
import zipfile
import uuid
from pathlib import Path
from unittest import mock

from comic_sol_product import __version__ as RELEASE_VERSION

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
            version=RELEASE_VERSION, platform="linux", architecture="x86_64"
        )

    @staticmethod
    def _elf(architecture: str) -> bytes:
        payload = bytearray(64)
        payload[:6] = b"\x7fELF\x02\x01"
        payload[6] = 1
        struct.pack_into("<H", payload, 18, {"x86_64": 62, "arm64": 183}[architecture])
        struct.pack_into("<I", payload, 20, 1)
        struct.pack_into("<H", payload, 52, 64)
        return bytes(payload)

    def _portable_runtime(self, root: Path, architecture: str = "x86_64") -> Path:
        runtime = root / "runtime"
        for member in REQUIRED_RUNTIME_SUFFIXES:
            relative = member.removeprefix("comic-sol/")
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"runtime")
        launcher = runtime / "comic-sol"
        launcher.write_bytes(self._elf(architecture))
        launcher.chmod(0o755)
        return runtime

    def _write_environment_sbom(self, release: Path) -> Path:
        components = [
            {
                "bom-ref": f"comic-sol=={RELEASE_VERSION}",
                "name": "comic-sol",
                "purl": f"pkg:pypi/comic-sol@{RELEASE_VERSION}",
                "type": "application",
                "version": RELEASE_VERSION,
            },
            {
                "bom-ref": "Pillow==12.3.0",
                "name": "Pillow",
                "purl": "pkg:pypi/pillow@12.3.0",
                "type": "library",
                "version": "12.3.0",
            },
            {
                "bom-ref": "mcp==2.0.0",
                "name": "mcp",
                "purl": "pkg:pypi/mcp@2.0.0",
                "type": "library",
                "version": "2.0.0",
            },
            {
                "bom-ref": "pyinstaller==6.15.0",
                "name": "pyinstaller",
                "purl": "pkg:pypi/pyinstaller@6.15.0",
                "type": "library",
                "version": "6.15.0",
            },
            {
                "bom-ref": "pkg:generic/python@3.11.9",
                "name": "Python",
                "purl": "pkg:generic/python@3.11.9",
                "type": "framework",
                "version": "3.11.9",
            },
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
        self.assertEqual(f"v{RELEASE_VERSION}", self.identity.tag)
        self.assertEqual(
            f"comic-sol-{RELEASE_VERSION}-linux-x86_64.tar.gz",
            artifact_name(self.identity, "tar.gz"),
        )
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0-rc2", "linux", "x86_64")
        with self.assertRaises(ValueError):
            ReleaseIdentity(RELEASE_VERSION, "Linux", "amd64")

    def test_plugin_manifest_has_public_legal_urls(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
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
            second = release / f"comic-sol-{RELEASE_VERSION}-linux-x86_64.sbom.json"
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
            self.assertEqual(RELEASE_VERSION, sbom_record["metadata"]["component"]["version"])
            self.assertEqual("application", sbom_record["metadata"]["component"]["type"])
            self.assertEqual(
                f"pkg:pypi/comic-sol@{RELEASE_VERSION}",
                sbom_record["metadata"]["component"]["purl"],
            )
            uuid.UUID(sbom_record["serialNumber"].removeprefix("urn:uuid:"))
            self.assertEqual(
                first.name,
                next(
                    item["value"]
                    for item in sbom_record["metadata"]["properties"]
                    if item["name"] == "comic-sol:release:artifact"
                ),
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
            validate_sbom_schema(write_sbom(release, self.identity, environment, artifact.name))

    def test_container_sbom_override_writes_payload_name_and_distinct_serial(self):
        try:
            import cyclonedx  # noqa: F401
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("CycloneDX JSON validation dependencies are not installed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            artifact = release / f"comic-sol-{RELEASE_VERSION}-linux-x86_64.container.tar"
            artifact.write_bytes(b"container-image")
            environment = self._write_environment_sbom(release)
            native = write_sbom(release, self.identity, environment, "payload.zip")
            override_name = f"comic-sol-{RELEASE_VERSION}-linux-x86_64.container.sbom.json"
            container = write_sbom(
                release,
                self.identity,
                environment,
                artifact.name,
                destination_name=override_name,
            )
            self.assertEqual(override_name, container.name)
            self.assertNotEqual(native.name, container.name)
            native_record = json.loads(native.read_text(encoding="utf-8"))
            container_record = json.loads(container.read_text(encoding="utf-8"))
            self.assertNotEqual(native_record["serialNumber"], container_record["serialNumber"])
            properties = {
                item["name"]: item["value"] for item in container_record["metadata"]["properties"]
            }
            self.assertEqual(artifact.name, properties["comic-sol:release:artifact"])
            validate_sbom_schema(container)

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
                [artifact, release / f"comic-sol-{RELEASE_VERSION}-linux-x86_64.sbom.json"],
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
                executable_mode = reader.getinfo("comic-sol/comic-sol").external_attr >> 16
            validate_runtime_members(members)
            if os.name != "nt":
                self.assertEqual(0o755, executable_mode & 0o777)

    def test_binary_architecture_detection_covers_release_formats(self):
        from comic_sol_product.portable import detect_binary_architectures

        self.assertEqual(frozenset({"x86_64"}), detect_binary_architectures(self._elf("x86_64")))
        self.assertEqual(frozenset({"arm64"}), detect_binary_architectures(self._elf("arm64")))

        macho_x86 = bytearray(32)
        macho_x86[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", macho_x86, 4, 0x01000007)
        struct.pack_into("<I", macho_x86, 12, 2)
        macho_arm = bytearray(macho_x86)
        struct.pack_into("<I", macho_arm, 4, 0x0100000C)
        self.assertEqual(frozenset({"arm64"}), detect_binary_architectures(bytes(macho_arm)))

        fat = bytearray(48 + len(macho_x86) + len(macho_arm))
        fat[:8] = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2)
        struct.pack_into(">IIIII", fat, 8, 0x01000007, 0, 48, len(macho_x86), 0)
        struct.pack_into(">IIIII", fat, 28, 0x0100000C, 0, 80, len(macho_arm), 0)
        fat[48:80] = macho_x86
        fat[80:112] = macho_arm
        self.assertEqual(frozenset({"arm64", "x86_64"}), detect_binary_architectures(bytes(fat)))

        pe = bytearray(64 + 24 + 112 + 40)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3C, 64)
        pe[64:68] = b"PE\0\0"
        struct.pack_into("<HH", pe, 68, 0x8664, 1)
        struct.pack_into("<H", pe, 84, 112)
        struct.pack_into("<H", pe, 88, 0x20B)
        self.assertEqual(frozenset({"x86_64"}), detect_binary_architectures(bytes(pe)))

        malformed_elf = bytearray(self._elf("x86_64"))
        struct.pack_into("<Q", malformed_elf, 32, 64)
        struct.pack_into("<H", malformed_elf, 54, 56)
        struct.pack_into("<H", malformed_elf, 56, 1)
        malformed_macho = bytearray(macho_arm)
        struct.pack_into("<I", malformed_macho, 16, 1)
        malformed_fat = bytearray(fat)
        struct.pack_into("<I", malformed_fat, 48 + 16, 1)
        malformed_pe = bytearray(pe)
        struct.pack_into("<I", malformed_pe, 88 + 108, 16)
        for malformed in (
            self._elf("x86_64")[:20],
            bytes(macho_arm[:8]),
            bytes(fat[:48]),
            bytes(pe[:70]),
            bytes(malformed_elf),
            bytes(malformed_macho),
            bytes(malformed_fat),
            bytes(malformed_pe),
        ):
            with self.subTest(malformed=malformed[:4]):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    detect_binary_architectures(malformed)

    def test_portable_archive_rejects_architecture_mismatch_atomically(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root, architecture="arm64")
            for extension in ("zip", "tar.gz"):
                archive = root / f"portable.{extension}"
                with self.subTest(extension=extension):
                    with self.assertRaisesRegex(
                        ValueError, "architecture mismatch.*requested x86_64"
                    ):
                        create_portable_archive(runtime, archive, architecture="x86_64")
                    self.assertFalse(archive.exists())

    def test_release_assembly_rejects_architecture_mismatch(self):
        from scripts import assemble_release

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root, architecture="arm64")
            environment = root / "environment.json"
            environment.write_text("{}", encoding="utf-8")
            output = root / "release"
            argv = [
                "assemble_release.py",
                "--runtime",
                os.fspath(runtime),
                "--environment",
                os.fspath(environment),
                "--output",
                os.fspath(output),
                "--platform",
                "linux",
                "--architecture",
                "x86_64",
            ]
            with mock.patch("sys.argv", argv):
                with self.assertRaisesRegex(ValueError, "architecture mismatch"):
                    assemble_release.main()
            self.assertEqual([], list(output.iterdir()))

    def test_portable_archives_reject_symlinked_runtime_root(self):
        from comic_sol_product.portable import create_portable_archive
        from scripts import assemble_release

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            linked_runtime = root / "linked-runtime"
            try:
                linked_runtime.symlink_to(runtime, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            for extension in ("zip", "tar.gz"):
                with self.subTest(extension=extension):
                    with self.assertRaisesRegex(ValueError, "symlinks or reparse points"):
                        create_portable_archive(
                            linked_runtime,
                            root / f"portable.{extension}",
                            architecture="x86_64",
                        )

            environment = root / "environment.json"
            environment.write_text("{}", encoding="utf-8")
            argv = [
                "assemble_release.py",
                "--runtime",
                os.fspath(linked_runtime),
                "--environment",
                os.fspath(environment),
                "--output",
                os.fspath(root / "release"),
                "--platform",
                "linux",
                "--architecture",
                "x86_64",
            ]
            with mock.patch("sys.argv", argv):
                with self.assertRaisesRegex(ValueError, "symlinks or reparse points"):
                    assemble_release.main()

    def test_portable_archives_reject_symlinked_files_and_directories(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            outside = root / "outside"
            outside.write_bytes(b"outside")
            try:
                (runtime / "linked-file").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            for extension in ("zip", "tar.gz"):
                with self.subTest(extension=extension):
                    with self.assertRaisesRegex(ValueError, "symlinks or reparse points"):
                        create_portable_archive(
                            runtime, root / f"portable.{extension}", architecture="x86_64"
                        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            outside = root / "outside"
            outside.mkdir()
            try:
                (runtime / "linked-directory").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            for extension in ("zip", "tar.gz"):
                with self.subTest(extension=extension):
                    with self.assertRaisesRegex(ValueError, "symlinks or reparse points"):
                        create_portable_archive(
                            runtime,
                            root / f"portable.{extension}",
                            architecture="x86_64",
                        )

    @unittest.skipIf(os.name == "nt", "FIFOs are a POSIX filesystem feature")
    def test_portable_archives_reject_non_regular_members(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            os.mkfifo(runtime / "named-pipe")
            for extension in ("zip", "tar.gz"):
                with self.subTest(extension=extension):
                    with self.assertRaisesRegex(ValueError, "regular file or directory"):
                        create_portable_archive(
                            runtime, root / f"portable.{extension}", architecture="x86_64"
                        )

    @unittest.skipIf(os.name == "nt", "Unix sockets are a POSIX filesystem feature")
    def test_portable_archives_reject_socket_members(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(os.fspath(runtime / "runtime.sock"))
                for extension in ("zip", "tar.gz"):
                    with self.subTest(extension=extension):
                        with self.assertRaisesRegex(ValueError, "regular file or directory"):
                            create_portable_archive(
                                runtime,
                                root / f"portable.{extension}",
                                architecture="x86_64",
                            )

    def test_portable_archive_rejects_member_replaced_after_scan(self):
        from comic_sol_product import portable

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            replacement = root / "replacement"
            replacement.write_bytes(b"replacement")
            original_read = portable._read_member
            replaced = False

            def replace_before_read(runtime_root, root_descriptor, member, directories):
                nonlocal replaced
                if not replaced:
                    os.replace(replacement, runtime_root / member.relative)
                    replaced = True
                return original_read(runtime_root, root_descriptor, member, directories)

            with mock.patch.object(portable, "_read_member", side_effect=replace_before_read):
                with self.assertRaisesRegex(ValueError, "changed before it could be read"):
                    portable.create_portable_archive(
                        runtime, root / "portable.zip", architecture="x86_64"
                    )
            self.assertFalse((root / "portable.zip").exists())

    def test_portable_archive_rejects_same_inode_rewrite_after_scan(self):
        from comic_sol_product import portable

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            original_write = portable._write_zip

            def rewrite_before_write(
                destination,
                runtime_root,
                root_descriptor,
                members,
                directories,
                architecture,
            ):
                member = members[0]
                target = runtime_root / member.relative
                metadata = target.stat()
                target.write_bytes(b"x" * member.size)
                os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                return original_write(
                    destination,
                    runtime_root,
                    root_descriptor,
                    members,
                    directories,
                    architecture,
                )

            with mock.patch.object(portable, "_write_zip", side_effect=rewrite_before_write):
                with self.assertRaisesRegex(ValueError, "changed before it could be read"):
                    portable.create_portable_archive(
                        runtime, root / "portable.zip", architecture="x86_64"
                    )

    def test_zip_and_tar_apply_the_same_deterministic_member_policy(self):
        from comic_sol_product.portable import create_portable_archive

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = self._portable_runtime(root)
            zip_first = create_portable_archive(runtime, root / "first.zip", architecture="x86_64")
            zip_second = create_portable_archive(
                runtime, root / "second.zip", architecture="x86_64"
            )
            tar_first = create_portable_archive(
                runtime, root / "first.tar.gz", architecture="x86_64"
            )
            tar_second = create_portable_archive(
                runtime, root / "second.tar.gz", architecture="x86_64"
            )

            self.assertEqual(zip_first.read_bytes(), zip_second.read_bytes())
            self.assertEqual(tar_first.read_bytes(), tar_second.read_bytes())
            with zipfile.ZipFile(zip_first) as zip_reader:
                zip_members = zip_reader.namelist()
                zip_payloads = {name: zip_reader.read(name) for name in zip_members}
                zip_modes = {
                    name: zip_reader.getinfo(name).external_attr >> 16 & 0o777
                    for name in zip_members
                }
            with tarfile.open(tar_first, "r:gz") as tar_reader:
                tar_members = tar_reader.getnames()
                tar_payloads = {
                    member.name: tar_reader.extractfile(member).read()
                    for member in tar_reader.getmembers()
                }
                tar_modes = {member.name: member.mode for member in tar_reader.getmembers()}
            self.assertEqual(zip_members, tar_members)
            self.assertEqual(zip_payloads, tar_payloads)
            self.assertEqual(zip_modes, tar_modes)

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
        """The container image and release workflow keep their supply-chain gates.

        Covers the non-root container contract, the tag-to-version verification in
        prepare, hash-pinned dependency installs, Sigstore signing and provenance
        attestation, and the rule that every `uses:` reference is pinned to a full
        commit SHA rather than a floating tag.
        """
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("USER comic-sol", dockerfile)
        self.assertIn("--uid 10001", dockerfile)
        self.assertIn("--gid 10001", dockerfile)
        self.assertIn(
            "mcp==2.0.0",
            dockerfile
            + (root / "requirements/locks/runtime-linux-x86_64.txt").read_text(encoding="utf-8"),
        )
        self.assertNotIn("mcp==1.28.1", dockerfile)
        self.assertIn("/data", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn('["comic-sol", "doctor"', dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("/data", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("- ALL", compose)
        self.assertIn("pids: 64", compose)
        self.assertIn('user: "10001:10001"', compose)
        self.assertIn("init: true", compose)
        self.assertIn("network_mode: none", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIsNone(re.search(r"^\s*-\s*seccomp", compose, re.MULTILINE))

        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("inputs.tag", workflow)
        self.assertIn("tags: [ 'v*' ]", workflow)
        self.assertIn("name: Prepare release", workflow)
        self.assertIn('git rev-parse "${TAG}^{commit}"', workflow)
        self.assertIn('test "$TRIGGER_REF" = "refs/tags/$TAG"', workflow)
        self.assertIn('git rev-parse "${TRIGGER_SHA}^{commit}"', workflow)
        self.assertIn("ref: ${{ github.ref }}", workflow)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", workflow)
        self.assertGreaterEqual(workflow.count("ref: ${{ needs.prepare.outputs.sha }}"), 5)
        self.assertIn(
            "needs: [prepare, full-tests, codeql, benchmark, native, container, source]", workflow
        )
        self.assertIn("uses: ./.github/workflows/tests.yml", workflow)
        self.assertIn("uses: ./.github/workflows/codeql.yml", workflow)
        self.assertIn("uses: ./.github/workflows/benchmark.yml", workflow)
        self.assertIn("uses: ./.github/workflows/release-qualification.yml", workflow)
        self.assertNotIn("blocking_quality", workflow)
        self.assertIn("name: release-production", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertNotIn("if: startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("requirements/locks/release-${{ matrix.platform }}-x86_64.txt", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertNotIn("DOCKER_BASE_DIGEST", workflow)
        self.assertNotIn("--build-arg", workflow)
        self.assertIn("ARG PYTHON_BASE=", dockerfile)
        self.assertEqual(1, dockerfile.count("sha256:"))
        self.assertIn("python:3.11.15-slim@sha256:", dockerfile)
        self.assertIn("FROM ${PYTHON_BASE} AS builder", dockerfile)
        self.assertIn("FROM ${PYTHON_BASE}", dockerfile)
        self.assertIn("scripts/container_runtime_audit.py", workflow)
        self.assertIn("--expect-version", workflow)
        self.assertIn("scripts/container_sbom.py", workflow)
        self.assertIn(
            "comic-sol-${{ needs.prepare.outputs.version }}-linux-x86_64.container.sbom.json",
            workflow,
        )
        self.assertIn("pip_audit -r requirements/locks/runtime-linux-x86_64.txt", workflow)
        self.assertIn("requirements/locks/runtime-linux-x86_64.txt", dockerfile)
        self.assertNotIn(f"refs/tags/v{RELEASE_VERSION}", workflow)
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, workflow)
        self.assertIn("scripts/build_portable.py", workflow)
        self.assertIn("build-environment.sbom.json", workflow)
        self.assertIn("--environment", workflow)
        self.assertIn(
            "cyclonedx-bom==7.3.1",
            workflow
            + (root / "requirements/locks/release-linux-x86_64.txt").read_text(encoding="utf-8"),
        )
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
        self.assertNotIn("inputs.tag || github.ref_name", workflow)
        self.assertIn("Verify tag matches package version", workflow)
        self.assertIn("comic-sol:${{ needs.prepare.outputs.version }}", workflow)
        self.assertIn("actions/attest-build-provenance@", workflow)
        self.assertIn("sigstore/cosign-installer@", workflow)
        self.assertIn("SHA256SUMS.sigstore.json", workflow)
        self.assertIn("--bundle", workflow)
        self.assertIn(
            "mcp==2.0.0",
            workflow
            + (root / "requirements/locks/release-linux-x86_64.txt").read_text(encoding="utf-8"),
        )
        for installer in (root / "installers/install.sh", root / "installers/install.ps1"):
            installer_text = installer.read_text(encoding="utf-8")
            self.assertIn("sigstore", installer_text.lower())
            self.assertIn("SHA256SUMS.sigstore.json", installer_text)
            self.assertIn("signature verification", installer_text.lower())
        self.assertNotIn("mcp==1.28.1", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertNotIn(f"v{RELEASE_VERSION}", workflow)
        self.assertNotIn(f"refs/tags/v{RELEASE_VERSION}", workflow)
        self.assertNotIn("refs/tags/v2.0.0rc1", workflow)
        for line in workflow.splitlines():
            if re.match(r"^\s*uses:\s+\S+", line):
                reference = line.split("uses:", 1)[1].strip().split()[0]
                if reference.startswith("./"):
                    self.assertRegex(reference, r"^\./\.github/workflows/[^/]+\.yml$")
                else:
                    self.assertRegex(reference, r"^[^@]+@[0-9a-f]{40}$")

    def test_release_native_matrix_pins_python_available_on_every_runner(self):
        """Each native leg must pin a CPython that actually ships for its runner.

        CPython 3.11 is in security-only maintenance, so actions/python-versions
        stops publishing macOS and Windows binaries after 3.11.9 and continues
        with Linux-only builds. One patch pin shared by all three legs therefore
        fails setup-python on macOS and Windows, which skips the publish job and
        produces a tag with no release.
        """
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        native = workflow.split("\n  native:\n", 1)[1].split("\n  container:\n", 1)[0]

        self.assertIn("python-version: ${{ matrix.python }}", native)
        self.assertNotRegex(
            native,
            r"python-version:[ \t]*'?\d+\.\d+\.\d+",
            "native legs must resolve the interpreter per platform, not share one patch pin",
        )

        legs = self._native_legs(native)
        self.assertEqual({"linux", "macos", "windows"}, set(legs), legs)

        # Newest 3.11 patch release carrying macOS and Windows binaries in
        # actions/python-versions versions-manifest.json.
        last_cross_platform_311 = (3, 11, 9)
        for platform in ("macos", "windows"):
            pinned = tuple(int(part) for part in legs[platform]["python"].split("."))
            self.assertLessEqual(
                pinned,
                last_cross_platform_311,
                f"{platform} pins Python {legs[platform]['python']}, "
                f"which ships no {platform} binary",
            )

    def test_release_publishes_macos_arm64_not_x86_64(self):
        """The macOS release must be arm64 at every stage that names an arch.

        cryptography removed x86_64 macOS support in 49.0.0 and publishes arm64
        wheels only. An x86_64 macOS leg has to build it from source against an
        OpenSSL that the frozen runtime then cannot load, which fails the bundled
        MCP smoke test and skips publish, leaving a tag with no release. The
        runner, the uploaded bundle name, the assembled architecture, and the
        publish-side identity all have to agree on arm64.
        """
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        native = workflow.split("\n  native:\n", 1)[1].split("\n  container:\n", 1)[0]

        legs = self._native_legs(native)
        for platform, fields in sorted(legs.items()):
            self.assertIn(
                "arch",
                fields,
                f"the {platform} leg declares no arch, so the artifact name and the "
                f"assembled architecture cannot be derived from the matrix: {fields}",
            )
        self.assertEqual("arm64", legs["macos"]["arch"], legs["macos"])
        self.assertEqual("macos-latest", legs["macos"]["os"], legs["macos"])
        for platform in ("linux", "windows"):
            self.assertEqual("x86_64", legs[platform]["arch"], legs[platform])

        # The arch must flow through, not be re-hardcoded downstream.
        self.assertIn("--architecture ${{ matrix.arch }}", native)
        self.assertIn("comic-sol-${{ matrix.platform }}-${{ matrix.arch }}", native)
        self.assertNotIn("--architecture x86_64", workflow)
        self.assertIn('("macos", "arm64")', workflow)

        qualification = (root / ".github/workflows/release-qualification.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("macos-26-intel", qualification)
        self.assertIn("arch: arm64", qualification)
        self.assertIn('--architecture "$ARCH"', qualification)
        self.assertIn("$architecture = $env:ARCH", qualification)
        # The WSL leg passes the architecture through a static dispatch script
        # with direct argv handoff, never an interpolated bash -lc string.
        self.assertNotIn("bash -lc", qualification)
        self.assertIn('--architecture "$architecture"', qualification)
        self.assertNotIn("-x86_64.zip", qualification.replace("linux-x86_64.zip", ""))

    @staticmethod
    def _native_legs(native):
        """Map platform -> matrix fields for each leg of the native job."""
        legs = {}
        for chunk in native.split("- os:")[1:]:
            fields = dict(
                re.findall(
                    r"^[ \t]*(platform|arch|python):[ \t]*'?([^'\n]+?)'?[ \t]*$",
                    chunk,
                    re.M,
                )
            )
            fields["os"] = chunk.splitlines()[0].strip()
            if "platform" in fields:
                legs[fields["platform"]] = fields
        return legs

    def test_release_locks_are_hashed_and_complete_for_every_target(self):
        root = Path(__file__).resolve().parents[1]
        for platform in ("linux", "macos", "windows"):
            for kind in ("base", "runtime", "release"):
                lock = (root / "requirements/locks" / f"{kind}-{platform}-x86_64.txt").read_text(
                    encoding="utf-8"
                )
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
        for symbol in (
            "QualityCheck",
            "QualityBinding",
            "quality_record_hash",
            "read_quality_record",
            "migrate_quality_record",
        ):
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
        self.assertIn(f'VERSION = "{RELEASE_VERSION}"', version)
        self.assertIn("__version__", distribution)
        self.assertIn("__version__", assembler)
        self.assertIn(f"comic-sol:{RELEASE_VERSION}", compose)
        for module in (
            "normalize_panels.py",
            "typography.py",
            "layouts.py",
            "page_quality.py",
            "pdf_quality.py",
            "quality_sample.py",
        ):
            self.assertIn(module, release_contract)


if __name__ == "__main__":
    unittest.main()
