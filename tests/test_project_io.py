import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import project_io
from scripts.project_io import contained_project_path
from tests.support import make_symlink


class ContainedProjectPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_rejects_absolute_traversal_and_windows_drive_paths(self):
        for bad in (
            "../outside.png", "/tmp/outside.png", "C:/outside.png",
            "C:outside.png", r"\\server\share\file.png", "//server/share/file.png",
        ):
            with self.subTest(path=bad):
                with self.assertRaisesRegex(ValueError, "relative project path"):
                    contained_project_path(self.project, bad)

    def test_nonexistent_contained_target_obeys_must_exist(self):
        # contained_project_path returns a resolved path, and the temp root is
        # itself a symlink on macOS (/var -> /private/var).
        expected = self.project.resolve() / "panels/new.png"
        self.assertEqual(expected, contained_project_path(self.project, "panels/new.png"))
        with self.assertRaises(FileNotFoundError):
            contained_project_path(self.project, "panels/new.png", must_exist=True)

    def test_rejects_sibling_prefix_escape(self):
        sibling = self.root / "project-other"
        sibling.mkdir()
        with self.assertRaisesRegex(ValueError, "relative project path"):
            contained_project_path(self.project, "../project-other/outside.png")

    def test_rejects_symlink_to_external_file(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        link = self.project / "linked.png"
        make_symlink(self, link, outside)
        with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
            contained_project_path(self.project, "linked.png", must_exist=True)

    def test_rejects_internal_directory_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.project / "panels"
        make_symlink(self, link, outside, directory=True)
        with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
            contained_project_path(self.project, "panels/image.png")

    @unittest.skipUnless(os.name == "nt", "Windows junction/reparse behavior requires native Windows")
    def test_rejects_windows_directory_junction_escape(self):
        outside = self.root / "outside-junction-target"
        outside.mkdir()
        junction = self.project / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.fspath(junction), os.fspath(outside)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            self.skipTest(f"junction creation unavailable: {result.stderr.strip()}")
        with self.assertRaisesRegex(ValueError, "escapes|symlinks"):
            contained_project_path(self.project, "junction/image.png")

    def test_returns_resolved_contained_path(self):
        nested = self.project / "panels/image.png"
        nested.parent.mkdir()
        nested.write_bytes(b"image")
        self.assertEqual(
            nested.resolve(),
            contained_project_path(self.project, "panels/image.png", must_exist=True),
        )

    def test_read_contained_bytes_rejects_final_symlink(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        link = self.project / "linked.txt"
        make_symlink(self, link, outside)
        with self.assertRaisesRegex(ValueError, "symlink|escapes"):
            project_io.read_contained_bytes(self.project, "linked.txt")


class DurableWriteTests(unittest.TestCase):
    def test_orders_write_flush_file_fsync_replace_and_directory_fsync(self):
        events = []

        class Handle:
            name = "/temporary/output.tmp"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def write(self, payload):
                events.append(("write", payload))

            def flush(self):
                events.append(("flush",))

            def fileno(self):
                return 17

        destination = Path("/destination/output.bin")
        with (
            mock.patch.object(
                project_io.tempfile, "NamedTemporaryFile", return_value=Handle()
            ),
            mock.patch.object(
                project_io.os,
                "fsync",
                side_effect=lambda fd: events.append(("fsync", fd)),
            ),
            mock.patch.object(
                project_io.os,
                "replace",
                side_effect=lambda source, target: events.append(
                    ("replace", Path(source), target)
                ),
            ),
            mock.patch.object(
                project_io,
                "fsync_directory",
                side_effect=lambda path: events.append(("directory fsync", path)),
            ),
            mock.patch.object(Path, "mkdir"),
        ):
            project_io.durable_atomic_write(destination, b"payload")

        self.assertEqual(
            [
                ("write", b"payload"),
                ("flush",),
                ("fsync", 17),
                ("replace", Path("/temporary/output.tmp"), destination),
                ("directory fsync", destination.parent),
            ],
            events,
        )

    def test_replace_failure_cleans_temporary_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = directory / "artifact.bin"
            destination.write_bytes(b"original")
            with mock.patch.object(
                project_io.os, "replace", side_effect=OSError("replace failed")
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    project_io.durable_atomic_write(destination, b"replacement")
            self.assertEqual(b"original", destination.read_bytes())
            self.assertEqual([destination], list(directory.iterdir()))


class ProjectTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        (self.project / "pages").mkdir(parents=True)
        (self.project / "logs").mkdir()
        (self.project / "pages/page-001.png").write_bytes(b"old-one")
        (self.project / "pages/page-002.png").write_bytes(b"old-two")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_second_publish_failure_restores_prior_set(self):
        real_replace = project_io.os.replace
        calls = 0

        def fail_second_staged_replace(source, destination, **kwargs):
            nonlocal calls
            source_path = Path(source)
            if source_path.name.startswith("staged-"):
                calls += 1
                if calls == 2:
                    raise OSError("injected second publish failure")
            return real_replace(source, destination, **kwargs)

        with self.assertRaisesRegex(OSError, "injected second publish failure"):
            with mock.patch.object(project_io.os, "replace", side_effect=fail_second_staged_replace):
                with project_io.ProjectTransaction(self.project, "composition") as transaction:
                    transaction.stage_bytes("pages/page-001.png", b"new-one")
                    transaction.stage_bytes("pages/page-002.png", b"new-two")
                    transaction.commit()

        self.assertEqual(b"old-one", (self.project / "pages/page-001.png").read_bytes())
        self.assertEqual(b"old-two", (self.project / "pages/page-002.png").read_bytes())

    def test_recover_restores_interrupted_publishing_transaction(self):
        real_replace = project_io.os.replace
        calls = 0

        def interrupt_after_first(source, destination, **kwargs):
            nonlocal calls
            source_path = Path(source)
            if source_path.name.startswith("staged-"):
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt("simulated process interruption")
            return real_replace(source, destination, **kwargs)

        tx = project_io.ProjectTransaction(self.project, "composition")
        tx.__enter__()
        tx.stage_bytes("pages/page-001.png", b"new-one")
        tx.stage_bytes("pages/page-002.png", b"new-two")
        try:
            with mock.patch.object(project_io.os, "replace", side_effect=interrupt_after_first):
                tx.commit()
        except KeyboardInterrupt:
            pass
        finally:
            if tx._lock is not None:
                tx._lock.__exit__(None, None, None)
                tx._lock = None

        project_io.ProjectTransaction.recover(self.project)
        self.assertEqual(b"old-one", (self.project / "pages/page-001.png").read_bytes())
        self.assertEqual(b"old-two", (self.project / "pages/page-002.png").read_bytes())

    def test_rollback_removes_newly_created_targets_without_backup(self):
        real_replace = project_io.os.replace
        staged_calls = 0
        def fail_second_publish(source, destination, **kwargs):
            nonlocal staged_calls
            if Path(source).name.startswith("staged-"):
                staged_calls += 1
                if staged_calls == 2:
                    raise OSError("injected second publish failure")
            return real_replace(source, destination, **kwargs)
        with self.assertRaisesRegex(OSError, "injected second publish failure"):
            with mock.patch.object(project_io.os, "replace", side_effect=fail_second_publish):
                with project_io.ProjectTransaction(self.project, "composition") as transaction:
                    transaction.stage_bytes("pages/page-003.png", b"new-page")
                    transaction.stage_bytes("pages/page-004.png", b"another-new")
                    transaction.commit()
        self.assertFalse((self.project / "pages/page-003.png").exists())
        self.assertFalse((self.project / "pages/page-004.png").exists())
        self.assertEqual(b"old-one", (self.project / "pages/page-001.png").read_bytes())
        self.assertEqual(b"old-two", (self.project / "pages/page-002.png").read_bytes())

    def test_recover_removes_newly_created_targets_after_interrupted_first_composition(self):
        (self.project / "pages/page-001.png").unlink()
        (self.project / "pages/page-002.png").unlink()
        real_replace = project_io.os.replace
        calls = 0
        def interrupt_after_first(source, destination, **kwargs):
            nonlocal calls
            source_path = Path(source)
            if source_path.name.startswith("staged-"):
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt("simulated interruption")
            return real_replace(source, destination, **kwargs)
        tx = project_io.ProjectTransaction(self.project, "first-composition")
        tx.__enter__()
        tx.stage_bytes("pages/page-001.png", b"page-one")
        tx.stage_bytes("pages/page-002.png", b"page-two")
        try:
            with mock.patch.object(project_io.os, "replace", side_effect=interrupt_after_first):
                tx.commit()
        except KeyboardInterrupt:
            pass
        finally:
            if tx._lock is not None:
                tx._lock.__exit__(None, None, None)
                tx._lock = None
        project_io.ProjectTransaction.recover(self.project)
        self.assertFalse((self.project / "pages/page-001.png").exists())
        self.assertFalse((self.project / "pages/page-002.png").exists())

    def test_stage_bytes_rejects_traversal(self):
        with project_io.ProjectTransaction(self.project, "test") as transaction:
            with self.assertRaises(ValueError):
                transaction.stage_bytes("../outside.bin", b"escaped")
            with self.assertRaises(ValueError):
                transaction.stage_bytes("sub/../../../outside.bin", b"escaped")
            with self.assertRaises(ValueError):
                transaction.stage_bytes("C:outside.bin", b"drive-path")
            with self.assertRaises(ValueError):
                transaction.stage_bytes(r"\\server\share\file.bin", b"unc-path")
        self.assertFalse((self.project.parent / "outside.bin").is_file())

    def test_recover_rejects_malicious_journal_paths(self):
        tx_dir = self.project / "logs/transactions/1"
        tx_dir.mkdir(parents=True)
        (tx_dir / "journal.json").write_text(
            '{"operation":"composition","phase":"publishing",'
            '"schema_version":"1.0","targets":['
            '{"path":"../outside.bin","backup":null,'
            '"staged":"logs/transactions/1/staged-001.bin"}]}'
        )
        outside = self.project.parent / "outside.bin"
        outside.write_bytes(b"must-survive")
        with self.assertRaises(ValueError):
            project_io.ProjectTransaction.recover(self.project)
        self.assertEqual(b"must-survive", outside.read_bytes())


if __name__ == "__main__":
    unittest.main()
