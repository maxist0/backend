"""Integration tests for the `/user` endpoints served by the auth module.

* `PATCH /user/me`: update display_name and/or email.
* `PATCH /user/me/password`: change the password (requires the current one).

`GET /user/me` belongs to the users module (`tests/integration/modules/users`).

Real PostgreSQL and Redis come from the integration fixtures; neither the
service nor the database layer is mocked. `captured_emails` is needed for
register and email change.
"""


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


def register_and_login(
    client,
    *,
    username: str = "alice",
    email: str | None = None,
    password: str = "Password123!",
    display_name: str | None = None,
) -> str:
    """Register a user, log in, and return the access token."""
    register(
        client,
        username=username,
        email=email,
        password=password,
        display_name=display_name,
    )
    r = login(client, username=username, password=password)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["data"]["access_token"]


class TestUpdateMe:
    def test_update_display_name_succeeds(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"display_name": "Alice Updated"},
            headers=bearer(token),
        )

        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["data"]["user"]["display_name"] == "Alice Updated"

    def test_update_display_name_is_reflected_in_get_me(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        client.patch(
            "/user/me",
            json={"display_name": "Persistent Name"},
            headers=bearer(token),
        )

        r = client.get("/user/me", headers=bearer(token))
        assert r.json()["data"]["user"]["display_name"] == "Persistent Name"

    def test_update_email_sends_change_token_and_keeps_old_email(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"email": "newalice@example.com"},
            headers=bearer(token),
        )

        assert r.status_code == 200
        # Email in the DB must NOT have switched yet (pending confirmation)
        user = r.json()["data"]["user"]
        assert user["email"] == "alice@example.com"
        # A confirmation email must have been dispatched to the new address
        assert "newalice@example.com" in captured_emails

    def test_update_display_name_and_email_together(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"display_name": "Combined", "email": "combo@example.com"},
            headers=bearer(token),
        )

        assert r.status_code == 200
        user = r.json()["data"]["user"]
        assert user["display_name"] == "Combined"
        # Email still old until confirmed
        assert user["email"] == "alice@example.com"
        assert "combo@example.com" in captured_emails

    def test_update_to_same_email_does_not_trigger_change_flow(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice")
        # Clear the registration email so we can check for new ones
        captured_emails.clear()

        r = client.patch(
            "/user/me",
            json={"email": "alice@example.com"},
            headers=bearer(token),
        )

        assert r.status_code == 200
        # No change-email token should be dispatched for the same address
        assert "alice@example.com" not in captured_emails

    def test_update_email_to_already_taken_email_returns_400(
        self, client, captured_emails
    ):
        register_and_login(client, username="bob", email="bob@example.com")
        token = register_and_login(client, username="alice", email="alice@example.com")

        r = client.patch(
            "/user/me",
            json={"email": "bob@example.com"},
            headers=bearer(token),
        )

        assert r.status_code == 400
        assert r.json()["success"] is False
        assert "already in use" in r.json()["error"]["message"].lower()

    def test_empty_body_returns_200_with_unchanged_profile(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice", display_name="Original")
        r = client.patch("/user/me", json={}, headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["data"]["user"]["display_name"] == "Original"

    def test_null_fields_in_body_leaves_values_unchanged(self, client, captured_emails):
        token = register_and_login(client, username="alice", display_name="Keep Me")
        r = client.patch(
            "/user/me",
            json={"display_name": None, "email": None},
            headers=bearer(token),
        )

        assert r.status_code == 200
        assert r.json()["data"]["user"]["display_name"] == "Keep Me"

    def test_display_name_too_long_returns_422(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"display_name": "x" * 129},  # max_length=128
            headers=bearer(token),
        )

        assert r.status_code == 422
        assert r.json()["success"] is False

    def test_invalid_email_format_returns_422(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"email": "not-an-email"},
            headers=bearer(token),
        )

        assert r.status_code == 422
        assert r.json()["success"] is False

    def test_returns_401_without_token(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.patch("/user/me", json={"display_name": "Hacker"})

        assert r.status_code == 401

    def test_email_is_lowercased_on_update(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me",
            json={"email": "NewEmail@Example.COM"},
            headers=bearer(token),
        )

        assert r.status_code == 200
        # Token dispatched to the lowercase-normalised address
        assert "newemail@example.com" in captured_emails

    def test_multiple_sequential_updates_are_each_persisted(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice")

        client.patch("/user/me", json={"display_name": "First"}, headers=bearer(token))
        client.patch("/user/me", json={"display_name": "Second"}, headers=bearer(token))

        r = client.get("/user/me", headers=bearer(token))
        assert r.json()["data"]["user"]["display_name"] == "Second"


class TestChangeMyPassword:
    def test_change_password_succeeds_with_correct_current_password(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "NewSecure456!",
            },
            headers=bearer(token),
        )

        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_can_login_with_new_password_after_change(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "NewSecure456!",
            },
            headers=bearer(token),
        )

        r = login(client, username="alice", password="NewSecure456!")
        assert r.status_code == 200

    def test_old_password_rejected_after_change(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "NewSecure456!",
            },
            headers=bearer(token),
        )

        r = login(client, username="alice", password="Password123!")
        assert r.status_code == 401

    def test_wrong_current_password_returns_400(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={
                "current_password": "WrongPassword!",
                "new_password": "ShouldNotWork1!",
            },
            headers=bearer(token),
        )

        assert r.status_code == 400
        assert r.json()["success"] is False
        assert "incorrect" in r.json()["error"]["message"].lower()

    def test_wrong_current_password_does_not_change_password(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="alice")
        client.patch(
            "/user/me/password",
            json={
                "current_password": "WrongPassword!",
                "new_password": "ShouldNotWork1!",
            },
            headers=bearer(token),
        )

        # Original password must still work
        r = login(client, username="alice", password="Password123!")
        assert r.status_code == 200

    def test_new_password_too_short_returns_422(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={"current_password": "Password123!", "new_password": "short"},
            headers=bearer(token),
        )

        assert r.status_code == 422
        assert r.json()["success"] is False

    def test_missing_current_password_returns_422(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={"new_password": "NewSecure456!"},
            headers=bearer(token),
        )

        assert r.status_code == 422

    def test_missing_new_password_returns_422(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={"current_password": "Password123!"},
            headers=bearer(token),
        )

        assert r.status_code == 422

    def test_returns_401_without_token(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "NewSecure456!",
            },
        )

        assert r.status_code == 401

    def test_returns_401_with_invalid_token(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "NewSecure456!",
            },
            headers=bearer("garbage.token.here"),
        )

        assert r.status_code == 401

    def test_password_change_is_user_specific(self, client, captured_emails):
        """Changing alice's password must not affect bob's credentials."""
        token_alice = register_and_login(
            client, username="alice", email="alice@example.com"
        )
        register_and_login(client, username="bob", email="bob@example.com")

        client.patch(
            "/user/me/password",
            json={
                "current_password": "Password123!",
                "new_password": "AliceNewPass1!",
            },
            headers=bearer(token_alice),
        )

        # Bob's original password must still work
        r = login(client, username="bob", password="Password123!")
        assert r.status_code == 200
