"""Request parsing contract across the FastAPI 0.136 -> 0.163 upgrade.

Each test pins a status code and validation-error type the frontend sees.
Before/after was measured on 2026-11-16 (FastAPI 0.136.1 vs 0.163.1); the
docstrings name the FastAPI release whose change the test covers and whether
the observable result changed.
"""

import pytest

from tests.helpers import PASSWORD, bearer, login, register_ok


@pytest.fixture(autouse=True)
def _no_real_email_dispatch(captured_emails):
    return captured_emails


def _validation_errors(response) -> list[dict]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    return body["error"]["details"]["validation_errors"]


def _access_token(client, username: str = "nulltest") -> str:
    register_ok(client, username=username)
    r = login(client, username=username)
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


class TestExplicitJsonNull:
    """FastAPI 0.151: explicit JSON null is no longer the same as an omitted value."""

    def test_patch_me_null_display_name_keeps_the_value(self, client):
        """0.151, unchanged: ``{"display_name": null}`` -> 200, no change.

        The field is ``str | None`` in a Pydantic body model, so null validates
        as None, which the service treats as "not provided". Clearing a
        display name is therefore not possible through null (pre-existing
        contract, not changed by the upgrade).
        """
        headers = bearer(_access_token(client))
        assert (
            client.patch(
                "/user/me", json={"display_name": "Alice"}, headers=headers
            ).status_code
            == 200
        )

        r = client.patch("/user/me", json={"display_name": None}, headers=headers)

        assert r.status_code == 200, r.text
        assert r.json()["data"]["user"]["display_name"] == "Alice"

    def test_patch_me_null_email_changes_nothing(self, client, captured_emails):
        """0.151, unchanged: ``{"email": null}`` -> 200, no change flow, no mail."""
        headers = bearer(_access_token(client))
        mails_before = len(captured_emails)

        r = client.patch("/user/me", json={"email": None}, headers=headers)

        assert r.status_code == 200, r.text
        assert r.json()["data"]["user"]["email"] == "nulltest@example.com"
        assert len(captured_emails) == mails_before

    def test_register_with_null_optional_field_is_accepted(self, client):
        """0.151, unchanged: optional ``display_name: null`` -> 201 with null."""
        r = client.post(
            "/auth/register",
            json={
                "username": "nullable",
                "email": "nullable@example.com",
                "password": PASSWORD,
                "display_name": None,
            },
        )

        assert r.status_code == 201, r.text
        assert r.json()["data"]["user"]["display_name"] is None

    @pytest.mark.parametrize(
        ("method", "path", "payload", "field"),
        [
            (
                "post",
                "/auth/register",
                {"username": None, "email": "x@example.com", "password": PASSWORD},
                "username",
            ),
            ("post", "/auth/password/forgot", {"email": None}, "email"),
            ("post", "/auth/email/verify", {"token": None}, "token"),
        ],
    )
    def test_null_for_a_required_field_is_a_type_error(
        self, client, method, path, payload, field
    ):
        """0.151, unchanged: null for a required string -> 422 ``string_type``."""
        errors = _validation_errors(getattr(client, method)(path, json=payload))

        assert {"field": field, "type": "string_type"}.items() <= errors[0].items()

    @pytest.mark.parametrize("path", ["/auth/register", "/user/me"])
    def test_null_as_the_whole_body_is_a_type_error(self, client, path):
        """0.151, CHANGED: a JSON ``null`` body -> 422 ``model_attributes_type``.

        Up to FastAPI 0.150 a null body counted as a missing body
        (422, type ``missing``, message "Field required"). Now null is
        validated against the body model and rejected as "Input should be a
        valid dictionary or object to extract fields from". Status code and
        envelope are unchanged; only the error type and message differ.
        """
        headers = {"content-type": "application/json"}
        if path == "/user/me":
            headers |= bearer(_access_token(client))
            response = client.patch(path, content="null", headers=headers)
        else:
            response = client.post(path, content="null", headers=headers)

        errors = _validation_errors(response)

        assert errors == [
            {
                "field": "",
                "message": "Input should be a valid dictionary or object to "
                "extract fields from",
                "type": "model_attributes_type",
            }
        ]

    def test_missing_body_is_still_reported_as_missing(self, client):
        """0.151, unchanged: no body at all -> 422 ``missing``."""
        headers = bearer(_access_token(client))

        errors = _validation_errors(client.patch("/user/me", headers=headers))

        assert errors == [{"field": "", "message": "Field required", "type": "missing"}]


class TestOAuth2PasswordForm:
    """OAuth2PasswordRequestForm is a class dependency with individual Form()
    parameters (not a form model), so the 0.147-0.162 form-model changes
    (booleans, empty strings, defaults) leave it unchanged. All results below
    were identical on FastAPI 0.136.1 and 0.163.1.
    """

    @pytest.fixture(autouse=True)
    def _user(self, client):
        register_ok(client, username="formuser")

    def test_plain_form_logs_in(self, client):
        r = client.post(
            "/auth/login", data={"username": "formuser", "password": PASSWORD}
        )
        assert r.status_code == 200, r.text

    def test_swagger_ui_form_with_empty_optional_fields_logs_in(self, client):
        """Swagger's "Authorize" dialog sends grant_type and empty extras."""
        r = client.post(
            "/auth/login",
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

    def test_empty_grant_type_counts_as_omitted(self, client):
        r = client.post(
            "/auth/login",
            data={"grant_type": "", "username": "formuser", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text

    def test_multipart_form_logs_in(self, client):
        r = client.post(
            "/auth/login",
            files={"username": (None, "formuser"), "password": (None, PASSWORD)},
        )
        assert r.status_code == 200, r.text

    def test_other_grant_type_is_rejected(self, client):
        errors = _validation_errors(
            client.post(
                "/auth/login",
                data={
                    "grant_type": "client_credentials",
                    "username": "formuser",
                    "password": PASSWORD,
                },
            )
        )
        assert errors[0]["field"] == "grant_type"
        assert errors[0]["type"] == "string_pattern_mismatch"

    @pytest.mark.parametrize(
        ("data", "missing"),
        [
            ({"username": "", "password": PASSWORD}, ["username"]),
            ({"username": "formuser", "password": ""}, ["password"]),
            ({"password": PASSWORD}, ["username"]),
            ({}, ["username", "password"]),
        ],
    )
    def test_empty_or_absent_credentials_are_missing(self, client, data, missing):
        """Empty strings are treated like absent fields: 422 ``missing``."""
        errors = _validation_errors(client.post("/auth/login", data=data))

        assert [(e["field"], e["type"]) for e in errors] == [
            (name, "missing") for name in missing
        ]

    def test_json_body_is_not_a_form(self, client):
        errors = _validation_errors(
            client.post(
                "/auth/login", json={"username": "formuser", "password": PASSWORD}
            )
        )
        assert [(e["field"], e["type"]) for e in errors] == [
            ("username", "missing"),
            ("password", "missing"),
        ]
