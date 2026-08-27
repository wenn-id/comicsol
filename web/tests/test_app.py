from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ENCRYPTION_SECRET, SESSION_SECRET, valid_environment


class WebConfigTests(unittest.TestCase):
    def test_config_rejects_missing_session_and_encryption_keys(self):
        from comic_sol_web.config import WebConfig

        for variable in (
            "COMIC_SOL_WEB_SESSION_SECRET",
            "COMIC_SOL_WEB_ENCRYPTION_SECRET",
        ):
            environment = valid_environment()
            environment.pop(variable)
            with self.subTest(variable=variable), self.assertRaises(ValueError) as caught:
                WebConfig.from_env(environment)
            self.assertIn(variable, str(caught.exception))

    def test_config_rejects_empty_and_control_character_secrets(self):
        from comic_sol_web.config import WebConfig

        for variable in (
            "COMIC_SOL_WEB_SESSION_SECRET",
            "COMIC_SOL_WEB_ENCRYPTION_SECRET",
        ):
            for value in ("", "secret\nvalue", "secret\x00value"):
                environment = valid_environment()
                environment[variable] = value
                with self.subTest(variable=variable, value=repr(value)):
                    with self.assertRaises(ValueError):
                        WebConfig.from_env(environment)

    def test_config_errors_never_include_secret_values(self):
        from comic_sol_web.config import WebConfig

        environment = valid_environment()
        environment["COMIC_SOL_WEB_SESSION_SECRET"] = "not-valid\n" + SESSION_SECRET
        with self.assertRaises(ValueError) as caught:
            WebConfig.from_env(environment)
        message = str(caught.exception)
        self.assertNotIn(SESSION_SECRET, message)
        self.assertNotIn(ENCRYPTION_SECRET, message)

    def test_config_accepts_whitespace_in_absolute_data_root(self):
        # A Windows profile / macOS home path legitimately contains spaces;
        # only secrets must reject whitespace and control characters.
        from comic_sol_web.config import WebConfig

        data_root_arg = Path("/tmp/Comic Sol projects")
        expected_root = data_root_arg.resolve()
        # Secrets reject whitespace and control characters even when the
        # data-root path legitimately contains spaces.
        for variable in (
            "COMIC_SOL_WEB_SESSION_SECRET",
            "COMIC_SOL_WEB_ENCRYPTION_SECRET",
        ):
            with self.subTest(variable=variable):
                environment = valid_environment(data_root_arg)
                environment[variable] = "has space-secret-value-000000000000000000000"
                with self.assertRaises(ValueError):
                    WebConfig.from_env(environment)
        config = WebConfig.from_env(valid_environment(data_root_arg))
        self.assertEqual(expected_root, config.data_root)

    def test_configuration_is_immutable_and_does_not_create_data_root(self):
        from comic_sol_web.config import WebConfig

        data_root = Path("/tmp/comic-sol-web-config-test") / "not-created"
        environment = valid_environment(data_root)
        with patch("pathlib.Path.mkdir") as mkdir:
            config = WebConfig.from_env(environment)
        mkdir.assert_not_called()
        self.assertFalse(data_root.exists())
        with self.assertRaises((AttributeError, TypeError)):
            config.data_root = Path("/tmp/other")  # type: ignore[misc]
        self.assertNotIn(SESSION_SECRET, repr(config))
        self.assertNotIn(ENCRYPTION_SECRET, repr(config))


class WebApplicationTests(unittest.TestCase):
    def test_health_does_not_import_provider_or_engine_network_code(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        before = set(sys.modules)
        config = WebConfig.from_env(valid_environment())
        app = create_app(config)
        forbidden = {
            name
            for name in set(sys.modules) - before
            if name.startswith(("scripts", "comic_sol_product", "provider", "providers"))
        }
        self.assertEqual(set(), forbidden)

        with patch("socket.socket.connect", side_effect=AssertionError("network access")):
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_health_is_deterministic(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with TestClient(create_app(WebConfig.from_env(valid_environment()))) as client:
            first = client.get("/healthz")
            second = client.get("/healthz")
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.content, second.content)

    def test_static_mount_is_present(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        app = create_app(WebConfig.from_env(valid_environment()))
        static_routes = [route for route in app.routes if getattr(route, "path", "") == "/static"]
        self.assertEqual(1, len(static_routes))

        # WP1 ships no UI assets. The future surface must still be mounted,
        # and a request for a static asset must return a deterministic 404
        # rather than surfacing Starlette's missing-directory RuntimeError
        # on the first request.
        with TestClient(app) as client:
            missing = client.get("/static/this-asset-does-not-exist")
        self.assertEqual(404, missing.status_code)

    def test_create_app_has_no_startup_background_or_network_work(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with (
            patch("socket.socket.connect", side_effect=AssertionError("network access")),
            patch("asyncio.create_task", side_effect=AssertionError("background task")),
        ):
            app = create_app(WebConfig.from_env(valid_environment()))
        self.assertIsNotNone(app)

    def test_web_distribution_stays_separate_from_root_distribution(self):
        web = Path(__file__).resolve().parents[1]
        root = web.parent
        web_project = (web / "pyproject.toml").read_text(encoding="utf-8")
        root_project = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "comic-sol-web"', web_project)
        self.assertNotIn("FastAPI", root_project)
        self.assertNotIn("fastapi", root_project.lower())


if __name__ == "__main__":
    unittest.main()
