"""Owner-isolated hosted, session-only, and encrypted BYOK credentials."""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import AsyncContextManager, Protocol

from comic_sol_web.database import Database
from comic_sol_web.security import CredentialCipher, MAX_CREDENTIAL_BYTES

from .catalog import CATALOG
from .providers.base import ProviderError
from .types import AuthMode, ErrorCategory

MAX_SESSION_TTL_SECONDS = 60 * 60
_OWNER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
_KEY_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SECRET_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_PROVIDER_IDS = frozenset(entry.provider for entry in CATALOG)


class CredentialBrokerError(Exception):
    """Base class for fixed-message, non-sensitive credential failures."""


class InvalidCredentialOwnerError(CredentialBrokerError):
    """The owner identifier is malformed."""


class UnknownProviderError(CredentialBrokerError):
    """The provider is outside the curated generation catalog."""


class UnknownAuthModeError(CredentialBrokerError):
    """The requested authentication mode is not supported."""


class CredentialUnavailableError(CredentialBrokerError):
    """No owner-scoped credential is currently available."""


class CredentialKeyUnavailableError(CredentialBrokerError):
    """A deployment master key needed for encrypted state is unavailable."""


class CredentialStorageError(CredentialBrokerError):
    """Encrypted credential state could not be read or changed safely."""


class InvalidCredentialError(CredentialBrokerError):
    """A supplied credential violates the bounded credential contract."""


class ConnectionTester(Protocol):
    def __call__(self, provider: str, credential: str | None) -> Awaitable[None]: ...


@dataclass(frozen=True)
class ConnectionTestResult:
    """A connection result containing only a normalized category."""

    ok: bool
    category: ErrorCategory | None


@dataclass(frozen=True)
class _SessionCredential:
    ciphertext: str = field(repr=False)
    expires_at: int


