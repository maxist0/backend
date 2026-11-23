"""The admin login is rate limited per client address before any compare."""

import hmac
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.helpers import SAME_ORIGIN, log_events

ATTEMPTS = 10


def _admin_login(client: TestClient, password: str) -> int:
    r = client.post(
        "/admin/login",
        data={"username": get_settings().ADMIN_USERNAME, "password": password},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    return r.status_code


def test_the_eleventh_attempt_is_refused_without_a_compare(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    compares: list[None] = []
    original = hmac.compare_digest

    def counting_compare(a: bytes, b: bytes) -> bool:
        compares.append(None)
        return original(a, b)

    monkeypatch.setattr("hmac.compare_digest", counting_compare)
    caplog.set_level(logging.INFO)
    failures = {_admin_login(client, "wrong") for _ in range(ATTEMPTS)}
    compared = len(compares)

    # The right password, but the address has used its attempts.
    status = _admin_login(client, get_settings().ADMIN_PASSWORD.get_secret_value())

    assert len(failures) == 1
    assert status in failures
    assert len(compares) == compared
    [event] = log_events(caplog, "admin.login_rate_limited")
    assert event["client_ip"] == "testclient"
