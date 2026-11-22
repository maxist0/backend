"""Admin panel actions: each one calls an auth service function.

Approach: the real sqladmin app is driven over HTTP (POST /admin/login,
then GET /admin/<identity>/action/<slug>?pks=<ids>), the same requests the
browser sends. This proves the wiring (query parameter name, auth, redirect)
and not only the service body. It is practical because the admin
credentials are plain settings and the admin session is a signed cookie in the
TestClient jar.

Every action must change state, write one audit row naming the admin, and do
both in one transaction per user (or session).

The admin is opt-in outside dev (`ADMIN_ENABLED`); `tests/conftest.py` sets
ADMIN_ENABLED=true for the test process.

The panel refuses state changes (login, actions) unless the browser marks
them `Sec-Fetch-Site: same-origin`; `admin_login` makes the client send that
header from then on, like a browser on the panel's own pages.

Note: the admin session and the API refresh cookie share one cookie jar, and the
`login_tokens` / `refresh_with` helpers clear it. Tests therefore create their
user sessions first and call `admin_login(client)` last.
"""

import logging
import uuid
from typing import Any, Never

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    PASSWORD,
    SAME_ORIGIN,
    Db,
    active_session_count,
    admin_login,
    bearer,
    log_events,
    login,
    login_tokens,
    refresh_with,
    register_ok,
    run_action,
    user_id_of,
)

# Every test here registers users; record their mails instead of skipping them.
pytestmark = pytest.mark.usefixtures("captured_emails")


@pytest.fixture
def actor() -> str:
    from app.core.config import get_settings

    return get_settings().ADMIN_USERNAME


def fail_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.modules.auth.crud.create_audit_log", _fail)


def audit_rows(db: Db, event: str) -> list[dict[str, Any]]:
    """Audit rows of one event type (the enum label, e.g. `ACCOUNT_LOCKED`).

    Any: the values are the columns `user_id` and `detail` as the driver
    returns them.
    """
    return [
        dict(row)
        for row in db.rows(
            "SELECT user_id::text AS user_id, detail FROM auth_audit_log "
            "WHERE event_type::text = :e ORDER BY created_at",
            e=event,
        )
    ]