class CredentialBroker:
    """Resolve credentials without exposing them outside a scoped context.

    Hosted values and master-key values are fetched only through explicitly
    declared deployment-environment references. Session credentials are held as
    process-local ciphertext under an ephemeral key. Persisted BYOK values use
    :class:`CredentialCipher` and the application database's atomic transaction
    boundary.
    """

    def __init__(
        self,
        database: Database,
        *,
        deployment_environment: Mapping[str, str],
        hosted_secret_references: Mapping[str, str] = MappingProxyType({}),
        master_key_references: Mapping[str, str] = MappingProxyType({}),
        active_key_id: str | None = None,
        clock: Callable[[], int] = lambda: int(time.time()),
        session_ttl_seconds: int = 15 * 60,
        connection_tester: ConnectionTester | None = None,
    ) -> None:
        if not 1 <= session_ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise ValueError("session credential lifetime is outside the allowed bound")
        self._validate_references(hosted_secret_references, provider_keys=True)
        self._validate_references(master_key_references, provider_keys=False)
        if active_key_id is not None and (
            _KEY_IDENTIFIER.fullmatch(active_key_id) is None
            or active_key_id not in master_key_references
        ):
            raise ValueError("active credential key ID must name a declared key")
        if master_key_references and active_key_id is None:
            raise ValueError("an active credential key ID is required")

        self._database = database
        self._hosted_secret_references = MappingProxyType(dict(hosted_secret_references))
        self._master_key_references = MappingProxyType(dict(master_key_references))
        self._active_key_id = active_key_id
        self._clock = clock
        self._session_ttl_seconds = session_ttl_seconds
        self._connection_tester = connection_tester
        self._memory_cipher = CredentialCipher(
            secrets.token_urlsafe(48),
            active_key_id="memory",
        )
        self._hosted_secret_ciphertexts = self._snapshot_deployment_secrets(
            deployment_environment,
            self._hosted_secret_references,
        )
        self._master_key_ciphertexts = self._snapshot_deployment_secrets(
            deployment_environment,
            self._master_key_references,
        )
        self._session_credentials: dict[tuple[str, str], _SessionCredential] = {}
        self._session_lock = threading.RLock()

    def __repr__(self) -> str:
        return "CredentialBroker()"

    @staticmethod
    def _validate_references(references: Mapping[str, str], *, provider_keys: bool) -> None:
        key_pattern = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}") if provider_keys else _KEY_IDENTIFIER
        for key, reference in references.items():
            if key_pattern.fullmatch(key) is None or _SECRET_REFERENCE.fullmatch(reference) is None:
                raise ValueError("credential secret references are invalid")

    def _snapshot_deployment_secrets(
        self,
        environment: Mapping[str, str],
        references: Mapping[str, str],
    ) -> Mapping[str, str]:
        """Copy only declared deployment values into process-local ciphertext."""
        encrypted: dict[str, str] = {}
        for identifier, reference in references.items():
            value = environment.get(reference)
            if value is None:
                continue
            if not isinstance(value, str):
                continue
            encoded = value.encode("utf-8")
            if not encoded or len(encoded) > MAX_CREDENTIAL_BYTES:
                value = ""
                continue
            encrypted[identifier] = self._memory_cipher.encrypt(value)
            value = ""
        return MappingProxyType(encrypted)

    @staticmethod
    def _validate_owner(user_id: str) -> str:
        if not isinstance(user_id, str) or _OWNER_IDENTIFIER.fullmatch(user_id) is None:
            raise InvalidCredentialOwnerError("credential owner is invalid")
        return user_id

    @staticmethod
    def _validate_provider(provider: str) -> str:
        if not isinstance(provider, str) or provider not in _PROVIDER_IDS:
            raise UnknownProviderError("credential provider is unsupported")
        return provider

    @staticmethod
    def _validate_auth_mode(auth_mode: AuthMode | str) -> AuthMode:
        try:
            return auth_mode if isinstance(auth_mode, AuthMode) else AuthMode(auth_mode)
        except (TypeError, ValueError):
            raise UnknownAuthModeError("credential authentication mode is unsupported") from None

    @staticmethod
    def _validate_plaintext(credential: str) -> str:
        if not isinstance(credential, str):
            raise InvalidCredentialError("credential value is invalid")
        encoded = credential.encode("utf-8")
        if not encoded or len(encoded) > MAX_CREDENTIAL_BYTES:
            raise InvalidCredentialError("credential value is invalid")
        return credential

    @staticmethod
    def _split_ciphertext(ciphertext: str, expected_key_id: str) -> str:
        key_id, separator, token = ciphertext.partition(":")
        if not separator or key_id != expected_key_id or not token:
            raise CredentialStorageError("credential storage is unavailable")
        return token

    def _master_cipher(self, required_key_id: str | None = None) -> CredentialCipher:
        active_key_id = self._active_key_id
        if active_key_id is None:
            raise CredentialKeyUnavailableError("credential encryption key is unavailable")
        if active_key_id not in self._master_key_ciphertexts or (
            required_key_id is not None and required_key_id not in self._master_key_ciphertexts
        ):
            raise CredentialKeyUnavailableError("credential encryption key is unavailable")
        configured: dict[str, str] = {}
        for key_id, ciphertext in self._master_key_ciphertexts.items():
            try:
                configured[key_id] = self._memory_cipher.decrypt(ciphertext)
            except ValueError:
                configured.clear()
                raise CredentialKeyUnavailableError(
                    "credential encryption key is unavailable"
                ) from None
        try:
            cipher = CredentialCipher(configured, active_key_id=active_key_id)
        except ValueError:
            configured.clear()
            raise CredentialKeyUnavailableError(
                "credential encryption key is unavailable"
            ) from None
        configured.clear()
        return cipher

    def store_session(
        self,
        user_id: str,
        provider: str,
        credential: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store owner-scoped BYOK as ephemeral process-local ciphertext."""
        owner = self._validate_owner(user_id)
        provider_id = self._validate_provider(provider)
        plaintext = self._validate_plaintext(credential)
        lifetime = self._session_ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(lifetime, int) or not 1 <= lifetime <= self._session_ttl_seconds:
            raise ValueError("session credential lifetime is outside the allowed bound")
        ciphertext = self._memory_cipher.encrypt(plaintext)
        del plaintext
        credential = ""
        with self._session_lock:
            self._session_credentials[(owner, provider_id)] = _SessionCredential(
                ciphertext=ciphertext,
                expires_at=self._clock() + lifetime,
            )

    def store_encrypted(self, user_id: str, provider: str, credential: str) -> None:
        """Persist owner-scoped BYOK using the active deployment master key."""
        owner = self._validate_owner(user_id)
        provider_id = self._validate_provider(provider)
        plaintext = self._validate_plaintext(credential)
        cipher = self._master_cipher()
        assert self._active_key_id is not None
        encrypted = cipher.encrypt(plaintext)
        del plaintext
        credential = ""
        token = self._split_ciphertext(encrypted, self._active_key_id)
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO credentials (
                        owner_id, provider, auth_mode, ciphertext, key_id, updated_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(owner_id, provider, auth_mode) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        key_id = excluded.key_id,
                        updated_at = excluded.updated_at,
                        revoked_at = NULL
                    """,
                    (
                        owner,
                        provider_id,
                        AuthMode.BYOK.value,
                        token,
                        self._active_key_id,
                        self._clock(),
                    ),
                )
        except sqlite3.DatabaseError:
            raise CredentialStorageError("credential storage is unavailable") from None

    def revoke(self, user_id: str, provider: str) -> None:
        """Immediately revoke only the calling owner's BYOK credential."""
        owner = self._validate_owner(user_id)
        provider_id = self._validate_provider(provider)
        with self._session_lock:
            removed_session = self._session_credentials.pop((owner, provider_id), None) is not None
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE credentials
                    SET revoked_at = ?, updated_at = ?
                    WHERE owner_id = ? AND provider = ? AND auth_mode = ?
                        AND revoked_at IS NULL
                    """,
                    (
                        self._clock(),
                        self._clock(),
                        owner,
                        provider_id,
                        AuthMode.BYOK.value,
                    ),
                )
        except sqlite3.DatabaseError:
            raise CredentialStorageError("credential storage is unavailable") from None
        if cursor.rowcount != 1 and not removed_session:
            raise CredentialUnavailableError("credential is unavailable")

    def _resolve_session(self, owner: str, provider: str) -> str | None:
        key = (owner, provider)
        with self._session_lock:
            stored = self._session_credentials.get(key)
            if stored is None:
                return None
            if stored.expires_at <= self._clock():
                self._session_credentials.pop(key, None)
                return None
            ciphertext = stored.ciphertext
        try:
            return self._memory_cipher.decrypt(ciphertext)
        except ValueError:
            with self._session_lock:
                self._session_credentials.pop(key, None)
            raise CredentialStorageError("credential storage is unavailable") from None

    def _resolve_persisted(self, owner: str, provider: str) -> str:
        plaintext: str | None = None
        failure: str | None = None
        try:
            with self._database.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT ciphertext, key_id, revoked_at
                    FROM credentials
                    WHERE owner_id = ? AND provider = ? AND auth_mode = ?
                    """,
                    (owner, provider, AuthMode.BYOK.value),
                ).fetchone()
                if row is None or row["revoked_at"] is not None:
                    raise CredentialUnavailableError("credential is unavailable")
                key_id = row["key_id"]
                cipher = self._master_cipher(key_id)
                try:
                    plaintext = cipher.decrypt(f"{key_id}:{row['ciphertext']}")
                except ValueError:
                    raise CredentialStorageError("credential storage is unavailable") from None
                if key_id != self._active_key_id:
                    assert self._active_key_id is not None
                    rotated = cipher.encrypt(plaintext)
                    token = self._split_ciphertext(rotated, self._active_key_id)
                    cursor = connection.execute(
                        """
                        UPDATE credentials
                        SET ciphertext = ?, key_id = ?, updated_at = ?
                        WHERE owner_id = ? AND provider = ? AND auth_mode = ?
                            AND revoked_at IS NULL AND key_id = ? AND ciphertext = ?
                        """,
                        (
                            token,
                            self._active_key_id,
                            self._clock(),
                            owner,
                            provider,
                            AuthMode.BYOK.value,
                            key_id,
                            row["ciphertext"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise CredentialUnavailableError("credential is unavailable")
        except CredentialUnavailableError:
            if plaintext is None:
                raise
            plaintext = None
            failure = "unavailable"
        except CredentialStorageError:
            plaintext = None
            failure = "storage"
        except CredentialKeyUnavailableError:
            raise
        except sqlite3.DatabaseError:
            plaintext = None
            failure = "storage"
        if failure == "unavailable":
            raise CredentialUnavailableError("credential is unavailable") from None
        if failure == "storage":
            raise CredentialStorageError("credential storage is unavailable") from None
        if plaintext is None:  # Defensive: every successful row must decrypt to text.
            raise CredentialStorageError("credential storage is unavailable")
        return plaintext

    def resolve(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> AsyncContextManager[str | None]:
        """Return a context that yields only the validated scoped credential."""
        return self._resolve_context(user_id, provider, auth_mode)

    @asynccontextmanager
    async def _resolve_context(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> AsyncIterator[str | None]:
        owner = self._validate_owner(user_id)
        provider_id = self._validate_provider(provider)
        mode = self._validate_auth_mode(auth_mode)
        plaintext: str | None = None
        try:
            if mode is AuthMode.AGENT:
                yield None
                return
            if mode is AuthMode.HOSTED:
                ciphertext = self._hosted_secret_ciphertexts.get(provider_id)
                if ciphertext is None:
                    raise CredentialUnavailableError("credential is unavailable")
                try:
                    plaintext = self._memory_cipher.decrypt(ciphertext)
                except ValueError:
                    raise CredentialUnavailableError("credential is unavailable") from None
                yield plaintext
                return

            plaintext = self._resolve_session(owner, provider_id)
            if plaintext is None:
                plaintext = self._resolve_persisted(owner, provider_id)
            yield plaintext
        finally:
            plaintext = None

    async def test_connection(
        self,
        user_id: str,
        provider: str,
        auth_mode: AuthMode | str,
    ) -> ConnectionTestResult:
        """Run only an injected offline probe and return normalized categories."""
        tester = self._connection_tester
        if tester is None:
            return ConnectionTestResult(ok=False, category=ErrorCategory.UNAVAILABLE)
        try:
            async with self.resolve(user_id, provider, auth_mode) as credential:
                await tester(provider, credential)
        except ProviderError as error:
            return ConnectionTestResult(ok=False, category=error.category)
        except CredentialBrokerError:
            raise
        except Exception:
            return ConnectionTestResult(ok=False, category=ErrorCategory.PROVIDER_ERROR)
        return ConnectionTestResult(ok=True, category=None)
