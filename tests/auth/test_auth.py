# tests/api/auth/test_auth_integration.py
"""
Integration tests for /auth endpoints.

Infrastructure:
  - Real PostgreSQL via testcontainers (Alembic migrations applied once per session)
  - Real Redis via testcontainers (flushed before every test)
  - No service-layer mocking — every test exercises the full stack
    - `captured_emails` fixture intercepts email task `.kiq(...)` calls and records
    every mail (recipient, kind, token) without an SMTP server

Fixtures (all defined in conftest.py):
  client          - function-scoped TestClient (fresh cookie jar per test)
  clean_db        - autouse; truncates all auth tables after each test
    captured_emails - patches email task dispatch; ordered list of CapturedEmail
                      (`captured_emails[addr]` still returns the latest token)
"""

from src.common.constants import MAXIMUM_LOGIN_ATTEMPTS


# ---------------------------------------------------------------------------
# Shared helpers  (plain functions, not fixtures — no magic needed)
# ---------------------------------------------------------------------------


def register(
    client,
    *,
    username: str,
    email: str | None = None,
    password: str = "Password123!",
    display_name: str | None = None,
):
    payload = {
        "username": username,
        "email": email or f"{username}@example.com",
        "password": password,
    }
    if display_name:
        payload["display_name"] = display_name
    return client.post("/auth/register", json=payload)


def login(client, *, username: str, password: str = "Password123!"):
    return client.post("/auth/login", data={"username": username, "password": password})


def bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    def test_success_returns_201_with_user_data(self, client, captured_emails):
        r = register(client, username="alice", display_name="Alice")
        assert r.status_code == 201
        body = r.json()
        assert body["success"] is True
        user = body["data"]["user"]
        assert user["username"] == "alice"
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice"
        assert user["is_active"] is True
        assert user["is_email_verified"] is False
        assert "id" in user

    def test_success_sends_verification_email(self, client, captured_emails):
        register(client, username="alice")
        assert "alice@example.com" in captured_emails
        assert len(captured_emails["alice@example.com"]) > 10  # non-trivial token

    def test_duplicate_username_returns_409(self, client, captured_emails):
        register(client, username="alice")
        r = register(client, username="alice", email="alice2@example.com")
        assert r.status_code == 409
        body = r.json()
        assert body["success"] is False
        assert "Username already taken" in body["error"]["message"]

    def test_duplicate_email_returns_409(self, client, captured_emails):
        register(client, username="alice", email="shared@example.com")
        r = register(client, username="alice2", email="shared@example.com")
        assert r.status_code == 409
        body = r.json()
        assert body["success"] is False
        assert "Email already registered" in body["error"]["message"]

    def test_invalid_email_format_returns_422(self, client):
        r = register(client, username="alice", email="not-an-email")
        assert r.status_code == 422
        body = r.json()
        assert body["success"] is False
        assert "validation_errors" in body["error"]["details"]

    def test_username_too_short_returns_422(self, client):
        r = register(client, username="ab")  # min_length=3
        assert r.status_code == 422

    def test_password_too_short_returns_422(self, client):
        r = register(client, username="alice", password="short")  # min_length=8
        assert r.status_code == 422

    def test_username_with_spaces_returns_422(self, client):
        r = register(client, username="bad user")
        assert r.status_code == 422

    def test_username_with_special_chars_returns_422(self, client):
        r = register(client, username="alice!")
        assert r.status_code == 422

    def test_email_is_lowercased(self, client, captured_emails):
        r = register(client, username="alice", email="Alice@Example.COM")
        assert r.status_code == 201
        assert r.json()["data"]["user"]["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_success_returns_access_token_and_sets_cookie(
        self, client, captured_emails
    ):
        register(client, username="alice")
        r = login(client, username="alice")

        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["metadata"]["request_id"]
        assert "access_token" in body["data"]
        assert body["access_token"] == body["data"]["access_token"]
        assert body["data"]["token_type"] == "bearer"
        assert body["token_type"] == body["data"]["token_type"]
        assert body["data"]["user"]["username"] == "alice"
        assert "refresh_token" in client.cookies

    def test_wrong_password_returns_401(self, client, captured_emails):
        register(client, username="alice")
        r = login(client, username="alice", password="WrongPassword!")
        assert r.status_code == 401

    def test_nonexistent_user_returns_401(self, client):
        r = login(client, username="ghost")
        assert r.status_code == 401

    def test_single_failed_attempt_does_not_lock_account(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice", password="badpass")

        # Should still be able to log in with correct credentials
        r = login(client, username="alice")
        assert r.status_code == 200

    def test_max_failed_attempts_locks_account(self, client, captured_emails):
        register(client, username="alice")

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            r = login(client, username="alice", password="badpass")
            assert r.status_code == 401

        # Even correct password must now be rejected
        r = login(client, username="alice")
        assert r.status_code == 401
        body = r.json()
        assert body["success"] is False

    def test_failed_attempt_counter_resets_on_successful_login(
        self, client, captured_emails
    ):
        register(client, username="alice")

        # One failure below the threshold
        login(client, username="alice", password="badpass")

        # Successful login should reset the counter
        r = login(client, username="alice")
        assert r.status_code == 200

        # Now fail again — should not be locked from that previous failure
        login(client, username="alice", password="badpass")
        r = login(client, username="alice")
        assert r.status_code == 200

    def test_inactive_account_returns_401(
        self, client, deactivate_user, captured_emails
    ):
        import asyncio

        r = register(client, username="alice")
        assert r.status_code == 201
        asyncio.run(deactivate_user("alice"))
        r = login(client, username="alice")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_success_returns_200_and_revokes_session(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice")

        r = client.post("/auth/logout")
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_refresh_after_logout_returns_401(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice")
        client.post("/auth/logout")

        # Session is revoked in DB and cache deleted — refresh must fail
        r = client.post("/auth/refresh")
        assert r.status_code == 401

    def test_without_cookie_returns_401(self, client):
        r = client.post("/auth/logout")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_success_returns_new_access_token_and_rotates_cookie(
        self, client, captured_emails
    ):
        register(client, username="alice")
        login(client, username="alice")

        old_refresh = client.cookies.get("refresh_token")
        r = client.post("/auth/refresh")

        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body["data"]
        assert body["access_token"] == body["data"]["access_token"]
        assert body["token_type"] == body["data"]["token_type"]

        new_refresh = client.cookies.get("refresh_token")
        assert new_refresh is not None
        assert new_refresh != old_refresh

    def test_new_access_token_is_accepted_by_authenticated_endpoints(
        self, client, captured_emails
    ):
        register(client, username="alice")
        login(client, username="alice")

        new_access = client.post("/auth/refresh").json()["data"]["access_token"]

        # Any authenticated endpoint will do; resend verification is convenient
        r = client.post("/auth/email/verify/resend", headers=bearer(new_access))
        # 401 would mean the token is invalid; 400 means auth passed but email
        # is already verified or some other business-logic rejection — both fine
        assert r.status_code != 401

    def test_old_refresh_token_triggers_reuse_detection(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice")

        old_refresh = client.cookies.get("refresh_token")

        # Legitimate rotation; the response puts the newest token in the jar.
        assert client.post("/auth/refresh").status_code == 200
        newest_refresh = client.cookies.get("refresh_token")
        assert newest_refresh and newest_refresh != old_refresh

        # Attempt to use the already-rotated token (explicit Cookie header so the
        # jar content cannot influence which token is sent).
        client.cookies.clear()
        r = client.post(
            "/auth/refresh", headers={"Cookie": f"refresh_token={old_refresh}"}
        )
        assert r.status_code == 401

        # Reuse detection must have revoked the whole family: the NEWEST token,
        # sent in isolation, must be dead too. (The previous version of this test
        # re-sent the replayed OLD token and therefore proved nothing.)
        r2 = client.post(
            "/auth/refresh", headers={"Cookie": f"refresh_token={newest_refresh}"}
        )
        assert r2.status_code == 401

    def test_without_cookie_returns_401(self, client):
        r = client.post("/auth/refresh")
        assert r.status_code == 401

    def test_garbage_token_returns_401(self, client):
        client.cookies.set("refresh_token", "not-a-real-token-at-all")
        r = client.post("/auth/refresh")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Password forgot / reset
# ---------------------------------------------------------------------------


class TestPasswordForgotReset:
    def test_forgot_existing_email_returns_200(self, client, captured_emails):
        register(client, username="alice")
        r = client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_forgot_sends_token(self, client, captured_emails):
        register(client, username="alice")
        captured_emails.clear()  # discard the registration token

        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        assert "alice@example.com" in captured_emails
        assert len(captured_emails["alice@example.com"]) > 10

    def test_forgot_nonexistent_email_still_returns_200(self, client):
        """Must not leak whether the address is registered."""
        r = client.post("/auth/password/forgot", json={"email": "nobody@example.com"})
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_forgot_missing_email_field_returns_422(self, client):
        r = client.post("/auth/password/forgot", json={})
        assert r.status_code == 422

    def test_reset_with_valid_token_returns_200(self, client, captured_emails):
        register(client, username="alice")
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        r = client.post(
            "/auth/password/reset",
            json={"token": token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_can_login_with_new_password_after_reset(self, client, captured_emails):
        register(client, username="alice")
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]
        client.post(
            "/auth/password/reset",
            json={"token": token, "new_password": "NewPassword456!"},
        )

        # Old password must no longer work
        assert (
            login(client, username="alice", password="Password123!").status_code == 401
        )

        # New password must work
        assert (
            login(client, username="alice", password="NewPassword456!").status_code
            == 200
        )

    def test_reset_with_invalid_token_returns_400(self, client):
        r = client.post(
            "/auth/password/reset",
            json={"token": "totallyinvalidtoken", "new_password": "NewPassword456!"},
        )
        assert r.status_code == 400
        assert r.json()["success"] is False

    def test_reset_token_cannot_be_reused(self, client, captured_emails):
        register(client, username="alice")
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        client.post(
            "/auth/password/reset",
            json={"token": token, "new_password": "NewPassword456!"},
        )
        r = client.post(
            "/auth/password/reset",
            json={"token": token, "new_password": "AnotherPass789!"},
        )
        assert r.status_code == 400

    def test_second_forgot_request_invalidates_first_token(
        self, client, captured_emails
    ):
        register(client, username="alice")

        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        first_token = captured_emails["alice@example.com"]

        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        second_token = captured_emails["alice@example.com"]

        assert first_token != second_token  # sanity check

        # First token must have been invalidated
        r = client.post(
            "/auth/password/reset",
            json={"token": first_token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 400

        # Second token must still work
        r = client.post(
            "/auth/password/reset",
            json={"token": second_token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 200

    def test_reset_new_password_too_short_returns_422(self, client, captured_emails):
        register(client, username="alice")
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        r = client.post(
            "/auth/password/reset",
            json={"token": token, "new_password": "short"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


class TestEmailVerification:
    def test_verify_with_valid_token_returns_200(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]

        r = client.post("/auth/email/verify", json={"token": token})
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_user_is_marked_verified_after_successful_verify(
        self, client, captured_emails
    ):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]
        client.post("/auth/email/verify", json={"token": token})

        r = login(client, username="alice")
        assert r.json()["data"]["user"]["is_email_verified"] is True

    def test_verify_with_invalid_token_returns_400(self, client):
        r = client.post("/auth/email/verify", json={"token": "notavalidtoken"})
        assert r.status_code == 400
        assert r.json()["success"] is False

    def test_verify_token_cannot_be_reused(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]

        client.post("/auth/email/verify", json={"token": token})
        r = client.post("/auth/email/verify", json={"token": token})
        assert r.status_code == 400

    def test_resend_requires_authentication(self, client):
        r = client.post("/auth/email/verify/resend")
        assert r.status_code == 401

    def test_resend_sends_new_token(self, client, captured_emails):
        register(client, username="alice")
        original_token = captured_emails["alice@example.com"]

        r = login(client, username="alice")
        access_token = r.json()["data"]["access_token"]

        captured_emails.clear()
        client.post("/auth/email/verify/resend", headers=bearer(access_token))

        assert "alice@example.com" in captured_emails
        new_token = captured_emails["alice@example.com"]
        assert new_token != original_token

    def test_resend_invalidates_old_token(self, client, captured_emails):
        register(client, username="alice")
        old_token = captured_emails["alice@example.com"]

        access_token = login(client, username="alice").json()["data"]["access_token"]
        client.post("/auth/email/verify/resend", headers=bearer(access_token))

        r = client.post("/auth/email/verify", json={"token": old_token})
        assert r.status_code == 400

    def test_resend_returns_200(self, client, captured_emails):
        register(client, username="alice")
        access_token = login(client, username="alice").json()["data"]["access_token"]

        r = client.post("/auth/email/verify/resend", headers=bearer(access_token))
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_resend_fails_if_already_verified(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]
        client.post("/auth/email/verify", json={"token": token})

        access_token = login(client, username="alice").json()["data"]["access_token"]
        r = client.post("/auth/email/verify/resend", headers=bearer(access_token))
        assert r.status_code == 400
        assert r.json()["success"] is False


# ---------------------------------------------------------------------------
# Complete user journey
# ---------------------------------------------------------------------------


class TestFullFlow:
    def test_register_verify_login_refresh_logout(self, client, captured_emails):
        # 1. Register
        assert register(client, username="alice").status_code == 201

        # 2. Verify email
        token = captured_emails["alice@example.com"]
        assert (
            client.post("/auth/email/verify", json={"token": token}).status_code == 200
        )

        # 3. Login — user should show as verified
        r = login(client, username="alice")
        assert r.status_code == 200
        access_token = r.json()["data"]["access_token"]
        assert r.json()["data"]["user"]["is_email_verified"] is True

        # 4. Refresh — should issue a new token pair
        r = client.post("/auth/refresh")
        assert r.status_code == 200
        new_access_token = r.json()["data"]["access_token"]
        assert new_access_token != access_token

        # 5. Old token should still work (access tokens are JWTs and remain
        #    valid until expiry; only the refresh token rotates)

        # 6. Logout
        assert client.post("/auth/logout").status_code == 200

        # 7. Refresh after logout must fail — session is revoked
        assert client.post("/auth/refresh").status_code == 401

    def test_password_reset_full_cycle(self, client, captured_emails):
        register(client, username="alice")

        # Request reset
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails["alice@example.com"]

        # Apply reset
        r = client.post(
            "/auth/password/reset",
            json={"token": reset_token, "new_password": "BrandNewPass99!"},
        )
        assert r.status_code == 200

        # Old password must fail
        assert (
            login(client, username="alice", password="Password123!").status_code == 401
        )

        # New password must succeed
        r = login(client, username="alice", password="BrandNewPass99!")
        assert r.status_code == 200
        assert "access_token" in r.json()["data"]
