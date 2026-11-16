"""The email tasks never log the recipient, the username or the token."""

import logging

import pytest

PII_USER = "zebra_user_91"
PII_MAIL = "zebra.mail91@example.com"


def _log_text(caplog) -> str:
    return "\n".join(f"{r.name} {r.msg} {r.getMessage()}" for r in caplog.records)


class TestLoggingHygiene:
    @pytest.mark.parametrize("outcome", ["sent", "failed"])
    @pytest.mark.parametrize("task_name", ["verification", "password_reset"])
    def test_email_tasks_do_not_log_recipient_or_username(
        self, monkeypatch, caplog, task_name, outcome
    ):
        import asyncio

        from app.utils import email_tasks

        async def _send(**_kwargs):
            if outcome == "failed":
                raise RuntimeError("smtp down")

        monkeypatch.setattr(email_tasks, "send_templated_email", _send)
        task = (
            email_tasks.send_verification_email_task
            if task_name == "verification"
            else email_tasks.send_password_reset_email_task
        )
        caplog.set_level(logging.DEBUG)

        try:
            asyncio.run(task.original_func(PII_MAIL, PII_USER, "tok-123"))
        except RuntimeError:
            assert outcome == "failed"

        text = _log_text(caplog)
        assert caplog.records
        assert PII_MAIL not in text
        assert PII_USER not in text
        assert "tok-123" not in text
