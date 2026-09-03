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

# WP3 originally declared version 5 beside its sole engine boundary. WP5 must
# be usable from the same database without importing that engine-owning module,
# so this bridge repeats the accepted version-5 SQL exactly. On an ordinary app
# path version 5 is already recorded and is skipped; on isolated queue tests it
# establishes the same prerequisite schema.
_GENERATION_PROJECT_BRIDGE = Migration(
    5,
    (
        """
        CREATE TABLE web_projects (
            project_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            storage_name TEXT NOT NULL UNIQUE,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            engine_state TEXT NOT NULL
        )
        """,
        "CREATE INDEX web_projects_owner ON web_projects (owner_id, project_id)",
    ),
)

# WP3 also owns the version-6 project-creation idempotency table. Repeat its
# accepted SQL so a queue-only database has the same contiguous prerequisite
# chain, while an EngineGateway-initialized database skips the recorded version
# and continues with generation at version 7.
_GENERATION_PROJECT_IDEMPOTENCY_BRIDGE = Migration(
    6,
    (
        """
        CREATE TABLE web_project_creations (
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES web_projects(project_id) ON DELETE CASCADE,
            PRIMARY KEY (owner_id, idempotency_key),
            UNIQUE (project_id)
        )
        """,
    ),
)

# Version 7 must be safe to apply to a database initialized by the parent
# commit's GENERATION_MIGRATIONS, which already recorded v6 and created
# `generation_jobs` and the related append-only tables. To keep that upgrade
# path open without altering the historical version-6 migration, every DDL
# statement here uses `IF NOT EXISTS` for tables and indexes and a
# `DROP TRIGGER IF EXISTS` guard for triggers. A fresh EngineGateway path that
# never saw v6 still lands at the same final schema.
_GENERATION_SCHEMA_MIGRATION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS generation_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
            request_json TEXT NOT NULL CHECK (length(request_json) <= 1048576),
            provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
            model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
            auth_mode TEXT NOT NULL CHECK (auth_mode IN ('agent', 'hosted', 'byok')),
            state TEXT NOT NULL CHECK (state IN (
                'queued', 'running', 'polling', 'validating', 'accepted',
                'awaiting_provider_confirmation', 'paused', 'failed', 'cancelled'
            )),
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
            max_retries INTEGER NOT NULL CHECK (max_retries BETWEEN 0 AND 10),
            external_job_id TEXT,
            lease_token TEXT,
            lease_owner TEXT,
            lease_expires_at INTEGER,
            staged_raster_name TEXT,
            result_checksum TEXT,
            accepted_project_revision INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
    "CREATE INDEX IF NOT EXISTS generation_jobs_owner ON generation_jobs (owner_id, job_id)",
    "CREATE INDEX IF NOT EXISTS generation_jobs_lease ON generation_jobs "
    "(state, lease_expires_at, created_at, job_id)",
    """
        CREATE TABLE IF NOT EXISTS generation_attempts (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES generation_jobs(job_id) ON DELETE RESTRICT,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            auth_mode TEXT NOT NULL,
            state TEXT NOT NULL,
            error_category TEXT,
            external_job_id TEXT,
            result_checksum TEXT,
            created_at INTEGER NOT NULL
        )
        """,
    "CREATE INDEX IF NOT EXISTS generation_attempts_job ON generation_attempts (job_id, event_id)",
    """
        CREATE TABLE IF NOT EXISTS generation_receipts (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES generation_jobs(job_id) ON DELETE RESTRICT,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            auth_mode TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            checksum TEXT NOT NULL CHECK (length(checksum) = 64),
            created_at INTEGER NOT NULL,
            UNIQUE (job_id, attempt_number, checksum)
        )
        """,
    "CREATE INDEX IF NOT EXISTS generation_receipts_job "
    "ON generation_receipts (job_id, receipt_id)",
    "DROP TRIGGER IF EXISTS generation_attempts_no_update",
    """
        CREATE TRIGGER generation_attempts_no_update
        BEFORE UPDATE ON generation_attempts
        BEGIN
            SELECT RAISE(ABORT, 'generation attempts are append-only');
        END
        """,
    "DROP TRIGGER IF EXISTS generation_attempts_no_delete",
    """
        CREATE TRIGGER generation_attempts_no_delete
        BEFORE DELETE ON generation_attempts
        BEGIN
            SELECT RAISE(ABORT, 'generation attempts are append-only');
        END
        """,
    "DROP TRIGGER IF EXISTS generation_receipts_no_update",
    """
        CREATE TRIGGER generation_receipts_no_update
        BEFORE UPDATE ON generation_receipts
        BEGIN
            SELECT RAISE(ABORT, 'generation receipts are append-only');
        END
        """,
    "DROP TRIGGER IF EXISTS generation_receipts_no_delete",
    """
        CREATE TRIGGER generation_receipts_no_delete
        BEFORE DELETE ON generation_receipts
        BEGIN
            SELECT RAISE(ABORT, 'generation receipts are append-only');
        END
        """,
)

# Databases created before the numbering fix recorded the generation schema as
# version 6 and never applied the version-6 project-idempotency bridge, so they
# also lack `web_project_creations`. This bridge creates that table only when
# absent; on a WP3/EngineGateway database version 6 is already recorded and the
# whole migration is skipped.
_GENERATION_PROJECT_IDEMPOTENCY_BRIDGE_SQL = """
        CREATE TABLE IF NOT EXISTS web_project_creations (
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES web_projects(project_id) ON DELETE CASCADE,
            PRIMARY KEY (owner_id, idempotency_key),
            UNIQUE (project_id)
        )
        """

