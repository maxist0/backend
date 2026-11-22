"""The service owns the transaction: no module's `crud.py` commits or rolls back.

A crud function that commits ends the caller's unit of work halfway, so a
use case can no longer be atomic. `begin()` and `begin_nested()` count as
well: used as context managers they commit (or release) on exit. The
modules come from `MODULES`, so a new module is checked without editing
this test.
"""

import ast
from pathlib import Path

import app.modules
from app.modules import MODULES

MODULES_DIR = Path(app.modules.__file__).resolve().parent
TRANSACTION_METHODS = {"commit", "rollback", "begin", "begin_nested"}


def _transaction_calls(path: Path) -> list[str]:
    """Every call of a `TRANSACTION_METHODS` method in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        f"{path.parent.name}/{path.name}:{node.lineno}: .{node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TRANSACTION_METHODS
    ]


def test_crud_modules_never_commit_or_roll_back() -> None:
    crud_files = [
        MODULES_DIR / module.name / "crud.py"
        for module in MODULES
        if (MODULES_DIR / module.name / "crud.py").is_file()
    ]
    assert crud_files, f"no crud.py found under {MODULES_DIR}"

    violations = [call for path in crud_files for call in _transaction_calls(path)]

    assert violations == []
