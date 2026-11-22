"""How request bodies and the login form are parsed: status and error codes.

Each test pins a status code and validation-error code the frontend sees:
explicit JSON `null` against omitted fields and bodies, and the OAuth2
password form in the shapes browsers and Swagger send it.
"""

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.helpers import (
    PASSWORD,
    CapturedEmails,
    bearer,
    register_and_login,
    register_ok,
)

pytestmark = pytest.mark.usefixtures("captured_emails")


def _validation_errors(response: Response) -> list[dict[str, str]]:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "validation_error"
    errors: list[dict[str, str]] = body["errors"]
    return errors


class TestExplicitJsonNull:
    """An explicit JSON null is validated; it is not the same as an omitted value."""

    def test_patch_me_null_display_name_keeps_the_value(
        self, client: TestClient
    ) -> None:
        """`{"display_name": null}` -> 200, no change.

        The field is `str | None` in a Pydantic body model, so null validates
        as None, which the service treats as "not provided". A display name
        cannot be cleared through null.
        """
        headers = bearer(register_and_login(client, username="nulltest"))
        assert (
            client.patch(
                "/api/v1/users/me", json={"display_name": "Alice"}, headers=headers
            ).status_code
            == 200
        )

        r = client.patch(
            "/api/v1/users/me", json={"display_name": None}, headers=headers
        )

        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "Alice"

    def test_patch_me_null_email_is_an_unknown_field(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        """`{"email": null}` -> 422 `extra_forbidden`, no mail.

        The profile update has no email field any more (`PUT /users/me/email`
        changes the address) and rejects unknown fields, null or not.
        """
        headers = bearer(register_and_login(client, username="nulltest"))
        mails_before = len(captured_emails)

        errors = _validation_errors(
            client.patch("/api/v1/users/me", json={"email": None}, headers=headers)
        )

        assert [(e["field"], e["code"]) for e in errors] == [
            ("email", "extra_forbidden")
        ]
        assert len(captured_emails) == mails_before

    def test_register_with_null_optional_field_is_accepted(
        self, client: TestClient
    ) -> None:
        """Optional `display_name: null` -> 201 with null."""
        r = client.post(
            "/api/v1/auth/register",
            json={
                "username": "nullable",
                "email": "nullable@example.com",
                "password": PASSWORD,
                "display_name": None,
            },
        )

        assert r.status_code == 201, r.text
        assert r.json()["display_name"] is None

    @pytest.mark.parametrize(
        ("method", "path", "payload", "field"),
        [
            (
                "post",
                "/api/v1/auth/register",
                {"username": None, "email": "x@example.com", "password": PASSWORD},
                "username",
            ),
            ("post", "/api/v1/auth/password/forgot", {"email": None}, "email"),
            ("post", "/api/v1/auth/email/verify", {"token": None}, "token"),
        ],
    )
    def test_null_for_a_required_field_is_a_type_error(
        self,
        client: TestClient,
        method: str,
        path: str,
        payload: dict[str, object] | dict[str, None],
        field: str,
    ) -> None:
        """Null for a required string -> 422 `string_type`."""
        errors = _validation_errors(getattr(client, method)(path, json=payload))

        assert {"field": field, "code": "string_type"}.items() <= errors[0].items()

    @pytest.mark.parametrize("path", ["/api/v1/auth/register", "/api/v1/users/me"])
    def test_null_as_the_whole_body_is_a_type_error(
        self, client: TestClient, path: str
    ) -> None:
        """A JSON `null` body -> 422 `model_attributes_type`.

        Null is validated against the body model, not treated as a missing
        body, and the whole body is reported as the field `body`.
        """
        headers = {"content-type": "application/json"}
        if path == "/api/v1/users/me":
            headers |= bearer(register_and_login(client, username="nulltest"))
            response = client.patch(path, content="null", headers=headers)
        else:
            response = client.post(path, content="null", headers=headers)

        errors = _validation_errors(response)

        assert errors == [
            {
                "field": "body",
                "message": "Input should be a valid dictionary or object to "
                "extract fields from",
                "code": "model_attributes_type",
            }
        ]

    def test_missing_body_is_still_reported_as_missing(
        self, client: TestClient
    ) -> None:
        """No body at all -> 422 `missing`."""
        headers = bearer(register_and_login(client, username="nulltest"))

        errors = _validation_errors(client.patch("/api/v1/users/me", headers=headers))

        assert errors == [
            {"field": "body", "message": "Field required", "code": "missing"}
        ]


class TestOAuth2PasswordForm:
    """The login form in every shape browsers and Swagger UI send.

    `OAuth2PasswordRequestForm` is a class dependency with individual `Form()`
    parameters, not a form model, so FastAPI's form-model rules for empty
    strings and defaults do not apply to it.
    """

    @pytest.fixture(autouse=True)
    def _user(self, client: TestClient) -> None:
        register_ok(client, username="formuser")

    def test_plain_form_logs_in(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/auth/login", data={"username": "formuser", "password": PASSWORD}
        )
        assert r.status_code == 200, r.text

    def test_swagger_ui_form_with_empty_optional_fields_logs_in(
        self, client: TestClient
    ) -> None:
        """Swagger's "Authorize" dialog sends grant_type and empty extras."""
        r = client.post(
            "/api/v1/auth/login",
            data={
                "grant_type": "password",
                "username": "formuser",
                "password": PASSWORD,
                "scope": "",
                "client_id": "",
                "client_secret": "",
            },
        )
        assert r.status_code == 200, r.text

    def test_empty_grant_type_counts_as_omitted(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/auth/login",
            data={"grant_type": "", "username": "formuser", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text

    def test_multipart_form_logs_in(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/auth/login",
            files={"username": (None, "formuser"), "password": (None, PASSWORD)},
        )
        assert r.status_code == 200, r.text

    def test_other_grant_type_is_rejected(self, client: TestClient) -> None:
        errors = _validation_errors(
            client.post(
                "/api/v1/auth/login",
                data={
                    "grant_type": "client_credentials",
                    "username": "formuser",
                    "password": PASSWORD,
                },
            )
        )
        assert errors[0]["field"] == "grant_type"
        assert errors[0]["code"] == "string_pattern_mismatch"

    @pytest.mark.parametrize(
        ("data", "missing"),
        [
            ({"username": "", "password": PASSWORD}, ["username"]),
            ({"username": "formuser", "password": ""}, ["password"]),
            ({"password": PASSWORD}, ["username"]),
            ({}, ["username", "password"]),
        ],
    )
    def test_empty_or_absent_credentials_are_missing(
        self, client: TestClient, data: dict[str, str], missing: list[str]
    ) -> None:
        """Empty strings are treated like absent fields: 422 `missing`."""
        errors = _validation_errors(client.post("/api/v1/auth/login", data=data))

        assert [(e["field"], e["code"]) for e in errors] == [
            (name, "missing") for name in missing
        ]

    def test_json_body_is_not_a_form(self, client: TestClient) -> None:
        errors = _validation_errors(
            client.post(
                "/api/v1/auth/login",
                json={"username": "formuser", "password": PASSWORD},
            )
        )
        assert [(e["field"], e["code"]) for e in errors] == [
            ("username", "missing"),
            ("password", "missing"),
        ]
