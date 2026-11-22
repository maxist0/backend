"""The base names constraints; the mixins and `value_enum` map as documented."""

import enum

from sqlalchemy import CheckConstraint, Enum, UniqueConstraint

import app.modules  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every module's tables on Base.metadata
from app.core import Base, value_enum
from app.core.database import NAMING_CONVENTION
from app.modules.users.models import EMAIL_UNIQUE_CONSTRAINT, USERNAME_UNIQUE_INDEX

USERS = Base.metadata.tables["users"]
SESSIONS = Base.metadata.tables["sessions"]
AUDIT_LOG = Base.metadata.tables["auth_audit_log"]


class _Colour(enum.Enum):
    LIGHT_BLUE = "light_blue"
    RED = "red"


def test_metadata_carries_the_naming_convention() -> None:
    assert NAMING_CONVENTION.items() <= Base.metadata.naming_convention.items()


def test_value_enum_is_a_checked_varchar_of_the_values() -> None:
    column_type = value_enum(_Colour)

    assert isinstance(column_type, Enum)
    assert column_type.native_enum is False
    assert column_type.create_constraint is True
    assert column_type.enums == ["light_blue", "red"]
    assert column_type.length == 64
    assert column_type.name == "_colour"


def test_mixins_put_the_key_first_and_the_timestamps_last() -> None:
    columns = [column.name for column in USERS.columns]

    assert columns[0] == "id"
    assert columns[-2:] == ["created_at", "updated_at"]


def test_timestamps_have_database_defaults() -> None:
    for table in (USERS, SESSIONS, AUDIT_LOG):
        created_at = table.c.created_at
        assert created_at.server_default is not None
        assert not created_at.nullable
    assert USERS.c.updated_at.server_default is not None
    assert USERS.c.updated_at.onupdate is not None


def test_only_rows_that_change_have_updated_at() -> None:
    assert "updated_at" not in SESSIONS.c
    assert "updated_at" not in AUDIT_LOG.c


def test_named_user_constraints_exist_under_their_constants() -> None:
    """The register conflict mapping relies on these names."""
    [username_index] = USERS.indexes
    unique_constraints = {
        c.name: [column.name for column in c.columns]
        for c in USERS.constraints
        if isinstance(c, UniqueConstraint)
    }

    assert username_index.name == USERNAME_UNIQUE_INDEX
    assert username_index.unique
    assert unique_constraints == {EMAIL_UNIQUE_CONSTRAINT: ["email"]}


def test_sessions_cannot_outlive_their_family() -> None:
    checks = {
        str(c.sqltext) for c in SESSIONS.constraints if isinstance(c, CheckConstraint)
    }

    assert "expires_at <= family_expires_at" in checks
