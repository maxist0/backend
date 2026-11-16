# src/common/db_errors.py

"""
Driver-independent inspection of database errors.

PostgreSQL error text can contain row values (``DETAIL: Key (email)=(...)``),
which are personal data. Code that reacts to a database error or logs one
uses only the structured fields read here: the exception type, the SQLSTATE
and the constraint name. Works with asyncpg (production URL) and psycopg 3
(test suite).
"""

from sqlalchemy.exc import DBAPIError


def _driver_error(exc: DBAPIError) -> object:
    return exc.orig


def constraint_name(exc: DBAPIError) -> str | None:
    """Name of the violated constraint, or None if the driver did not report one."""
    orig = _driver_error(exc)
    # psycopg 3: Diagnostic object on the exception.
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if isinstance(name, str) and name:
        return name
    # asyncpg: SQLAlchemy's adapter raises its own error ``from`` the asyncpg
    # exception, which carries the field.
    cause = getattr(orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) and name else None


def sqlstate(exc: DBAPIError) -> str | None:
    """SQLSTATE code of the error (e.g. ``23505`` unique violation)."""
    code = getattr(_driver_error(exc), "sqlstate", None)
    return code if isinstance(code, str) and code else None


def find_db_error(exc: BaseException) -> DBAPIError | None:
    """Return the first database error in *exc*'s cause/context chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, DBAPIError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
