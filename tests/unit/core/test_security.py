"""Security defaults: access-token lifetime."""

from datetime import timedelta


class TestAccessTokenExpiry:
    def test_default_lifetime_is_fifteen_minutes(self, settings_factory):
        """Spec guard: a change of the default must be a deliberate decision."""
        lifetime = timedelta(minutes=settings_factory().ACCESS_TOKEN_EXPIRE_MINUTES)

        assert lifetime == timedelta(minutes=15)
