"""Integration tests for `GET /user/me`: the authenticated user's profile.

Real PostgreSQL and Redis come from the integration fixtures; neither the
service nor the database layer is mocked. `captured_emails` is needed for
register.
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


class TestGetMe:
    def test_returns_profile_when_authenticated(self, client, captured_emails):
        token = register_and_login(
            client, username="alice", display_name="Alice Liddell"
        )
        r = client.get("/user/me", headers=bearer(token))

        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        user = body["data"]["user"]
        assert user["username"] == "alice"
        assert user["email"] == "alice@example.com"
        assert user["display_name"] == "Alice Liddell"
        assert user["is_active"] is True
        assert user["is_email_verified"] is False
        assert "id" in user
        assert "created_at" in user

    def test_returns_401_without_token(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.get("/user/me")

        assert r.status_code == 401

    def test_returns_401_with_invalid_token(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.get("/user/me", headers=bearer("not.a.valid.token"))

        assert r.status_code == 401

    def test_returns_401_with_malformed_bearer_header(self, client, captured_emails):
        register_and_login(client, username="alice")
        r = client.get("/user/me", headers={"Authorization": "Token somejunk"})

        assert r.status_code == 401

    def test_profile_reflects_display_name_from_registration(
        self, client, captured_emails
    ):
        token = register_and_login(client, username="bob", display_name="Bob Builder")
        r = client.get("/user/me", headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["data"]["user"]["display_name"] == "Bob Builder"

    def test_profile_has_no_display_name_when_not_set(self, client, captured_emails):
        token = register_and_login(client, username="alice")
        r = client.get("/user/me", headers=bearer(token))

        assert r.status_code == 200
        assert r.json()["data"]["user"]["display_name"] is None

    def test_each_user_sees_only_their_own_profile(self, client, captured_emails):
        token_alice = register_and_login(
            client, username="alice", email="alice@example.com"
        )
        token_bob = register_and_login(client, username="bob", email="bob@example.com")

        r = client.get("/user/me", headers=bearer(token_alice))
        assert r.json()["data"]["user"]["username"] == "alice"

        r = client.get("/user/me", headers=bearer(token_bob))
        assert r.json()["data"]["user"]["username"] == "bob"
