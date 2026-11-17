"""`create_app()` assembly and the import-time contract of `app.main`."""

import os
import subprocess
import sys

from fastapi import APIRouter
from fastapi.routing import iter_route_contexts

import app.main as main
from app.core import Module
from app.core.config import Settings
from app.modules import MODULES
from tests.helpers import BACKEND_DIR

STRONG = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
}


class TestImport:
    def test_import_without_environment_fails_clearly(self):
        """No silent fallback to dev, even next to a .env.local."""
        env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}

        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0
        assert "ConfigurationError: ENVIRONMENT is not set" in proc.stderr


class TestCreateApp:
    def test_explicit_settings_drive_the_assembly(self, settings_factory):
        settings = settings_factory(APP_NAME="factory-app", METRICS_ENABLED=False)

        app = main.create_app(settings)

        assert app.title == "factory-app"
        assert app.state.settings is settings
        # FastAPI >= 0.137 keeps included routers as a tree in app.routes;
        # iter_route_contexts() flattens it with the full (prefixed) paths.
        paths = {context.path for context in iter_route_contexts(app.routes)}
        assert {"/", "/health", "/api/v1/auth/login", "/api/v1/users/me"} <= paths
        assert "/metrics" not in paths
        assert "/admin" not in paths

    def test_without_arguments_uses_get_settings(self, monkeypatch, settings_factory):
        settings = settings_factory(APP_NAME="from-get-settings")
        monkeypatch.setattr(main, "get_settings", lambda: settings)

        app = main.create_app()

        assert app.state.settings is settings
        assert app.title == "from-get-settings"

    def test_production_hides_the_api_docs(self):
        settings = Settings(_env_file=None, ENVIRONMENT="prod", **STRONG)

        app = main.create_app(settings)

        assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)

    def test_create_app_performs_no_io(self, monkeypatch, settings_factory):
        """Unreachable DB and Redis: assembling the app must still work."""
        settings = settings_factory(
            DATABASE_URL="postgresql+psycopg://u:p@127.0.0.1:1/none",
            CACHE_PORT=1,
        )

        app = main.create_app(settings)

        assert app.state.session_factory.kw.get("bind") is None


def _paths(routes) -> set[str]:
    return {context.path for context in iter_route_contexts(routes)}


class TestModuleMounting:
    def test_every_route_of_every_module_is_mounted(self, settings_factory):
        app = main.create_app(settings_factory())

        mounted = _paths(app.routes)

        for module in MODULES:
            module_paths = {f"/api/v1{path}" for path in _paths(module.router.routes)}
            assert module_paths, f"module {module.name} has no routes"
            assert module_paths <= mounted, module.name

    def test_a_registered_module_is_mounted_without_core_changes(
        self, monkeypatch, settings_factory
    ):
        """A new entry in `MODULES` is all it takes; it is mounted under the
        API prefix."""
        router = APIRouter(prefix="/probe-items", tags=["probe"])

        @router.get("/")
        async def list_probe_items() -> list[str]:
            return []

        probe = Module(name="probe", router=router)
        monkeypatch.setattr(main, "MODULES", (*MODULES, probe))

        app = main.create_app(settings_factory())

        assert "/api/v1/probe-items/" in _paths(app.routes)

    def test_module_routers_follow_the_api_prefix_setting(self, settings_factory):
        app = main.create_app(settings_factory(API_PREFIX="/api/v2"))

        paths = _paths(app.routes)

        assert "/api/v2/users/me" in paths
        assert not any(path.startswith("/api/v1") for path in paths)
        assert {"/", "/metrics", "/health", "/health/ready"} <= paths
