"""GitHub OAuth, replay-safe state, server sessions, and CSRF boundaries."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import HTTPException, Request, Response, status

from comic_sol_web.database import Database

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
SESSION_COOKIE_NAME = "comic_sol_session"
CSRF_COOKIE_NAME = "comic_sol_csrf"
OAUTH_BINDING_COOKIE_NAME = "comic_sol_oauth_binding"
CSRF_HEADER_NAME = "x-csrf-token"
MAX_OUTSTANDING_OAUTH_STATES = 1024


class AuthError(ValueError):
    """Authentication or CSRF validation failed closed."""


class OAuthStateError(AuthError):
    """OAuth state is missing, expired, or already consumed."""


@dataclass(frozen=True)
class SessionPrincipal:
    user_id: str
    login: str


@dataclass(frozen=True)
class AuthenticatedSession:
    principal: SessionPrincipal
    session_token: str
    csrf_token: str
    expires_at: int


class GitHubOAuthProtocol(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    async def exchange_code(self, *, code: str, redirect_uri: str) -> SessionPrincipal: ...


class GitHubOAuth:
    """Small Authlib-backed GitHub OAuth client with no implicit network work."""

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ValueError("GitHub OAuth client configuration is required")
        self._client_id = client_id
        self._client_secret = client_secret

    def _client(self, redirect_uri: str) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=redirect_uri,
            scope="read:user",
        )

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        client = self._client(redirect_uri)
        url, _ = client.create_authorization_url(GITHUB_AUTHORIZE_URL, state=state)
        return url

    async def exchange_code(self, *, code: str, redirect_uri: str) -> SessionPrincipal:
        if not code or len(code) > 2048:
            raise AuthError("OAuth authorization code is missing")
        try:
            async with self._client(redirect_uri) as client:
                await client.fetch_token(GITHUB_TOKEN_URL, code=code)
                response = await client.get(GITHUB_USER_URL)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            raise AuthError("GitHub OAuth exchange failed") from None
        user_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
        login = payload.get("login", "") if isinstance(payload, dict) else ""
        if (
            not user_id
            or len(user_id) > 64
            or not isinstance(login, str)
            or not login
            or len(login) > 128
        ):
            raise AuthError("GitHub OAuth identity is invalid")
        return SessionPrincipal(user_id=user_id, login=login)


def build_github_oauth(*, client_id: str, client_secret: str) -> GitHubOAuth:
    return GitHubOAuth(client_id=client_id, client_secret=client_secret)


class AuthService:
    def __init__(
        self,
        database: Database,
        *,
        session_secret: str,
        github_oauth: GitHubOAuthProtocol | None,
        clock: Callable[[], float] = time.time,
        state_ttl_seconds: int = 600,
        session_ttl_seconds: int = 8 * 60 * 60,
        secure_cookies: bool = True,
    ) -> None:
        if not session_secret or state_ttl_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("valid session security settings are required")
        self.database = database
        self._secret = session_secret.encode("utf-8")
        self._github_oauth = github_oauth
        self._clock = clock
        self.state_ttl_seconds = state_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.secure_cookies = secure_cookies
        self.session_cookie_name = SESSION_COOKIE_NAME
        self.csrf_cookie_name = CSRF_COOKIE_NAME
        self.oauth_binding_cookie_name = OAUTH_BINDING_COOKIE_NAME
        self.csrf_header_name = CSRF_HEADER_NAME

    def _now(self) -> int:
        return int(self._clock())

    def _digest(self, namespace: bytes, token: str) -> str:
        """Return a keyed, domain-separated digest of a server-issued token.

        Every value reaching this function is a fresh 256-bit token from
        ``secrets.token_urlsafe(32)`` — an OAuth state, a browser binding, a
        session token, or a CSRF token. None is a password, a passphrase, or any
        other low-entropy or user-chosen secret.

        That is why a keyed hash is correct here and a password KDF is not.
        Argon2, scrypt, bcrypt, and PBKDF2 exist to add a work factor over a
        small guessable input space. A 256-bit random token has no such space to
        search, so a work factor would buy no security while charging its cost
        on every authenticated request. The properties this call does need are
        preimage resistance, so a database read cannot recover a live token, and
        a server-held key, so an attacker with the table cannot precompute
        candidates offline. Keyed BLAKE2b provides both.

        ``person`` separates namespaces, so a digest minted for one purpose can
        never be redeemed as another.
        """
        key = hashlib.blake2b(self._secret, digest_size=32, person=b"comic-sol-key").digest()
        return hashlib.blake2b(
            token.encode("utf-8"), key=key, digest_size=32, person=namespace[:16]
        ).hexdigest()

    def begin_oauth(self, redirect_uri: str) -> tuple[str, str, str]:
        if self._github_oauth is None:
            raise AuthError("OAuth is unavailable")
        if not redirect_uri.startswith("https://"):
            raise AuthError("OAuth redirect URI must use HTTPS")
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        state_hash = self._digest(b"oauth-state:", state)
        binding_hash = self._digest(b"oauth-binding:", binding)
        with self.database.transaction() as connection:
            now = self._now()
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at <= ? OR consumed_at IS NOT NULL", (now,)
            )
            connection.execute(
                "DELETE FROM oauth_states WHERE state_hash IN "
                "(SELECT state_hash FROM oauth_states ORDER BY expires_at, state_hash "
                "LIMIT MAX(0, (SELECT COUNT(*) FROM oauth_states) - ?))",
                (MAX_OUTSTANDING_OAUTH_STATES - 1,),
            )
            connection.execute(
                "INSERT INTO oauth_states (state_hash, expires_at, binding_hash) VALUES (?, ?, ?)",
                (state_hash, now + self.state_ttl_seconds, binding_hash),
            )
        return (
            state,
            binding,
            self._github_oauth.authorization_url(state=state, redirect_uri=redirect_uri),
        )

    def _consume_oauth_state(self, state: str, binding: str | None) -> None:
        if not state or len(state) > 256 or not binding or len(binding) > 256:
            raise OAuthStateError("OAuth state is invalid")
        state_hash = self._digest(b"oauth-state:", state)
        binding_hash = self._digest(b"oauth-binding:", binding)
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT expires_at, consumed_at, binding_hash FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= now
                or row["binding_hash"] is None
                or not hmac.compare_digest(row["binding_hash"], binding_hash)
            ):
                raise OAuthStateError("OAuth state is invalid")
            updated = connection.execute(
                "UPDATE oauth_states SET consumed_at = ? "
                "WHERE state_hash = ? AND consumed_at IS NULL",
                (now, state_hash),
            ).rowcount
            if updated != 1:
                raise OAuthStateError("OAuth state is invalid")

    async def complete_oauth(
        self,
        *,
        state: str,
        binding: str | None,
        code: str,
        redirect_uri: str,
    ) -> AuthenticatedSession:
        if self._github_oauth is None:
            raise AuthError("OAuth is unavailable")
        self._consume_oauth_state(state, binding)
        principal = await self._github_oauth.exchange_code(code=code, redirect_uri=redirect_uri)
        return self.create_session(principal)

    def create_session(self, principal: SessionPrincipal) -> AuthenticatedSession:
        if (
            not principal.user_id
            or len(principal.user_id) > 64
            or not principal.login
            or len(principal.login) > 128
        ):
            raise AuthError("authenticated identity is invalid")
        now = self._now()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_hash = self._digest(b"session:", session_token)
        csrf_hash = self._digest(b"csrf:", csrf_token)
        expires_at = now + self.session_ttl_seconds
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users (user_id, login, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET login = excluded.login, "
                "updated_at = excluded.updated_at",
                (principal.user_id, principal.login, now),
            )
            connection.execute(
                "INSERT INTO sessions "
                "(token_hash, user_id, csrf_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (session_hash, principal.user_id, csrf_hash, now, expires_at),
            )
        return AuthenticatedSession(principal, session_token, csrf_token, expires_at)

    def authenticate_token(self, session_token: str | None) -> SessionPrincipal:
        if not session_token or len(session_token) > 256:
            raise AuthError("authentication required")
        token_hash = self._digest(b"session:", session_token)
        now = self._now()
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT sessions.expires_at, users.user_id, users.login "
                "FROM sessions JOIN users ON users.user_id = sessions.user_id "
                "WHERE sessions.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            raise AuthError("authentication required")
        if row["expires_at"] <= now:
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            raise AuthError("authentication required")
        return SessionPrincipal(row["user_id"], row["login"])

    def authenticate_request(self, request: Request) -> SessionPrincipal:
        return self.authenticate_token(request.cookies.get(self.session_cookie_name))

    def require_csrf(self, request: Request) -> SessionPrincipal:
        session_token = request.cookies.get(self.session_cookie_name)
        principal = self.authenticate_token(session_token)
        cookie_token = request.cookies.get(self.csrf_cookie_name)
        header_token = request.headers.get(self.csrf_header_name)
        if (
            not cookie_token
            or not header_token
            or len(cookie_token) > 256
            or len(header_token) > 256
            or not hmac.compare_digest(cookie_token.encode(), header_token.encode())
        ):
            raise AuthError("CSRF validation failed")
        assert session_token is not None
        session_hash = self._digest(b"session:", session_token)
        supplied_hash = self._digest(b"csrf:", cookie_token)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT csrf_hash FROM sessions WHERE token_hash = ?", (session_hash,)
            ).fetchone()
        if row is None or not hmac.compare_digest(row["csrf_hash"], supplied_hash):
            raise AuthError("CSRF validation failed")
        return principal

    def revoke(self, session_token: str | None) -> None:
        if not session_token or len(session_token) > 256:
            return
        token_hash = self._digest(b"session:", session_token)
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def set_session_cookies(self, response: Response, session: AuthenticatedSession) -> None:
        response.set_cookie(
            self.session_cookie_name,
            session.session_token,
            max_age=self.session_ttl_seconds,
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            self.csrf_cookie_name,
            session.csrf_token,
            max_age=self.session_ttl_seconds,
            secure=self.secure_cookies,
            httponly=False,
            samesite="lax",
            path="/",
        )

    def clear_session_cookies(self, response: Response) -> None:
        response.delete_cookie(
            self.session_cookie_name, secure=self.secure_cookies, httponly=True, path="/"
        )
        response.delete_cookie(
            self.csrf_cookie_name, secure=self.secure_cookies, httponly=False, path="/"
        )

    def set_oauth_binding_cookie(self, response: Response, binding: str) -> None:
        response.set_cookie(
            self.oauth_binding_cookie_name,
            binding,
            max_age=self.state_ttl_seconds,
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
            path="/api/auth/callback",
        )

    def clear_oauth_binding_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.oauth_binding_cookie_name,
            secure=self.secure_cookies,
            httponly=True,
            path="/api/auth/callback",
        )


async def require_principal(request: Request) -> SessionPrincipal:
    """FastAPI dependency that rejects anonymous and stale sessions."""
    service = getattr(request.app.state, "auth", None)
    if not isinstance(service, AuthService):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        return service.authenticate_request(request)
    except AuthError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        ) from error
