"""The auth module is registered and declares its tables and tasks."""

from app.modules import MODULES
from app.modules.auth import module
from tests.helpers import declared_tasks, module_tables

# Queued messages carry these names; renaming one strands messages in Redis.
TASK_NAMES = [
    "auth.cleanup",
    "email.send_account_locked_notice",
    "email.send_email_change_completed_notice",
    "email.send_email_change_requested_notice",
    "email.send_token_email",
]


def test_the_module_is_registered() -> None:
    assert module in MODULES


def test_declares_its_tables() -> None:
    assert module_tables(module) == {
        "auth_audit_log",
        "email_tokens",
        "password_credentials",
        "sessions",
    }


def test_declares_its_tasks_under_stable_names() -> None:
    assert sorted(t.task_name for t in declared_tasks(module.tasks)) == TASK_NAMES
