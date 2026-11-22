"""Integration tests for the `/api/v1/users/me` routes served by the auth module.

* `PUT /users/me/password`: change the password (requires the current one).
* `PUT /users/me/email`: request an email change (requires the current
  password); `POST /auth/email/verify` with the mailed token completes it.

`GET` and `PATCH /users/me` belong to the users module
(`tests/integration/modules/users`).

Real PostgreSQL and Redis come from the integration fixtures; neither the
service nor the database layer is mocked. `captured_emails` is needed for
register and email change.
"""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    PASSWORD,
    CapturedEmails,
    Db,
    audit_count,
    bearer,
    change_email,
    change_password,
    login,
    register,
    register_and_login,
    reset_password,
)

PASSWORD_URL = "/api/v1/users/me/password"
EMAIL_URL = "/api/v1/users/me/email"
VERIFY_URL = "/api/v1/auth/email/verify"


class TestChangeMyPassword:
    @pytest.mark.usefixtures("captured_emails")
    def test_change_password_succeeds_with_correct_current_password(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client)
        r = change_password(client, token, PASSWORD, "NewSecure456!")

        assert r.status_code == 204
        assert r.content == b""

    @pytest.mark.usefixtures("captured_emails")
    def test_can_login_with_new_password_after_change(self, client: TestClient) -> None:
        token = register_and_login(client)
        change_password(client, token, PASSWORD, "NewSecure456!")

        assert (
            login(client, username="alice", password="NewSecure456!").status_code == 200
        )

    @pytest.mark.usefixtures("captured_emails")
    def test_old_password_rejected_after_change(self, client: TestClient) -> None:
        token = register_and_login(client)
        change_password(client, token, PASSWORD, "NewSecure456!")

        assert login(client, username="alice", password=PASSWORD).status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_wrong_current_password_returns_400(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = change_password(client, token, "WrongPassword!", "ShouldNotWork1!")

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"
        assert "incorrect" in r.json()["detail"].lower()

    @pytest.mark.usefixtures("captured_emails")
    def test_wrong_current_password_does_not_change_password(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client)
        change_password(client, token, "WrongPassword!", "ShouldNotWork1!")

        assert login(client, username="alice", password=PASSWORD).status_code == 200

    @pytest.mark.usefixtures("captured_emails")
    def test_new_password_too_short_returns_422(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = change_password(client, token, PASSWORD, "short")

        assert r.status_code == 422
        assert [e["field"] for e in r.json()["errors"]] == ["new_password"]

    @pytest.mark.usefixtures("captured_emails")
    def test_missing_current_password_returns_422(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = client.put(
            PASSWORD_URL, json={"new_password": "NewSecure456!"}, headers=bearer(token)
        )

        assert r.status_code == 422

    @pytest.mark.usefixtures("captured_emails")
    def test_missing_new_password_returns_422(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = client.put(
            PASSWORD_URL, json={"current_password": PASSWORD}, headers=bearer(token)
        )

        assert r.status_code == 422

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_without_token(self, client: TestClient) -> None:
        register_and_login(client)
        r = change_password(client, None, PASSWORD, "NewSecure456!")

        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_with_invalid_token(self, client: TestClient) -> None:
        register_and_login(client)
        r = change_password(client, "garbage.token.here", PASSWORD, "NewSecure456!")

        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_patch_is_no_longer_routed(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = client.patch(
            PASSWORD_URL,
            json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
            headers=bearer(token),
        )

        assert r.status_code == 405
        assert r.json()["code"] == "method_not_allowed"

    @pytest.mark.usefixtures("captured_emails")
    def test_password_change_is_user_specific(self, client: TestClient) -> None:
        """Changing alice's password must not affect bob's credentials."""
        token_alice = register_and_login(client, username="alice")
        register_and_login(client, username="bob")

        change_password(client, token_alice, PASSWORD, "AliceNewPass1!")

        assert login(client, username="bob", password=PASSWORD).status_code == 200


class TestChangeMyEmail:
    def test_full_flow_switches_and_verifies_the_address(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)
        captured_emails.clear()

        r = change_email(client, token, "New.Alice@Example.com")

        assert r.status_code == 202
        assert r.content == b""
        # The old address was never verified, so it gets no notice.
        [mail] = captured_emails
        assert (mail.to, mail.kind) == ("new.alice@example.com", "change_email")

        r = client.post(VERIFY_URL, json={"token": mail.token})

        assert r.status_code == 204
        row = db.one("SELECT email, is_email_verified FROM users")
        assert row["email"] == "new.alice@example.com"
        assert row["is_email_verified"] is True
        # Login is by username; it still works and shows the new address.
        r = login(client, username="alice")
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "new.alice@example.com"
        assert audit_count(db, "email_changed", "alice") == 1

    def test_old_address_stays_until_confirmed(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)

        assert change_email(client, token, "new@example.com").status_code == 202

        row = db.one("SELECT email, is_email_verified FROM users")
        assert row["email"] == "alice@example.com"
        assert row["is_email_verified"] is False
        r = client.get("/api/v1/users/me", headers=bearer(token))
        assert r.json()["email"] == "alice@example.com"
        # Nothing went to the old address for the change.
        assert captured_emails.of_kind("change_email")[0].to == "new@example.com"
        assert audit_count(db, "email_changed", "alice") == 0

    @pytest.mark.usefixtures("captured_emails")
    def test_request_is_audited_as_verification_sent(
        self, client: TestClient, db: Db
    ) -> None:
        token = register_and_login(client)
        before = audit_count(db, "email_verification_sent", "alice")

        change_email(client, token, "new@example.com")

        assert audit_count(db, "email_verification_sent", "alice") == before + 1
        detail = db.scalar(
            "SELECT detail FROM auth_audit_log WHERE event_type::text = "
            "'email_verification_sent' ORDER BY created_at DESC LIMIT 1"
        )
        assert detail == "Email change requested"

    def test_token_is_single_use(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)
        change_email(client, token, "new@example.com")
        mail_token = captured_emails.latest_token("new@example.com", "change_email")

        assert client.post(VERIFY_URL, json={"token": mail_token}).status_code == 204
        r = client.post(VERIFY_URL, json={"token": mail_token})

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"
        assert audit_count(db, "email_changed", "alice") == 1

    def test_confirmed_change_invalidates_reset_links_sent_before(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        """A reset link mailed to the old address must not outlive the change."""
        token = register_and_login(client)
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")
        change_email(client, token, "new@example.com")
        mail_token = captured_emails.latest_token("new@example.com", "change_email")

        assert client.post(VERIFY_URL, json={"token": mail_token}).status_code == 204
        r = reset_password(client, reset_token, "AttackerPass789!")

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"
        assert login(client, username="alice").status_code == 200

    def test_unconfirmed_change_keeps_reset_links_valid(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        """Only the confirmation switches the address, so only it ends them."""
        token = register_and_login(client)
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")
        change_email(client, token, "new@example.com")

        r = reset_password(client, reset_token, "NewPassword456!")

        assert r.status_code == 204

    def test_a_new_request_invalidates_the_previous_link(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)
        change_email(client, token, "first@example.com")
        first = captured_emails.latest_token("first@example.com", "change_email")
        change_email(client, token, "second@example.com")
        second = captured_emails.latest_token("second@example.com", "change_email")

        assert client.post(VERIFY_URL, json={"token": first}).status_code == 400
        assert client.post(VERIFY_URL, json={"token": second}).status_code == 204
        assert db.scalar("SELECT email FROM users") == "second@example.com"

    def test_address_taken_between_request_and_confirm_returns_409(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)
        change_email(client, token, "contested@example.com")
        mail_token = captured_emails.latest_token(
            "contested@example.com", "change_email"
        )
        register(client, username="bob", email="contested@example.com")

        r = client.post(VERIFY_URL, json={"token": mail_token})

        assert r.status_code == 409
        assert r.json()["code"] == "email_taken"
        alice = db.one("SELECT email FROM users WHERE username = 'alice'")
        assert alice["email"] == "alice@example.com"
        # The redemption was rolled back with the failed change.
        assert (
            db.scalar(
                "SELECT used_at FROM email_tokens WHERE purpose::text = 'change_email'"
            )
            is None
        )
        assert audit_count(db, "email_changed", "alice") == 0

    def test_wrong_password_returns_400(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        token = register_and_login(client)
        captured_emails.clear()

        r = change_email(client, token, "new@example.com", password="WrongPassword!")

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"
        assert captured_emails == []
        assert (
            db.scalar(
                "SELECT count(*) FROM email_tokens WHERE purpose::text = 'change_email'"
            )
            == 0
        )

    @pytest.mark.usefixtures("captured_emails")
    def test_wrong_password_hides_whether_the_address_is_taken(
        self, client: TestClient
    ) -> None:
        register(client, username="bob", email="bob@example.com")
        token = register_and_login(client)

        r = change_email(client, token, "bob@example.com", password="WrongPassword!")

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"

    def test_address_of_another_account_returns_409(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register(client, username="bob", email="bob@example.com")
        token = register_and_login(client)
        captured_emails.clear()

        r = change_email(client, token, "BOB@example.com")

        assert r.status_code == 409
        assert r.json()["code"] == "email_taken"
        assert captured_emails == []

    def test_current_address_returns_400_email_unchanged(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        token = register_and_login(client)
        captured_emails.clear()

        r = change_email(client, token, "alice@example.com")

        assert r.status_code == 400
        assert r.json()["code"] == "email_unchanged"
        assert captured_emails == []

    @pytest.mark.usefixtures("captured_emails")
    def test_invalid_address_returns_422(self, client: TestClient) -> None:
        token = register_and_login(client)
        r = change_email(client, token, "not-an-email")

        assert r.status_code == 422
        assert [e["field"] for e in r.json()["errors"]] == ["new_email"]

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_without_token(self, client: TestClient) -> None:
        register_and_login(client)
        r = change_email(client, None, "new@example.com")

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
