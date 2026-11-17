"""`run.py` starts uvicorn with trusted proxy headers from the settings."""

import sys

import pytest
import uvicorn

from app.core.config import get_settings


def test_uvicorn_trusts_only_the_configured_proxies(monkeypatch, fresh_settings):
    import run

    fresh_settings.setenv("FORWARDED_ALLOW_IPS", "10.0.0.2,10.0.0.3")
    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["run.py", "unittest"])

    run.main()

    [kwargs] = calls
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "10.0.0.2,10.0.0.3"
    assert kwargs["log_config"] is None
    assert get_settings().ENVIRONMENT.value == "unittest"


@pytest.fixture(autouse=True)
def _keep_environment(monkeypatch):
    """`run.main()` writes ENVIRONMENT into os.environ; restore it afterwards."""
    monkeypatch.setenv("ENVIRONMENT", "unittest")
