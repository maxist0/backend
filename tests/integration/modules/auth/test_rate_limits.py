"""The rate limits of the auth routes: each policy refuses at limit + 1.

Every test exhausts one policy with the smallest number of requests and
checks the 429 problem, `Retry-After` and that the key is the one the policy
names (client address, address and username, email address, user, session
family). Client addresses come from `X-Forwarded-For` through uvicorn's
proxy-header middleware (`behind_proxy`), as behind the production proxy.

The account lock is unchanged and runs behind these limits; the lockout
tests cover it.
"""

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime

import pytest
import time_machine
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from prometheus_client import REGISTRY

from app.modules.auth import dependencies as auth_dependencies
from app.modules.auth import service
from app.modules.users import crud as users_crud
from tests.helpers import (
    PASSWORD,
    CapturedEmails,
    Db,
    active_session_count,
    assert_rate_limited,
    bearer,
    behind_proxy,
    change_email,
    change_password,
    forwarded_for,
    login_tokens,
    new_test_client,
    rate_limit_keys,
    refresh_with,
    register_and_login,
    register_ok,
    verify_email,
)

IP_A = "203.0.113.10"
IP_B = "198.51.100.20"
MINUTE = 60
QUARTER_HOUR = 900
HOUR = 3_600
WRONG_PASSWORD = "WrongPass!1"


@pytest.fixture
def proxied(app: FastAPI) -> Iterator[TestClient]:
    """The test app behind the proxy-header middleware."""
    with new_test_client(behind_proxy(app)) as client:
        yield client


def _login(
    client: TestClient, username: str, ip: str, password: str = WRONG_PASSWORD
) -> Response:
    return client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
        headers=forwarded_for(ip),
    )


def _register(client: TestClient, username: str, ip: str) -> Response:
    return client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": PASSWORD,
        },
        headers=forwarded_for(ip),
    )


def _forgot(client: TestClient, email: str, ip: str) -> Response:
    return client.post(
        "/api/v1/auth/password/forgot", json={"email": email}, headers=forwarded_for(ip)
    )


def _without_request_id(r: Response) -> dict[str, object]:
    body: dict[str, object] = r.json()
    body.pop("request_id")
    return body


class _CallCounter:
    """Wraps an async function and counts its calls."""

    def __init__(self, func: Callable[..., Awaitable[object]]) -> None:
        self.func = func
        self.calls = 0

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        return await self.func(*args, **kwargs)


class TestLogin:
    def test_twenty_attempts_per_minute_per_address(self, proxied: TestClient) -> None:
        for i in range(20):
            assert _login(proxied, f"nobody{i}", IP_A).status_code == 401

        assert_rate_limited(_login(proxied, "nobody99", IP_A), window=MINUTE)
        assert _login(proxied, "nobody99", IP_B).status_code == 401

    def test_ten_attempts_per_quarter_hour_per_address_and_username(
        self, proxied: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(proxied, username="alice")
        authenticate = _CallCounter(service.authenticate_user)
        monkeypatch.setattr(auth_dependencies, "authenticate_user", authenticate)
        for _ in range(10):
            assert _login(proxied, "alice", IP_A).status_code == 401

        # Any casing is the same username, as for the lookup.
        r = _login(proxied, "ALICE", IP_A, password=PASSWORD)

        assert_rate_limited(r, window=QUARTER_HOUR)
        # Refused before the lookup, the admission limiter and Argon2.
        assert authenticate.calls == 10
        assert _login(proxied, "bob", IP_A).status_code == 401
        assert _login(proxied, "alice", IP_B).status_code == 401

    def test_the_refusal_does_not_tell_whether_the_account_exists(
        self, proxied: TestClient
    ) -> None:
        register_ok(proxied, username="alice")
        for _ in range(10):
            _login(proxied, "alice", IP_A)
            _login(proxied, "ghost", IP_B)

        # alice is locked by now (five failures), ghost never existed.
        existing = _login(proxied, "alice", IP_A)
        unknown = _login(proxied, "ghost", IP_B)

        assert existing.status_code == unknown.status_code == 429
        assert _without_request_id(existing) == _without_request_id(unknown)
        assert set(existing.headers) == set(unknown.headers)


class TestRegister:
    def test_five_registrations_per_hour_per_address(self, proxied: TestClient) -> None:
        for i in range(5):
            assert _register(proxied, f"user{i}", IP_A).status_code == 201

        assert_rate_limited(_register(proxied, "user9", IP_A), window=HOUR)
        assert _register(proxied, "user9", IP_B).status_code == 201


class TestPasswordForgot:
    def test_five_requests_per_quarter_hour_per_address(
        self, proxied: TestClient
    ) -> None:
        for i in range(5):
            assert _forgot(proxied, f"nobody{i}@example.com", IP_A).status_code == 202

        r = _forgot(proxied, "nobody9@example.com", IP_A)

        assert_rate_limited(r, window=QUARTER_HOUR)
        assert _forgot(proxied, "nobody9@example.com", IP_B).status_code == 202

    def test_three_requests_per_hour_per_address_before_any_lookup(
        self,
        proxied: TestClient,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_ok(proxied, username="alice")
        lookup = _CallCounter(users_crud.get_user_by_email)
        monkeypatch.setattr(users_crud, "get_user_by_email", lookup)
        for ip in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
            assert _forgot(proxied, "alice@example.com", ip).status_code == 202

        # Another address and another casing: the email address is the key.
        r = _forgot(proxied, "ALICE@example.com", "192.0.2.4")

        assert_rate_limited(r, window=HOUR)
        assert lookup.calls == 3
        assert len(captured_emails.of_kind("reset")) == 3

    def test_an_unknown_address_is_refused_the_same_way(
        self, proxied: TestClient
    ) -> None:
        register_ok(proxied, username="alice")
        for ip in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
            _forgot(proxied, "alice@example.com", ip)
            _forgot(proxied, "ghost@example.com", ip)

        known = _forgot(proxied, "alice@example.com", "192.0.2.4")
        unknown = _forgot(proxied, "ghost@example.com", "192.0.2.5")

        assert known.status_code == unknown.status_code == 429
        assert _without_request_id(known) == _without_request_id(unknown)


class TestTokenRoutes:
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (
                "/api/v1/auth/password/reset",
                {"token": "x", "new_password": "N3w-pass!"},
            ),
            ("/api/v1/auth/email/verify", {"token": "x"}),
        ],
    )
    def test_ten_attempts_per_quarter_hour_per_address(
        self, proxied: TestClient, path: str, body: dict[str, str]
    ) -> None:
        for _ in range(10):
            r = proxied.post(path, json=body, headers=forwarded_for(IP_A))
            assert r.status_code == 400

        r = proxied.post(path, json=body, headers=forwarded_for(IP_A))

        assert_rate_limited(r, window=QUARTER_HOUR)
        assert (
            proxied.post(path, json=body, headers=forwarded_for(IP_B)).status_code
            == 400
        )

    def test_reset_and_verify_count_separately(self, proxied: TestClient) -> None:
        for _ in range(10):
            proxied.post(
                "/api/v1/auth/email/verify",
                json={"token": "x"},
                headers=forwarded_for(IP_A),
            )

        r = proxied.post(
            "/api/v1/auth/password/reset",
            json={"token": "x", "new_password": "N3w-pass!"},
            headers=forwarded_for(IP_A),
        )

        assert r.status_code == 400


