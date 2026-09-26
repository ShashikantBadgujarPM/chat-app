"""Scheduling domain rules (docs/design/09 §16.3, §17.4)."""

import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.modules.conversations.domain.errors import ConversationNotFound
from app.modules.messaging.domain.errors import BodyEmpty
from app.modules.scheduling.domain.errors import (
    InvalidTimezone,
    NaiveDatetime,
    ScheduledInPast,
    ScheduledTooFar,
)
from app.modules.scheduling.domain.scheduled_message import (
    Permanent,
    PermanentDeliveryError,
    Transient,
    backoff,
    classify_failure,
    normalize_scheduled_at,
    validate_timezone,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
LEAD = timedelta(seconds=30)
HORIZON = timedelta(days=365)


def normalize(value: datetime) -> datetime:
    return normalize_scheduled_at(value, now=NOW, min_lead=LEAD, max_horizon=HORIZON)


class TestNormalizeScheduledAt:
    def test_offset_time_is_converted_to_utc(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        result = normalize(datetime(2026, 10, 1, 18, 0, tzinfo=ist))
        assert result == datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
        assert result.utcoffset() == timedelta(0)

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetime):
            normalize(datetime(2026, 10, 1, 18, 0))  # the point of the test

    def test_less_than_the_lead_time_ahead_is_in_the_past(self) -> None:
        with pytest.raises(ScheduledInPast):
            normalize(NOW + timedelta(seconds=29))

    def test_exactly_the_lead_time_ahead_is_allowed(self) -> None:
        assert normalize(NOW + LEAD) == NOW + LEAD

    def test_more_than_a_year_ahead_is_too_far(self) -> None:
        with pytest.raises(ScheduledTooFar):
            normalize(NOW + HORIZON + timedelta(seconds=1))


class TestValidateTimezone:
    @pytest.mark.parametrize("name", ["Asia/Kolkata", "Europe/London", "UTC"])
    def test_iana_names_are_accepted(self, name: str) -> None:
        assert validate_timezone(name) == name

    @pytest.mark.parametrize("name", ["", "Mars/Olympus", "../etc/passwd", "+05:30", "x" * 65])
    def test_anything_else_is_rejected(self, name: str) -> None:
        with pytest.raises(InvalidTimezone):
            validate_timezone(name)


class TestBackoff:
    @pytest.mark.parametrize(
        ("attempts", "base_s"), [(1, 10), (2, 20), (3, 40), (7, 640), (8, 900), (20, 900)]
    )
    def test_doubles_from_10s_and_caps_at_15_min_with_jitter(
        self, attempts: int, base_s: int
    ) -> None:
        rng = random.Random(attempts)  # noqa: S311 - jitter, not cryptography
        for _ in range(50):
            delay = backoff(attempts, rng=rng).total_seconds()
            assert base_s * 0.8 <= delay <= base_s * 1.2


class TestClassifyFailure:
    def test_membership_loss_is_permanent(self) -> None:
        assert classify_failure(ConversationNotFound()) == Permanent("sender_not_member")

    def test_explicit_permanent_error_keeps_its_reason(self) -> None:
        assert classify_failure(PermanentDeliveryError("expired")) == Permanent("expired")

    def test_invalid_body_is_permanent(self) -> None:
        assert classify_failure(BodyEmpty()) == Permanent("invalid_body")

    def test_anything_else_is_transient_by_class_name_only(self) -> None:
        result = classify_failure(RuntimeError("contains user data"))
        assert result == Transient("RuntimeError")
