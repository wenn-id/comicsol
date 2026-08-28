"""Numbered, rollback-safe SQLite application migrations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from comic_sol_web.database import Database


@dataclass(frozen=True)
class Migration:
    """A single atomic application migration."""

    version: int
    statements: tuple[str, ...]


APPLICATION_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        (
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                login TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE oauth_states (
                state_hash TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER
            )
            """,
            "CREATE INDEX oauth_states_expiry ON oauth_states (expires_at)",
            """
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                csrf_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """,
            "CREATE INDEX sessions_user ON sessions (user_id)",
            "CREATE INDEX sessions_expiry ON sessions (expires_at)",
        ),
    ),
    Migration(
        2,
        (
            """
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                storage_name TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK (byte_size > 0),
                width INTEGER NOT NULL CHECK (width > 0),
                height INTEGER NOT NULL CHECK (height > 0),
                created_at INTEGER NOT NULL
            )
            """,
            "CREATE INDEX assets_owner ON assets (owner_id, asset_id)",
        ),
    ),
    Migration(3, ("ALTER TABLE oauth_states ADD COLUMN binding_hash TEXT",)),
    Migration(
        4,
        (
            """
            CREATE TABLE credentials (
                owner_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                provider TEXT NOT NULL CHECK (
                    length(provider) BETWEEN 1 AND 32
                ),
                auth_mode TEXT NOT NULL CHECK (auth_mode = 'byok'),
                ciphertext TEXT NOT NULL CHECK (
                    length(ciphertext) BETWEEN 1 AND 131072
                ),
                key_id TEXT NOT NULL CHECK (
                    length(key_id) BETWEEN 1 AND 32
                ),
                updated_at INTEGER NOT NULL,
                revoked_at INTEGER,
                PRIMARY KEY (owner_id, provider, auth_mode)
            )
            """,
        ),
    ),
)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    versions = tuple(migration.version for migration in migrations)
    if versions != tuple(range(1, len(versions) + 1)):
        raise ValueError("migration versions must be unique, ordered, and contiguous from 1")
    if any(not migration.statements for migration in migrations):
        raise ValueError("each migration must contain at least one statement")


def apply_migrations(
    database: Database,
    migrations: Sequence[Migration] = APPLICATION_MIGRATIONS,
) -> tuple[int, ...]:
    """Apply pending migrations in order, one rollback-safe transaction each."""
    _validate_migrations(migrations)
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
            """
        )

    applied: list[int] = []
    for migration in migrations:
        # Serialize the presence check and non-idempotent statements.
        with database.transaction() as connection:
            present = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if present is not None:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, CAST(strftime('%s', 'now') AS INTEGER))",
                (migration.version,),
            )
        applied.append(migration.version)
    return tuple(applied)
