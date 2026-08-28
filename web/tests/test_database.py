from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from comic_sol_web.database import Database
from comic_sol_web.migrations import Migration, apply_migrations


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "web.sqlite3"
        self.database = Database(self.database_path)

    def test_application_migrations_are_numbered_and_applied_in_order(self) -> None:
        applied = apply_migrations(self.database)
        self.assertEqual((1, 2, 3), applied)
        with self.database.read() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertEqual((1, 2, 3), versions)
        self.assertTrue({"oauth_states", "sessions", "assets"}.issubset(tables))
        self.assertEqual((), apply_migrations(self.database))

    def test_migration_failure_rolls_back_all_statements_and_version(self) -> None:
        migrations = (
            Migration(1, ("CREATE TABLE first_table (id INTEGER PRIMARY KEY)",)),
            Migration(
                2,
                (
                    "CREATE TABLE must_be_rolled_back (id INTEGER PRIMARY KEY)",
                    "INSERT INTO table_that_does_not_exist VALUES (1)",
                ),
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(self.database, migrations)
        with self.database.read() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        self.assertIn("first_table", tables)
        self.assertNotIn("must_be_rolled_back", tables)
        self.assertEqual((1,), versions)

    def test_migrations_reject_duplicate_or_non_contiguous_numbers(self) -> None:
        invalid_sets = (
            (Migration(1, ("SELECT 1",)), Migration(1, ("SELECT 1",))),
            (Migration(1, ("SELECT 1",)), Migration(3, ("SELECT 1",))),
            (Migration(2, ("SELECT 1",)),),
        )
        for migrations in invalid_sets:
            with self.subTest(migrations=migrations), self.assertRaises(ValueError):
                apply_migrations(self.database, migrations)

    def test_concurrent_migration_workers_apply_once(self) -> None:
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def migrate():
            try:
                barrier.wait()
                results.append(apply_migrations(self.database))
            except BaseException as error:
                failures.append(error)

        workers = [threading.Thread(target=migrate) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([], failures)
        self.assertEqual([(1, 2, 3), ()], sorted(results, reverse=True))

    def test_transaction_rolls_back_on_exception(self) -> None:
        apply_migrations(self.database)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO oauth_states (state_hash, expires_at) VALUES (?, ?)",
                    ("rolled-back", 100),
                )
                raise RuntimeError("abort")
        with self.database.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM oauth_states WHERE state_hash = ?", ("rolled-back",)
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_every_connection_enforces_wal_and_foreign_keys(self) -> None:
        apply_migrations(self.database)
        with self.database.read() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
        with self.database.transaction() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO assets (asset_id, owner_id, storage_name, media_type, byte_size, "
                    "width, height, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("asset", "missing-user", "asset.png", "image/png", 1, 1, 1, 1),
                )


if __name__ == "__main__":
    unittest.main()
