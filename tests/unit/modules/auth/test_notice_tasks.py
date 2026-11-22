"""The lock notice's time text and the cleanup schedule."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from taskiq import ScheduledTask, async_shared_broker
from taskiq.cli.scheduler.run import is_cron_task_now
from taskiq.schedule_sources import LabelScheduleSource

from app.modules.auth.tasks import CLEANUP_CRON, lock_end_text

NOW = datetime(2026, 11, 17, 14, 20, tzinfo=UTC)


class TestLockEndText:
    def test_whole_minute_on_the_same_day(self) -> None:
        assert lock_end_text(datetime(2026, 11, 17, 14, 35, tzinfo=UTC), NOW) == (
            "14:35 UTC"
        )

    def test_seconds_round_up_so_the_time_is_never_early(self) -> None:
        end = datetime(2026, 11, 17, 14, 35, 0, 1, tzinfo=UTC)

        assert lock_end_text(end, NOW) == "14:36 UTC"

    def test_date_is_added_when_the_lock_ends_on_the_next_utc_day(self) -> None:
        now = datetime(2026, 11, 17, 23, 55, tzinfo=UTC)

        assert lock_end_text(now + timedelta(minutes=15), now) == (
            "00:10 UTC on 2026-11-18"
        )

    def test_other_offsets_are_converted_to_utc(self) -> None:
        berlin = timezone(timedelta(hours=1))
        end = datetime(2026, 11, 17, 15, 35, tzinfo=berlin)

        assert lock_end_text(end, NOW) == "14:35 UTC"


class TestCleanupSchedule:
    def _cleanup_schedule(self) -> ScheduledTask:
        # The scheduler's label source (`build_scheduler`).
        source = LabelScheduleSource(async_shared_broker)

        async def _schedules() -> list[ScheduledTask]:
            await source.startup()
            return await source.get_schedules()

        [schedule] = [
            s for s in asyncio.run(_schedules()) if s.task_name == "auth.cleanup"
        ]
        return schedule

    def test_runs_daily_at_three_utc_without_offset(self) -> None:
        schedule = self._cleanup_schedule()

        assert (schedule.cron, schedule.cron_offset) == ("0 3 * * *", None)

    @pytest.mark.parametrize(
        ("now", "due"),
        [
            (datetime(2026, 11, 18, 3, 0, tzinfo=UTC), True),
            (datetime(2026, 11, 18, 2, 0, tzinfo=UTC), False),
            (datetime(2026, 11, 18, 3, 1, tzinfo=UTC), False),
        ],
    )
    def test_taskiq_reads_the_cron_in_utc(self, now: datetime, *, due: bool) -> None:
        """The scheduler passes `datetime.now(tz=timezone.utc)`; no offset."""
        assert is_cron_task_now(CLEANUP_CRON, now, offset=None) is due
