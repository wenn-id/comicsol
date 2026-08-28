"""SQLite application-state boundary for Comic Sol Web."""

from __future__ import annotations

import sqlite3
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

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
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
