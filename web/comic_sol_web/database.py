"""SQLite application-state boundary for Comic Sol Web."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Database:
    """Open consistently hardened SQLite connections and atomic transactions."""

    def __init__(self, path: Path | str, *, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        if str(self.path) == ":memory:":
            raise ValueError("Database requires a durable filesystem path")

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Switch the database file to WAL, retrying while a writer holds it.

        Changing the journal mode needs brief exclusive access and SQLite does
        not run the busy handler for it, so ``PRAGMA busy_timeout`` cannot cover
        this one statement. Concurrent workers opening a fresh database would
        otherwise fail to start. WAL is persistent once set, so the retry only
        runs until whichever connection wins finishes its transaction.
        """
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            return
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            except sqlite3.OperationalError:
                mode = None
            if mode is not None and mode.lower() == "wal":
                return
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("could not enable WAL journal mode")
            time.sleep(0.01)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            self._enable_wal(connection)
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Yield a read connection with the same safety pragmas as writers."""
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield an immediate transaction that commits or fully rolls back."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
