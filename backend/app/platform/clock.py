"""The Clock port (docs/design/03 §5). Application code asks it for "now" so tests can
freeze or advance time instead of sleeping or monkeypatching `datetime`.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A test clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
