"""Integration tests for the `/api/v1/auth` endpoints.

Real PostgreSQL and Redis come from the integration fixtures; no service layer
is mocked, so every test exercises the full stack. The `captured_emails`
fixture intercepts the email task `.kiq(...)` calls and records every mail
(recipient, kind, token) without an SMTP server;
`captured_emails[addr]` returns the latest token.
"""

from app.modules.auth.service import MAXIMUM_LOGIN_ATTEMPTS


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
    return client.post("/api/v1/auth/register", json=payload)


def login(client, *, username: str, password: str = "Password123!"):
    return client.post(
        "/api/v1/auth/login", data={"username": username, "password": password}
    )


def bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


class TestRegister:
    def test_success_returns_201_with_user_data(self, client, captured_emails):
        r = register(client, username="alice", display_name="Alice")
        assert r.status_code == 201
        user = r.json()
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
        assert body["code"] == "user_exists"
        assert body["detail"] == "Username already taken"

    def test_duplicate_email_returns_409(self, client, captured_emails):
        register(client, username="alice", email="shared@example.com")
        r = register(client, username="alice2", email="shared@example.com")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "user_exists"
        assert body["detail"] == "Email already registered"

    def test_invalid_email_format_returns_422(self, client):
        r = register(client, username="alice", email="not-an-email")
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "validation_error"
        assert [e["field"] for e in body["errors"]] == ["email"]

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
        assert r.json()["email"] == "alice@example.com"


class TestLogin:
    def test_success_returns_access_token_and_sets_cookie(
        self, client, captured_emails
    ):
        register(client, username="alice")
        r = login(client, username="alice")

        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"access_token", "token_type", "expires_in", "user"}
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 15 * 60
        assert body["user"]["username"] == "alice"
        assert "refresh_token" in client.cookies

    def test_refresh_cookie_is_scoped_to_the_prefixed_auth_routes(
        self, client, captured_emails
    ):
        """The browser must send the cookie to `/api/v1/auth/refresh`."""
        register(client, username="alice")
        r = login(client, username="alice")

        cookie = r.headers["set-cookie"]
        assert "Path=/api/v1/auth" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie

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
        assert r.json()["code"] == "invalid_credentials"

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

    def test_inactive_account_returns_401(self, client, db, captured_emails):
        r = register(client, username="alice")
        assert r.status_code == 201
        db.execute("UPDATE users SET is_active = false WHERE username = :u", u="alice")
        r = login(client, username="alice")
        assert r.status_code == 401


