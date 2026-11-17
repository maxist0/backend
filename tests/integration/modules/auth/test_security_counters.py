"""The auth security counters count each refusal once, by reason."""

import pytest
from prometheus_client import REGISTRY

from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    login,
    login_tokens,
    refresh_with,
    register_ok,
)


def _failures(reason: str) -> float:
    value = REGISTRY.get_sample_value("auth_login_failures_total", {"reason": reason})
    return value or 0.0


def _counter(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


@pytest.fixture
def alice(client, captured_emails):
    register_ok(client, username="alice")


def test_every_reason_is_exported_from_the_start(client):
    body = client.get("/metrics").text

    for reason in ("invalid_password", "unknown_user", "locked", "inactive"):
        assert f'auth_login_failures_total{{reason="{reason}"}}' in body
    assert "auth_lockouts_total" in body
    assert "auth_refresh_token_reuse_total" in body


def test_unknown_user(client):
    before = _failures("unknown_user")

    assert login(client, username="ghost").status_code == 401

    assert _failures("unknown_user") == before + 1


def test_inactive_account(client, alice, db):
    db.execute("UPDATE users SET is_active = false WHERE username = 'alice'")
    before = _failures("inactive")

    assert login(client, username="alice").status_code == 401

    assert _failures("inactive") == before + 1


def test_wrong_passwords_then_lockout_then_locked(client, alice):
    wrong, locks, locked = (
        _failures("invalid_password"),
        _counter("auth_lockouts_total"),
        _failures("locked"),
    )

    for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
        assert (
            login(client, username="alice", password="Wrong-pass1").status_code == 401
        )
    assert login(client, username="alice").status_code == 401

    assert _failures("invalid_password") == wrong + MAXIMUM_LOGIN_ATTEMPTS
    assert _counter("auth_lockouts_total") == locks + 1
    assert _failures("locked") == locked + 1


def test_refresh_token_reuse(client, alice):
    tokens = login_tokens(client, "alice")
    first, _ = refresh_with(client, tokens.refresh)
    assert first.status_code == 200
    before = _counter("auth_refresh_token_reuse_total")

    replay, _ = refresh_with(client, tokens.refresh)

    assert replay.status_code == 401
    assert _counter("auth_refresh_token_reuse_total") == before + 1


def test_a_successful_login_counts_nothing(client, alice):
    before = {
        reason: _failures(reason)
        for reason in ("invalid_password", "unknown_user", "locked", "inactive")
    }

    assert login(client, username="alice").status_code == 200

    assert {reason: _failures(reason) for reason in before} == before
