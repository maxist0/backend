"""Oversized headers and malformed addresses on the auth routes."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    PASSWORD,
    CapturedEmails,
    Db,
    audit_count,
    login,
    login_tokens,
    register,
    register_ok,
)


class TestInputBounds:
    @pytest.mark.usefixtures("captured_emails")
    def test_login_with_2000_char_user_agent_succeeds_and_is_audited(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        r = login(client, username="alice", headers={"User-Agent": "A" * 2000})
        client.cookies.clear()

        assert r.status_code == 200, r.text
        assert audit_count(db, "login_success", "alice") == 1
        assert db.scalar("SELECT max(length(user_agent)) FROM sessions") <= 512
        assert db.scalar("SELECT max(length(user_agent)) FROM auth_audit_log") <= 512

    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("header", ["X-Device-Name", "X-Device-Id"])
    def test_login_with_1000_char_device_header_succeeds(
        self, client: TestClient, db: Db, header: str
    ) -> None:
        register_ok(client, username="alice")
        r = login(client, username="alice", headers={header: "D" * 1000})
        client.cookies.clear()

        assert r.status_code == 200, r.text
        column = "device_name" if header == "X-Device-Name" else "device_id"
        assert db.scalar(f"SELECT max(length({column})) FROM sessions") <= 255
        assert audit_count(db, "login_success", "alice") == 1

    @pytest.mark.usefixtures("captured_emails")
    def test_long_user_agent_on_refresh_succeeds(self, client: TestClient) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        r = client.post(
            "/api/v1/auth/refresh",
            headers={
                "Cookie": f"refresh_token={t.refresh}",
                "User-Agent": "A" * 2000,
            },
        )
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize(
        "email",
        ["alice@example.com\n", "alice@example.com\r\n", "alice@@example.com", " "],
        ids=repr,
    )
    def test_register_with_malformed_email_returns_422(
        self, client: TestClient, email: str
    ) -> None:
        r = client.post(
            "/api/v1/auth/register",
            json={"username": "alice", "email": email, "password": PASSWORD},
        )
        assert r.status_code == 422

    def test_register_with_trailing_newline_creates_no_user(
        self, client: TestClient, db: Db
    ) -> None:
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com\n",
                "password": PASSWORD,
            },
        )
        assert db.scalar("SELECT count(*) FROM users") == 0

    @pytest.mark.parametrize("email", ["not-an-email", "a@b.com\n"], ids=repr)
    def test_forgot_password_with_malformed_email_returns_422(
        self, client: TestClient, email: str
    ) -> None:
        r = client.post("/api/v1/auth/password/forgot", json={"email": email})
        assert r.status_code == 422

    def test_reset_mail_is_sent_to_the_stored_address(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register(client, username="alice", email="alice@example.com")
        captured_emails.clear()

        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "ALICE@Example.COM"}
        )

        assert r.status_code == 202
        resets = captured_emails.of_kind("reset")
        assert len(resets) == 1
        assert resets[0].to == "alice@example.com"

    def test_forgot_for_unknown_address_sends_no_mail(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        """No mail and still 202 (no enumeration)."""
        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "ghost@example.com"}
        )
        assert r.status_code == 202
        assert captured_emails == []

    def test_registration_sends_exactly_one_verification_mail(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register(client, username="alice")
        assert len(captured_emails) == 1
        assert captured_emails[0].kind == "verify"
        assert captured_emails[0].to == "alice@example.com"
