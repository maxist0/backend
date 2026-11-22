"""The migrations create exactly the models' schema, with names, and round-trip.

`upgrade head`, `downgrade base` and `upgrade head` again run in a throwaway
database of the session's container, so the shared test schema is never
downgraded. `alembic check` compares the result with the models; it cannot
see the values of a CHECK constraint, so the enum checks are compared with
the models here. Everything is derived from `Base.metadata`, so a new
module's tables are covered without an edit here. Alembic gets a `Config`
without an ini file: the ini's logging setup would disable the loggers other
tests assert on.
"""

import os
import re
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, Enum, create_engine, make_url, text

import app.modules  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every module's tables on Base.metadata
from app.core import Base
from app.core.database import NAMING_CONVENTION
from tests.helpers import BACKEND_DIR

# A value in PostgreSQL's rendering of `col IN (...)` on a varchar column.
_CHECK_VALUE = re.compile(r"'((?:[^']|'')*)'::character varying")
_PREFIX_BY_CONSTRAINT_TYPE = {"p": "pk_", "f": "fk_", "u": "uq_", "c": "ck_"}


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def scratch_db(
    sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """An empty database; `DATABASE_URL` points at it for Alembic."""
    name = f"migration_{uuid.uuid4().hex[:12]}"
    admin = sync_engine.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(os.environ["DATABASE_URL"]).set(database=name)
    rendered = url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", rendered)
    engine = create_engine(rendered)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _public_tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _public_types(engine: Engine) -> set[str]:
    """Enum, domain and range types; table row types are not counted."""
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'r')"
                )
            ).scalars()
        )


def test_round_trip_leaves_nothing_behind_and_matches_the_models(
    scratch_db: Engine,
) -> None:
    cfg = _alembic_config()

    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _public_tables(scratch_db) == {*Base.metadata.tables, "alembic_version"}

    command.downgrade(cfg, "base")
    assert _public_tables(scratch_db) == {"alembic_version"}
    assert _public_types(scratch_db) == set()

    command.upgrade(cfg, "head")
    command.check(cfg)


def _enum_columns() -> list[tuple[str, str, Enum]]:
    """`(table, column, type)` of every non-native enum column of the models."""
    return [
        (table.name, column.name, column.type)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, Enum) and not column.type.native_enum
    ]


def test_models_store_enums_without_native_types() -> None:
    """Every enum column uses `value_enum`: VARCHAR plus a CHECK, no PG enum."""
    native = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, Enum) and column.type.native_enum
    ]

    assert native == []
    assert _enum_columns()


@pytest.mark.parametrize(
    ("table", "column", "enum_type"),
    _enum_columns(),
    ids=[f"{table}.{column}" for table, column, _ in _enum_columns()],
)
def test_enum_check_constraints_hold_the_model_values(
    sync_engine: Engine, table: str, column: str, enum_type: Enum
) -> None:
    """A value added to a Python enum needs a migration of its CHECK."""
    name = NAMING_CONVENTION["ck"] % {
        "table_name": table,
        "constraint_name": enum_type.name,
    }
    with sync_engine.connect() as conn:
        definition = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = CAST(:table AS regclass)"
            ),
            {"name": name, "table": table},
        ).scalar_one()

    assert f"({column})::text" in definition
    stored = {value.replace("''", "'") for value in _CHECK_VALUE.findall(definition)}
    assert stored == set(enum_type.enums)


def test_constraint_names_follow_the_naming_convention(sync_engine: Engine) -> None:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, c.contype FROM pg_constraint c "
                "JOIN pg_class r ON r.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = r.relnamespace "
                "WHERE n.nspname = 'public' AND r.relname <> 'alembic_version' "
                "AND c.contype IN ('p', 'f', 'u', 'c')"
            )
        ).all()

    misnamed = [
        name
        for name, contype in rows
        if not name.startswith(_PREFIX_BY_CONSTRAINT_TYPE[contype])
    ]
    assert rows
    assert misnamed == []
