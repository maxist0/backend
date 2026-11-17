"""`app.core.admin`: action helpers, the admin session cookie and the
cross-site request guard."""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.middleware import Middleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from app.core.admin import (
    AdminAuth,
    FetchMetadataGuard,
    back_to_list,
    selected_ids,
    session_factory,
)


def _request(query_string: str) -> Request:
    return Request({"type": "http", "query_string": query_string.encode()})


class TestSelectedIds:
    def test_returns_the_uuids_in_request_order(self):
        first, second = uuid.uuid4(), uuid.uuid4()

        assert selected_ids(_request(f"pks={first},{second}")) == [first, second]

    def test_skips_values_that_are_not_uuids(self):
        valid = uuid.uuid4()

        ids = selected_ids(_request(f"pks=1,{valid},,%27%3B--,not-a-uuid"))

        assert ids == [valid]

    @pytest.mark.parametrize("query", ["", "pks=", "other=1"])
    def test_no_selection_is_an_empty_list(self, query):
        assert selected_ids(_request(query)) == []


class TestSessionFactory:
    def test_returns_the_async_factory_of_the_view(self):
        factory = async_sessionmaker(class_=AsyncSession)

        assert session_factory(SimpleNamespace(session_maker=factory)) is factory

    def test_refuses_a_sync_factory(self):
        with pytest.raises(TypeError, match="async session factory"):
            session_factory(SimpleNamespace(session_maker=sessionmaker()))


class TestBackToList:
    @pytest.fixture
    def client(self) -> TestClient:
        def list_page(_request: Request) -> Response:
            return PlainTextResponse("list")

        def action(request: Request) -> Response:
            return back_to_list(request, "user")

        admin = Mount(
            "/admin",
            routes=[
                Route("/{identity}/list", list_page, name="list"),
                Route("/user/action/lock", action),
            ],
            name="admin",
        )
        return TestClient(Starlette(routes=[admin]))

    def test_redirects_to_the_list_path_with_303(self, client):
        r = client.get("/admin/user/action/lock", follow_redirects=False)

        assert r.status_code == 303
        assert r.headers["location"] == "/admin/user/list"

    def test_ignores_referer_and_host(self, client):
        r = client.get(
            "/admin/user/action/lock",
            headers={"Referer": "https://evil.example/phish", "Host": "evil.example"},
            follow_redirects=False,
        )

        assert r.headers["location"] == "/admin/user/list"


STRONG_ADMIN_SECRETS = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
    "FRONTEND_URL": "https://app.example.com",
}


def _admin_session_cookie(settings) -> list[str]:
    """Attributes of the cookie the admin session middleware writes (no value)."""

    def sign_in(request: Request) -> Response:
        request.session["admin"] = True
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", sign_in)], middleware=AdminAuth(settings).middlewares
    )
    header = TestClient(app).get("/").headers["set-cookie"]
    return [part.strip().lower() for part in header.split(";")[1:]]


class TestAdminSessionCookie:
    @pytest.mark.parametrize("environment", ["dev", "unittest"])
    def test_plain_http_environments_get_a_strict_cookie_without_secure(
        self, settings_factory, environment
    ):
        cookie = _admin_session_cookie(settings_factory(ENVIRONMENT=environment))

        assert "samesite=strict" in cookie
        assert "httponly" in cookie
        assert f"max-age={8 * 60 * 60}" in cookie
        assert "secure" not in cookie

    @pytest.mark.parametrize("environment", ["test", "stage", "prod"])
    def test_tls_environments_get_a_secure_strict_cookie(
        self, settings_factory, environment
    ):
        settings = settings_factory(ENVIRONMENT=environment, **STRONG_ADMIN_SECRETS)

        cookie = _admin_session_cookie(settings)

        assert "samesite=strict" in cookie
        assert "httponly" in cookie
        assert f"max-age={8 * 60 * 60}" in cookie
        assert "secure" in cookie


class TestFetchMetadataGuard:
    @pytest.fixture
    def client(self) -> TestClient:
        def ok(_request: Request) -> Response:
            return PlainTextResponse("ok")

        admin = Mount(
            "/admin",
            routes=[
                Route("/login", ok, methods=["GET", "POST"]),
                Route("/user/list", ok),
                Route("/user/action/lock-users", ok),
                Route("/user/delete", ok, methods=["DELETE"]),
            ],
        )
        return TestClient(
            Starlette(routes=[admin], middleware=[Middleware(FetchMetadataGuard)])
        )

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/admin/user/action/lock-users"),
            ("POST", "/admin/login"),
            ("DELETE", "/admin/user/delete"),
        ],
    )
    @pytest.mark.parametrize(
        "fetch_site", ["cross-site", "same-site", "none", None], ids=str
    )
    def test_rejects_state_changes_that_are_not_same_origin(
        self, client, method, path, fetch_site
    ):
        headers = {"Sec-Fetch-Site": fetch_site} if fetch_site else {}

        r = client.request(method, path, headers=headers)

        assert r.status_code == 403

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/admin/user/action/lock-users"),
            ("POST", "/admin/login"),
            ("DELETE", "/admin/user/delete"),
        ],
    )
    def test_allows_same_origin_state_changes(self, client, method, path):
        r = client.request(method, path, headers={"Sec-Fetch-Site": "same-origin"})

        assert r.status_code == 200

    @pytest.mark.parametrize("path", ["/admin/user/list", "/admin/login"])
    @pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {}])
    def test_allows_reading_pages_from_anywhere(self, client, path, headers):
        assert client.get(path, headers=headers).status_code == 200