class TestUserActions:
    def test_lock_locks_selected_users_indefinitely_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        for name in ("alice", "bob", "carol"):
            register_ok(client, username=name)
        ids = [user_id_of(db, "alice"), user_id_of(db, "bob")]
        admin_login(client)

        r = run_action(client, "user", "lock-users", ids)

        assert r.status_code == 303, r.text
        rows = {
            row["username"]: row
            for row in db.rows("SELECT username, is_locked, locked_until FROM users")
        }
        assert rows["alice"]["is_locked"]
        assert rows["alice"]["locked_until"] is None
        assert rows["bob"]["is_locked"]
        assert rows["bob"]["locked_until"] is None
        assert not rows["carol"]["is_locked"], "unselected user must be untouched"
        audits = audit_rows(db, "ACCOUNT_LOCKED")
        assert sorted(a["user_id"] for a in audits) == sorted(ids)
        assert all(f"by admin {actor}" in a["detail"] for a in audits)
        assert (
            db.scalar(
                "SELECT DISTINCT ip_address FROM auth_audit_log "
                "WHERE event_type = 'ACCOUNT_LOCKED'"
            )
            == "testclient"
        )

    def test_lock_ends_sessions_refresh_and_access_tokens(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        assert active_session_count(db, "alice") == 1
        admin_login(client)

        run_action(client, "user", "lock-users", [user_id_of(db, "alice")])

        assert active_session_count(db, "alice") == 0
        assert db.scalar("SELECT revoke_reason FROM sessions") == "admin_lock"
        assert "revoked 1 session(s)" in audit_rows(db, "ACCOUNT_LOCKED")[0]["detail"]
        assert refresh_with(client, t.refresh)[0].status_code == 401
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 401
        )

    def test_lock_turns_a_timed_lock_into_an_indefinite_one(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '1 hour'"
        )
        admin_login(client)

        run_action(client, "user", "lock-users", [user_id_of(db, "alice")])

        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is None

    def test_admin_locked_user_cannot_log_in_with_correct_password(
        self, client: TestClient, db: Db
    ) -> None:
        """End to end: admin lock, then a login attempt; the lock stays."""
        register_ok(client, username="alice")
        admin_login(client)
        run_action(client, "user", "lock-users", [user_id_of(db, "alice")])

        r = login(client, username="alice")

        assert r.status_code == 401
        assert db.scalar("SELECT is_locked FROM users") is True

    def test_unlock_clears_lock_and_failed_attempts_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() + interval '1 hour'"
        )
        admin_login(client)

        run_action(client, "user", "unlock-users", [user_id_of(db, "alice")])

        row = db.one("SELECT is_locked, failed_login_attempts, locked_until FROM users")
        assert row["is_locked"] is False
        assert row["failed_login_attempts"] == 0
        assert row["locked_until"] is None
        assert audit_rows(db, "ACCOUNT_UNLOCKED") == [
            {"user_id": user_id_of(db, "alice"), "detail": f"Unlocked by admin {actor}"}
        ]

    def test_deactivate_deactivates_ends_sessions_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        admin_login(client)

        run_action(client, "user", "deactivate-users", [user_id_of(db, "alice")])

        assert db.scalar("SELECT is_active FROM users") is False
        assert active_session_count(db, "alice") == 0
        assert db.scalar("SELECT revoke_reason FROM sessions") == "admin_deactivate"
        assert audit_rows(db, "ACCOUNT_DEACTIVATED") == [
            {
                "user_id": user_id_of(db, "alice"),
                "detail": f"Deactivated by admin {actor}, revoked 1 session(s)",
            }
        ]
        assert refresh_with(client, t.refresh)[0].status_code == 401
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 401
        )

    def test_reactivate_lets_the_user_log_in_again_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        register_ok(client, username="alice")
        db.execute("UPDATE users SET is_active = false")
        assert login(client, username="alice").status_code == 401
        admin_login(client)

        run_action(client, "user", "reactivate-users", [user_id_of(db, "alice")])

        assert db.scalar("SELECT is_active FROM users") is True
        assert audit_rows(db, "ACCOUNT_REACTIVATED") == [
            {
                "user_id": user_id_of(db, "alice"),
                "detail": f"Reactivated by admin {actor}",
            }
        ]
        assert login(client, username="alice", password=PASSWORD).status_code == 200

    def test_reactivate_keeps_an_admin_lock(self, client: TestClient, db: Db) -> None:
        register_ok(client, username="alice")
        db.execute("UPDATE users SET is_active = false, is_locked = true")
        admin_login(client)

        run_action(client, "user", "reactivate-users", [user_id_of(db, "alice")])

        row = db.one("SELECT is_active, is_locked, locked_until FROM users")
        assert row["is_active"] is True
        assert row["is_locked"] is True
        assert row["locked_until"] is None

    def test_force_password_change_sets_must_change_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        register_ok(client, username="alice")
        admin_login(client)

        run_action(client, "user", "force-password-change", [user_id_of(db, "alice")])

        assert db.scalar("SELECT must_change FROM password_credentials") is True
        assert audit_rows(db, "PASSWORD_CHANGE_FORCED") == [
            {
                "user_id": user_id_of(db, "alice"),
                "detail": f"Password change forced by admin {actor}",
            }
        ]

    def test_force_password_change_without_credential_changes_nothing(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        db.execute("DELETE FROM password_credentials")
        admin_login(client)

        r = run_action(
            client, "user", "force-password-change", [user_id_of(db, "alice")]
        )

        assert r.status_code == 303
        assert audit_rows(db, "PASSWORD_CHANGE_FORCED") == []

    @pytest.mark.parametrize(
        ("slug", "event"),
        [
            ("lock-users", "ACCOUNT_LOCKED"),
            ("unlock-users", "ACCOUNT_UNLOCKED"),
            ("deactivate-users", "ACCOUNT_DEACTIVATED"),
            ("reactivate-users", "ACCOUNT_REACTIVATED"),
            ("force-password-change", "PASSWORD_CHANGE_FORCED"),
        ],
    )
    def test_a_user_that_no_longer_exists_is_skipped(
        self, client: TestClient, db: Db, slug: str, event: str
    ) -> None:
        register_ok(client, username="alice")
        alice = user_id_of(db, "alice")
        admin_login(client)

        r = run_action(client, "user", slug, [str(uuid.uuid4()), alice])

        assert r.status_code == 303
        assert [a["user_id"] for a in audit_rows(db, event)] == [alice]


class TestSessionActions:
    def test_revoke_revokes_only_the_selected_session_and_audits(
        self, client: TestClient, db: Db, actor: str
    ) -> None:
        register_ok(client, username="alice")
        first = login_tokens(client, "alice")
        second = login_tokens(client, "alice")
        first_id = str(db.scalar("SELECT id FROM sessions ORDER BY created_at LIMIT 1"))
        admin_login(client)

        run_action(client, "session", "revoke-sessions", [first_id])

        row = db.one(
            "SELECT is_revoked, revoke_reason, revoked_at FROM sessions WHERE id = :i",
            i=first_id,
        )
        assert row["is_revoked"] is True
        assert row["revoke_reason"] == "admin"
        assert row["revoked_at"] is not None
        assert active_session_count(db, "alice") == 1
        assert audit_rows(db, "SESSION_REVOKED") == [
            {
                "user_id": user_id_of(db, "alice"),
                "detail": f"Session {first_id} revoked by admin {actor}",
            }
        ]
        assert refresh_with(client, first.refresh)[0].status_code == 401
        assert refresh_with(client, second.refresh)[0].status_code == 200

    def test_revoking_a_revoked_session_keeps_its_reason_and_audits_nothing(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        db.execute(
            "UPDATE sessions SET is_revoked = true, revoke_reason = 'reuse_detected', "
            "revoked_at = now()"
        )
        session_id = str(db.scalar("SELECT id FROM sessions"))
        admin_login(client)

        r = run_action(client, "session", "revoke-sessions", [session_id])

        assert r.status_code == 303
        assert db.scalar("SELECT revoke_reason FROM sessions") == "reuse_detected"
        assert audit_rows(db, "SESSION_REVOKED") == []

    def test_a_session_that_no_longer_exists_is_skipped(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        session_id = str(db.scalar("SELECT id FROM sessions"))
        admin_login(client)

        r = run_action(
            client, "session", "revoke-sessions", [str(uuid.uuid4()), session_id]
        )

        assert r.status_code == 303
        assert active_session_count(db, "alice") == 0
        assert len(audit_rows(db, "SESSION_REVOKED")) == 1


class TestOneTransactionPerAction:
    """A failure after the state change must leave nothing behind.

    The audit write is the last statement before the commit. Making it fail
    (the only patch) proves that the user update and the session revocation,
    already executed in the database, are rolled back with it.
    """

    @pytest.mark.parametrize(
        ("slug", "column", "unchanged"),
        [("lock-users", "is_locked", False), ("deactivate-users", "is_active", True)],
    )
    def test_user_action_persists_nothing_when_the_audit_fails(
        self,
        client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        slug: str,
        column: str,
        *,
        unchanged: bool,
    ) -> None:
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        admin_login(client)
        fail_audit_writes(monkeypatch)

        r = run_action(client, "user", slug, [user_id_of(db, "alice")])

        assert r.status_code == 500
        assert db.scalar(f"SELECT {column} FROM users") is unchanged
        assert active_session_count(db, "alice") == 1
        assert (
            db.scalar(
                "SELECT count(*) FROM auth_audit_log WHERE detail LIKE '%by admin%'"
            )
            == 0
        )

    def test_session_revoke_persists_nothing_when_the_audit_fails(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        session_id = str(db.scalar("SELECT id FROM sessions"))
        admin_login(client)
        fail_audit_writes(monkeypatch)

        r = run_action(client, "session", "revoke-sessions", [session_id])

        assert r.status_code == 500
        assert active_session_count(db, "alice") == 1


class TestActionSecurity:
    def test_admin_session_cookie_is_strict_and_expires(
        self, client: TestClient
    ) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        failed = client.post(
            "/admin/login",
            data={"username": "nobody", "password": "wrong"},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )

        r = client.post(
            "/admin/login",
            data={
                "username": settings.ADMIN_USERNAME,
                "password": settings.ADMIN_PASSWORD.get_secret_value(),
            },
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )

        assert "set-cookie" not in failed.headers
        assert r.status_code == 302
        attributes = [
            part.strip().lower() for part in r.headers["set-cookie"].split(";")[1:]
        ]
        assert "samesite=strict" in attributes
        assert "httponly" in attributes
        assert f"max-age={8 * 60 * 60}" in attributes

    def test_cross_site_action_is_rejected_without_effect(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        admin_login(client)

        r = run_action(
            client,
            "user",
            "lock-users",
            [user_id_of(db, "alice")],
            headers={"Sec-Fetch-Site": "cross-site"},
        )

        assert r.status_code == 403
        assert db.scalar("SELECT is_locked FROM users") is False
        assert audit_rows(db, "ACCOUNT_LOCKED") == []

    def test_action_without_fetch_metadata_is_rejected(
        self, client: TestClient, db: Db
    ) -> None:
        """Fail safe: a client that does not say where it comes from is refused."""
        register_ok(client, username="alice")
        admin_login(client)
        del client.headers["Sec-Fetch-Site"]

        r = run_action(client, "user", "lock-users", [user_id_of(db, "alice")])

        assert r.status_code == 403
        assert db.scalar("SELECT is_locked FROM users") is False

    def test_admin_logins_are_logged_without_the_typed_username(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        from app.core.config import get_settings

        caplog.set_level(logging.INFO)
        client.post(
            "/admin/login",
            data={"username": "typed-name-91", "password": "wrong"},
            headers=SAME_ORIGIN,
            follow_redirects=False,
        )
        admin_login(client)

        events = {
            e["event"]: e
            for e in log_events(caplog)
            if e["event"].startswith("admin.login")
        }
        assert events["admin.login_failed"]["client_ip"] == "testclient"
        assert "typed-name-91" not in str(events["admin.login_failed"])
        assert events["admin.login_succeeded"]["client_ip"] == "testclient"
        assert events["admin.login_succeeded"]["admin"] == get_settings().ADMIN_USERNAME

    def test_same_origin_action_runs(self, client: TestClient, db: Db) -> None:
        register_ok(client, username="alice")
        admin_login(client)

        r = run_action(
            client,
            "user",
            "lock-users",
            [user_id_of(db, "alice")],
            headers={"Sec-Fetch-Site": "same-origin"},
        )

        assert r.status_code == 303
        assert db.scalar("SELECT is_locked FROM users") is True

    def test_cross_site_login_is_rejected(self, client: TestClient) -> None:
        from app.core.config import get_settings

        settings = get_settings()

        r = client.post(
            "/admin/login",
            data={
                "username": settings.ADMIN_USERNAME,
                "password": settings.ADMIN_PASSWORD.get_secret_value(),
            },
            headers={"Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )

        assert r.status_code == 403
        assert "set-cookie" not in r.headers

    def test_action_without_admin_login_changes_nothing(
        self, client: TestClient, db: Db
    ) -> None:
        """Unauthenticated callers are redirected to login, no effect."""
        register_ok(client, username="alice")
        client.cookies.clear()

        r = run_action(
            client, "user", "lock-users", [user_id_of(db, "alice")], headers=SAME_ORIGIN
        )

        assert r.status_code == 302
        assert r.headers["location"].endswith("/admin/login")
        assert db.scalar("SELECT is_locked FROM users") is False
        assert audit_rows(db, "ACCOUNT_LOCKED") == []

    def test_action_redirect_ignores_referer_header(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        admin_login(client)

        r = run_action(
            client,
            "user",
            "lock-users",
            [user_id_of(db, "alice")],
            headers={"Referer": "https://evil.example/phish"},
        )

        assert r.status_code in (302, 303)
        location = r.headers["location"]
        assert "evil.example" not in location
        assert "/admin" in location

    def test_action_with_garbage_pks_does_not_crash(self, client: TestClient) -> None:
        """Malformed ids must not surface as an unhandled server error."""
        admin_login(client)
        try:
            r = run_action(client, "user", "lock-users", ["not-a-uuid"])
        except Exception as exc:  # noqa: BLE001 - TestClient re-raises app errors
            pytest.fail(f"server raised {type(exc).__name__}")
        assert r.status_code < 500
