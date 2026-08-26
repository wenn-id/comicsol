from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import os
import stat
import tempfile
import types
import unittest
import warnings
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from comic_sol_product import cli as product_cli
from comic_sol_product.release import REQUIRED_SDIST_SUFFIXES, REQUIRED_WHEEL_MEMBERS
from scripts import comic_sol, sync_plugin_bundle
from scripts.comic_sol import main as source_main
from scripts.core_primitives import canonical_json_bytes


# WP3 intentionally freezes this smallest public archive surface and layout.
EXPECTED_ARCHIVE_CONSTANTS = {
    "ARCHIVE_FORMAT_VERSION": "1.0",
    "ARCHIVE_SUFFIX": ".comic-sol-handoff",
    "FORMAT_METADATA_MEMBER": "comic-sol-handoff.json",
    "CHECKSUM_MANIFEST_MEMBER": "checksums.json",
    "PROJECT_MEMBER_PREFIX": "project/",
    "FIXED_ZIP_DATETIME": (1980, 1, 1, 0, 0, 0),
}
EXPECTED_ARCHIVE_FUNCTIONS = (
    "export_handoff_archive",
    "inspect_handoff_archive",
    "import_handoff_archive",
)
EXPECTED_LIMIT_CONSTANTS = (
    "MAX_ARCHIVE_MEMBERS",
    "MAX_MEMBER_BYTES",
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    "MAX_COMPRESSION_RATIO",
)
FORMAT_METADATA_MEMBER = EXPECTED_ARCHIVE_CONSTANTS["FORMAT_METADATA_MEMBER"]
CHECKSUM_MANIFEST_MEMBER = EXPECTED_ARCHIVE_CONSTANTS["CHECKSUM_MANIFEST_MEMBER"]
PROJECT_MEMBER_PREFIX = EXPECTED_ARCHIVE_CONSTANTS["PROJECT_MEMBER_PREFIX"]
FIXED_ZIP_DATETIME = EXPECTED_ARCHIVE_CONSTANTS["FIXED_ZIP_DATETIME"]


class _MissingWp3Implementation(AssertionError):
    pass


def _archive_module(testcase: unittest.TestCase):
    try:
        return importlib.import_module("scripts.handoff_archive")
    except ModuleNotFoundError as error:
        if error.name == "scripts.handoff_archive":
            raise _MissingWp3Implementation(
                "required WP3 module scripts.handoff_archive is not implemented"
            ) from None
        raise


def _archive_api(testcase: unittest.TestCase):
    module = _archive_module(testcase)
    for name, expected in EXPECTED_ARCHIVE_CONSTANTS.items():
        testcase.assertTrue(
            hasattr(module, name),
            f"required WP3 constant scripts.handoff_archive.{name} is not implemented",
        )
        testcase.assertEqual(expected, getattr(module, name), name)
    for name in EXPECTED_LIMIT_CONSTANTS:
        testcase.assertIsInstance(getattr(module, name, None), (int, float), name)
        testcase.assertGreater(getattr(module, name), 0, name)
    testcase.assertTrue(
        isinstance(getattr(module, "HandoffArchiveError", None), type),
        "required WP3 error scripts.handoff_archive.HandoffArchiveError is not implemented",
    )
    for name in EXPECTED_ARCHIVE_FUNCTIONS:
        testcase.assertTrue(
            callable(getattr(module, name, None)),
            f"required WP3 API scripts.handoff_archive.{name} is not implemented",
        )
    return module


def _prepared_project(testcase: unittest.TestCase) -> Path:
    # Import locally so unittest does not rediscover the imported TestCase class.
    from tests.test_handoff_lifecycle import HandoffLifecycleGoldenTests

    _root, project = HandoffLifecycleGoldenTests._planner_project(testcase)
    comic_sol.prepare_handoff(project)
    return project