class TestLogout:
    def test_success_returns_204_and_clears_cookie(self, client, captured_emails, db):
        register(client, username="alice")
        login(client, username="alice")

        r = client.post("/api/v1/auth/logout")
        assert r.status_code == 204
        assert r.content == b""
        assert 'refresh_token=""' in r.headers["set-cookie"]
        assert "Path=/api/v1/auth" in r.headers["set-cookie"]
        assert db.scalar("SELECT revoke_reason FROM sessions") == "logout"

    def test_refresh_after_logout_returns_401(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice")
        client.post("/api/v1/auth/logout")

        # Session is revoked in DB and cache deleted — refresh must fail
        r = client.post("/api/v1/auth/refresh")
        assert r.status_code == 401

    def test_without_cookie_returns_401(self, client):
        r = client.post("/api/v1/auth/logout")
        assert r.status_code == 401
        assert r.json()["code"] == "invalid_refresh_token"

    def test_unknown_session_returns_400(self, client):
        r = client.post(
            "/api/v1/auth/logout", headers={"Cookie": "refresh_token=unknown"}
        )
        assert r.status_code == 400
        assert r.json()["code"] == "session_not_found"


class TestRefresh:
    def test_success_returns_new_access_token_and_rotates_cookie(
        self, client, captured_emails
    ):
        register(client, username="alice")
        login(client, username="alice")

        old_refresh = client.cookies.get("refresh_token")
        r = client.post("/api/v1/auth/refresh")

        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"access_token", "token_type", "expires_in"}
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 15 * 60

        new_refresh = client.cookies.get("refresh_token")
        assert new_refresh is not None
        assert new_refresh != old_refresh

    def test_new_access_token_is_accepted_by_authenticated_endpoints(
        self, client, captured_emails
    ):
        register(client, username="alice")
        login(client, username="alice")

        new_access = client.post("/api/v1/auth/refresh").json()["access_token"]

        # Any authenticated endpoint will do; resend verification is convenient
        r = client.post("/api/v1/auth/email/verify/resend", headers=bearer(new_access))
        assert r.status_code == 202

    def test_old_refresh_token_triggers_reuse_detection(self, client, captured_emails):
        register(client, username="alice")
        login(client, username="alice")

        old_refresh = client.cookies.get("refresh_token")

        # Legitimate rotation; the response puts the newest token in the jar.
        assert client.post("/api/v1/auth/refresh").status_code == 200
        newest_refresh = client.cookies.get("refresh_token")
        assert newest_refresh and newest_refresh != old_refresh

        # Attempt to use the already-rotated token (explicit Cookie header so the
        # jar content cannot influence which token is sent).
        client.cookies.clear()
        r = client.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={old_refresh}"}
        )
        assert r.status_code == 401
        assert r.json()["code"] == "invalid_refresh_token"

        # Reuse detection must have revoked the whole family: the NEWEST token,
        # sent in isolation, must be dead too. (The previous version of this test
        # re-sent the replayed OLD token and therefore proved nothing.)
        r2 = client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"refresh_token={newest_refresh}"},
        )
        assert r2.status_code == 401

    def test_without_cookie_returns_401(self, client):
        r = client.post("/api/v1/auth/refresh")
        assert r.status_code == 401
        assert r.json()["code"] == "invalid_refresh_token"
        assert r.headers["www-authenticate"] == "Bearer"

    def test_garbage_token_returns_401(self, client):
        client.cookies.set("refresh_token", "not-a-real-token-at-all")
        r = client.post("/api/v1/auth/refresh")
        assert r.status_code == 401


class TestPasswordForgotReset:
    def test_forgot_existing_email_returns_202(self, client, captured_emails):
        register(client, username="alice")
        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
        )
        assert r.status_code == 202
        assert r.content == b""

    def test_forgot_sends_token(self, client, captured_emails):
        register(client, username="alice")
        captured_emails.clear()  # discard the registration token

        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        assert "alice@example.com" in captured_emails
        assert len(captured_emails["alice@example.com"]) > 10

    def test_forgot_nonexistent_email_still_returns_202(self, client):
        """Must not leak whether the address is registered."""
        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "nobody@example.com"}
        )
        assert r.status_code == 202
        assert r.content == b""

    def test_forgot_missing_email_field_returns_422(self, client):
        r = client.post("/api/v1/auth/password/forgot", json={})
        assert r.status_code == 422

    def test_reset_with_valid_token_returns_204(self, client, captured_emails):
        register(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 204

    def test_can_login_with_new_password_after_reset(self, client, captured_emails):
        register(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]
        client.post(
            "/api/v1/auth/password/reset",
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
            "/api/v1/auth/password/reset",
            json={"token": "totallyinvalidtoken", "new_password": "NewPassword456!"},
        )
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"

    def test_reset_token_cannot_be_reused(self, client, captured_emails):
        register(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        client.post(
            "/api/v1/auth/password/reset",
            json={"token": token, "new_password": "NewPassword456!"},
        )
        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": token, "new_password": "AnotherPass789!"},
        )
        assert r.status_code == 400

    def test_second_forgot_request_invalidates_first_token(
        self, client, captured_emails
    ):
        register(client, username="alice")

        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        first_token = captured_emails["alice@example.com"]

        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        second_token = captured_emails["alice@example.com"]

        assert first_token != second_token  # sanity check

        # First token must have been invalidated
        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": first_token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 400

        # Second token must still work
        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": second_token, "new_password": "NewPassword456!"},
        )
        assert r.status_code == 204

    def test_reset_new_password_too_short_returns_422(self, client, captured_emails):
        register(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        token = captured_emails["alice@example.com"]

        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": token, "new_password": "short"},
        )
        assert r.status_code == 422


