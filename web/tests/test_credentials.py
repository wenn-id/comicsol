from __future__ import annotations

import dataclasses
import hashlib
import io
import logging
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.database import Database
from comic_sol_web.generation.providers.base import ProviderError
from comic_sol_web.generation.types import AuthMode, ErrorCategory
from comic_sol_web.migrations import apply_migrations

CANARY = "credential-canary-" + hashlib.sha256(b"wp6-runtime-fixture").hexdigest()
KEY_ONE = "credential-master-key-one-000000000000000000000000"
KEY_TWO = "credential-master-key-two-000000000000000000000000"


class MutableClock:
    def __init__(self, value: int = 1_800_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class ForbiddenEnvironment(Mapping[str, str]):
    """Mapping that proves agent mode does not inspect host credentials."""

    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"agent mode inspected deployment environment: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("agent mode iterated deployment environment")

    def __len__(self) -> int:
        raise AssertionError("agent mode measured deployment environment")


class CredentialBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = Database(self.root / "web.sqlite3")
        apply_migrations(self.database)
        with self.database.transaction() as connection:
            connection.executemany(
                "INSERT INTO users (user_id, login, updated_at) VALUES (?, ?, ?)",
                (("owner-a", "alice", 1), ("owner-b", "bob", 1)),
            )
        self.clock = MutableClock()

    def make_broker(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        hosted: Mapping[str, str] | None = None,
        key_references: Mapping[str, str] | None = None,
        active_key_id: str | None = None,
        connection_tester=None,
    ):
        from comic_sol_web.generation.credentials import CredentialBroker

        return CredentialBroker(
            self.database,
            deployment_environment={} if environment is None else environment,
            hosted_secret_references={} if hosted is None else hosted,
            master_key_references={} if key_references is None else key_references,
            active_key_id=active_key_id,
            clock=self.clock,
            session_ttl_seconds=60,
            connection_tester=connection_tester,
        )

    async def test_hosted_lookup_uses_only_declared_deployment_secret(self) -> None:
        broker = self.make_broker(
            environment={"DEPLOYMENT_BFL_KEY": CANARY, "UNDECLARED_KEY": "must-not-win"},
            hosted={"bfl": "DEPLOYMENT_BFL_KEY"},
        )
        async with broker.resolve("owner-a", "bfl", AuthMode.HOSTED) as credential:
            self.assertEqual(CANARY, credential)
        self.assertNotIn(CANARY, repr(vars(broker)))
        with self.database.read() as connection:
            rows = connection.execute("SELECT * FROM credentials").fetchall()
        self.assertEqual([], rows)

    async def test_missing_hosted_secret_fails_with_sanitized_typed_error(self) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        broker = self.make_broker(
            environment={},
            hosted={"bfl": "DEPLOYMENT_BFL_KEY"},
        )
        with self.assertRaises(CredentialUnavailableError) as caught:
            async with broker.resolve("owner-a", "bfl", AuthMode.HOSTED):
                self.fail("missing hosted secret resolved")
        rendered = f"{caught.exception!s} {caught.exception!r}"
        self.assertNotIn("DEPLOYMENT_BFL_KEY", rendered)
        self.assertNotIn("bfl", rendered)

    async def test_agent_mode_returns_none_without_inspecting_host_tokens_or_cookies(self) -> None:
        broker = self.make_broker(environment=ForbiddenEnvironment())
        async with broker.resolve("owner-a", "bfl", AuthMode.AGENT) as credential:
            self.assertIsNone(credential)
        rendered = repr(broker).lower()
        for forbidden in ("chatgpt", "browser", "codex", "claude", "antigravity", "zcode"):
            self.assertNotIn(forbidden, rendered)

    async def test_session_byok_is_bounded_and_expires_under_injected_clock(self) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        broker = self.make_broker()
        broker.store_session("owner-a", "bfl", CANARY, ttl_seconds=10)
        self.assertNotIn(CANARY, repr(broker))
        async with broker.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)
        self.clock.value += 10
        with self.assertRaises(CredentialUnavailableError):
            async with broker.resolve("owner-a", "bfl", AuthMode.BYOK):
                self.fail("expired session credential resolved")
        self.assertNotIn(CANARY, repr(broker))

    async def test_session_and_persisted_credentials_are_owner_isolated(self) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        session = self.make_broker()
        session.store_session("owner-a", "bfl", CANARY)
        with self.assertRaises(CredentialUnavailableError):
            async with session.resolve("owner-b", "bfl", AuthMode.BYOK):
                self.fail("cross-owner session credential resolved")

        persisted = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        persisted.store_encrypted("owner-a", "bfl", CANARY)
        with self.assertRaises(CredentialUnavailableError) as caught:
            async with persisted.resolve("owner-b", "bfl", AuthMode.BYOK):
                self.fail("cross-owner persisted credential resolved")
        self.assertNotIn("owner-a", str(caught.exception))
        self.assertNotIn("exists", str(caught.exception).lower())

    async def test_encrypted_persistence_stores_ciphertext_and_key_id_without_plaintext(
        self,
    ) -> None:
        broker = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        broker.store_encrypted("owner-a", "bfl", CANARY)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT owner_id, provider, auth_mode, ciphertext, key_id, revoked_at "
                "FROM credentials"
            ).fetchone()
        self.assertEqual(("owner-a", "bfl", "byok"), tuple(row[:3]))
        self.assertEqual("k1", row[4])
        self.assertIsNone(row[5])
        self.assertNotIn(CANARY, row[3])
        self.assertNotIn(KEY_ONE, row[3])
        self.assertNotIn(CANARY.encode(), self.database.path.read_bytes())
        self.assertNotIn(KEY_ONE.encode(), self.database.path.read_bytes())
        async with broker.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)
        self.assertNotIn(CANARY, repr(broker))

    async def test_key_id_rotation_rewrites_ciphertext_atomically_on_resolve(self) -> None:
        original = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        original.store_encrypted("owner-a", "bfl", CANARY)
        with self.database.read() as connection:
            before = connection.execute(
                "SELECT ciphertext, key_id FROM credentials WHERE owner_id = ?", ("owner-a",)
            ).fetchone()

        rotated = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE, "MASTER_TWO": KEY_TWO},
            key_references={"k1": "MASTER_ONE", "k2": "MASTER_TWO"},
            active_key_id="k2",
        )
        async with rotated.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)
        with self.database.read() as connection:
            after = connection.execute(
                "SELECT ciphertext, key_id FROM credentials WHERE owner_id = ?", ("owner-a",)
            ).fetchone()
        self.assertEqual("k1", before[1])
        self.assertEqual("k2", after[1])
        self.assertNotEqual(before[0], after[0])
        self.assertNotIn(CANARY, after[0])

        new_key_only = self.make_broker(
            environment={"MASTER_TWO": KEY_TWO},
            key_references={"k2": "MASTER_TWO"},
            active_key_id="k2",
        )
        async with new_key_only.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)

    async def test_old_ciphertext_requires_its_authorized_rotation_key(self) -> None:
        from comic_sol_web.generation.credentials import CredentialKeyUnavailableError

        original = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        original.store_encrypted("owner-a", "bfl", CANARY)
        missing_old_key = self.make_broker(
            environment={"MASTER_TWO": KEY_TWO},
            key_references={"k2": "MASTER_TWO"},
            active_key_id="k2",
        )
        with self.assertRaises(CredentialKeyUnavailableError) as caught:
            async with missing_old_key.resolve("owner-a", "bfl", AuthMode.BYOK):
                self.fail("ciphertext resolved without its key")
        rendered = f"{caught.exception!s} {caught.exception!r}"
        for forbidden in (CANARY, KEY_ONE, "k1", "MASTER_ONE"):
            self.assertNotIn(forbidden, rendered)

    async def test_rotation_failure_rolls_back_ciphertext_and_key_id(self) -> None:
        from comic_sol_web.generation.credentials import CredentialStorageError

        original = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        original.store_encrypted("owner-a", "bfl", CANARY)
        with self.database.read() as connection:
            before = tuple(
                connection.execute(
                    "SELECT ciphertext, key_id FROM credentials WHERE owner_id = ?", ("owner-a",)
                ).fetchone()
            )
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_credential_rotation BEFORE UPDATE OF ciphertext ON credentials "
                "BEGIN SELECT RAISE(ABORT, 'database payload must not escape'); END"
            )
        rotated = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE, "MASTER_TWO": KEY_TWO},
            key_references={"k1": "MASTER_ONE", "k2": "MASTER_TWO"},
            active_key_id="k2",
        )
        with self.assertRaises(CredentialStorageError) as caught:
            async with rotated.resolve("owner-a", "bfl", AuthMode.BYOK):
                self.fail("failed rotation yielded plaintext")
        self.assertNotIn("database payload", str(caught.exception))
        traceback_locals = []
        traceback = caught.exception.__traceback__
        while traceback is not None:
            traceback_locals.append(repr(traceback.tb_frame.f_locals))
            traceback = traceback.tb_next
        self.assertNotIn(CANARY, "".join(traceback_locals))
        with self.database.read() as connection:
            after = tuple(
                connection.execute(
                    "SELECT ciphertext, key_id FROM credentials WHERE owner_id = ?", ("owner-a",)
                ).fetchone()
            )
        self.assertEqual(before, after)
        with self.database.transaction() as connection:
            connection.execute("DROP TRIGGER reject_credential_rotation")
        async with original.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)

    async def test_revocation_is_owner_bound_and_immediately_effective(self) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        broker = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        broker.store_session("owner-a", "bfl", "session-" + CANARY)
        broker.store_encrypted("owner-a", "bfl", CANARY)
        broker.revoke("owner-a", "bfl")
        with self.assertRaises(CredentialUnavailableError):
            async with broker.resolve("owner-a", "bfl", AuthMode.BYOK):
                self.fail("revoked credential resolved")
        with self.database.read() as connection:
            revoked_at = connection.execute(
                "SELECT revoked_at FROM credentials WHERE owner_id = ?", ("owner-a",)
            ).fetchone()[0]
        self.assertEqual(self.clock.value, revoked_at)

    async def test_wrong_owner_revocation_fails_closed_without_changing_owner_row(self) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        broker = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        broker.store_encrypted("owner-a", "bfl", CANARY)
        with self.assertRaises(CredentialUnavailableError) as caught:
            broker.revoke("owner-b", "bfl")
        self.assertNotIn("owner-a", str(caught.exception))
        async with broker.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
            self.assertEqual(CANARY, credential)

    async def test_connection_test_returns_only_normalized_success_or_error_categories(
        self,
    ) -> None:
        from comic_sol_web.generation.credentials import ConnectionTestResult

        seen: list[tuple[str, str | None]] = []

        async def succeeds(provider: str, credential: str | None) -> None:
            seen.append((provider, credential))

        success = self.make_broker(connection_tester=succeeds)
        success.store_session("owner-a", "bfl", CANARY)
        result = await success.test_connection("owner-a", "bfl", AuthMode.BYOK)
        self.assertEqual(ConnectionTestResult(ok=True, category=None), result)
        self.assertEqual([("bfl", CANARY)], seen)
        self.assertNotIn(CANARY, repr(result))

        async def normalized_failure(provider: str, credential: str | None) -> None:
            raise ProviderError(ErrorCategory.INVALID_CREDENTIALS, status_code=401)

        failure = self.make_broker(connection_tester=normalized_failure)
        failure.store_session("owner-a", "bfl", CANARY)
        result = await failure.test_connection("owner-a", "bfl", AuthMode.BYOK)
        self.assertEqual(
            ConnectionTestResult(ok=False, category=ErrorCategory.INVALID_CREDENTIALS), result
        )
        self.assertEqual(
            {"ok": False, "category": ErrorCategory.INVALID_CREDENTIALS},
            dataclasses.asdict(result),
        )

        async def raw_failure(provider: str, credential: str | None) -> None:
            raise RuntimeError(f"raw body endpoint?token={CANARY} Authorization: Bearer {CANARY}")

        unknown = self.make_broker(connection_tester=raw_failure)
        unknown.store_session("owner-a", "bfl", CANARY)
        result = await unknown.test_connection("owner-a", "bfl", AuthMode.BYOK)
        self.assertEqual(
            ConnectionTestResult(ok=False, category=ErrorCategory.PROVIDER_ERROR), result
        )
        self.assertNotIn(CANARY, repr(result))

    async def test_plaintext_never_appears_in_logs_exceptions_repr_files_archives_or_results(
        self,
    ) -> None:
        from comic_sol_web.generation.credentials import CredentialUnavailableError

        broker = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)
        with redirect_stdout(captured), redirect_stderr(captured):
            broker.store_encrypted("owner-a", "bfl", CANARY)
            async with broker.resolve("owner-a", "bfl", AuthMode.BYOK) as credential:
                self.assertEqual(CANARY, credential)
            with self.assertRaises(CredentialUnavailableError) as caught:
                async with broker.resolve("owner-b", "bfl", AuthMode.BYOK):
                    self.fail("wrong owner resolved")
        api_shaped = {"ok": False, "error": str(caught.exception)}
        self.assertNotIn(CANARY, repr(broker))
        self.assertNotIn(CANARY, repr(vars(broker)))
        self.assertNotIn(KEY_ONE, repr(vars(broker)))
        self.assertNotIn(CANARY, repr(caught.exception))
        self.assertNotIn(CANARY, repr(api_shaped))
        self.assertNotIn(CANARY, captured.getvalue())

        receipt = self.root / "receipt.json"
        receipt.write_text('{"category":"invalid_credentials"}', encoding="utf-8")
        archive = self.root / "project.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("project.json", '{"schema_version":"2.0"}')
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), path.as_posix())
                self.assertNotIn(KEY_ONE.encode(), path.read_bytes(), path.as_posix())

    async def test_provider_user_and_auth_mode_are_validated_before_decryption(self) -> None:
        from comic_sol_web.generation.credentials import (
            InvalidCredentialOwnerError,
            UnknownAuthModeError,
            UnknownProviderError,
        )

        broker = self.make_broker(
            environment={"MASTER_ONE": KEY_ONE},
            key_references={"k1": "MASTER_ONE"},
            active_key_id="k1",
        )
        broker.store_encrypted("owner-a", "bfl", CANARY)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE credentials SET ciphertext = ? WHERE owner_id = ?",
                ("not-valid-ciphertext", "owner-a"),
            )
        cases = (
            (UnknownProviderError, ("owner-a", "not-a-provider", AuthMode.BYOK)),
            (UnknownAuthModeError, ("owner-a", "bfl", "not-an-auth-mode")),
            (InvalidCredentialOwnerError, ("../owner", "bfl", AuthMode.BYOK)),
        )
        for error_type, arguments in cases:
            with self.subTest(error=error_type.__name__), self.assertRaises(error_type) as caught:
                async with broker.resolve(*arguments):
                    self.fail("invalid metadata reached decryption")
            self.assertNotIn("cipher", str(caught.exception).lower())
            self.assertNotIn(arguments[0], str(caught.exception))


