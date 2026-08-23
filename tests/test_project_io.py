import json
import os
import errno
import subprocess
import sys
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
            "../outside.png",
            "/tmp/outside.png",
            "C:/outside.png",
            "C:outside.png",
            r"\\server\share\file.png",
            "//server/share/file.png",
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

    def test_rejects_directory_link_to_another_path_inside_project(self):
        target = self.project / "real-panels"
        target.mkdir()
        link = self.project / "panels"
        make_symlink(self, link, target, directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks|reparse"):
            contained_project_path(self.project, "panels/image.png")

    @unittest.skipUnless(
        os.name == "nt", "Windows junction/reparse behavior requires native Windows"
    )
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

    def test_open_path_nofollow_rejects_intermediate_symlink(self):
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "value.txt").write_text("outside", encoding="utf-8")
        link = self.project / "linked-dir"
        make_symlink(self, link, outside, directory=True)
        with self.assertRaises((OSError, ValueError)):
            project_io.open_path_nofollow(link / "value.txt")

    @unittest.skipUnless(sys.platform == "darwin", "macOS alias behavior requires macOS")
    def test_open_path_nofollow_allows_macos_var_alias_only(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp", prefix="comic-sol-") as temporary:
            target = Path(temporary) / "value.txt"
            target.write_bytes(b"var-alias")
            with project_io.open_path_nofollow(target) as stream:
                self.assertEqual(b"var-alias", stream.read())

    @unittest.skipUnless(os.name == "posix", "macOS alias behavior requires POSIX")
    def test_open_path_nofollow_handles_deep_macos_temp_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            nested = Path(temporary) / "output" / "project" / "panels" / "p01-01"
            nested.mkdir(parents=True)
            target = nested / "lettered.png"
            target.write_bytes(b"deep-temp")
            with mock.patch.object(sys, "platform", "darwin"):
                with project_io.open_path_nofollow(target) as stream:
                    self.assertEqual(b"deep-temp", stream.read())

    def test_open_path_nofollow_honors_write_and_readwrite_flags(self):
        target = (self.project / "mode.bin").resolve()
        with project_io.open_path_nofollow(
            target, flags=os.O_WRONLY | os.O_CREAT, mode=0o600
        ) as stream:
            stream.write(b"write")
        with project_io.open_path_nofollow(target, flags=os.O_RDWR) as stream:
            self.assertEqual(b"write", stream.read())
            stream.seek(0)
            stream.write(b"W")

    def test_empty_lock_metadata_reraises_nonretryable_error(self):
        (self.project / ".comic-sol.lock").write_bytes(b"")
        error = OSError(errno.EINVAL, "invalid lock")
        with mock.patch.object(project_io.ProjectLock, "_lock", side_effect=error):
            with self.assertRaises(OSError) as raised:
                with project_io.ProjectLock(self.project, timeout=0.01):
                    pass
        self.assertEqual(errno.EINVAL, raised.exception.errno)


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
            mock.patch.object(project_io.tempfile, "NamedTemporaryFile", return_value=Handle()),
            mock.patch.object(
                project_io.os,
                "fsync",
                side_effect=lambda fd: events.append(("fsync", fd)),
            ),
            mock.patch.object(
                project_io.os,
                "replace",
                side_effect=lambda source, target: events.append(("replace", Path(source), target)),
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
            with mock.patch.object(project_io.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    project_io.durable_atomic_write(destination, b"replacement")
            self.assertEqual(b"original", destination.read_bytes())
            self.assertEqual([destination], list(directory.iterdir()))


class DirectoryPublicationTests(unittest.TestCase):
    def test_atomically_moves_source_to_an_absent_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging"
            destination = root / "project"
            source.mkdir()
            (source / "complete.txt").write_bytes(b"complete")

            project_io.publish_directory_noreplace(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(b"complete", (destination / "complete.txt").read_bytes())

    def test_existing_destinations_are_never_replaced(self):
        for populated in (False, True):
            with self.subTest(populated=populated), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "staging"
                destination = root / "project"
                source.mkdir()
                destination.mkdir()
                (source / "new.txt").write_bytes(b"new")
                if populated:
                    (destination / "original.txt").write_bytes(b"original")

                with self.assertRaises(FileExistsError):
                    project_io.publish_directory_noreplace(source, destination)

                self.assertEqual(b"new", (source / "new.txt").read_bytes())
                self.assertTrue(destination.is_dir())
                if populated:
                    self.assertEqual(
                        b"original",
                        (destination / "original.txt").read_bytes(),
                    )
                else:
                    self.assertEqual([], list(destination.iterdir()))

    def test_publish_failure_leaves_source_and_unrelated_entries_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging"
            destination = root / "project"
            unrelated = root / "unrelated.txt"
            source.mkdir()
            (source / "new.txt").write_bytes(b"new")
            unrelated.write_bytes(b"unrelated")

            with mock.patch.object(
                project_io,
                "_atomic_rename_noreplace",
                side_effect=OSError("injected publication failure"),
            ):
                with self.assertRaisesRegex(OSError, "publication failure"):
                    project_io.publish_directory_noreplace(source, destination)

            self.assertEqual(b"new", (source / "new.txt").read_bytes())
            self.assertFalse(destination.exists())
            self.assertEqual(b"unrelated", unrelated.read_bytes())

    def test_publication_restores_a_substituted_source_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staging"
            displaced_owned = root / "displaced-owned"
            destination = root / "project"
            source.mkdir()
            (source / "complete.txt").write_bytes(b"complete")
            metadata = source.stat(follow_symlinks=False)
            expected_identity = (metadata.st_dev, metadata.st_ino)
            real_rename = project_io._atomic_rename_noreplace
            swapped = False

            def substitute_before_publication(current_source, current_destination):
                nonlocal swapped
                if not swapped and Path(current_source) == source:
                    swapped = True
                    os.rename(source, displaced_owned)
                    source.mkdir()
                    (source / "unrelated.txt").write_bytes(b"unrelated")
                return real_rename(current_source, current_destination)

            with mock.patch.object(
                project_io,
                "_atomic_rename_noreplace",
                side_effect=substitute_before_publication,
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed during publication"):
                    project_io.publish_directory_noreplace(
                        source,
                        destination,
                        expected_identity=expected_identity,
                    )

            self.assertFalse(destination.exists())
            self.assertEqual(b"unrelated", (source / "unrelated.txt").read_bytes())
            self.assertEqual(b"complete", (displaced_owned / "complete.txt").read_bytes())

    def test_fsync_directory_tree_flushes_children_before_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "staging"
            nested = root / "references/characters"
            nested.mkdir(parents=True)
            flushed = []

            with mock.patch.object(
                project_io,
                "fsync_directory",
                side_effect=lambda path: flushed.append(Path(path)),
            ):
                project_io.fsync_directory_tree(root)

            self.assertEqual(root, flushed[-1])
            self.assertEqual({root, root / "references", nested}, set(flushed))

    def test_cleanup_quarantines_owned_tree_before_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".comic-sol-init-owned.tmp"
            staging.mkdir()
            (staging / "partial.txt").write_bytes(b"partial")
            metadata = staging.stat(follow_symlinks=False)

            removed = project_io.cleanup_owned_directory(
                staging,
                (metadata.st_dev, metadata.st_ino),
            )

            self.assertTrue(removed)
            self.assertFalse(staging.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_cleanup_restores_a_substituted_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".comic-sol-init-owned.tmp"
            moved_owned = root / "moved-owned"
            staging.mkdir()
            (staging / "partial.txt").write_bytes(b"partial")
            metadata = staging.stat(follow_symlinks=False)
            real_rename = project_io._atomic_rename_noreplace
            swapped = False

            def substitute_before_quarantine(source, destination):
                nonlocal swapped
                if not swapped and Path(source) == staging:
                    swapped = True
                    os.rename(staging, moved_owned)
                    staging.mkdir()
                    (staging / "unrelated.txt").write_bytes(b"unrelated")
                return real_rename(source, destination)

            with mock.patch.object(
                project_io,
                "_atomic_rename_noreplace",
                side_effect=substitute_before_quarantine,
            ):
                removed = project_io.cleanup_owned_directory(
                    staging,
                    (metadata.st_dev, metadata.st_ino),
                )

            self.assertFalse(removed)
            self.assertEqual(b"unrelated", (staging / "unrelated.txt").read_bytes())
            self.assertEqual(b"partial", (moved_owned / "partial.txt").read_bytes())
            self.assertEqual([], list(root.glob(".comic-sol-cleanup-*.tmp")))

    def test_windows_uses_rename_without_replace_semantics(self):
        source = Path("staging")
        destination = Path("project")
        with (
            mock.patch.object(project_io.os, "name", "nt"),
            mock.patch.object(project_io.os, "rename") as rename,
        ):
            project_io._atomic_rename_noreplace(source, destination)
        rename.assert_called_once_with(source, destination)

    def test_darwin_uses_exclusive_native_rename(self):
        import ctypes

        native_rename = mock.Mock(return_value=0)
        library = mock.Mock(renameatx_np=native_rename)
        source = Path("staging")
        destination = Path("project")
        with (
            mock.patch.object(project_io.sys, "platform", "darwin"),
            mock.patch.object(ctypes, "CDLL", return_value=library),
        ):
            project_io._atomic_rename_noreplace(source, destination)

        native_rename.assert_called_once_with(
            -2,
            os.fsencode(source),
            -2,
            os.fsencode(destination),
            0x4,
        )

    def test_native_rename_errors_and_unsupported_posix_fail_closed(self):
        import ctypes

        native_rename = mock.Mock(return_value=-1)
        library = mock.Mock(renameat2=native_rename)
        with (
            mock.patch.object(project_io.sys, "platform", "linux"),
            mock.patch.object(ctypes, "CDLL", return_value=library),
            mock.patch.object(ctypes, "get_errno", return_value=errno.EIO),
        ):
            with self.assertRaises(OSError) as raised:
                project_io._atomic_rename_noreplace(Path("staging"), Path("project"))
        self.assertEqual(errno.EIO, raised.exception.errno)

        with (
            mock.patch.object(project_io.sys, "platform", "freebsd"),
            mock.patch.object(ctypes, "CDLL", return_value=mock.Mock()),
        ):
            with self.assertRaises(OSError) as unsupported:
                project_io._atomic_rename_noreplace(Path("staging"), Path("project"))
        self.assertEqual(errno.ENOTSUP, unsupported.exception.errno)

    def test_publication_rejects_cross_parent_and_non_directory_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_parent = root / "source-parent"
            destination_parent = root / "destination-parent"
            source_parent.mkdir()
            destination_parent.mkdir()
            source = source_parent / "staging"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "share one parent"):
                project_io.publish_directory_noreplace(
                    source,
                    destination_parent / "project",
                )

            regular_file = source_parent / "staging-file"
            regular_file.write_bytes(b"not a directory")
            with self.assertRaisesRegex(ValueError, "must be a directory"):
                project_io.publish_directory_noreplace(
                    regular_file,
                    source_parent / "project",
                )


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
            with mock.patch.object(
                project_io.os, "replace", side_effect=fail_second_staged_replace
            ):
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

    def test_recover_restores_target_replaced_before_a_crash(self):
        # A write entry carries no "operation" key; recovery must still roll
        # it back. Regression test: an over-strict journal shape check once
        # skipped these journals entirely, leaking staged bytes into the
        # project after an interrupted publish.
        tx_dir = self.project / "logs/transactions/1"
        tx_dir.mkdir(parents=True)
        (tx_dir / "backup-001-artifact.json").write_bytes(b"original")
        (tx_dir / "staged-001-artifact.json").write_bytes(b"new")
        (self.project / "artifact.json").write_bytes(b"new")
        (tx_dir / "journal.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "operation": "composition",
                    "phase": "publishing",
                    "targets": [
                        {
                            "path": "artifact.json",
                            "backup": "logs/transactions/1/backup-001-artifact.json",
                            "staged": "logs/transactions/1/staged-001-artifact.json",
                        }
                    ],
                }
            )
        )
        project_io.ProjectTransaction.recover(self.project)
        self.assertEqual(b"original", (self.project / "artifact.json").read_bytes())

    def test_recover_skips_journal_with_unknown_entry_operation(self):
        tx_dir = self.project / "logs/transactions/1"
        tx_dir.mkdir(parents=True)
        (self.project / "artifact.json").write_bytes(b"untouched")
        (tx_dir / "journal.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "phase": "publishing",
                    "targets": [{"path": "artifact.json", "operation": "explode"}],
                }
            )
        )
        project_io.ProjectTransaction.recover(self.project)
        self.assertEqual(b"untouched", (self.project / "artifact.json").read_bytes())

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

    def test_append_bytes_rolls_back_after_a_later_publish_failure(self):
        event_path = self.project / "logs/events.jsonl"
        event_path.write_bytes(b'{"event":"before"}\n')
        before = event_path.read_bytes()
        real_replace = project_io.os.replace

        def fail_staged_replace(source, destination, **kwargs):
            if Path(source).name.startswith("staged-"):
                raise OSError("injected replacement failure")
            return real_replace(source, destination, **kwargs)

        with self.assertRaisesRegex(OSError, "injected replacement failure"):
            with mock.patch.object(project_io.os, "replace", side_effect=fail_staged_replace):
                with project_io.ProjectTransaction(self.project, "append") as transaction:
                    transaction.append_bytes("logs/events.jsonl", b'{"event":"after"}\n')
                    transaction.stage_bytes("pages/page-001.png", b"new-one")

        self.assertEqual(before, event_path.read_bytes())

    def test_recover_restores_interrupted_append_with_torn_tail(self):
        event_path = self.project / "logs/events.jsonl"
        before = b'{"event":"before"}\n{"event":"torn"'
        event_path.write_bytes(before)
        transaction = project_io.ProjectTransaction(self.project, "append")
        transaction.__enter__()
        transaction.append_bytes(
            "logs/events.jsonl", b'{"event":"after"}\n', repair_torn_jsonl=True
        )
        transaction._phase = "publishing"
        transaction._write_journal()
        with project_io.open_contained(
            self.project,
            "logs/events.jsonl",
            flags=os.O_RDWR,
        ) as handle:
            handle.seek(transaction._journal[0]["repair_size"])
            handle.truncate()
            handle.write(b'{"event":"after"}\n')
            handle.flush()
            os.fsync(handle.fileno())
        transaction._lock.__exit__(None, None, None)
        transaction._lock = None

        project_io.ProjectTransaction.recover(self.project)

        self.assertEqual(before, event_path.read_bytes())

    def test_append_repairs_large_torn_tail_without_reordering_chunks(self):
        event_path = self.project / "logs/events.jsonl"
        valid = b'{"event":"before"}\n'
        torn = b"x" * (64 * 1024 + 17)
        event_path.write_bytes(valid + torn)

        with project_io.ProjectTransaction(self.project, "append") as transaction:
            transaction.append_bytes(
                "logs/events.jsonl",
                b'{"event":"after"}\n',
                repair_torn_jsonl=True,
            )

        self.assertEqual(valid + b'{"event":"after"}\n', event_path.read_bytes())

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
