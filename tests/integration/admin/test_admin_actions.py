"""Admin panel actions: lock, unlock, deactivate, revoke and invalidate.

Approach: the real sqladmin app is driven over HTTP (POST /admin/login,
then GET /admin/<identity>/action/<slug>?pks=<ids>), the same requests the
browser sends. This proves the wiring (query parameter name, auth, redirect)
and not only the handler body. A direct call of the action functions with a
fake Request would have missed the original bug (the actions read the form
field `pk`, sqladmin sends `?pks=`). It is practical because the admin
credentials are plain settings and the admin session is a signed cookie in the
TestClient jar.

The admin is opt-in outside dev (`ADMIN_ENABLED`); `tests/conftest.py` sets
ADMIN_ENABLED=true for the test process.

Note: the admin session and the API refresh cookie share one cookie jar, and the
`login_tokens` / `refresh_with` helpers clear it. Tests therefore create their
user sessions first and call `admin_login(client)` last.
"""

import pytest

from tests.helpers import (
    active_session_count,
    login_tokens,
    refresh_with,
    register_ok,
    secret_value,
)


@pytest.fixture(autouse=True)
def _no_real_email_dispatch(captured_emails):
    """Every test here registers users; keep `.kiq` off the real taskiq broker."""
    return captured_emails


def admin_login(client) -> None:
    from app.common.config import get_settings

    settings = get_settings()

    r = client.post(
        "/admin/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": secret_value(settings.ADMIN_PASSWORD),
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, f"admin login failed: {r.status_code} {r.text[:200]}"


def _action(client, identity: str, slug: str, pks: list[str], **kwargs):
    return client.get(
        f"/admin/{identity}/action/{slug}",
        params={"pks": ",".join(pks)},
        follow_redirects=False,
        **kwargs,
    )


def _user_id(db, username: str) -> str:
    return str(db.scalar("SELECT id FROM users WHERE username = :u", u=username))


class TestUserActions:
    def test_lock_action_locks_selected_users_indefinitely(self, client, db):
        register_ok(client, username="alice")
        register_ok(client, username="bob")
        register_ok(client, username="carol")
        ids = [_user_id(db, "alice"), _user_id(db, "bob")]
        admin_login(client)

        r = _action(client, "user", "lock-users", ids)

        assert r.status_code in (302, 303), r.text
        rows = {
            row["username"]: row
            for row in db.rows("SELECT username, is_locked, locked_until FROM users")
        }
        assert rows["alice"]["is_locked"] and rows["alice"]["locked_until"] is None
        assert rows["bob"]["is_locked"] and rows["bob"]["locked_until"] is None
        assert not rows["carol"]["is_locked"], "unselected user must be untouched"

    def test_lock_action_revokes_the_users_sessions(self, client, db):
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        assert active_session_count(db, "alice") == 1
        admin_login(client)

        _action(client, "user", "lock-users", [_user_id(db, "alice")])

        assert active_session_count(db, "alice") == 0
        assert refresh_with(client, t.refresh)[0].status_code == 401

    def test_unlock_action_clears_lock_and_failed_attempts(self, client, db):
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() + interval '1 hour'"
        )
        admin_login(client)

        _action(client, "user", "unlock-users", [_user_id(db, "alice")])

        row = db.one("SELECT is_locked, failed_login_attempts, locked_until FROM users")
        assert row["is_locked"] is False
        assert row["failed_login_attempts"] == 0
        assert row["locked_until"] is None

    def test_deactivate_action_deactivates_user_and_revokes_sessions(self, client, db):
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        admin_login(client)

        _action(client, "user", "deactivate-users", [_user_id(db, "alice")])

        assert db.scalar("SELECT is_active FROM users") is False
        assert active_session_count(db, "alice") == 0
        assert refresh_with(client, t.refresh)[0].status_code == 401

    def test_force_password_change_action_sets_must_change(self, client, db):
        register_ok(client, username="alice")
        admin_login(client)

        _action(client, "user", "force-password-change", [_user_id(db, "alice")])

        assert db.scalar("SELECT must_change FROM password_credentials") is True

    def test_admin_locked_user_cannot_log_in_with_correct_password(self, client, db):
        """End to end: admin lock followed by a login attempt (the lock must
        not lift itself)."""
        register_ok(client, username="alice")
        admin_login(client)
        _action(client, "user", "lock-users", [_user_id(db, "alice")])

        r = client.post(
            "/auth/login", data={"username": "alice", "password": "Password123!"}
        )

        assert r.status_code == 401
        assert db.scalar("SELECT is_locked FROM users") is True


class TestSessionAndTokenActions:
    def test_revoke_action_revokes_selected_sessions(self, client, db):
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        session_id = str(db.scalar("SELECT id FROM sessions"))
        admin_login(client)

        _action(client, "session", "revoke-sessions", [session_id])

        row = db.one("SELECT is_revoked, revoke_reason, revoked_at FROM sessions")
        assert row["is_revoked"] is True
        assert row["revoke_reason"] == "admin"
        assert row["revoked_at"] is not None
        assert refresh_with(client, t.refresh)[0].status_code == 401

    def test_invalidate_action_invalidates_selected_email_tokens(
        self, client, db, captured_emails
    ):
        register_ok(client, username="alice")
        token_id = str(db.scalar("SELECT id FROM email_tokens"))
        raw = captured_emails.latest_token("alice@example.com", "verify")
        admin_login(client)

        _action(client, "email-token", "invalidate-tokens", [token_id])

        assert db.scalar("SELECT invalidated_at FROM email_tokens") is not None
        r = client.post("/auth/email/verify", json={"token": raw})
        assert r.status_code == 400


class TestActionSecurity:
    def test_action_without_admin_login_changes_nothing(self, client, db):
        """GUARD: unauthenticated callers are redirected to login, no effect."""
        register_ok(client, username="alice")
        client.cookies.clear()

        r = _action(client, "user", "lock-users", [_user_id(db, "alice")])

        assert r.status_code in (302, 303, 401, 403)
        assert db.scalar("SELECT is_locked FROM users") is False

    def test_action_redirect_ignores_referer_header(self, client, db):
        register_ok(client, username="alice")
        admin_login(client)

        r = _action(
            client,
            "user",
            "lock-users",
            [_user_id(db, "alice")],
            headers={"Referer": "https://evil.example/phish"},
        )

        assert r.status_code in (302, 303)
        location = r.headers["location"]
        assert "evil.example" not in location
        assert "/admin" in location

    def test_action_with_garbage_pks_does_not_crash(self, client):
        """Malformed ids must not surface as an unhandled server error."""
        admin_login(client)
        try:
            r = _action(client, "user", "lock-users", ["not-a-uuid"])
        except Exception as exc:  # noqa: BLE001 - TestClient re-raises app errors
            pytest.fail(f"server raised {type(exc).__name__}")
        assert r.status_code < 500