class CredentialMigrationConcurrencyTests(unittest.TestCase):
    def test_concurrent_initialization_applies_next_credential_migration_once(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database = Database(Path(temporary_directory.name) / "web.sqlite3")
        worker_count = 6
        barrier = threading.Barrier(worker_count)
        results: list[tuple[int, ...]] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def initialize() -> None:
            try:
                barrier.wait()
                applied = apply_migrations(database)
            except BaseException as error:  # noqa: BLE001 - asserted below
                with lock:
                    failures.append(error)
            else:
                with lock:
                    results.append(applied)

        workers = [threading.Thread(target=initialize) for _ in range(worker_count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([], failures)
        self.assertEqual([1, 2, 3, 4], sorted(version for result in results for version in result))
        with database.read() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(credentials)"))
        self.assertEqual((1, 2, 3, 4), versions)
        self.assertEqual(
            (
                "owner_id",
                "provider",
                "auth_mode",
                "ciphertext",
                "key_id",
                "updated_at",
                "revoked_at",
            ),
            columns,
        )
        self.assertEqual((), apply_migrations(database))

    def test_credential_migration_rolls_back_cleanly(self) -> None:
        from comic_sol_web.migrations import APPLICATION_MIGRATIONS, Migration

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database = Database(Path(temporary_directory.name) / "rollback.sqlite3")
        broken = APPLICATION_MIGRATIONS[:3] + (
            Migration(
                4,
                (
                    APPLICATION_MIGRATIONS[3].statements[0],
                    "INSERT INTO missing_table VALUES (1)",
                ),
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(database, broken)
        with database.read() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        self.assertNotIn("credentials", tables)
        self.assertEqual((1, 2, 3), versions)


class CredentialConfigurationTests(unittest.TestCase):
    def test_config_declares_valid_secret_references_without_retaining_values_in_repr(self) -> None:
        from comic_sol_web.config import WebConfig

        environment = _support.valid_environment()
        environment.update(
            {
                "COMIC_SOL_WEB_HOSTED_SECRET_REFS": "bfl=DEPLOYMENT_BFL_KEY",
                "COMIC_SOL_WEB_CREDENTIAL_KEY_REFS": "k1=DEPLOYMENT_MASTER_ONE,k2=DEPLOYMENT_MASTER_TWO",
                "COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID": "k2",
                "DEPLOYMENT_BFL_KEY": CANARY,
                "DEPLOYMENT_MASTER_ONE": KEY_ONE,
                "DEPLOYMENT_MASTER_TWO": KEY_TWO,
            }
        )
        config = WebConfig.from_env(environment)
        self.assertEqual({"bfl": "DEPLOYMENT_BFL_KEY"}, config.hosted_secret_references)
        self.assertEqual(
            {"k1": "DEPLOYMENT_MASTER_ONE", "k2": "DEPLOYMENT_MASTER_TWO"},
            config.master_key_references,
        )
        self.assertEqual("k2", config.active_credential_key_id)
        rendered = repr(config)
        for forbidden in (CANARY, KEY_ONE, KEY_TWO):
            self.assertNotIn(forbidden, rendered)

    def test_config_rejects_invalid_identifiers_and_references_without_printing_values(
        self,
    ) -> None:
        from comic_sol_web.config import WebConfig

        cases = (
            ("COMIC_SOL_WEB_HOSTED_SECRET_REFS", "../bfl=SECRET_REF"),
            ("COMIC_SOL_WEB_HOSTED_SECRET_REFS", "bfl=secret-value-not-a-reference"),
            ("COMIC_SOL_WEB_CREDENTIAL_KEY_REFS", "key:bad=MASTER_REF"),
            ("COMIC_SOL_WEB_CREDENTIAL_ACTIVE_KEY_ID", "missing-key"),
        )
        for variable, value in cases:
            environment = _support.valid_environment()
            environment[variable] = value
            with self.subTest(variable=variable):
                with self.assertRaises(ValueError) as caught:
                    WebConfig.from_env(environment)
                self.assertNotIn(value, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