def _artifact_snapshot(project: Path) -> dict[str, bytes]:
    snapshot = {}
    for path in sorted(project.rglob("*"), key=lambda item: item.relative_to(project).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(project).as_posix()
        if relative == ".comic-sol.lock" or relative.startswith("logs/transactions/"):
            continue
        snapshot[relative] = path.read_bytes()
    return snapshot


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    if not root.exists():
        return {}
    snapshot = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.fsencode(os.readlink(path)))
        elif path.is_dir():
            snapshot[relative] = ("directory", b"")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _archive_entries(archive: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(archive, "r") as bundle:
        return [(copy.copy(info), bundle.read(info)) for info in bundle.infolist()]


def _write_entries(archive: Path, entries: list[tuple[zipfile.ZipInfo, bytes]]) -> None:
    with (
        warnings.catch_warnings(),
        zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle,
    ):
        warnings.simplefilter("ignore", UserWarning)
        for info, payload in entries:
            bundle.writestr(info, payload)


def _corrupt_raw_deflate_member(archive: Path, member: str) -> None:
    with zipfile.ZipFile(archive, "r") as bundle:
        info = bundle.getinfo(member)
    payload = bytearray(archive.read_bytes())
    header = info.header_offset
    name_size = int.from_bytes(payload[header + 26 : header + 28], "little")
    extra_size = int.from_bytes(payload[header + 28 : header + 30], "little")
    compressed_offset = header + 30 + name_size + extra_size
    if info.compress_size <= 0:
        raise AssertionError(f"fixture member has no compressed payload: {member}")
    payload[compressed_offset] = (payload[compressed_offset] & 0xF8) | 0x07
    archive.write_bytes(payload)


def _zero_compressed_deflate_member(archive: Path, member: str) -> None:
    with zipfile.ZipFile(archive, "r") as bundle:
        info = bundle.getinfo(member)
    if info.file_size != 0 or info.compress_size <= 0:
        raise AssertionError(f"fixture member is not compressed empty data: {member}")

    payload = bytearray(archive.read_bytes())
    local = info.header_offset
    name_size = int.from_bytes(payload[local + 26 : local + 28], "little")
    extra_size = int.from_bytes(payload[local + 28 : local + 30], "little")
    compressed_offset = local + 30 + name_size + extra_size
    del payload[compressed_offset : compressed_offset + info.compress_size]
    payload[local + 18 : local + 22] = (0).to_bytes(4, "little")

    eocd = payload.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise AssertionError("fixture archive has no end-of-central-directory record")
    central_offset = int.from_bytes(payload[eocd + 16 : eocd + 20], "little")
    central_offset -= info.compress_size
    payload[eocd + 16 : eocd + 20] = central_offset.to_bytes(4, "little")
    entry_count = int.from_bytes(payload[eocd + 10 : eocd + 12], "little")

    cursor = central_offset
    found = False
    for _index in range(entry_count):
        if payload[cursor : cursor + 4] != b"PK\x01\x02":
            raise AssertionError("fixture central directory is malformed")
        central_name_size = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        central_extra_size = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_size = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = bytes(payload[cursor + 46 : cursor + 46 + central_name_size]).decode()
        if name == member:
            payload[cursor + 20 : cursor + 24] = (0).to_bytes(4, "little")
            found = True
        cursor += 46 + central_name_size + central_extra_size + comment_size
    if not found:
        raise AssertionError(f"fixture central directory did not contain {member}")

    archive.write_bytes(payload)
    with zipfile.ZipFile(archive, "r") as bundle:
        patched = bundle.getinfo(member)
        if patched.file_size != 0 or patched.compress_size != 0:
            raise AssertionError("fixture zero-size metadata patch failed")


def _regular_info(name: str, *, payload_mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_ZIP_DATETIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | payload_mode) << 16
    return info


def _replace_entry(
    entries: list[tuple[zipfile.ZipInfo, bytes]],
    name: str,
    payload: bytes,
) -> list[tuple[zipfile.ZipInfo, bytes]]:
    replaced = False
    result = []
    for info, current in entries:
        if info.filename == name:
            current = payload
            replaced = True
        result.append((info, current))
    if not replaced:
        raise AssertionError(f"fixture archive did not contain {name}")
    return result


def _canonical_checksum_payload(
    entries: list[tuple[zipfile.ZipInfo, bytes]],
) -> bytes:
    files = [
        {"path": info.filename, "sha256": hashlib.sha256(payload).hexdigest()}
        for info, payload in entries
        if info.filename.startswith(PROJECT_MEMBER_PREFIX)
    ]
    files.sort(key=lambda item: item["path"])
    return canonical_json_bytes(
        {
            "algorithm": "sha256",
            "files": files,
            "format_version": EXPECTED_ARCHIVE_CONSTANTS["ARCHIVE_FORMAT_VERSION"],
        }
    )


def _refresh_checksums(
    entries: list[tuple[zipfile.ZipInfo, bytes]],
) -> list[tuple[zipfile.ZipInfo, bytes]]:
    payload = _canonical_checksum_payload(entries)
    return _replace_entry(entries, CHECKSUM_MANIFEST_MEMBER, payload)


class HandoffArchiveContractTests(unittest.TestCase):
    def _export_fixture(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        archive_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(archive_root, ignore_errors=True))
        archive = archive_root / f"{project.name}.comic-sol-handoff"
        module.export_handoff_archive(project, archive)
        self.assertTrue(archive.is_file())
        return module, project, archive, archive_root

    def _mutated_archive(self, archive: Path, entries, suffix: str) -> Path:
        mutated = archive.with_name(f"{archive.stem}-{suffix}{archive.suffix}")
        _write_entries(mutated, entries)
        return mutated

    def _assert_import_rejected(
        self,
        module,
        archive: Path,
        message_pattern: str,
    ) -> Path:
        output_root = archive.parent / "imports"
        output_root.mkdir(exist_ok=True)
        unrelated = output_root / "unrelated.txt"
        unrelated.write_bytes(b"preserve me")
        before = _tree_snapshot(output_root)
        with self.assertRaisesRegex(module.HandoffArchiveError, message_pattern):
            module.import_handoff_archive(archive, output_root)
        self.assertEqual(before, _tree_snapshot(output_root))
        return output_root

    def _assert_inspect_rejected_without_residue(
        self,
        module,
        archive: Path,
        message_pattern: str,
    ) -> None:
        archive_bytes = archive.read_bytes()
        before = _tree_snapshot(archive.parent)
        temporary_paths = []
        real_mkdtemp = tempfile.mkdtemp

        def record_mkdtemp(*args, **kwargs):
            path = Path(real_mkdtemp(*args, **kwargs))
            temporary_paths.append(path)
            return str(path)

        with (
            mock.patch.object(
                module.tempfile,
                "mkdtemp",
                side_effect=record_mkdtemp,
            ),
            self.assertRaisesRegex(module.HandoffArchiveError, message_pattern),
        ):
            module.inspect_handoff_archive(archive)

        self.assertEqual(archive_bytes, archive.read_bytes())
        self.assertEqual(before, _tree_snapshot(archive.parent))
        self.assertTrue(all(not path.exists() for path in temporary_paths))

    def test_public_api_constants_and_functions_are_stable(self):
        _archive_api(self)

    def test_export_is_byte_identical_and_has_canonical_sorted_layout(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        first = root / "first.comic-sol-handoff"
        second = root / "second.comic-sol-handoff"

        module.export_handoff_archive(project, first)
        module.export_handoff_archive(project, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        project_snapshot = _artifact_snapshot(project)
        expected_project_names = {PROJECT_MEMBER_PREFIX + relative for relative in project_snapshot}
        with zipfile.ZipFile(first, "r") as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            self.assertEqual(sorted(names), names)
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(
                expected_project_names | {FORMAT_METADATA_MEMBER, CHECKSUM_MANIFEST_MEMBER},
                set(names),
            )
            for info in infos:
                with self.subTest(member=info.filename):
                    self.assertEqual(FIXED_ZIP_DATETIME, info.date_time)
                    self.assertEqual(3, info.create_system)
                    self.assertEqual(stat.S_IFREG | 0o644, info.external_attr >> 16)
                    self.assertEqual(zipfile.ZIP_DEFLATED, info.compress_type)
            metadata_bytes = bundle.read(FORMAT_METADATA_MEMBER)
            metadata = json.loads(metadata_bytes)
            self.assertEqual(canonical_json_bytes(metadata), metadata_bytes)
            self.assertEqual(
                {
                    "format": "comic-sol-handoff",
                    "project_id": project.name,
                    "version": "1.0",
                },
                metadata,
            )
            checksums_bytes = bundle.read(CHECKSUM_MANIFEST_MEMBER)
            checksums = json.loads(checksums_bytes)
            self.assertEqual(canonical_json_bytes(checksums), checksums_bytes)
            self.assertEqual(
                json.loads(_canonical_checksum_payload(_archive_entries(first))), checksums
            )

    def test_export_import_export_round_trip_is_byte_identical(self):
        module, project, first, root = self._export_fixture()
        output_root = root / "imported"
        output_root.mkdir()

        module.import_handoff_archive(first, output_root)
        imported = output_root / project.name
        second = root / "round-trip.comic-sol-handoff"
        module.export_handoff_archive(imported, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(_artifact_snapshot(project), _artifact_snapshot(imported))
        inspection = comic_sol.inspect_handoff(imported)
        self.assertTrue(inspection["prepared"])
        self.assertEqual("current", inspection["scope_state"])

    def test_export_accepts_lexically_aliased_output_parent(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        parent = root / "archives"
        nested = parent / "nested"
        nested.mkdir(parents=True)
        requested = nested / ".." / "lexical.comic-sol-handoff"

        result = module.export_handoff_archive(project, requested)

        expected = parent / requested.name
        self.assertEqual(str(expected.resolve()), result["archive_path"])
        self.assertTrue(expected.is_file())

    def test_import_accepts_lexically_aliased_output_root(self):
        module, project, archive, root = self._export_fixture()
        output_root = root / "imports"
        nested = output_root / "nested"
        nested.mkdir(parents=True)
        requested = nested / ".."

        result = module.import_handoff_archive(archive, requested)

        expected = output_root / project.name
        self.assertEqual(str(expected.resolve()), result["project_dir"])
        self.assertEqual(_artifact_snapshot(project), _artifact_snapshot(expected))

    def test_export_and_import_accept_symlinked_ancestor_directories(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        real = root / "real"
        archive_parent = real / "archives"
        output_root = real / "imports"
        archive_parent.mkdir(parents=True)
        output_root.mkdir()
        alias = root / "alias"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        archive = alias / "archives" / "ancestor.comic-sol-handoff"

        export_result = module.export_handoff_archive(project, archive)
        expected_archive = (archive_parent / archive.name).resolve()
        import_result = module.import_handoff_archive(expected_archive, alias / "imports")

        expected_project = (output_root / project.name).resolve()
        self.assertEqual(str(expected_archive), export_result["archive_path"])
        self.assertEqual(str(expected_project), import_result["project_dir"])
        self.assertEqual(_artifact_snapshot(project), _artifact_snapshot(expected_project))

    def test_exported_repetitive_project_member_remains_safe_and_round_trips(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        repetitive = project / "logs" / "repetitive.txt"
        repetitive.write_bytes(b"0" * 8192)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        first = root / "repetitive.comic-sol-handoff"

        module.export_handoff_archive(project, first)
        inspection = module.inspect_handoff_archive(first)
        output_root = root / "imports"
        output_root.mkdir()
        module.import_handoff_archive(first, output_root)
        imported = output_root / project.name
        second = root / "repetitive-round-trip.comic-sol-handoff"
        module.export_handoff_archive(imported, second)

        self.assertTrue(inspection["valid"])
        self.assertEqual(repetitive.read_bytes(), (imported / "logs/repetitive.txt").read_bytes())
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_nested_import_creates_verified_parents_on_no_nofollow_fallback(self):
        module, project, archive, root = self._export_fixture()
        output_root = root / "fallback-import"
        output_root.mkdir()

        with mock.patch("scripts.project_io._HAS_NOFOLLOW", False):
            module.import_handoff_archive(archive, output_root)

        imported = output_root / project.name
        self.assertEqual(_artifact_snapshot(project), _artifact_snapshot(imported))
        self.assertTrue((imported / "generation" / "batches.json").is_file())
        self.assertTrue((imported / "handoff" / "manifest.json").is_file())

    def test_archive_inspection_is_read_only_and_reports_verified_identity(self):
        module, project, archive, _root = self._export_fixture()
        before = archive.read_bytes()

        result = module.inspect_handoff_archive(archive)

        self.assertEqual(before, archive.read_bytes())
        self.assertTrue(result["valid"])
        self.assertEqual("1.0", result["format_version"])
        self.assertEqual(project.name, result["project_id"])
        self.assertEqual(len(_archive_entries(archive)), result["member_count"])
        self.assertEqual(len(_artifact_snapshot(project)), result["checksum_count"])

    def test_export_fsyncs_archive_and_parent_directory_before_success(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "durable.comic-sol-handoff"
        real_fsync = os.fsync
        flushed = []

        def observe_fsync(descriptor):
            mode = os.fstat(descriptor).st_mode
            flushed.append("directory" if stat.S_ISDIR(mode) else "file")
            return real_fsync(descriptor)

        with mock.patch.object(module.os, "fsync", side_effect=observe_fsync):
            module.export_handoff_archive(project, destination)

        self.assertTrue(destination.is_file())
        self.assertIn("file", flushed)
        if os.name != "nt":
            self.assertIn("directory", flushed)
            self.assertLess(flushed.index("file"), flushed.index("directory"))

    def test_export_rolls_back_owned_destination_when_parent_fsync_fails(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "fsync-failure.comic-sol-handoff"
        unrelated = root / "unrelated.txt"
        unrelated.write_bytes(b"preserve me")

        with (
            mock.patch.object(
                module,
                "fsync_directory",
                side_effect=OSError("simulated parent fsync failure"),
            ),
            self.assertRaisesRegex(module.HandoffArchiveError, "publish|interruption"),
        ):
            module.export_handoff_archive(project, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(b"preserve me", unrelated.read_bytes())
        quarantines = list(root.glob(".comic-sol-rollback-*.tmp"))
        self.assertEqual(1, len(quarantines))
        self.assertTrue(quarantines[0].is_file())

    def test_export_supports_platforms_without_descriptor_chmod(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "portable.comic-sol-handoff"

        with mock.patch.object(module.os, "fchmod", None, create=True):
            result = module.export_handoff_archive(project, destination)
            inspection = module.inspect_handoff_archive(destination)

        self.assertEqual(str(destination.resolve()), result["archive_path"])
        self.assertTrue(inspection["valid"])

    def test_export_closes_and_removes_temporary_when_initial_fstat_fails(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "fstat-failure.comic-sol-handoff"
        observed = {}
        real_mkstemp = module.tempfile.mkstemp
        real_fstat = module.os.fstat
        failed = False

        def record_mkstemp(*args, **kwargs):
            descriptor, name = real_mkstemp(*args, **kwargs)
            observed.update(descriptor=descriptor, path=Path(name))
            return descriptor, name

        def fail_initial_fstat(descriptor):
            nonlocal failed
            if descriptor == observed.get("descriptor") and not failed:
                failed = True
                raise OSError("simulated initial temporary identity failure")
            return real_fstat(descriptor)

        try:
            with (
                mock.patch.object(module.tempfile, "mkstemp", side_effect=record_mkstemp),
                mock.patch.object(module.os, "fstat", side_effect=fail_initial_fstat),
                self.assertRaisesRegex(module.HandoffArchiveError, "publish|interruption"),
            ):
                module.export_handoff_archive(project, destination)

            with self.assertRaises(OSError):
                real_fstat(observed["descriptor"])
            self.assertFalse(observed["path"].exists())
            self.assertEqual([], list(root.iterdir()))
        finally:
            if "descriptor" in observed:
                try:
                    os.close(observed["descriptor"])
                except OSError:
                    pass
            if "path" in observed:
                observed["path"].unlink(missing_ok=True)

    def test_export_closes_temporary_descriptor_when_mode_setup_fails(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "mode-failure.comic-sol-handoff"
        observed = {}
        real_mkstemp = module.tempfile.mkstemp
        real_fstat = module.os.fstat

        def record_mkstemp(*args, **kwargs):
            descriptor, name = real_mkstemp(*args, **kwargs)
            observed.update(descriptor=descriptor, path=Path(name))
            return descriptor, name

        try:
            with (
                mock.patch.object(module.tempfile, "mkstemp", side_effect=record_mkstemp),
                mock.patch.object(
                    module,
                    "_set_descriptor_mode",
                    side_effect=OSError("simulated temporary mode failure"),
                ),
                self.assertRaisesRegex(module.HandoffArchiveError, "publish|interruption"),
            ):
                module.export_handoff_archive(project, destination)

            with self.assertRaises(OSError):
                real_fstat(observed["descriptor"])
            self.assertFalse(observed["path"].exists())
            self.assertEqual([], list(root.iterdir()))
        finally:
            if "descriptor" in observed:
                try:
                    os.close(observed["descriptor"])
                except OSError:
                    pass
            if "path" in observed:
                observed["path"].unlink(missing_ok=True)

    def test_export_uses_sibling_temporary_and_cleans_it_on_publish_interruption(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "interrupted.comic-sol-handoff"
        rename = getattr(module, "_atomic_rename_noreplace", None)
        self.assertTrue(
            callable(rename),
            "WP3 export must bind project_io._atomic_rename_noreplace for no-clobber publication",
        )
        observed = {}

        def interrupt(source, target):
            observed["source"] = Path(source)
            observed["target"] = Path(target)
            self.assertTrue(Path(source).is_file())
            raise OSError("simulated archive publish interruption")

        with (
            mock.patch.object(module, "_atomic_rename_noreplace", side_effect=interrupt),
            self.assertRaisesRegex(module.HandoffArchiveError, "publish|interruption"),
        ):
            module.export_handoff_archive(project, destination)

        expected_destination = destination.parent.resolve() / destination.name
        self.assertEqual(expected_destination, observed["target"])
        self.assertEqual(expected_destination.parent, observed["source"].parent)
        self.assertFalse(expected_destination.exists())
        self.assertEqual([], list(root.iterdir()))

    def test_export_never_clobbers_an_existing_archive_or_leaves_temporary_files(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "existing.comic-sol-handoff"
        destination.write_bytes(b"existing archive bytes")
        before = _tree_snapshot(root)

        with self.assertRaisesRegex(module.HandoffArchiveError, "exist|clobber"):
            module.export_handoff_archive(project, destination)

        self.assertEqual(b"existing archive bytes", destination.read_bytes())
        self.assertEqual(before, _tree_snapshot(root))

    def test_export_rejects_noncanonical_archive_suffix_without_writing(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        destination = root / "handoff.zip"
        before = _tree_snapshot(root)

        with self.assertRaisesRegex(module.HandoffArchiveError, "suffix|comic-sol-handoff"):
            module.export_handoff_archive(project, destination)

        self.assertEqual(before, _tree_snapshot(root))

    def test_export_rejects_destination_inside_source_project_without_writing(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        nested = project / "exports" / "portable"
        nested.mkdir(parents=True)
        destination = nested / "handoff.comic-sol-handoff"
        before = _tree_snapshot(project)

        with self.assertRaisesRegex(module.HandoffArchiveError, "inside|source|project"):
            module.export_handoff_archive(project, destination)

        self.assertEqual(before, _tree_snapshot(project))

    def test_export_member_limit_stops_before_reading_the_over_limit_member(self):
        module = _archive_api(self)
        project = _prepared_project(self)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        base_member_count = len(_artifact_snapshot(project)) + 2
        extra_relative = "zz-over-member-limit.bin"
        (project / extra_relative).write_bytes(b"not read")
        reads = []
        real_read = module.read_contained_bytes

        def observe_read(project_dir, relative, **kwargs):
            reads.append(os.fspath(relative).replace("\\", "/"))
            return real_read(project_dir, relative, **kwargs)

        with (
            mock.patch.object(
                module,
                "MAX_ARCHIVE_MEMBERS",
                base_member_count,
            ),
            mock.patch.object(
                module,
                "read_contained_bytes",
                side_effect=observe_read,
            ),
            self.assertRaisesRegex(module.HandoffArchiveError, "member.*count|too many"),
        ):
            module.export_handoff_archive(
                project,
                root / "member-limit.comic-sol-handoff",
            )

        self.assertNotIn(extra_relative, reads)
        self.assertEqual({}, _tree_snapshot(root))

    def test_export_aggregate_limit_stops_before_reading_the_over_limit_member(self):
        module, project, archive, root = self._export_fixture()
        with zipfile.ZipFile(archive, "r") as bundle:
            base_total = sum(info.file_size for info in bundle.infolist())
        extra_relative = "zz-over-aggregate-limit.bin"
        (project / extra_relative).write_bytes(b"not read")
        reads = []
        real_read = module.read_contained_bytes

        def observe_read(project_dir, relative, **kwargs):
            reads.append(os.fspath(relative).replace("\\", "/"))
            return real_read(project_dir, relative, **kwargs)

        destination = root / "aggregate-limit.comic-sol-handoff"
        with (
            mock.patch.object(
                module,
                "MAX_TOTAL_UNCOMPRESSED_BYTES",
                base_total,
            ),
            mock.patch.object(
                module,
                "read_contained_bytes",
                side_effect=observe_read,
            ),
            self.assertRaisesRegex(module.HandoffArchiveError, "aggregate|total|uncompressed"),
        ):
            module.export_handoff_archive(project, destination)

        self.assertNotIn(extra_relative, reads)
        self.assertFalse(destination.exists())

    def test_source_traversal_budget_refuses_before_accumulating_beyond_limit(self):
        module = _archive_api(self)
        project = Path("/prepared-project")
        directory_metadata = types.SimpleNamespace(st_mode=stat.S_IFDIR)
        file_metadata = types.SimpleNamespace(st_mode=stat.S_IFREG)

        directory_entry = mock.Mock()
        directory_entry.name = "nested"
        directory_entry.stat.return_value = directory_metadata
        within_budget_entry = mock.Mock()
        within_budget_entry.name = "first.txt"
        within_budget_entry.stat.return_value = file_metadata
        over_budget_entry = mock.Mock()
        over_budget_entry.name = "second.txt"
        over_budget_entry.stat.return_value = file_metadata

        class ObservedScanner:
            def __init__(self, entries):
                self._entries = iter(entries)
                self.returned = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self._entries)
                self.returned.append(entry)
                return entry

        root_scanner = ObservedScanner([directory_entry])
        nested_scanner = ObservedScanner([within_budget_entry, over_budget_entry])
        scanners = {
            project: root_scanner,
            project / "nested": nested_scanner,
        }

        with (
            mock.patch.object(
                module,
                "MAX_ARCHIVE_MEMBERS",
                2,
            ),
            mock.patch.object(
                module.os,
                "scandir",
                side_effect=lambda directory: scanners[Path(directory)],
            ),
        ):
            entries = module._iter_project_entries(project)
            self.assertEqual(("nested", directory_metadata), next(entries))
            with self.assertRaisesRegex(module.HandoffArchiveError, "member.*count|traversal"):
                next(entries)

        self.assertEqual([directory_entry], root_scanner.returned)
        self.assertEqual(
            [within_budget_entry, over_budget_entry],
            nested_scanner.returned,
        )
        directory_entry.stat.assert_called_once_with(follow_symlinks=False)
        within_budget_entry.stat.assert_not_called()
        over_budget_entry.stat.assert_not_called()

    def test_rejects_unsupported_archive_version(self):
        module, _project, archive, _root = self._export_fixture()
        entries = _archive_entries(archive)
        metadata = json.loads(
            next(payload for info, payload in entries if info.filename == FORMAT_METADATA_MEMBER)
        )
        metadata["version"] = "999.0"
        entries = _replace_entry(entries, FORMAT_METADATA_MEMBER, canonical_json_bytes(metadata))
        malicious = self._mutated_archive(archive, entries, "unsupported-version")
        self._assert_import_rejected(module, malicious, "unsupported.*version")

    def test_rejects_non_zip_and_truncated_central_directory(self):
        module, _project, archive, _root = self._export_fixture()
        cases = {
            "not-zip": b"not a ZIP archive",
            "truncated": archive.read_bytes()[:-16],
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                malformed = archive.with_name(f"{archive.stem}-{label}{archive.suffix}")
                malformed.write_bytes(payload)
                self._assert_import_rejected(module, malformed, "archive|central|corrupt|zip")

    def test_public_routes_classify_malformed_deflate_as_handoff_archive_error(self):
        from comic_sol_product.errors import classify_exception
        from scripts.command_service import CommandService

        module, project, archive, root = self._export_fixture()
        malformed = archive.with_name(f"{archive.stem}-malformed-deflate{archive.suffix}")
        malformed.write_bytes(archive.read_bytes())
        _corrupt_raw_deflate_member(malformed, FORMAT_METADATA_MEMBER)
        output_root = root / "malformed-deflate-output"
        output_root.mkdir()
        before = _tree_snapshot(output_root)
        cases = (
            ("handoff.inspect", {"archive_path": malformed}),
            (
                "handoff.import",
                {"archive_path": malformed, "output_root": output_root},
            ),
        )

        for route, arguments in cases:
            with self.subTest(route=route):
                with self.assertRaises(module.HandoffArchiveError) as raised:
                    CommandService().execute(route, **arguments)
                self.assertEqual("CS-HANDOFF-002", classify_exception(raised.exception).code)
                self.assertEqual(before, _tree_snapshot(output_root))
                self.assertFalse((output_root / project.name).exists())

    def test_public_routes_reject_zero_compressed_size_deflated_member(self):
        from comic_sol_product.errors import classify_exception
        from scripts.command_service import CommandService

        module, project, archive, root = self._export_fixture()
        member = "project/zz-empty.bin"
        entries = [*_archive_entries(archive), (_regular_info(member), b"")]
        entries = _refresh_checksums(entries)
        malformed = self._mutated_archive(archive, entries, "zero-compressed-size")
        _zero_compressed_deflate_member(malformed, member)
        output_root = root / "zero-compressed-size-output"
        output_root.mkdir()
        before = _tree_snapshot(output_root)
        cases = (
            ("handoff.inspect", {"archive_path": malformed}),
            (
                "handoff.import",
                {"archive_path": malformed, "output_root": output_root},
            ),
        )

        for route, arguments in cases:
            with self.subTest(route=route):
                with self.assertRaises(module.HandoffArchiveError) as raised:
                    CommandService().execute(route, **arguments)
                self.assertEqual("CS-HANDOFF-002", classify_exception(raised.exception).code)
                self.assertEqual(before, _tree_snapshot(output_root))
                self.assertFalse((output_root / project.name).exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_archive_input_must_be_a_nonblocking_verified_regular_file(self):
        module, _project, archive, _root = self._export_fixture()
        fifo = archive.with_name(f"{archive.stem}-fifo{archive.suffix}")
        os.mkfifo(fifo)
        holder = os.open(fifo, os.O_RDWR | getattr(os, "O_NONBLOCK", 0))
        try:
            with self.assertRaisesRegex(module.HandoffArchiveError, "regular.*file"):
                module.inspect_handoff_archive(fifo)
        finally:
            os.close(holder)

    def test_rejects_duplicate_and_case_colliding_members(self):
        module, _project, archive, _root = self._export_fixture()
        base = _archive_entries(archive)
        project_entry = next(
            (copy.copy(info), payload)
            for info, payload in base
            if info.filename.startswith(PROJECT_MEMBER_PREFIX)
        )
        cases = {
            "duplicate": [*base, project_entry],
            "case-collision": [
                *base,
                (_regular_info("project/COLLISION.txt"), b"upper"),
                (_regular_info("project/collision.txt"), b"lower"),
            ],
        }
        for label, entries in cases.items():
            with self.subTest(case=label):
                malicious = self._mutated_archive(archive, entries, label)
                self._assert_import_rejected(
                    module,
                    malicious,
                    "duplicate" if label == "duplicate" else "case.*collid|collision",
                )

    def test_rejects_absolute_drive_unc_and_traversal_member_paths(self):
        module, _project, archive, _root = self._export_fixture()
        cases = {
            "posix-absolute": "/absolute.txt",
            "drive-absolute": "C:/absolute.txt",
            "unc-absolute": "//server/share/absolute.txt",
            "parent-traversal": "project/../escape.txt",
        }
        for label, member in cases.items():
            with self.subTest(case=label):
                entries = [*_archive_entries(archive), (_regular_info(member), b"unsafe")]
                malicious = self._mutated_archive(archive, entries, label)
                self._assert_import_rejected(module, malicious, "absolute|traversal|unsafe.*path")

    def test_rejects_symlink_reparse_and_other_non_regular_members(self):
        module, _project, archive, _root = self._export_fixture()
        symlink = zipfile.ZipInfo("project/symlink", FIXED_ZIP_DATETIME)
        symlink.create_system = 3
        symlink.compress_type = zipfile.ZIP_DEFLATED
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        reparse = zipfile.ZipInfo("project/reparse", FIXED_ZIP_DATETIME)
        reparse.create_system = 0
        reparse.compress_type = zipfile.ZIP_DEFLATED
        reparse.external_attr = 0x0400
        fifo = zipfile.ZipInfo("project/fifo", FIXED_ZIP_DATETIME)
        fifo.create_system = 3
        fifo.compress_type = zipfile.ZIP_DEFLATED
        fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
        cases = (
            ("symlink", symlink, b"project.json", "symlink"),
            ("reparse", reparse, b"", "reparse"),
            ("non-regular", fifo, b"", "regular"),
        )
        for label, info, payload, pattern in cases:
            with self.subTest(case=label):
                entries = [*_archive_entries(archive), (info, payload)]
                malicious = self._mutated_archive(archive, entries, label)
                self._assert_import_rejected(module, malicious, pattern)

    def test_rejects_excessive_member_count(self):
        module, _project, archive, _root = self._export_fixture()
        member_count = len(_archive_entries(archive))
        with mock.patch.object(module, "MAX_ARCHIVE_MEMBERS", member_count - 1):
            self._assert_import_rejected(module, archive, "member.*count|too many")

    def test_rejects_per_member_and_aggregate_uncompressed_limits(self):
        module, _project, archive, _root = self._export_fixture()
        with zipfile.ZipFile(archive, "r") as bundle:
            sizes = [info.file_size for info in bundle.infolist()]
        cases = (
            ("MAX_MEMBER_BYTES", max(sizes) - 1, "member.*limit|too large"),
            (
                "MAX_TOTAL_UNCOMPRESSED_BYTES",
                sum(sizes) - 1,
                "aggregate|total.*limit|uncompressed.*limit",
            ),
        )
        for constant, limit, pattern in cases:
            with self.subTest(limit=constant), mock.patch.object(module, constant, limit):
                self._assert_import_rejected(module, archive, pattern)

    def test_rejects_unsafe_compression_ratio(self):
        module, _project, archive, _root = self._export_fixture()
        entries = _archive_entries(archive)
        entries.append((_regular_info("project/high-ratio.txt"), b"0" * 8192))
        entries = _refresh_checksums(entries)
        malicious = self._mutated_archive(archive, entries, "compression-ratio")
        with mock.patch.object(module, "MAX_COMPRESSION_RATIO", 2.0):
            self._assert_import_rejected(module, malicious, "compression.*ratio")

    def test_rejects_checksum_mismatch(self):
        module, _project, archive, _root = self._export_fixture()
        entries = _archive_entries(archive)
        index = next(
            index
            for index, (info, _payload) in enumerate(entries)
            if info.filename.startswith(PROJECT_MEMBER_PREFIX)
        )
        info, payload = entries[index]
        entries[index] = (info, payload + b"tampered")
        malicious = self._mutated_archive(archive, entries, "checksum")
        self._assert_import_rejected(module, malicious, "checksum")

    def test_rejects_project_id_mismatch(self):
        module, project, archive, _root = self._export_fixture()
        entries = _archive_entries(archive)
        metadata = json.loads(
            next(payload for info, payload in entries if info.filename == FORMAT_METADATA_MEMBER)
        )
        metadata["project_id"] = project.name + "-other"
        entries = _replace_entry(entries, FORMAT_METADATA_MEMBER, canonical_json_bytes(metadata))
        malicious = self._mutated_archive(archive, entries, "project-id")
        self._assert_import_rejected(module, malicious, "project.*id|identity")

    def test_existing_destination_conflict_preserves_both_archive_and_destination(self):
        module, project, archive, root = self._export_fixture()
        output_root = root / "existing-output"
        destination = output_root / project.name
        destination.mkdir(parents=True)
        (destination / "sentinel.txt").write_bytes(b"existing project")
        before_archive = archive.read_bytes()
        before_output = _tree_snapshot(output_root)

        with self.assertRaisesRegex(module.HandoffArchiveError, "exist|destination"):
            module.import_handoff_archive(archive, output_root)

        self.assertEqual(before_archive, archive.read_bytes())
        self.assertEqual(before_output, _tree_snapshot(output_root))

    def test_complete_project_and_handoff_validation_failures_leave_no_publication(self):
        module, _project, archive, _root = self._export_fixture()
        original = _archive_entries(archive)

        missing_handoff = [
            (info, payload)
            for info, payload in original
            if info.filename != "project/handoff/manifest.json"
        ]
        missing_handoff = _refresh_checksums(missing_handoff)

        invalid_story = _replace_entry(
            [(copy.copy(info), payload) for info, payload in original],
            "project/plan/story-plan.json",
            canonical_json_bytes({}),
        )
        invalid_story = _refresh_checksums(invalid_story)

        stale_scope = _replace_entry(
            [(copy.copy(info), payload) for info, payload in original],
            "project/prompts/references/mira.txt",
            b"scope changed after handoff preparation\n",
        )
        stale_scope = _refresh_checksums(stale_scope)

        cases = (
            ("missing-handoff", missing_handoff, "valid|handoff|manifest"),
            ("invalid-project", invalid_story, "valid|project|story"),
            ("stale-scope", stale_scope, "stale|scope|digest"),
        )
        for label, entries, pattern in cases:
            with self.subTest(case=label):
                malicious = self._mutated_archive(archive, entries, label)
                self._assert_inspect_rejected_without_residue(
                    module,
                    malicious,
                    pattern,
                )
                self._assert_import_rejected(module, malicious, pattern)

    @unittest.skipUnless(
        os.name != "nt" and getattr(os, "O_NOFOLLOW", 0) and getattr(os, "O_DIRECTORY", 0),
        "retained no-follow staging descriptors require POSIX support",
    )
    def test_inspect_and_import_cleanup_after_initial_staging_identity_failure(self):
        module, _project, archive, root = self._export_fixture()
        real_mkdtemp = module.tempfile.mkdtemp
        real_path_stat = Path.stat
        real_fstat = module.os.fstat

        cases = (
            ("inspect", lambda output: module.inspect_handoff_archive(archive)),
            (
                "import",
                lambda output: module.import_handoff_archive(archive, output),
            ),
        )
        for route, invoke in cases:
            with self.subTest(route=route):
                output_root = root / f"{route}-identity-failure"
                output_root.mkdir()
                observed = {}
                failed = False

                def record_mkdtemp(*args, **kwargs):
                    path = Path(real_mkdtemp(*args, **kwargs))
                    observed["path"] = path
                    return str(path)

                def fail_initial_path_stat(path, *args, **kwargs):
                    nonlocal failed
                    if Path(path) == observed.get("path") and not failed:
                        failed = True
                        raise OSError("simulated staging identity failure")
                    return real_path_stat(path, *args, **kwargs)

                def fail_initial_fstat(descriptor):
                    nonlocal failed
                    metadata = real_fstat(descriptor)
                    if (
                        observed.get("path") is not None
                        and stat.S_ISDIR(metadata.st_mode)
                        and not failed
                    ):
                        failed = True
                        raise OSError("simulated staging identity failure")
                    return metadata

                try:
                    with (
                        mock.patch.object(
                            module.tempfile,
                            "mkdtemp",
                            side_effect=record_mkdtemp,
                        ),
                        mock.patch.object(
                            Path,
                            "stat",
                            autospec=True,
                            side_effect=fail_initial_path_stat,
                        ),
                        mock.patch.object(
                            module.os,
                            "fstat",
                            side_effect=fail_initial_fstat,
                        ),
                        self.assertRaises(module.HandoffArchiveError),
                    ):
                        invoke(output_root)

                    self.assertTrue(failed)
                    self.assertFalse(observed["path"].exists())
                    self.assertEqual([], list(output_root.iterdir()))
                finally:
                    if "path" in observed:
                        __import__("shutil").rmtree(observed["path"], ignore_errors=True)

    def test_inspect_and_import_preserve_unverified_staging_without_descriptor(self):
        module, project, archive, root = self._export_fixture()
        real_mkdtemp = module.tempfile.mkdtemp
        real_path_stat = Path.stat
        cases = (
            ("inspect", lambda output: module.inspect_handoff_archive(archive)),
            (
                "import",
                lambda output: module.import_handoff_archive(archive, output),
            ),
        )

        for route, invoke in cases:
            with self.subTest(route=route):
                output_root = root / f"{route}-unverified-staging"
                output_root.mkdir()
                observed = {}

                def record_mkdtemp(*args, **kwargs):
                    path = Path(real_mkdtemp(*args, **kwargs))
                    observed["path"] = path
                    return str(path)

                def fail_staging_stat(path, *args, **kwargs):
                    if Path(path) == observed.get("path"):
                        raise OSError("simulated unverified staging identity")
                    return real_path_stat(path, *args, **kwargs)

                cleanup = mock.Mock(wraps=module.cleanup_owned_directory)
                try:
                    with (
                        mock.patch.object(
                            module.tempfile,
                            "mkdtemp",
                            side_effect=record_mkdtemp,
                        ),
                        mock.patch.object(
                            module,
                            "_staging_directory_descriptor",
                            return_value=-1,
                        ),
                        mock.patch.object(
                            Path,
                            "stat",
                            autospec=True,
                            side_effect=fail_staging_stat,
                        ),
                        mock.patch.object(
                            module,
                            "cleanup_owned_directory",
                            cleanup,
                        ),
                        self.assertRaises(module.HandoffArchiveError),
                    ):
                        invoke(output_root)

                    cleanup.assert_not_called()
                    self.assertTrue(observed["path"].is_dir())
                    self.assertFalse((output_root / project.name).exists())
                finally:
                    if "path" in observed:
                        __import__("shutil").rmtree(observed["path"], ignore_errors=True)

    def test_post_rename_base_exception_rolls_back_only_owned_publication(self):
        module, project, archive, root = self._export_fixture()
        output_root = root / "publish-interruption"
        output_root.mkdir()
        unrelated = output_root / "unrelated.txt"
        unrelated.write_bytes(b"preserve me")
        before = _tree_snapshot(output_root)
        observed = {}

        canonical_output_root = output_root.resolve()

        def interrupt(staging, destination, *, expected_identity=None):
            staging = Path(staging)
            destination = Path(destination)
            observed.update(
                staging=staging,
                destination=destination,
                expected_identity=expected_identity,
            )
            metadata = staging.stat(follow_symlinks=False)
            self.assertEqual(canonical_output_root, staging.parent)
            self.assertEqual(canonical_output_root / project.name, destination)
            self.assertEqual((metadata.st_dev, metadata.st_ino), expected_identity)
            os.rename(staging, destination)
            self.assertTrue((destination / "project.json").is_file())
            raise KeyboardInterrupt("simulated interruption after rename")

        with (
            mock.patch.object(
                module,
                "publish_directory_noreplace",
                side_effect=interrupt,
            ),
            self.assertRaisesRegex(KeyboardInterrupt, "after rename"),
        ):
            module.import_handoff_archive(archive, output_root)

        self.assertEqual(canonical_output_root / project.name, observed["destination"])
        self.assertEqual(before, _tree_snapshot(output_root))


class InstalledHandoffArchiveCliTests(unittest.TestCase):
    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = product_cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_with_service(self, argv: list[str], service) -> tuple[int, str, str]:
        service_module = types.SimpleNamespace(CommandService=mock.Mock(return_value=service))
        with (
            mock.patch.object(product_cli, "_load_engine", return_value=types.SimpleNamespace()),
            mock.patch.object(
                product_cli, "_load_engine_module", return_value=types.SimpleNamespace()
            ),
            mock.patch.object(product_cli, "_load_command_service", return_value=service_module),
        ):
            return self.invoke(argv)

    def test_installed_routes_forward_exact_paths_and_stable_json_envelopes(self):
        project = Path("/shared/demo")
        archive = Path("/transfer/demo.comic-sol-handoff")
        output_root = Path("/shared/imports")
        cases = (
            (
                ["--json", "handoff", "export", str(project), "--output", str(archive)],
                "handoff.export",
                {"project_dir": project, "output_path": archive},
                {"project_id": "demo", "archive_path": str(archive)},
            ),
            (
                ["--json", "handoff", "inspect", str(archive)],
                "handoff.inspect",
                {"archive_path": archive},
                {"project_id": "demo", "valid": True, "format_version": "1.0"},
            ),
            (
                [
                    "--json",
                    "handoff",
                    "import",
                    str(archive),
                    "--output-root",
                    str(output_root),
                ],
                "handoff.import",
                {"archive_path": archive, "output_root": output_root},
                {"project_id": "demo", "project_dir": str(output_root / "demo")},
            ),
        )
        for argv, route, expected_arguments, result in cases:
            service = types.SimpleNamespace(execute=mock.Mock(return_value=result))
            with self.subTest(route=route):
                code, stdout, stderr = self.invoke_with_service(argv, service)
                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                self.assertEqual(1, len(stdout.splitlines()))
                self.assertEqual(
                    {"ok": True, "command": route, "data": result, "error": None},
                    json.loads(stdout),
                )
                service.execute.assert_called_once_with(route, **expected_arguments)

    def test_installed_directory_inspect_route_remains_backward_compatible(self):
        project = Path("/shared/demo")
        result = {"prepared": True, "scope_state": "current"}
        service = types.SimpleNamespace(execute=mock.Mock(return_value=result))

        code, _stdout, stderr = self.invoke_with_service(
            ["--json", "handoff", "inspect", str(project)], service
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        service.execute.assert_called_once_with("handoff.inspect", project_dir=project)

    def test_installed_archive_human_rendering_is_bounded_and_not_an_envelope(self):
        archive = Path("/transfer/demo.comic-sol-handoff")
        result = {
            "project_id": "demo",
            "archive_path": str(archive),
            "format_version": "1.0",
            "valid": True,
        }
        service = types.SimpleNamespace(execute=mock.Mock(return_value=result))

        code, stdout, stderr = self.invoke_with_service(
            ["handoff", "inspect", str(archive)], service
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertIn("demo", stdout)
        self.assertIn("1.0", stdout)
        self.assertIn("valid", stdout.lower())

    def test_archive_failures_use_handoff_002_envelope_and_exit_two(self):
        module = _archive_api(self)
        error = module.HandoffArchiveError("checksum mismatch")
        service = types.SimpleNamespace(execute=mock.Mock(side_effect=error))

        code, stdout, stderr = self.invoke_with_service(
            ["--json", "handoff", "inspect", "/transfer/bad.comic-sol-handoff"],
            service,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("handoff.inspect", payload["command"])
        self.assertEqual("CS-HANDOFF-002", payload["error"]["code"])
        self.assertNotIn("checksum mismatch", json.dumps(payload))


class SourceHandoffArchiveCliTests(unittest.TestCase):
    def invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = source_main(argv)
            except SystemExit as error:
                code = int(error.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_with_service(self, argv: list[str], service) -> tuple[int, str, str]:
        with mock.patch("scripts.command_service.CommandService", return_value=service):
            return self.invoke(argv)

    def test_source_routes_forward_exact_paths_and_render_raw_json(self):
        project = Path("/shared/demo")
        archive = Path("/transfer/demo.comic-sol-handoff")
        output_root = Path("/shared/imports")
        cases = (
            (
                [
                    "handoff",
                    "export",
                    str(project),
                    "--output",
                    str(archive),
                    "--json",
                ],
                "handoff.export",
                {"project_dir": project, "output_path": archive},
                {"project_id": "demo", "archive_path": str(archive)},
            ),
            (
                ["handoff", "inspect", str(archive), "--json"],
                "handoff.inspect",
                {"archive_path": archive},
                {"project_id": "demo", "valid": True, "format_version": "1.0"},
            ),
            (
                [
                    "handoff",
                    "import",
                    str(archive),
                    "--output-root",
                    str(output_root),
                    "--json",
                ],
                "handoff.import",
                {"archive_path": archive, "output_root": output_root},
                {"project_id": "demo", "project_dir": str(output_root / "demo")},
            ),
        )
        for argv, route, expected_arguments, result in cases:
            service = types.SimpleNamespace(execute=mock.Mock(return_value=result))
            with self.subTest(route=route):
                code, stdout, stderr = self.invoke_with_service(argv, service)
                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                self.assertEqual(result, json.loads(stdout))
                service.execute.assert_called_once_with(route, **expected_arguments)

    def test_source_directory_inspect_route_remains_backward_compatible(self):
        project = Path("/shared/demo")
        result = {"prepared": True, "scope_state": "current"}
        service = types.SimpleNamespace(execute=mock.Mock(return_value=result))

        code, _stdout, stderr = self.invoke_with_service(
            ["handoff", "inspect", str(project), "--json"], service
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        service.execute.assert_called_once_with("handoff.inspect", project_dir=project)

    def test_source_archive_human_rendering_is_bounded_and_not_json(self):
        archive = Path("/transfer/demo.comic-sol-handoff")
        result = {
            "project_id": "demo",
            "archive_path": str(archive),
            "format_version": "1.0",
            "valid": True,
        }
        service = types.SimpleNamespace(execute=mock.Mock(return_value=result))

        code, stdout, stderr = self.invoke_with_service(
            ["handoff", "inspect", str(archive)], service
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertIn("demo", stdout)
        self.assertIn("1.0", stdout)
        self.assertIn("valid", stdout.lower())

    def test_source_archive_failures_are_typed_input_errors(self):
        module = _archive_api(self)
        error = module.HandoffArchiveError("checksum mismatch")
        service = types.SimpleNamespace(execute=mock.Mock(side_effect=error))

        code, stdout, stderr = self.invoke_with_service(
            ["handoff", "inspect", "/transfer/bad.comic-sol-handoff", "--json"],
            service,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual(
            f"ERROR {type(error).__name__}: {error}\n",
            stderr,
        )


class HandoffArchiveDistributionInventoryTests(unittest.TestCase):
    def test_plugin_inventory_manages_archive_module_and_mirror(self):
        relative = Path("scripts/handoff_archive.py")
        self.assertIn(relative, sync_plugin_bundle.synchronized_paths())
        canonical = Path(__file__).resolve().parents[1] / relative
        bundled = Path(__file__).resolve().parents[1] / "skills/comic-sol" / relative
        self.assertTrue(canonical.is_file())
        self.assertTrue(bundled.is_file())
        self.assertEqual(canonical.read_bytes(), bundled.read_bytes())

    def test_release_inventory_requires_archive_module_in_wheel_and_sdist(self):
        self.assertIn("comic_sol_product/engine/handoff_archive.py", REQUIRED_WHEEL_MEMBERS)
        self.assertIn("/scripts/handoff_archive.py", REQUIRED_SDIST_SUFFIXES)

    def test_source_packaging_discovers_archive_module_without_build_only_exclusion(self):
        root = Path(__file__).resolve().parents[1]
        setup_text = (root / "setup.py").read_text(encoding="utf-8")
        self.assertIn('(ROOT / "scripts").glob("*.py")', setup_text)
        self.assertNotIn('"handoff_archive.py",', setup_text)
        self.assertTrue((root / "scripts/handoff_archive.py").is_file())


if __name__ == "__main__":
    unittest.main()
