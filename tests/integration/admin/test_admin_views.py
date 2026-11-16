"""sqladmin 0.32 list, detail and edit pages on SQLAlchemy 2.1.

The action tests drive sqladmin's action endpoints only. The pages below are
where sqladmin builds its own ORM queries with relationship loader options
(SQLAlchemy 2.1 changed how loader options apply across paths) and renders
relationship values. Every view gets a populated row: a user with a password
credential, a session, an email token and audit entries.
"""

import pytest

from tests.helpers import login_tokens, register_ok
from tests.integration.admin.test_admin_actions import admin_login

# (sqladmin identity, table, has an edit page)
VIEWS = [
    ("user", "users", True),
    ("password-credential", "password_credentials", True),
    ("auth-audit-log", "auth_audit_log", False),
    ("email-token", "email_tokens", True),
    ("session", "sessions", True),
]


@pytest.fixture
def populated_admin(client, db, captured_emails):
    register_ok(client, username="adminview")
    login_tokens(client, "adminview")  # creates a session row
    admin_login(client)
    return client


@pytest.mark.parametrize(("identity", "table", "editable"), VIEWS)
def test_list_detail_and_edit_pages_render(
    populated_admin, db, identity, table, editable
):
    client = populated_admin
    pk = str(db.scalar(f"SELECT id FROM {table} LIMIT 1"))  # noqa: S608 (fixed names)

    list_page = client.get(f"/admin/{identity}/list")
    detail_page = client.get(f"/admin/{identity}/details/{pk}")

    assert list_page.status_code == 200, list_page.text[:500]
    assert detail_page.status_code == 200, detail_page.text[:500]
    assert pk in list_page.text
    if editable:
        edit_page = client.get(f"/admin/{identity}/edit/{pk}")
        assert edit_page.status_code == 200, edit_page.text[:500]


def test_user_detail_shows_its_related_rows(populated_admin, db):
    """Relationships of the user (sessions, tokens, audit log) are loaded."""
    user_id = str(db.scalar("SELECT id FROM users WHERE username = 'adminview'"))
    session_id = str(db.scalar("SELECT id FROM sessions LIMIT 1"))

    r = populated_admin.get(f"/admin/user/details/{user_id}")

    assert r.status_code == 200
    assert "adminview" in r.text
    assert session_id in r.text
