"""Session revocation: password reset, password change, logout-all and `sid`.

The access token carries the claim `sid`, equal to `sessions.family_id` and
stable across rotation, so the calling session can be told apart.
"""

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    NEW_PASSWORD,
    CapturedEmails,
    Db,
    active_session_count,
    audit_count,
    bearer,
    change_password,
    login,
    login_tokens,
    refresh_with,
    register,
    register_ok,
    request_reset,
    reset_password,
)


class TestSessionRevocation:
    @pytest.mark.usefixtures("captured_emails")
    def test_access_token_carries_sid_claim_equal_to_session_family(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        claims = jwt.decode(t0.access, options={"verify_signature": False})
        family = db.scalar("SELECT family_id FROM sessions")
        assert claims.get("sid") == str(family)

    @pytest.mark.usefixtures("captured_emails")
    def test_sid_claim_is_stable_across_rotation(self, client: TestClient) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        _, t1 = refresh_with(client, t0.refresh)
        assert t1
        sid0 = jwt.decode(t0.access, options={"verify_signature": False}).get("sid")
        sid1 = jwt.decode(t1.access, options={"verify_signature": False}).get("sid")
        assert sid0
        assert sid0 == sid1

    def test_password_reset_revokes_every_existing_session(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        a = login_tokens(client, "alice")
        b = login_tokens(client, "alice")
        assert active_session_count(db, "alice") == 2

        assert (
            reset_password(client, request_reset(client, captured_emails)).status_code
            == 204
        )

        assert refresh_with(client, a.refresh)[0].status_code == 401
        assert refresh_with(client, b.refresh)[0].status_code == 401
        assert active_session_count(db, "alice") == 0

    def test_password_reset_still_allows_login_with_new_password(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        """Revoking sessions must not break the new credentials."""
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        reset_password(
            client, request_reset(client, captured_emails), "NewPassword456!"
        )
        r = login(client, username="alice", password=NEW_PASSWORD)
        assert r.status_code == 200

    @pytest.mark.usefixtures("captured_emails")
    def test_change_password_revokes_other_sessions_but_keeps_calling_one(
        self, client: TestClient
    ) -> None:
        register_ok(client, username="alice")
        current = login_tokens(client, "alice")
        other = login_tokens(client, "alice")

        r = change_password(client, current.access)
        assert r.status_code == 204

        assert refresh_with(client, other.refresh)[0].status_code == 401
        assert refresh_with(client, current.refresh)[0].status_code == 200

    @pytest.mark.usefixtures("captured_emails")
    def test_change_password_keeps_session_that_was_rotated_before(
        self, client: TestClient
    ) -> None:
        """`sid` is the family id, so a rotated session is still the calling one."""
        register_ok(client, username="alice")
        first = login_tokens(client, "alice")
        other = login_tokens(client, "alice")
        _, rotated = refresh_with(client, first.refresh)
        assert rotated

        r = change_password(client, rotated.access)
        assert r.status_code == 204
        assert refresh_with(client, other.refresh)[0].status_code == 401
        assert refresh_with(client, rotated.refresh)[0].status_code == 200

    def test_change_password_invalidates_outstanding_reset_tokens(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")
        t = login_tokens(client, "alice")

        change_password(client, t.access)
        r = reset_password(client, reset_token, "AttackerPass789!")
        assert r.status_code == 400

    @pytest.mark.usefixtures("captured_emails")
    def test_logout_all_revokes_every_session_and_clears_cookie(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        a = login_tokens(client, "alice")
        b = login_tokens(client, "alice")

        r = client.post("/api/v1/auth/logout-all", headers=bearer(a.access))

        assert r.status_code == 204, r.text
        set_cookie = r.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()
        assert "path=/api/v1/auth" in set_cookie.lower()
        assert active_session_count(db, "alice") == 0
        assert refresh_with(client, a.refresh)[0].status_code == 401
        assert refresh_with(client, b.refresh)[0].status_code == 401
        assert audit_count(db, "session_revoked", "alice") >= 1

    @pytest.mark.usefixtures("captured_emails")
    def test_logout_all_does_not_touch_other_users_sessions(
        self, client: TestClient, db: Db
    ) -> None:
        register(client, username="alice")
        register(client, username="bob")
        a = login_tokens(client, "alice")
        b = login_tokens(client, "bob")

        r = client.post("/api/v1/auth/logout-all", headers=bearer(a.access))
        assert r.status_code == 204, r.text

        assert active_session_count(db, "bob") == 1
        assert refresh_with(client, b.refresh)[0].status_code == 200

    def test_logout_all_without_token_returns_401(self, client: TestClient) -> None:
        assert client.post("/api/v1/auth/logout-all").status_code == 401

    def test_logout_all_with_invalid_token_returns_401(
        self, client: TestClient
    ) -> None:
        r = client.post("/api/v1/auth/logout-all", headers=bearer("not.a.jwt"))
        assert r.status_code == 401
