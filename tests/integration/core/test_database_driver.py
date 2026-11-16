"""Constraint names from psycopg's diagnostics on the async path."""

import asyncio
import os

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.users.models import User
from app.core.errors import constraint_name, find_db_error, sqlstate
from tests.helpers import LOOP_FACTORY


class TestPsycopgConstraintNames:
    def test_unique_violation_reports_constraint_and_sqlstate(self):
        """GUARD: the register conflict mapping relies on these names (async psycopg)."""

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
        try:
            raise RuntimeError("wrapper") from exc
        except RuntimeError as wrapper:
            assert find_db_error(wrapper) is exc
        assert constraint_name(exc) == "users_email_key"
        assert sqlstate(exc) == "23505"
