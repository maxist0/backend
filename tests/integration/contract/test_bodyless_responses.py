"""202 and 204 answers carry no body and no Content-Type (RFC 9110 15.3.5)."""

from tests.helpers import PASSWORD, bearer, login, login_tokens, register_ok


def _assert_bodyless(response, status: int) -> None:
    assert response.status_code == status, response.text
    assert response.content == b""
    assert "content-type" not in response.headers


def test_every_204_route_answers_without_body_and_content_type(client, captured_emails):
    register_ok(client, username="alice")
    tokens = login_tokens(client, "alice")

    _assert_bodyless(
        client.post(
            "/api/v1/auth/email/verify",
            json={"token": captured_emails.latest_token("alice@example.com")},
        ),
        204,
    )
    _assert_bodyless(
        client.put(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
            headers=bearer(tokens.access),
        ),
        204,
    )
    client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
    _assert_bodyless(
        client.post(
            "/api/v1/auth/password/reset",
            json={
                "token": captured_emails.latest_token("alice@example.com", "reset"),
                "new_password": "Password123!",
            },
        ),
        204,
    )
    login(client, username="alice")  # refresh cookie in the jar
    _assert_bodyless(client.post("/api/v1/auth/logout"), 204)
    access = login(client, username="alice").json()["access_token"]
    response = client.post("/api/v1/auth/logout-all", headers=bearer(access))
    _assert_bodyless(response, 204)
    assert "refresh_token=" in response.headers["set-cookie"]


def test_every_202_route_answers_without_body_and_content_type(client, captured_emails):
    register_ok(client, username="alice")
    access = login_tokens(client, "alice").access

    _assert_bodyless(
        client.post("/api/v1/auth/password/forgot", json={"email": "a@example.com"}),
        202,
    )
    _assert_bodyless(
        client.post("/api/v1/auth/email/verify/resend", headers=bearer(access)), 202
    )
    _assert_bodyless(
        client.put(
            "/api/v1/users/me/email",
            json={"current_password": PASSWORD, "new_email": "new@example.com"},
            headers=bearer(access),
        ),
        202,
    )