class TestVerificationResend:
    def test_three_requests_per_hour_per_user(self, proxied: TestClient) -> None:
        alice = bearer(register_and_login(proxied, username="alice"))
        bob = bearer(register_and_login(proxied, username="bob"))
        resend = "/api/v1/auth/email/verify/resend"
        for _ in range(3):
            assert proxied.post(resend, headers=alice).status_code == 202

        assert_rate_limited(proxied.post(resend, headers=alice), window=HOUR)
        assert proxied.post(resend, headers=bob).status_code == 202


class TestRefresh:
    def test_a_rotated_token_is_reuse_even_when_the_family_limit_is_used_up(
        self, proxied: TestClient, db: Db
    ) -> None:
        """A thief who keeps the family's window full cannot hide the reuse.

        The victim's rotated token skips the limit and revokes the family
        (RFC 9700 section 4.14.2).
        """
        register_ok(proxied, username="alice")
        first = login_tokens(proxied, "alice")
        current = first
        for _ in range(10):
            r, rotated = refresh_with(proxied, current.refresh)
            assert r.status_code == 200
            assert rotated
            current = rotated
        assert refresh_with(proxied, current.refresh)[0].status_code == 429
        reuse_before = REGISTRY.get_sample_value("auth_refresh_token_reuse_total") or 0

        r, _ = refresh_with(proxied, first.refresh)

        assert r.status_code == 401
        assert active_session_count(db, "alice") == 0
        assert REGISTRY.get_sample_value("auth_refresh_token_reuse_total") == (
            reuse_before + 1
        )

    def test_sixty_refreshes_per_minute_per_address(self, proxied: TestClient) -> None:
        cookie = {"Cookie": "refresh_token=unknown"}
        for _ in range(60):
            r = proxied.post(
                "/api/v1/auth/refresh", headers={**cookie, **forwarded_for(IP_A)}
            )
            assert r.status_code == 401

        r = proxied.post(
            "/api/v1/auth/refresh", headers={**cookie, **forwarded_for(IP_A)}
        )

        assert_rate_limited(r, window=MINUTE)
        assert "set-cookie" not in r.headers
        r = proxied.post(
            "/api/v1/auth/refresh", headers={**cookie, **forwarded_for(IP_B)}
        )
        assert r.status_code == 401

    def test_ten_refreshes_per_minute_per_session_family(
        self, proxied: TestClient
    ) -> None:
        register_ok(proxied, username="alice")
        start = datetime.now(UTC)
        with time_machine.travel(start, tick=True) as clock:
            tokens = login_tokens(proxied, "alice")
            for _ in range(10):
                r, rotated = refresh_with(proxied, tokens.refresh)
                assert r.status_code == 200
                assert rotated
                tokens = rotated

            r, _ = refresh_with(proxied, tokens.refresh)

            assert_rate_limited(r, window=MINUTE)
            # The refusal neither rotates nor clears the cookie.
            assert "set-cookie" not in r.headers
            other_family = login_tokens(proxied, "alice")
            assert refresh_with(proxied, other_family.refresh)[0].status_code == 200
            clock.shift(int(r.headers["retry-after"]))
            assert refresh_with(proxied, tokens.refresh)[0].status_code == 200