class TestEmailVerification:
    def test_verify_with_valid_token_returns_204(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]

        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 204

    def test_user_is_marked_verified_after_successful_verify(
        self, client, captured_emails
    ):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]
        client.post("/api/v1/auth/email/verify", json={"token": token})

        r = login(client, username="alice")
        assert r.json()["user"]["is_email_verified"] is True

    def test_verify_with_invalid_token_returns_400(self, client):
        r = client.post("/api/v1/auth/email/verify", json={"token": "notavalidtoken"})
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"

    def test_verify_token_cannot_be_reused(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]

        client.post("/api/v1/auth/email/verify", json={"token": token})
        r = client.post("/api/v1/auth/email/verify", json={"token": token})
        assert r.status_code == 400

    def test_resend_requires_authentication(self, client):
        r = client.post("/api/v1/auth/email/verify/resend")
        assert r.status_code == 401

    def test_resend_sends_new_token(self, client, captured_emails):
        register(client, username="alice")
        original_token = captured_emails["alice@example.com"]

        r = login(client, username="alice")
        access_token = r.json()["access_token"]

        captured_emails.clear()
        client.post("/api/v1/auth/email/verify/resend", headers=bearer(access_token))

        assert "alice@example.com" in captured_emails
        new_token = captured_emails["alice@example.com"]
        assert new_token != original_token

    def test_resend_invalidates_old_token(self, client, captured_emails):
        register(client, username="alice")
        old_token = captured_emails["alice@example.com"]

        access_token = login(client, username="alice").json()["access_token"]
        client.post("/api/v1/auth/email/verify/resend", headers=bearer(access_token))

        r = client.post("/api/v1/auth/email/verify", json={"token": old_token})
        assert r.status_code == 400

    def test_resend_returns_202(self, client, captured_emails):
        register(client, username="alice")
        access_token = login(client, username="alice").json()["access_token"]

        r = client.post(
            "/api/v1/auth/email/verify/resend", headers=bearer(access_token)
        )
        assert r.status_code == 202
        assert r.content == b""

    def test_resend_fails_if_already_verified(self, client, captured_emails):
        register(client, username="alice")
        token = captured_emails["alice@example.com"]
        client.post("/api/v1/auth/email/verify", json={"token": token})

        access_token = login(client, username="alice").json()["access_token"]
        captured_emails.clear()
        r = client.post(
            "/api/v1/auth/email/verify/resend", headers=bearer(access_token)
        )
        assert r.status_code == 400
        assert r.json()["code"] == "email_already_verified"
        assert captured_emails == []


class TestFullFlow:
    def test_register_verify_login_refresh_logout(self, client, captured_emails):
        # 1. Register
        assert register(client, username="alice").status_code == 201

        # 2. Verify email
        token = captured_emails["alice@example.com"]
        assert (
            client.post("/api/v1/auth/email/verify", json={"token": token}).status_code
            == 204
        )

        # 3. Login — user should show as verified
        r = login(client, username="alice")
        assert r.status_code == 200
        access_token = r.json()["access_token"]
        assert r.json()["user"]["is_email_verified"] is True

        # 4. Refresh — should issue a new token pair
        r = client.post("/api/v1/auth/refresh")
        assert r.status_code == 200
        new_access_token = r.json()["access_token"]
        assert new_access_token != access_token

        # 5. Old token should still work (access tokens are JWTs and remain
        #    valid until expiry; only the refresh token rotates)

        # 6. Logout
        assert client.post("/api/v1/auth/logout").status_code == 204

        # 7. Refresh after logout must fail — session is revoked
        assert client.post("/api/v1/auth/refresh").status_code == 401

    def test_password_reset_full_cycle(self, client, captured_emails):
        register(client, username="alice")

        # Request reset
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails["alice@example.com"]

        # Apply reset
        r = client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": "BrandNewPass99!"},
        )
        assert r.status_code == 204

        # Old password must fail
        assert (
            login(client, username="alice", password="Password123!").status_code == 401
        )

        # New password must succeed
        r = login(client, username="alice", password="BrandNewPass99!")
        assert r.status_code == 200
        assert "access_token" in r.json()


class TestTwoFactorStubs:
    def test_every_stub_answers_501_not_implemented(self, client):
        for action in ("enable", "disable", "verify"):
            r = client.post(f"/api/v1/auth/2fa/{action}")
            assert r.status_code == 501, action
            assert r.json()["code"] == "not_implemented"
