"""Integration tests for `GET` and `PATCH /api/v1/users/me`: the user's profile.

Real PostgreSQL and Redis come from the integration fixtures; neither the
service nor the database layer is mocked. `captured_emails` is needed for
register.
"""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    CapturedEmails,
    Db,
    bearer,
    register_and_login,
)

ME = "/api/v1/users/me"


class TestGetMe:
    @pytest.mark.usefixtures("captured_emails")
    def test_returns_profile_when_authenticated(self, client: TestClient) -> None:
        token = register_and_login(
            client, username="alice", display_name="Alice Liddell"
        )
        r = client.get(ME, headers=bearer(token))

        assert r.status_code == 200
        user = r.json()
        assert user["username"] == "alice"
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice Liddell"
        assert user["is_active"] is True
        assert user["is_email_verified"] is False
        assert "id" in user
        assert "created_at" in user

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_without_token(self, client: TestClient) -> None:
        register_and_login(client, username="alice")
        r = client.get(ME)

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json()["code"] == "unauthorized"

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_with_invalid_token(self, client: TestClient) -> None:
        register_and_login(client, username="alice")
        r = client.get(ME, headers=bearer("not.a.valid.token"))

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json()["code"] == "unauthorized"

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_with_malformed_bearer_header(self, client: TestClient) -> None:
        register_and_login(client, username="alice")
        r = client.get(ME, headers={"Authorization": "Token somejunk"})

        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_profile_reflects_display_name_from_registration(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client, username="bob", display_name="Bob Builder")
        r = client.get(ME, headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["display_name"] == "Bob Builder"

    @pytest.mark.usefixtures("captured_emails")
    def test_profile_has_no_display_name_when_not_set(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice")
        r = client.get(ME, headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["display_name"] is None

    @pytest.mark.usefixtures("captured_emails")
    def test_each_user_sees_only_their_own_profile(self, client: TestClient) -> None:
        token_alice = register_and_login(client, username="alice")
        token_bob = register_and_login(client, username="bob")

        assert client.get(ME, headers=bearer(token_alice)).json()["username"] == "alice"
        assert client.get(ME, headers=bearer(token_bob)).json()["username"] == "bob"


class TestUpdateMe:
    @pytest.mark.usefixtures("captured_emails")
    def test_update_display_name_succeeds(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice")
        r = client.patch(
            ME, json={"display_name": "Alice Updated"}, headers=bearer(token)
        )

        assert r.status_code == 200
        assert r.json()["display_name"] == "Alice Updated"

    @pytest.mark.usefixtures("captured_emails")
    def test_update_display_name_is_reflected_in_get_me(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client, username="alice")
        client.patch(
            ME, json={"display_name": "Persistent Name"}, headers=bearer(token)
        )

        r = client.get(ME, headers=bearer(token))
        assert r.json()["display_name"] == "Persistent Name"

    def test_email_field_is_rejected(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        """The address changes only through `PUT /users/me/email`."""
        token = register_and_login(client, username="alice")
        captured_emails.clear()

        r = client.patch(
            ME,
            json={"display_name": "X", "email": "new@example.com"},
            headers=bearer(token),
        )

        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "validation_error"
        assert body["errors"] == [
            {
                "field": "email",
                "code": "extra_forbidden",
                "message": "Extra inputs are not permitted",
            }
        ]
        assert "new@example.com" not in r.text  # the input is never echoed
        assert db.scalar("SELECT email FROM users") == "alice@example.com"
        assert db.scalar("SELECT display_name FROM users") is None
        assert captured_emails == []

    @pytest.mark.usefixtures("captured_emails")
    def test_empty_body_returns_200_with_unchanged_profile(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client, username="alice", display_name="Original")
        r = client.patch(ME, json={}, headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["display_name"] == "Original"

    @pytest.mark.usefixtures("captured_emails")
    def test_null_display_name_leaves_value_unchanged(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice", display_name="Keep Me")
        r = client.patch(ME, json={"display_name": None}, headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["display_name"] == "Keep Me"

    @pytest.mark.usefixtures("captured_emails")
    def test_display_name_too_long_returns_422(self, client: TestClient) -> None:
        token = register_and_login(client, username="alice")
        r = client.patch(
            ME,
            json={"display_name": "x" * 129},  # max_length=128
            headers=bearer(token),
        )

        assert r.status_code == 422
        assert [e["field"] for e in r.json()["errors"]] == ["display_name"]

    @pytest.mark.usefixtures("captured_emails")
    def test_returns_401_without_token(self, client: TestClient) -> None:
        register_and_login(client, username="alice")
        r = client.patch(ME, json={"display_name": "Hacker"})

        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"

    @pytest.mark.usefixtures("captured_emails")
    def test_multiple_sequential_updates_are_each_persisted(
        self, client: TestClient
    ) -> None:
        token = register_and_login(client, username="alice")

        client.patch(ME, json={"display_name": "First"}, headers=bearer(token))
        client.patch(ME, json={"display_name": "Second"}, headers=bearer(token))

        r = client.get(ME, headers=bearer(token))
        assert r.json()["display_name"] == "Second"
