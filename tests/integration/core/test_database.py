"""The app's database layer on PostgreSQL: session factory and constraint names."""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import build_engine, create_session_factory
from app.core.errors import constraint_name, find_db_error, sqlstate
from app.modules.auth.models import AuditEventType, AuthAuditLog
from app.modules.users.models import User
from tests.helpers import (
    LOOP_FACTORY,
    run_async,
)


class TestPsycopgConstraintNames:
    def test_unique_violation_reports_constraint_and_sqlstate(self) -> None:
        """The register conflict mapping relies on these names (async psycopg)."""

        async def _duplicate_email() -> IntegrityError:
            engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
            try:
                async with AsyncSession(engine) as session:
                    session.add(User(username="first_user", email="dup@example.com"))
                    await session.commit()
                    session.add(User(username="second_user", email="dup@example.com"))
                    with pytest.raises(IntegrityError) as excinfo:
                        await session.commit()
                    return excinfo.value
            finally:
                await engine.dispose()

        exc = asyncio.run(_duplicate_email(), loop_factory=LOOP_FACTORY)

        assert type(exc.orig).__module__.startswith("psycopg")
        wrapper = RuntimeError("wrapper")
        wrapper.__cause__ = exc  # what `raise ... from exc` sets
        assert find_db_error(wrapper) is exc
        assert constraint_name(exc) == "uq_users_email"
        assert sqlstate(exc) == "23505"


class TestSessionFactory:
    def test_app_sessions_do_not_autoflush_before_core_statements(self) -> None:
        """Pending objects stay invisible to a raw statement in the same session.

        The app's session factory sets `autoflush=False`. The control session
        with `autoflush=True` proves the statement would see the row:
        SQLAlchemy autoflushes before Core and `text()` statements too.
        """
        count = text("SELECT count(*) FROM auth_audit_log")

        async def scenario() -> tuple[int, int]:
            engine = build_engine(get_settings())
            try:
                async with create_session_factory(engine)() as session:
                    session.add(AuthAuditLog(event_type=AuditEventType.REGISTER))
                    app_session = await session.scalar(count)
                    await session.rollback()
                autoflushing = async_sessionmaker(engine, autoflush=True)
                async with autoflushing() as session:
                    session.add(AuthAuditLog(event_type=AuditEventType.REGISTER))
                    control = await session.scalar(count)
                    await session.rollback()
            finally:
                await engine.dispose()
            return app_session, control

        assert run_async(scenario()) == (0, 1)
