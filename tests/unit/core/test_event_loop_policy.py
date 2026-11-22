"""No code sets the deprecated asyncio event loop policy."""

from pathlib import Path

from tests.helpers import BACKEND_DIR


def test_nothing_sets_the_deprecated_event_loop_policy() -> None:
    """Loops are made through loop factories only.

    Asyncio's policy system is deprecated (removal in 3.16).
    """
    needle = "set_event_loop" + "_policy("
    selector_policy = "WindowsSelector" + "EventLoopPolicy("
    offenders = [
        str(path.relative_to(BACKEND_DIR))
        for root in ("src", "tests", "alembic")
        for path in Path(BACKEND_DIR, root).rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
        or selector_policy in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
