"""The users module is registered and declares its table and no tasks."""

from app.modules import MODULES
from app.modules.users import module
from tests.helpers import module_tables


def test_the_module_is_registered() -> None:
    assert module in MODULES


def test_declares_the_users_table() -> None:
    assert module_tables(module) == {"users"}


def test_declares_no_tasks() -> None:
    assert module.tasks is None
