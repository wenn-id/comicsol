from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Response
from starlette.requests import Request

from comic_sol_web.auth import (
    AuthError,
    AuthService,
    OAuthStateError,
    SessionPrincipal,
    build_github_oauth,
    require_principal,
)
from comic_sol_web.database import Database
from comic_sol_web.migrations import apply_migrations
from comic_sol_web.security import CredentialCipher, redact_mapping, redact_text
from support import ENCRYPTION_SECRET, SESSION_SECRET


class FakeGitHubOAuth:
    def __init__(self) -> None:
        self.exchanges = 0
        self.fail = False

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://github.com/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> SessionPrincipal:
        self.exchanges += 1
        if self.fail:
            raise AuthError("oauth exchange failed")
        self.asserted_code = code
        self.asserted_redirect_uri = redirect_uri
        return SessionPrincipal(user_id="github-42", login="octocat")


def request_for(
    app: FastAPI,
    *,
    method: str = "GET",
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    all_headers = dict(headers or {})
    if cookies:
        all_headers["cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/api/test",
        "raw_path": b"/api/test",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in all_headers.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
        "app": app,
    }
    return Request(scope)


class AuthServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "web.sqlite3")
        apply_migrations(self.database)
        self.now = 1_900_000_000
        self.oauth = FakeGitHubOAuth()
        self.service = AuthService(
            self.database,
            session_secret=SESSION_SECRET,
            github_oauth=self.oauth,
            clock=lambda: self.now,
            state_ttl_seconds=300,
            session_ttl_seconds=600,
        )

    async def test_oauth_state_is_one_shot_and_replay_safe(self) -> None:
        state, binding, authorization_url = self.service.begin_oauth(
            "https://example.test/callback"
        )
        self.assertIn("github.com/login/oauth/authorize", authorization_url)
        first = await self.service.complete_oauth(
            state=state,
            binding=binding,
            code="code-once",
            redirect_uri="https://example.test/callback",
        )
        self.assertEqual(SessionPrincipal("github-42", "octocat"), first.principal)
        self.assertEqual(1, self.oauth.exchanges)
        with self.assertRaises(OAuthStateError):
            await self.service.complete_oauth(
                state=state,
                binding=binding,
                code="code-twice",
                redirect_uri="https://example.test/callback",
            )
        self.assertEqual(1, self.oauth.exchanges)

    async def test_oauth_state_is_consumed_before_a_failed_exchange(self) -> None:
        state, binding, _ = self.service.begin_oauth("https://example.test/callback")
        self.oauth.fail = True
        with self.assertRaises(AuthError):
            await self.service.complete_oauth(
                state=state,
                binding=binding,
                code="bad-code",
                redirect_uri="https://example.test/callback",
            )
        self.oauth.fail = False
        with self.assertRaises(OAuthStateError):
            await self.service.complete_oauth(
                state=state,
                binding=binding,
                code="replay",
                redirect_uri="https://example.test/callback",
            )

    async def test_expired_oauth_state_is_rejected(self) -> None:
        state, binding, _ = self.service.begin_oauth("https://example.test/callback")
        self.now += 301
        with self.assertRaises(OAuthStateError):
            await self.service.complete_oauth(
                state=state,
                binding=binding,
                code="late",
                redirect_uri="https://example.test/callback",
            )
        self.assertEqual(0, self.oauth.exchanges)

    async def test_oauth_state_is_bound_to_initiating_browser(self) -> None:
        state, binding, _ = self.service.begin_oauth("https://example.test/callback")
        with self.assertRaises(OAuthStateError):
            await self.service.complete_oauth(
                state=state,
                binding="other-browser",
                code="code",
                redirect_uri="https://example.test/callback",
            )
        result = await self.service.complete_oauth(
            state=state,
            binding=binding,
            code="code",
            redirect_uri="https://example.test/callback",
        )
        self.assertEqual("github-42", result.principal.user_id)

    def test_begin_oauth_purges_expired_states(self) -> None:
        self.service.begin_oauth("https://example.test/callback")
        self.now += 301
        self.service.begin_oauth("https://example.test/callback")
        with self.database.read() as connection:
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0]
            )

    async def test_outstanding_oauth_state_retention_is_bounded(self) -> None:
        live = []
        with patch("comic_sol_web.auth.MAX_OUTSTANDING_OAUTH_STATES", 4):
            for _ in range(6):
                live.append(self.service.begin_oauth("https://example.test/callback"))
                # Eviction orders by expiry, so distinct start times make which
                # states survive deterministic rather than digest-ordered.
                self.now += 1
        with self.database.read() as connection:
            self.assertEqual(
                4, connection.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0]
            )
        for state, binding, _ in live[:2]:
            with self.subTest(state="evicted"), self.assertRaises(OAuthStateError):
                await self.service.complete_oauth(
                    state=state,
                    binding=binding,
                    code="code",
                    redirect_uri="https://example.test/callback",
                )
        newest_state, newest_binding, _ = live[-1]
        retained = await self.service.complete_oauth(
            state=newest_state,
            binding=newest_binding,
            code="code",
            redirect_uri="https://example.test/callback",
        )
        self.assertEqual("github-42", retained.principal.user_id)

    def test_sessions_are_hashed_and_cookies_are_secure(self) -> None:
        authenticated = self.service.create_session(SessionPrincipal("github-42", "octocat"))
        with self.database.read() as connection:
            row = connection.execute("SELECT token_hash, csrf_hash FROM sessions").fetchone()
        self.assertNotEqual(authenticated.session_token, row[0])
        self.assertNotEqual(authenticated.csrf_token, row[1])

        response = Response()
        self.service.set_session_cookies(response, authenticated)
        cookies = response.headers.getlist("set-cookie")
        session_cookie = next(value for value in cookies if value.startswith("comic_sol_session="))
        csrf_cookie = next(value for value in cookies if value.startswith("comic_sol_csrf="))
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("Secure", session_cookie)
        self.assertIn("SameSite=lax", session_cookie)
        self.assertIn("Path=/", session_cookie)
        self.assertIn("Secure", csrf_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertNotIn(SESSION_SECRET, "".join(cookies))

    async def test_anonymous_and_stale_sessions_fail_closed(self) -> None:
        app = FastAPI()
        app.state.auth = self.service
        with self.assertRaises(HTTPException) as anonymous:
            await require_principal(request_for(app))
        self.assertEqual(401, anonymous.exception.status_code)

        authenticated = self.service.create_session(SessionPrincipal("github-42", "octocat"))
        self.now += 601
        stale_request = request_for(
            app, cookies={self.service.session_cookie_name: authenticated.session_token}
        )
        with self.assertRaises(HTTPException) as stale:
            await require_principal(stale_request)
        self.assertEqual(401, stale.exception.status_code)
        with self.database.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    async def test_double_submit_csrf_is_required_and_bound_to_session(self) -> None:
        authenticated = self.service.create_session(SessionPrincipal("github-42", "octocat"))
        base_cookies = {self.service.session_cookie_name: authenticated.session_token}
        cases: tuple[tuple[dict[str, str], dict[str, str], str], ...] = (
            (base_cookies, {}, "missing"),
            (
                {**base_cookies, self.service.csrf_cookie_name: authenticated.csrf_token},
                {self.service.csrf_header_name: "wrong"},
                "mismatch",
            ),
            (
                {**base_cookies, self.service.csrf_cookie_name: "other"},
                {self.service.csrf_header_name: "other"},
                "not-bound",
            ),
        )
        for cookies, headers, label in cases:
            with self.subTest(label=label), self.assertRaises(AuthError):
                self.service.require_csrf(
                    request_for(FastAPI(), method="POST", cookies=cookies, headers=headers)
                )

        valid = request_for(
            FastAPI(),
            method="POST",
            cookies={
                **base_cookies,
                self.service.csrf_cookie_name: authenticated.csrf_token,
            },
            headers={self.service.csrf_header_name: authenticated.csrf_token},
        )
        principal = self.service.require_csrf(valid)
        self.assertEqual(SessionPrincipal("github-42", "octocat"), principal)

    def test_github_oauth_uses_pinned_authlib_primitives_without_network(self) -> None:
        oauth = build_github_oauth(client_id="test-client", client_secret="test-secret")
        url = oauth.authorization_url(state="state-value", redirect_uri="https://example.test/cb")
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("state=state-value", url)


class SecurityPrimitiveTests(unittest.TestCase):
    def test_credential_cipher_round_trips_without_exposing_plaintext(self) -> None:
        cipher = CredentialCipher(ENCRYPTION_SECRET)
        encrypted = cipher.encrypt("provider-secret-value")
        self.assertNotIn("provider-secret-value", encrypted)
        self.assertEqual("provider-secret-value", cipher.decrypt(encrypted))
        with self.assertRaises(ValueError):
            cipher.decrypt(encrypted[:-2] + "xx")

    def test_secret_redaction_is_recursive_and_bounded(self) -> None:
        secret = "ghp_example_token_value"
        source = {
            "Authorization": f"Bearer {secret}",
            "nested": {"api_key": secret, "message": f"failed with token={secret}"},
            "safe": "provider unavailable",
        }
        redacted = redact_mapping(source, secrets=(secret,))
        rendered = json.dumps(redacted)
        self.assertNotIn(secret, rendered)
        self.assertEqual("provider unavailable", redacted["safe"])
        bounded = redact_text("x" * 20_000 + secret, secrets=(secret,), limit=512)
        self.assertLessEqual(len(bounded), 512)
        self.assertNotIn(secret, bounded)


if __name__ == "__main__":
    unittest.main()
