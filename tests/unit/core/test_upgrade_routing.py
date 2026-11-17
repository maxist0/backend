"""App construction across the FastAPI 0.136 -> 0.163 upgrade.

* 0.142/0.143: native OpenTelemetry, explicitly disabled in `create_app`.
* 0.145/0.146/0.155: removed `**extra` kwargs, deprecated `root_path`.
"""

import warnings

import pytest
from fastapi import FastAPI
from fastapi.exceptions import FastAPIDeprecationWarning
from opentelemetry import _logs as otel_logs

from app.main import create_app
from tests.helpers import new_test_client


class _RecordingLogger(otel_logs.Logger):
    def __init__(self, records: list) -> None:
        self.records = records

    def enabled(self, *args, **kwargs) -> bool:
        return True

    def emit(self, record=None, **kwargs) -> None:
        self.records.append(record or kwargs)


class _RecordingLoggerProvider(otel_logs.LoggerProvider):
    """A configured provider built on the OpenTelemetry API only (no SDK)."""

    def __init__(self) -> None:
        self.records: list = []

    def get_logger(self, name, version=None, schema_url=None, attributes=None):
        return _RecordingLogger(self.records)


class TestNativeTelemetryIsOff:
    def test_no_fastapi_otel_log_records_even_with_a_configured_provider(
        self, monkeypatch, settings_factory
    ):
        """0.142: FastAPI emits its own OTel records once a logger provider is set.

        It would export e.g. `fastapi.validation.failed` and unhandled
        exceptions with message and stack trace. `create_app` disables native
        telemetry, so nothing is emitted.
        """
        provider = _RecordingLoggerProvider()
        # What set_logger_provider() does, but undone after the test (the
        # public setter works only once per process).
        monkeypatch.setattr("opentelemetry._logs._internal._LOGGER_PROVIDER", provider)
        app = create_app(settings_factory())

        r = new_test_client(app).post("/api/v1/auth/login", data={})

        assert r.status_code == 422
        assert provider.records == []


class TestAppConstruction:
    def test_create_app_emits_no_warning_and_sets_no_root_path(self, settings_factory):
        """0.145/0.146/0.155: no removed ``**extra`` kwargs, no ``root_path``.

        ``FastAPI(root_path=...)`` is deprecated since 0.146 (use the server's
        ``--root-path``); removed kwargs raise ``TypeError`` at construction.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app = create_app(settings_factory())

        assert [str(w.message) for w in caught] == []
        assert app.root_path == ""

    def test_root_path_argument_is_deprecated(self):
        """GUARD for the test above: the deprecation is real in this version."""
        with pytest.warns(FastAPIDeprecationWarning, match="root_path"):
            FastAPI(root_path="/api")
