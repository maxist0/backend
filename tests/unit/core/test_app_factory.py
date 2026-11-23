"""`create_app()` assembly and the import-time contract of `app.main`."""

import os
import subprocess
import sys
import warnings
from collections.abc import Sequence
from typing import Any, override

import pytest
from fastapi import APIRouter
from fastapi.routing import iter_route_contexts
from opentelemetry import _logs as otel_logs
from opentelemetry.context import Context
from opentelemetry.util.types import AnyValue, Attributes
from starlette.routing import BaseRoute

from app import main
from app.core import Module, rate_limiting
from app.core.config import Environment, Settings
from app.modules import MODULES
from tests.helpers import (
    BACKEND_DIR,
    SettingsFactory,
    new_test_client,
)

# Any: `Settings` converts these strings itself (SecretStr, URL checks).
STRONG: dict[str, Any] = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
    "FRONTEND_URL": "https://app.example.com",
}


class TestImport:
    def test_import_without_environment_fails_clearly(self) -> None:
        """No silent fallback to dev, even next to a .env.local."""
        env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}

        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert proc.returncode != 0
        assert "ConfigurationError: ENVIRONMENT is not set" in proc.stderr


class TestCreateApp:
    def test_explicit_settings_drive_the_assembly(
        self, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(APP_NAME="factory-app", METRICS_ENABLED=False)

        app = main.create_app(settings)

        assert app.title == "factory-app"
        assert app.state.settings is settings
        # `app.routes` keeps included routers as a tree; iter_route_contexts()
        # flattens it with the full (prefixed) paths.
        paths = {context.path for context in iter_route_contexts(app.routes)}
        assert {"/", "/health", "/api/v1/auth/login", "/api/v1/users/me"} <= paths
        assert "/metrics" not in paths
        assert "/admin" not in paths

    def test_without_arguments_uses_get_settings(
        self, monkeypatch: pytest.MonkeyPatch, settings_factory: SettingsFactory
    ) -> None:
        settings = settings_factory(APP_NAME="from-get-settings")
        monkeypatch.setattr(main, "get_settings", lambda: settings)

        app = main.create_app()

        assert app.state.settings is settings
        assert app.title == "from-get-settings"

    def test_production_hides_the_api_docs(self) -> None:
        settings = Settings(ENVIRONMENT=Environment.PROD, **STRONG)

        app = main.create_app(settings)

        assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)

    def test_create_app_performs_no_io(self, settings_factory: SettingsFactory) -> None:
        """Unreachable DB and Redis: assembling the app must still work."""
        settings = settings_factory(
            DATABASE_URL="postgresql+psycopg://u:p@127.0.0.1:1/none",
            CACHE_PORT=1,
        )

        app = main.create_app(settings)

        assert app.state.session_factory.kw.get("bind") is None

    def test_create_app_emits_no_warning_and_sets_no_root_path(
        self, settings_factory: SettingsFactory
    ) -> None:
        """No deprecated or removed `FastAPI` arguments.

        `FastAPI(root_path=...)` is deprecated (the server's `--root-path` is
        the way); removed keyword arguments raise `TypeError`.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app = main.create_app(settings_factory())

        assert [str(w.message) for w in caught] == []
        assert app.root_path == ""


def _paths(routes: Sequence[BaseRoute]) -> set[str]:
    return {
        context.path
        for context in iter_route_contexts(routes)
        if context.path is not None
    }


class TestCors:
    def test_retry_after_the_request_id_and_the_replay_mark_are_exposed(
        self, settings_factory: SettingsFactory
    ) -> None:
        """A browser app on another origin may read them (`Retry-After` on 429)."""
        app = main.create_app(settings_factory())

        r = new_test_client(app).get(
            "/health", headers={"Origin": "http://localhost:5173"}
        )

        exposed = {
            name.strip().lower()
            for name in r.headers["access-control-expose-headers"].split(",")
        }
        assert {"retry-after", "x-request-id", "idempotent-replayed"} <= exposed


class TestModuleMounting:
    def test_every_route_of_every_module_is_mounted(
        self, settings_factory: SettingsFactory
    ) -> None:
        app = main.create_app(settings_factory())

        mounted = _paths(app.routes)

        for module in MODULES:
            module_paths = {f"/api/v1{path}" for path in _paths(module.router.routes)}
            assert module_paths, f"module {module.name} has no routes"
            assert module_paths <= mounted, module.name

    def test_a_registered_module_is_mounted_without_core_changes(
        self, monkeypatch: pytest.MonkeyPatch, settings_factory: SettingsFactory
    ) -> None:
        """A new entry in `MODULES` is all it takes; it is mounted under the prefix."""
        router = APIRouter(prefix="/probe-items", tags=["probe"])

        @router.get("/")
        async def list_probe_items() -> list[str]:
            return []

        probe = Module(name="probe", router=router)
        monkeypatch.setattr(main, "MODULES", (*MODULES, probe))

        app = main.create_app(settings_factory())

        assert "/api/v1/probe-items/" in _paths(app.routes)

    def test_module_routers_follow_the_api_prefix_setting(
        self, settings_factory: SettingsFactory
    ) -> None:
        app = main.create_app(settings_factory(API_PREFIX="/api/v2"))

        paths = _paths(app.routes)

        assert "/api/v2/users/me" in paths
        assert not any(path.startswith("/api/v1") for path in paths)
        assert {"/", "/metrics", "/health", "/health/ready"} <= paths


class _RecordingLogger(otel_logs.Logger):
    def __init__(self, name: str, records: list[object]) -> None:
        super().__init__(name)
        self.records = records

    @override
    def enabled(
        self,
        *,
        context: Context | None = None,
        severity_number: otel_logs.SeverityNumber | None = None,
        event_name: str | None = None,
    ) -> bool:
        return True

    @override
    def emit(
        self,
        record: otel_logs.LogRecord | None = None,
        *,
        timestamp: int | None = None,
        observed_timestamp: int | None = None,
        context: Context | None = None,
        severity_number: otel_logs.SeverityNumber | None = None,
        severity_text: str | None = None,
        body: AnyValue | None = None,
        attributes: Attributes = None,
        event_name: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        self.records.append(record or event_name or body)


class _RecordingLoggerProvider(otel_logs.LoggerProvider):
    """A configured provider built on the OpenTelemetry API only (no SDK)."""

    def __init__(self) -> None:
        self.records: list[object] = []

    @override
    def get_logger(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> otel_logs.Logger:
        return _RecordingLogger(name, self.records)


class TestNativeTelemetryIsOff:
    def test_no_fastapi_otel_log_records_even_with_a_configured_provider(
        self, monkeypatch: pytest.MonkeyPatch, settings_factory: SettingsFactory
    ) -> None:
        """FastAPI emits its own OTel records once a logger provider is set.

        It would export e.g. `fastapi.validation.failed` and unhandled
        exceptions with message and stack trace. `create_app` disables native
        telemetry, so nothing is emitted.
        """
        provider = _RecordingLoggerProvider()
        # What set_logger_provider() does, but undone after the test (the
        # public setter works only once per process).
        monkeypatch.setattr("opentelemetry._logs._internal._LOGGER_PROVIDER", provider)
        # The login's rate limit runs before validation and needs the Redis
        # store, which only a lifespan sets up; a unit test has neither.
        no_limits = settings_factory(RATE_LIMIT_ENABLED=False)
        monkeypatch.setattr(rate_limiting, "get_settings", lambda: no_limits)
        app = main.create_app(settings_factory())

        r = new_test_client(app).post("/api/v1/auth/login", data={})

        assert r.status_code == 422
        assert provider.records == []
