"""Security defaults and the password-verify timing helpers."""

from datetime import timedelta

import anyio
import pytest

from app.core import security
from app.core.security import VerifyDurationAverage
from tests.helpers import SettingsFactory


class TestAccessTokenExpiry:
    def test_default_lifetime_is_fifteen_minutes(
        self, settings_factory: SettingsFactory
    ) -> None:
        """Spec guard: a change of the default must be a deliberate decision."""
        lifetime = timedelta(minutes=settings_factory().ACCESS_TOKEN_EXPIRE_MINUTES)

        assert lifetime == timedelta(minutes=15)


class TestVerifyDurationAverage:
    def test_cold_start_value_until_the_first_sample(self) -> None:
        assert VerifyDurationAverage(0.05).seconds == 0.05

    def test_first_sample_replaces_the_cold_start_value(self) -> None:
        average = VerifyDurationAverage(0.05)

        average.observe(0.2)

        assert average.seconds == 0.2

    def test_later_samples_move_the_average_by_the_weight(self) -> None:
        average = VerifyDurationAverage(0.05, weight=0.25)
        average.observe(0.1)

        average.observe(0.3)  # 0.1 + 0.25 * (0.3 - 0.1)
        assert average.seconds == pytest.approx(0.15)
        average.observe(0.15)  # unchanged: sample equals the average
        assert average.seconds == pytest.approx(0.15)

    def test_one_outlier_moves_the_average_only_by_its_weight(self) -> None:
        average = VerifyDurationAverage(0.05)
        for _ in range(20):
            average.observe(0.04)

        average.observe(1.04)

        assert average.seconds == pytest.approx(0.24)


@pytest.fixture
def fresh_average(monkeypatch: pytest.MonkeyPatch) -> VerifyDurationAverage:
    average = VerifyDurationAverage(0.05)
    monkeypatch.setattr(security, "password_verify_seconds", average)
    return average


class TestWaitLikePasswordVerify:
    def test_sleeps_for_the_current_average(
        self, fresh_average: VerifyDurationAverage
    ) -> None:
        fresh_average.observe(0.042)
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        anyio.run(security.wait_like_password_verify, record)

        assert slept == [0.042]

    @pytest.mark.usefixtures("fresh_average")
    def test_sleeps_for_the_cold_start_value_before_any_verify(self) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        anyio.run(security.wait_like_password_verify, record)

        assert slept == [0.05]


class TestVerifiesFeedTheAverage:
    def test_verify_password_records_its_duration(
        self, fresh_average: VerifyDurationAverage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _timed_verify(_password: str, _hash: str) -> tuple[bool, float]:
            return False, 0.123

        monkeypatch.setattr(security, "_timed_verify_sync", _timed_verify)

        valid = anyio.run(security.verify_password, "pw", "hash")

        assert valid is False
        assert fresh_average.seconds == 0.123

    def test_equalize_password_timing_records_its_duration(
        self, fresh_average: VerifyDurationAverage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _burn_verify(_password: str) -> float:
            return 0.077

        monkeypatch.setattr(security, "_burn_verify_sync", _burn_verify)

        anyio.run(security.equalize_password_timing, "pw")

        assert fresh_average.seconds == 0.077


class TestDeriveKey:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            (
                b"rate-limit identifier key v1",
                "c9e1bbe703ef6bbe49825b170ffffb4f4db53a13e5ba7bb352023beb4afd4a02",
            ),
            (
                b"idempotency fingerprint key v1",
                "fa0733b78baaffc49252a8395bb40603b3aa160db0a244da8574178a5c4aea96",
            ),
        ],
    )
    def test_pinned_vectors(self, label: bytes, expected: str) -> None:
        """HMAC-SHA256 of the label under the secret, one key per purpose."""
        assert security.derive_key("k" * 32, label).hex() == expected
