"""Access token validation: required claims, type, algorithm and expiry.

Tokens are forged with the app's own key, so each negative case fails only
because of the claim or header it changes.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.helpers import (
    Db,
    bearer,
    forge_access_token,
    jwt_secret,
    register_ok,
    user_id_of,
)


def _call_me(client: TestClient, token: str) -> Response:
    """GET /users/me; a server-side exception fails the contract as well."""
    try:
        return client.get("/api/v1/users/me", headers=bearer(token))
    except Exception as exc:  # noqa: BLE001 - TestClient re-raises app exceptions
        pytest.fail(f"server raised {type(exc).__name__} instead of answering 401")


class TestJwtValidation:
    @pytest.mark.usefixtures("captured_emails")
    def test_forged_token_with_all_claims_is_accepted(
        self, client: TestClient, db: Db
    ) -> None:
        """Control: a forged token with every claim is accepted.

        So the negative cases below fail only because of the claim they remove.
        """
        register_ok(client, username="alice")
        token = forge_access_token(user_id_of(db, "alice"), secret=jwt_secret())
        assert _call_me(client, token).status_code == 200

    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("claim", ["exp", "sub", "jti", "iat", "nbf"])
    def test_token_missing_required_claim_returns_401(
        self, client: TestClient, db: Db, claim: str
    ) -> None:
        register_ok(client, username="alice")
        token = forge_access_token(
            user_id_of(db, "alice"), secret=jwt_secret(), drop=(claim,)
        )
        assert _call_me(client, token).status_code == 401

    @pytest.mark.parametrize(
        "sub", ["None", "not-a-uuid", "", "1", "' OR 1=1 --"], ids=repr
    )
    def test_token_with_non_uuid_sub_returns_401(
        self, client: TestClient, sub: str
    ) -> None:
        token = forge_access_token("x", secret=jwt_secret(), sub=sub)
        assert _call_me(client, token).status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_token_of_wrong_type_returns_401(self, client: TestClient, db: Db) -> None:
        register_ok(client, username="alice")
        token = forge_access_token(
            user_id_of(db, "alice"), secret=jwt_secret(), type="refresh"
        )
        assert _call_me(client, token).status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_token_signed_with_other_algorithm_returns_401(
        self, client: TestClient, db: Db
    ) -> None:
        """The algorithm list is fixed in code."""
        register_ok(client, username="alice")
        token = forge_access_token(
            user_id_of(db, "alice"), secret=jwt_secret() * 2, algorithm="HS512"
        )
        assert _call_me(client, token).status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_unsigned_alg_none_token_returns_401(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        claims = jwt.decode(
            forge_access_token(user_id_of(db, "alice"), secret=jwt_secret()),
            options={"verify_signature": False},
        )
        # PyJWT maps the empty key to "no key" for the `none` algorithm.
        unsigned = jwt.encode(claims, key="", algorithm="none")
        assert _call_me(client, unsigned).status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_expired_token_returns_401(self, client: TestClient, db: Db) -> None:

        register_ok(client, username="alice")
        token = forge_access_token(
            user_id_of(db, "alice"),
            secret=jwt_secret(),
            exp=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert _call_me(client, token).status_code == 401