GENERATION_SCHEMA_MIGRATION = Migration(
    7,
    (_GENERATION_PROJECT_IDEMPOTENCY_BRIDGE_SQL, *_GENERATION_SCHEMA_MIGRATION_DDL),
)

PROVIDER_SWITCH_PROPOSAL_MIGRATION = Migration(
    8,
    (
        """
        CREATE TABLE provider_switch_proposals (
            proposal_id TEXT PRIMARY KEY CHECK (length(proposal_id) BETWEEN 32 AND 128),
            idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) = 36),
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
            job_ids_json TEXT NOT NULL CHECK (length(job_ids_json) BETWEEN 4 AND 1048576),
            from_provider TEXT NOT NULL CHECK (length(from_provider) BETWEEN 1 AND 64),
            from_model TEXT NOT NULL CHECK (length(from_model) BETWEEN 1 AND 128),
            to_provider TEXT NOT NULL CHECK (length(to_provider) BETWEEN 1 AND 64),
            to_model TEXT NOT NULL CHECK (length(to_model) BETWEEN 1 AND 128),
            to_auth_mode TEXT NOT NULL CHECK (to_auth_mode IN ('agent', 'hosted', 'byok')),
            reason TEXT NOT NULL CHECK (reason IN (
                'invalid_credentials', 'quota_exhausted', 'rate_limited', 'moderated',
                'capability_missing', 'timeout', 'cancelled', 'unavailable',
                'invalid_output', 'provider_error'
            )),
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE (owner_id, project_id, idempotency_key)
        )
        """,
        "CREATE INDEX provider_switch_proposals_owner "
        "ON provider_switch_proposals (owner_id, proposal_id)",
        """
        CREATE TABLE provider_switch_decisions (
            proposal_id TEXT PRIMARY KEY REFERENCES provider_switch_proposals(proposal_id)
                ON DELETE RESTRICT,
            decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'expired')),
            idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) = 36),
            decided_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TRIGGER provider_switch_proposals_no_update
        BEFORE UPDATE ON provider_switch_proposals
        BEGIN
            SELECT RAISE(ABORT, 'provider switch proposals are immutable');
        END
        """,
        """
        CREATE TRIGGER provider_switch_proposals_no_delete
        BEFORE DELETE ON provider_switch_proposals
        BEGIN
            SELECT RAISE(ABORT, 'provider switch proposals are immutable');
        END
        """,
        """
        CREATE TRIGGER provider_switch_decisions_no_update
        BEFORE UPDATE ON provider_switch_decisions
        BEGIN
            SELECT RAISE(ABORT, 'provider switch decisions are immutable');
        END
        """,
        """
        CREATE TRIGGER provider_switch_decisions_no_delete
        BEFORE DELETE ON provider_switch_decisions
        BEGIN
            SELECT RAISE(ABORT, 'provider switch decisions are immutable');
        END
        """,
    ),
)

GENERATION_MIGRATIONS = (
    *APPLICATION_MIGRATIONS,
    _GENERATION_PROJECT_BRIDGE,
    _GENERATION_PROJECT_IDEMPOTENCY_BRIDGE,
    GENERATION_SCHEMA_MIGRATION,
)

APPROVAL_MIGRATIONS = (*GENERATION_MIGRATIONS, PROVIDER_SWITCH_PROPOSAL_MIGRATION)

PLANNING_MIGRATION = Migration(
    9,
    (
        """
        CREATE TABLE planning_jobs (
            job_id TEXT PRIMARY KEY CHECK (length(job_id) = 36),
            idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) = 36),
            owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 128),
            project_id TEXT NOT NULL REFERENCES web_projects(project_id),
            project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
            provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
            model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
            state TEXT NOT NULL CHECK (state IN (
                'queued', 'running', 'repairing', 'ready_for_review', 'failed', 'cancelled'
            )),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
            usage_json TEXT NOT NULL DEFAULT '{}' CHECK (length(usage_json) <= 4096),
            error_category TEXT CHECK (error_category IN (
                'invalid_credentials', 'quota_exhausted', 'rate_limited', 'moderated',
                'capability_missing', 'timeout', 'cancelled', 'unavailable',
                'invalid_output', 'provider_error', 'stale_revision'
            )),
            lease_token TEXT CHECK (length(lease_token) = 36),
            lease_owner TEXT CHECK (length(lease_owner) BETWEEN 1 AND 128),
            lease_expires_at INTEGER,
            publication_sha256 TEXT CHECK (length(publication_sha256) = 64),
            published_revision INTEGER CHECK (published_revision >= 1),
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            updated_at INTEGER NOT NULL,
            UNIQUE (owner_id, idempotency_key)
        )
        """,
        "CREATE INDEX planning_jobs_owner ON planning_jobs (owner_id, project_id, job_id)",
        "CREATE INDEX planning_jobs_lease ON planning_jobs (state, lease_expires_at, created_at)",
        "CREATE UNIQUE INDEX planning_jobs_active ON planning_jobs (project_id) "
        "WHERE state IN ('queued', 'running', 'repairing')",
    ),
)

PLANNING_MIGRATIONS = (*APPROVAL_MIGRATIONS, PLANNING_MIGRATION)


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
