from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from web.tests.support import (
    ENCRYPTION_SECRET,
    SESSION_SECRET,
    make_symlink,
    valid_environment,
)


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

        data_root = Path(tempfile.gettempdir()) / "comic-sol-web-config-test" / "not-created"
        environment = valid_environment(data_root)
        with patch("pathlib.Path.mkdir") as mkdir:
            config = WebConfig.from_env(environment)
        mkdir.assert_not_called()
        self.assertFalse(data_root.exists())
        with self.assertRaises((AttributeError, TypeError)):
            config.data_root = Path("/tmp/other")  # type: ignore[misc]
        self.assertNotIn(SESSION_SECRET, repr(config))
        self.assertNotIn(ENCRYPTION_SECRET, repr(config))


def registered_api_routes(app):
    """Flatten FastAPI's `app.routes`, which on FastAPI >= 0.121 wraps included
    routers in `_IncludedRouter` objects that have no `.path` of their own.

    Yield every concrete route's `(path, methods)` so route-registration
    contract tests remain behavior-focused rather than coupled to a specific
    internal route representation.
    """
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        path = getattr(route, "path", None)
        router = getattr(route, "original_router", None)
        if path is not None and router is None:
            yield path, frozenset(getattr(route, "methods", None) or ())
        if router is not None and hasattr(router, "routes"):
            stack.extend(router.routes)


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
            if name.startswith(("scripts", "comic_sol_product", "PIL", "provider", "providers"))
        }
        self.assertEqual(set(), forbidden)

        # The /healthz handler must not resolve names. Patch getaddrinfo (DNS),
        # which is what any external connect would need, rather than
        # socket.socket.connect: on Windows ProactorEventLoop opens an internal
        # socketpair during TestClient setup, so a blanket connect mock would
        # assert even though the handler itself never connects.
        with patch("socket.getaddrinfo", side_effect=AssertionError("network access")):
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

    def test_project_routes_are_registered_without_creating_application_state(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "not-created"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            routes = {
                (route_path, methods)
                for route_path, methods in registered_api_routes(app)
                if route_path.startswith("/api/projects")
            }
        self.assertEqual(
            {
                ("/api/projects", frozenset({"POST"})),
                ("/api/projects/import", frozenset({"POST"})),
                ("/api/projects/current", frozenset({"GET"})),
                (
                    "/api/projects/{project_id}/accepted-raster/{job_id}",
                    frozenset({"GET"}),
                ),
                ("/api/projects/{project_id}/qa", frozenset({"POST"})),
                ("/api/projects/{project_id}/export", frozenset({"POST"})),
                ("/api/projects/{project_id}", frozenset({"GET"})),
            },
            routes,
        )
        self.assertFalse(data_root.exists())
        self.assertFalse(hasattr(app.state, "projects"))

    def test_generation_routes_are_registered_without_constructing_queue_state(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "not-created"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            routes = {
                (route_path, methods)
                for route_path, methods in registered_api_routes(app)
                if route_path.startswith("/api/generation")
            }
            self.assertEqual(
                {
                    ("/api/generation/options", frozenset({"GET"})),
                    ("/api/generation/recommendations", frozenset({"GET"})),
                    ("/api/generation/jobs", frozenset({"GET"})),
                    ("/api/generation/queue", frozenset({"POST"})),
                    ("/api/generation/{job_id}", frozenset({"GET"})),
                    ("/api/generation/{job_id}/retry", frozenset({"POST"})),
                    ("/api/generation/{job_id}/cancel", frozenset({"POST"})),
                    (
                        "/api/generation/{job_id}/pause-for-switch",
                        frozenset({"POST"}),
                    ),
                    (
                        "/api/generation/{job_id}/submit-staged",
                        frozenset({"POST"}),
                    ),
                },
                routes,
            )
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "generation"))
            self.assertFalse(hasattr(app.state, "assets"))

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get(f"/api/generation/{'a' * 64}")
            self.assertEqual(401, response.status_code)
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "generation"))
            self.assertFalse(hasattr(app.state, "assets"))

    def test_asset_and_agent_routes_are_registered_without_constructing_state(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "not-created"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            routes = {
                (route_path, methods)
                for route_path, methods in registered_api_routes(app)
                if route_path.startswith("/api/assets")
            }
            self.assertEqual(
                {
                    ("/api/assets", frozenset({"POST"})),
                    ("/api/assets/{asset_id}", frozenset({"GET"})),
                    (
                        "/api/assets/agent-handoff/{job_id}",
                        frozenset({"GET"}),
                    ),
                    (
                        "/api/assets/{asset_id}/submit-agent",
                        frozenset({"POST"}),
                    ),
                },
                routes,
            )
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "assets"))
            self.assertFalse(hasattr(app.state, "generation"))

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get(f"/api/assets/{'A' * 32}")
            self.assertEqual(401, response.status_code)
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "assets"))
            self.assertFalse(hasattr(app.state, "generation"))

    def test_trusted_agent_capabilities_are_explicit_disabled_and_validated(self):
        from types import SimpleNamespace

        from comic_sol_web.app import _generation_service, create_app
        from comic_sol_web.config import WebConfig
        from comic_sol_web.database import Database

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = WebConfig.from_env(valid_environment(root / "data"))
            disabled = create_app(config)
            self.assertEqual(frozenset(), disabled.state.agent_image_capabilities)

            with self.assertRaises(ValueError):
                create_app(
                    config,
                    active_agent_image_capabilities=frozenset({"not-an-image-capability"}),
                )
            with self.assertRaises(ValueError):
                create_app(
                    config,
                    active_agent_image_capabilities=frozenset({"reference_images"}),
                )

            enabled = create_app(
                config,
                active_agent_image_capabilities=frozenset({"text_to_image", "custom_dimensions"}),
            )
            self.assertEqual(
                frozenset({"text_to_image", "custom_dimensions"}),
                enabled.state.agent_image_capabilities,
            )
            staging_root = root / "staging"
            staging_root.mkdir()
            projects = SimpleNamespace(
                gateway=SimpleNamespace(
                    database=Database(root / "application.sqlite3"),
                    staging_root=staging_root,
                )
            )
            request = SimpleNamespace(app=enabled)
            with patch("comic_sol_web.app._project_service", return_value=projects):
                service = _generation_service(request)
            provider = service._providers.get("agent")
            self.assertEqual(
                frozenset({"text_to_image", "custom_dimensions"}),
                provider.active_capabilities,
            )

    def test_approval_routes_are_registered_without_constructing_proposal_state(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "not-created"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            routes = {
                (route_path, methods)
                for route_path, methods in registered_api_routes(app)
                if route_path.startswith("/api/approvals")
            }
            self.assertEqual(
                {
                    (
                        "/api/approvals/{proposal_id}/approve",
                        frozenset({"POST"}),
                    ),
                    (
                        "/api/approvals/{proposal_id}/reject",
                        frozenset({"POST"}),
                    ),
                },
                routes,
            )
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "approvals"))

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.post(
                    "/api/approvals/not-a-proposal/approve",
                    json={},
                )
            self.assertEqual(401, response.status_code)
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "approvals"))

    def test_health_remains_isolated_after_approval_route_registration(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "approval-health-must-not-create"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            with (
                patch(
                    "comic_sol_web.database.Database._connect",
                    side_effect=AssertionError("database access"),
                ),
                patch("pathlib.Path.mkdir", side_effect=AssertionError("filesystem write")),
                patch("socket.getaddrinfo", side_effect=AssertionError("network access")),
                patch("asyncio.create_task", side_effect=AssertionError("background task")),
            ):
                with TestClient(app) as client:
                    response = client.get("/healthz")
            self.assertEqual(200, response.status_code)
            self.assertEqual({"status": "ok"}, response.json())
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "approvals"))

    def test_application_does_not_enable_the_test_fake_provider(self):
        from types import SimpleNamespace

        from comic_sol_web.app import _generation_service
        from comic_sol_web.auth import SessionPrincipal
        from comic_sol_web.config import WebConfig
        from comic_sol_web.database import Database
        from comic_sol_web.generation.types import AuthMode

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging_root = root / "staging"
            staging_root.mkdir()
            config = WebConfig.from_env(valid_environment(root / "data"))
            projects = SimpleNamespace(
                gateway=SimpleNamespace(
                    database=Database(root / "application.sqlite3"),
                    staging_root=staging_root,
                )
            )
            state = SimpleNamespace(
                web_config=config,
                agent_image_capabilities=frozenset(),
            )
            request = SimpleNamespace(app=SimpleNamespace(state=state))
            with patch("comic_sol_web.app._project_service", return_value=projects):
                service = _generation_service(request)

            self.assertEqual("agent", service._providers.get("agent").provider_id)
            with self.assertRaisesRegex(ValueError, "not currently executable"):
                service.queue(
                    SessionPrincipal("owner-id", "owner"),
                    "project-id",
                    1,
                    provider="fake",
                    model="fake-raster-v1",
                    auth_mode=AuthMode.AGENT,
                )

    def test_health_remains_database_filesystem_and_background_free_with_generation_routes(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "health-must-not-create"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            with (
                patch(
                    "comic_sol_web.database.Database._connect",
                    side_effect=AssertionError("database access"),
                ),
                patch("pathlib.Path.mkdir", side_effect=AssertionError("filesystem write")),
                patch("socket.getaddrinfo", side_effect=AssertionError("network access")),
            ):
                with TestClient(app) as client:
                    response = client.get("/healthz")
            self.assertEqual(200, response.status_code)
            self.assertEqual({"status": "ok"}, response.json())
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "generation"))
            self.assertFalse(hasattr(app.state, "assets"))

    def test_anonymous_project_request_fails_before_lazy_storage_initialization(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "anonymous-data"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            with TestClient(app) as client:
                response = client.get(f"/api/projects/{'A' * 32}")
            self.assertEqual(401, response.status_code)
            self.assertEqual({"detail": "authentication required"}, response.json())
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "projects"))

    def test_project_endpoint_rejects_a_symlinked_data_root_parent(self):
        from comic_sol_web.app import create_app
        from comic_sol_web.auth import SessionPrincipal, require_principal
        from comic_sol_web.config import WebConfig
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            external = base / "external"
            external.mkdir()
            alias = base / "alias"
            make_symlink(self, alias, external, directory=True)
            data_root = alias / "web-data"
            environment = valid_environment(base / "ordinary-data")
            environment["COMIC_SOL_WEB_DATA_ROOT"] = str(data_root)
            app = create_app(WebConfig.from_env(environment))
            app.dependency_overrides[require_principal] = lambda: SessionPrincipal(
                "linked-user", "linked"
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(f"/api/projects/{'A' * 32}")
            self.assertEqual(400, response.status_code)
            self.assertEqual({"detail": "project request rejected"}, response.json())
            self.assertFalse((external / "web-data").exists())
            self.assertFalse(hasattr(app.state, "projects"))

    def test_project_endpoint_constructs_the_real_gateway_lazily(self):
        from fastapi.testclient import TestClient

        from comic_sol_web.app import create_app
        from comic_sol_web.auth import SessionPrincipal, require_principal
        from comic_sol_web.config import WebConfig

        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "lazy-data"
            app = create_app(WebConfig.from_env(valid_environment(data_root)))
            app.dependency_overrides[require_principal] = lambda: SessionPrincipal(
                "lazy-user", "lazy"
            )
            self.assertFalse(data_root.exists())
            self.assertFalse(hasattr(app.state, "projects"))
            with TestClient(app) as client:
                response = client.get("/api/projects/not-an-opaque-project-id")
            self.assertEqual(404, response.status_code)
            self.assertEqual({"detail": "project unavailable"}, response.json())
            self.assertTrue(data_root.is_dir())
            self.assertEqual(
                "ProjectService",
                app.state.projects.__class__.__name__,
            )
            from comic_sol_web import engine_gateway

            self.assertIs(
                engine_gateway.EngineGateway,
                app.state.projects.gateway.__class__,
            )
            self.assertEqual(
                "scripts.comic_sol",
                engine_gateway.comic_sol.__name__,
            )

    def test_web_declares_the_exact_canonical_root_engine_version(self):
        from comic_sol_product import __version__ as root_version

        web_project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        metadata = tomllib.loads(web_project.read_text(encoding="utf-8"))["project"]
        self.assertIn(f"comic-sol=={root_version}", metadata["dependencies"])

    def test_clean_installed_wheels_resolve_the_bundled_engine_outside_checkout(self):
        code = """
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from fastapi.testclient import TestClient
from comic_sol_web.app import create_app
from comic_sol_web.config import WebConfig
with TemporaryDirectory() as temporary:
    root = Path(temporary) / "data"
    config = WebConfig(
        session_secret="s" * 32,
        encryption_secret="e" * 32,
        data_root=root,
        hosted_secret_references={},
        master_key_references={},
        active_credential_key_id=None,
    )
    before = set(sys.modules)
    app = create_app(config)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not root.exists()
    forbidden = {
        name for name in set(sys.modules) - before
        if name.startswith(("scripts", "comic_sol_product", "PIL", "provider", "providers"))
    }
    assert forbidden == set(), forbidden
import PIL
import comic_sol_web.engine_gateway as gateway
import comic_sol_product
from comic_sol_product.cli import _load_engine_module
engine = _load_engine_module("comic_sol")
assert engine.__name__ == "comic_sol_product.engine.comic_sol"
assert gateway.comic_sol is engine
assert version("comic-sol") == comic_sol_product.__version__
assert version("comic-sol-web") == "0.1.0"
assert "site-packages" in Path(engine.__file__).as_posix()
print(engine.__name__)
"""
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", code],
                cwd=temporary,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(
            0,
            completed.returncode,
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}",
        )
        self.assertEqual(
            "comic_sol_product.engine.comic_sol",
            completed.stdout.strip(),
        )

    def test_web_ci_builds_and_installs_local_root_before_the_web_wheel(self):
        workflow = (
            Path(__file__).resolve().parents[2] / ".github/workflows/web-tests.yml"
        ).read_text(encoding="utf-8")
        required = (
            "requirements/locks/web-${{ matrix.lock }}-x86_64.txt",
            "requirements/locks/base-${{ matrix.lock }}-x86_64.txt",
            "python -m build --no-isolation --wheel -o dist",
            "python -m pip install --no-deps --force-reinstall dist/comic_sol-*.whl",
            "python -m build --no-isolation --wheel -o dist",
            "python -m pip install --no-deps --force-reinstall dist/comic_sol_web-*.whl",
            'python -m unittest discover -s web/tests -p "test_*.py" -v',
            "python -m unittest web.tests.test_projects -v",
        )
        cursor = 0
        for expected in required:
            with self.subTest(expected=expected):
                cursor = workflow.index(expected, cursor) + len(expected)

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