class TestIpv6Sites:
    """A client that rotates through the /64s of its /48 is still bounded."""

    @staticmethod
    def _site_address(n: int) -> str:
        return f"2001:db8:1:{n:x}::1"

    def test_login_allows_a_hundred_attempts_a_minute_per_48(
        self, proxied: TestClient
    ) -> None:
        # An empty form is a 422, but the route's limits count it first.
        for n in range(100):
            r = proxied.post(
                "/api/v1/auth/login",
                data={},
                headers=forwarded_for(self._site_address(n)),
            )
            assert r.status_code == 422

        r = proxied.post(
            "/api/v1/auth/login",
            data={},
            headers=forwarded_for(self._site_address(100)),
        )

        assert_rate_limited(r, window=MINUTE)
        other_site = forwarded_for("2001:db8:2::1")
        assert (
            proxied.post("/api/v1/auth/login", data={}, headers=other_site).status_code
            == 422
        )

    def test_forgot_password_allows_25_per_quarter_hour_per_48(
        self, proxied: TestClient
    ) -> None:
        for n in range(25):
            r = _forgot(proxied, f"nobody{n}@example.com", self._site_address(n))
            assert r.status_code == 202

        r = _forgot(proxied, "nobody99@example.com", self._site_address(25))

        assert_rate_limited(r, window=QUARTER_HOUR)

    def test_ipv4_clients_are_not_counted_per_48(self, proxied: TestClient) -> None:
        _login(proxied, "nobody", IP_A)
        _forgot(proxied, "nobody@example.com", IP_A)

        policies = {key.decode().split(":")[1] for key in rate_limit_keys()}
        assert "auth.login.ipv6_48" not in policies
        assert "auth.password_forgot.ipv6_48" not in policies
        assert {"auth.login.ip", "auth.password_forgot.ip"} <= policies


class TestMailRecipients:
    """Per-recipient limits against mail bombing, across accounts."""

    @pytest.mark.parametrize("registered", [False, True])
    def test_three_email_change_links_per_hour_to_one_address(
        self, proxied: TestClient, *, registered: bool
    ) -> None:
        target = "target@example.com"
        if registered:
            register_ok(proxied, username="eve", email=target)
        expected = 409 if registered else 202
        for name in ("alice", "bob", "carol"):
            access = register_and_login(proxied, username=name)
            assert change_email(proxied, access, target).status_code == expected

        dave = register_and_login(proxied, username="dave")
        r = change_email(proxied, dave, target)

        # The same answer whether or not the address belongs to an account.
        assert_rate_limited(r, window=HOUR)
        assert change_email(proxied, dave, "other@example.com").status_code == 202

    def test_three_verification_mails_per_hour_to_one_address(
        self, proxied: TestClient, captured_emails: CapturedEmails
    ) -> None:
        shared = "shared@example.com"
        alice = register_and_login(proxied, username="alice", email=shared)
        resend = "/api/v1/auth/email/verify/resend"
        for _ in range(3):
            assert proxied.post(resend, headers=bearer(alice)).status_code == 202
        # alice moves to another address, which frees the shared one.
        assert change_email(proxied, alice, "alice.new@example.com").status_code == 202
        token = captured_emails.latest_token("alice.new@example.com", "change_email")
        assert verify_email(proxied, token).status_code == 204
        bob = register_and_login(proxied, username="bob", email=shared)

        r = proxied.post(resend, headers=bearer(bob))

        assert_rate_limited(r, window=HOUR)


class TestCredentialChanges:
    def test_five_password_changes_per_quarter_hour_per_user(
        self, proxied: TestClient
    ) -> None:
        alice = register_and_login(proxied, username="alice")
        bob = register_and_login(proxied, username="bob")
        for _ in range(5):
            assert change_password(proxied, alice, current="wrong").status_code == 400

        r = change_password(proxied, alice, current="wrong")

        assert_rate_limited(r, window=QUARTER_HOUR)
        assert change_password(proxied, bob, current="wrong").status_code == 400

    def test_five_email_changes_per_quarter_hour_per_user(
        self, proxied: TestClient
    ) -> None:
        alice = register_and_login(proxied, username="alice")
        bob = register_and_login(proxied, username="bob")
        for i in range(5):
            r = change_email(proxied, alice, f"new{i}@example.com", password="wrong")
            assert r.status_code == 400

        r = change_email(proxied, alice, "new9@example.com", password="wrong")

        assert_rate_limited(r, window=QUARTER_HOUR)
        r = change_email(proxied, bob, "new9@example.com", password="wrong")
        assert r.status_code == 400
