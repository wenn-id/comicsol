from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from comic_sol_web.app import create_app
from comic_sol_web.config import WebConfig


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "comic-sol-data"
        self.config = WebConfig.local_from_env({"COMIC_SOL_WEB_DATA_ROOT": str(self.root)})

    def test_local_config_needs_only_a_data_root(self) -> None:
        self.assertTrue(self.config.local_mode)
        self.assertEqual(self.root, self.config.data_root)
        self.assertNotIn(self.config.session_secret, repr(self.config))
        self.assertNotIn(self.config.encryption_secret, repr(self.config))

    def test_bootstrap_issues_session_and_csrf_cookies_on_loopback(self) -> None:
        with TestClient(create_app(self.config), client=("127.0.0.1", 50000)) as client:
            response = client.post("/api/auth/local-session")
        self.assertEqual(200, response.status_code)
        self.assertEqual("local", response.json()["login"])
        self.assertIn("comic_sol_session", response.cookies)
        self.assertIn("comic_sol_csrf", response.cookies)

    def test_bootstrap_rejects_non_loopback_client(self) -> None:
        with TestClient(create_app(self.config), client=("192.0.2.10", 50000)) as client:
            response = client.post("/api/auth/local-session")
        self.assertEqual(403, response.status_code)

    def test_repeated_bootstrap_keeps_the_fixed_local_owner(self) -> None:
        with TestClient(create_app(self.config), client=("::1", 50000)) as client:
            first = client.post("/api/auth/local-session")
            second = client.post("/api/auth/local-session")
        self.assertEqual("comic-sol-local-user", first.json()["user_id"])
        self.assertEqual(first.json(), second.json())

    def test_local_bootstrap_uses_one_database_and_http_loopback_cookies(self) -> None:
        app = create_app(self.config)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.post("/api/auth/local-session")
        cookies = response.headers.get_list("set-cookie")
        self.assertTrue(all("Secure" not in cookie for cookie in cookies))
        self.assertIs(app.state.auth.database, app.state.projects.gateway.database)

    def test_local_config_keeps_provider_values_out_of_configuration(self) -> None:
        config = WebConfig.local_from_env(
            {
                "COMIC_SOL_WEB_DATA_ROOT": str(self.root),
                "OPENAI_API_KEY": "openai-secret-value",
            }
        )
        self.assertEqual({"openai": "OPENAI_API_KEY"}, dict(config.hosted_secret_references))
        self.assertNotIn("openai-secret-value", repr(config))

    def test_local_config_rejects_relative_data_root(self) -> None:
        with self.assertRaises(ValueError):
            WebConfig.local_from_env({"COMIC_SOL_WEB_DATA_ROOT": "relative-data"})

    def test_launcher_uses_loopback_host(self) -> None:
        launched_app = object()
        with (
            patch("comic_sol_web.__main__.WebConfig.local_from_env", return_value=self.config),
            patch("comic_sol_web.__main__.uvicorn.run") as run,
            patch("comic_sol_web.__main__.create_app", return_value=launched_app),
        ):
            from comic_sol_web.__main__ import main

            self.assertEqual(0, main())
        run.assert_called_once_with(
            launched_app, host=self.config.host, port=8765, log_level="info"
        )


if __name__ == "__main__":
    unittest.main()
