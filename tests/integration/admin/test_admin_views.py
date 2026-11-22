"""sqladmin 0.32 pages on SQLAlchemy 2.1, and which views exist and allow what.

The list and detail pages are where sqladmin builds its own ORM queries with
relationship loader options (SQLAlchemy 2.1 changed how loader options apply
across paths) and renders relationship values. Every view gets a populated
row: a user with a session and audit entries.

All views are read-only: every change goes through an action that calls a
service. sqladmin answers the create, edit and delete endpoints of a view
with `can_*` False with 403, before it reads a form or a row.
"""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    Db,
    admin_login,
    login_tokens,
    register_ok,
    run_action,
)

# (sqladmin identity, table)
VIEWS: list[tuple[str, str]] = [
    ("user", "users"),
    ("auth-audit-log", "auth_audit_log"),
    ("session", "sessions"),
]


# Registration sends a verification mail; record it instead of skipping it.
pytestmark = pytest.mark.usefixtures("captured_emails")


@pytest.fixture
def populated_admin(client: TestClient) -> TestClient:
    register_ok(client, username="adminview")
    login_tokens(client, "adminview")  # creates a session row
    admin_login(client)
    return client


def _first_pk(db: Db, table: str) -> str:
    return str(db.scalar(f"SELECT id FROM {table} LIMIT 1"))


@pytest.mark.parametrize(("identity", "table"), VIEWS)
def test_list_and_detail_pages_render(
    populated_admin: TestClient, db: Db, identity: str, table: str
) -> None:
    client = populated_admin
    pk = _first_pk(db, table)

    list_page = client.get(f"/admin/{identity}/list")
    detail_page = client.get(f"/admin/{identity}/details/{pk}")

    assert list_page.status_code == 200, list_page.text[:500]
    assert detail_page.status_code == 200, detail_page.text[:500]
    assert pk in list_page.text


@pytest.mark.parametrize(("identity", "table"), VIEWS)
def test_views_refuse_create_edit_and_delete(
    populated_admin: TestClient, db: Db, identity: str, table: str
) -> None:
    client = populated_admin
    pk = _first_pk(db, table)
    rows_before = db.rows(f"SELECT * FROM {table} ORDER BY id")

    responses = {
        "create page": client.get(f"/admin/{identity}/create"),
        "create": client.post(f"/admin/{identity}/create", data={"username": "x"}),
        "edit page": client.get(f"/admin/{identity}/edit/{pk}"),
        "edit": client.post(f"/admin/{identity}/edit/{pk}", data={"username": "x"}),
        "delete": client.delete(f"/admin/{identity}/delete", params={"pks": pk}),
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
        responses, 403
    )
    assert db.rows(f"SELECT * FROM {table} ORDER BY id") == rows_before


@pytest.mark.parametrize(
    "path",
    [
        "/admin/password-credential/list",
        "/admin/email-token/list",
    ],
)
def test_removed_views_are_gone(populated_admin: TestClient, path: str) -> None:
    assert populated_admin.get(path).status_code == 404


@pytest.mark.parametrize(
    ("identity", "slug"),
    [
        ("email-token", "invalidate-tokens"),
        ("session", "revoke-all-expired"),
    ],
)
def test_removed_actions_are_gone(
    populated_admin: TestClient, db: Db, identity: str, slug: str
) -> None:
    pk = _first_pk(db, "sessions")

    r = run_action(populated_admin, identity, slug, [pk])

    assert r.status_code == 404
    assert db.scalar("SELECT count(*) FROM sessions WHERE is_revoked") == 0


def test_user_list_offers_the_service_actions(populated_admin: TestClient) -> None:
    page = populated_admin.get("/admin/user/list").text

    for slug in (
        "lock-users",
        "unlock-users",
        "deactivate-users",
        "reactivate-users",
        "force-password-change",
    ):
        assert f"/admin/user/action/{slug}" in page
    assert "/admin/user/create" not in page


def test_session_detail_shows_its_user(populated_admin: TestClient, db: Db) -> None:
    """The many-to-one relationship `Session.user` is loaded on the detail page.

    `User` has no reverse relationships (the users module does not depend on
    auth), so the relationship loading is checked from the session side.
    """
    session_id = _first_pk(db, "sessions")

    r = populated_admin.get(f"/admin/session/details/{session_id}")

    assert r.status_code == 200
    assert "adminview (adminview@example.com)" in r.text  # User.__str__
