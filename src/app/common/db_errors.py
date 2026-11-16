# src/app/common/db_errors.py

"""
Driver-independent inspection of database errors.

PostgreSQL error text can contain row values (``DETAIL: Key (email)=(...)``),
which are personal data. Code that reacts to a database error or logs one
uses only the structured fields read here: the exception type, the SQLSTATE
and the constraint name. Reads psycopg 3's diagnostics (``exc.orig.diag``),
the only supported driver.
"""

from sqlalchemy.exc import DBAPIError


def _driver_error(exc: DBAPIError) -> object:
    return exc.orig


def constraint_name(exc: DBAPIError) -> str | None:
    """Name of the violated constraint, or None if the driver did not report one."""
    # psycopg 3: Diagnostic object on the exception.
    diag = getattr(_driver_error(exc), "diag", None)
    name = getattr(diag, "constraint_name", None)
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
