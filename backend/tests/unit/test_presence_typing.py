"""Presence transitions, the grace timer and the typing throttle, with fakes (M08)."""

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.presence.application.presence_service import PresenceService
from app.modules.presence.application.typing_service import NotAMember, TypingService
from app.modules.presence.domain.presence import Presence, PresenceStatus


class FakeStore:
    def __init__(self, co_members: set[UUID]) -> None:
        self.writes: list[tuple[UUID, PresenceStatus]] = []
        self.co_members = co_members

    async def upsert_status(self, user_id: UUID, status: PresenceStatus) -> Presence:
        self.writes.append((user_id, status))
        return Presence(user_id, status, datetime.now(UTC))

    async def get_many(self, user_ids: Sequence[UUID]) -> dict[UUID, Presence]:
        return {}

    async def co_member_ids(self, user_id: UUID) -> set[UUID]:
        return self.co_members


class FakeBroadcaster:
    def __init__(self) -> None:
        self.online: set[UUID] = set()
        self.frames: list[tuple[dict[str, object], set[UUID]]] = []

    def deliver_frame(self, frame: str, user_ids: Iterable[UUID]) -> int:
        self.frames.append((json.loads(frame), set(user_ids)))
        return 1

    def is_online(self, user_id: UUID) -> bool:
        return user_id in self.online


class FakeTimer:
    def __init__(self, callback: Callable[[], Awaitable[None]]) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class ManualScheduler:
    """Timers fire only when the test says so."""

    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, delay: float, callback: Callable[[], Awaitable[None]]) -> FakeTimer:
        timer = FakeTimer(callback)
        self.timers.append(timer)
        return timer

    async def fire_all(self) -> None:
        for timer in [t for t in self.timers if not t.cancelled]:
            self.timers.remove(timer)
            await timer.callback()


@pytest.fixture
def world() -> tuple[PresenceService, FakeStore, FakeBroadcaster, ManualScheduler, UUID, UUID]:
    user, friend = uuid4(), uuid4()
    store, broadcaster, scheduler = FakeStore({friend}), FakeBroadcaster(), ManualScheduler()
    service = PresenceService(store, broadcaster, grace_seconds=10, schedule=scheduler)
    return service, store, broadcaster, scheduler, user, friend


async def test_first_connection_announces_online_to_co_members(world) -> None:  # type: ignore[no-untyped-def]
    service, store, broadcaster, _, user, friend = world
    broadcaster.online.add(user)

    await service.connection_opened(user)

    assert store.writes == [(user, PresenceStatus.ONLINE)]
    [(frame, audience)] = broadcaster.frames
    assert frame["type"] == "presence.updated"
    assert frame["id"] is None  # ephemeral
    assert frame["payload"]["status"] == "online"
    assert audience == {friend}


async def test_offline_only_after_the_grace_period(world) -> None:  # type: ignore[no-untyped-def]
    service, store, broadcaster, scheduler, user, _ = world
    broadcaster.online.add(user)
    await service.connection_opened(user)
    broadcaster.online.discard(user)

    service.connection_closed(user)
    assert store.writes[-1] == (user, PresenceStatus.ONLINE)  # nothing yet
    await scheduler.fire_all()

    assert store.writes[-1] == (user, PresenceStatus.OFFLINE)
    assert broadcaster.frames[-1][0]["payload"]["status"] == "offline"


async def test_reconnect_within_grace_changes_nothing_r8(world) -> None:  # type: ignore[no-untyped-def]
    service, store, broadcaster, scheduler, user, _ = world
    broadcaster.online.add(user)
    await service.connection_opened(user)
    broadcaster.online.discard(user)
    service.connection_closed(user)

    broadcaster.online.add(user)  # reload: back before the timer fires
    await service.connection_opened(user)
    await scheduler.fire_all()

    assert store.writes == [(user, PresenceStatus.ONLINE)]
    assert len(broadcaster.frames) == 1


class FakeMembers:
    def __init__(self, members: dict[UUID, frozenset[UUID]]) -> None:
        self.members = members

    async def members_of(self, conversation_id: UUID) -> frozenset[UUID]:
        return self.members.get(conversation_id, frozenset())


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_typing_is_throttled_and_never_echoed_to_the_sender() -> None:
    alice, bob, carol, cid = uuid4(), uuid4(), uuid4(), uuid4()
    broadcaster, clock = FakeBroadcaster(), FakeClock()
    typing = TypingService(
        FakeMembers({cid: frozenset({alice, bob, carol})}), broadcaster, monotonic=clock
    )

    sent = []
    for _ in range(10):  # ten starts within one second
        sent.append(await typing.update(user_id=alice, conversation_id=cid, is_typing=True))
        clock.now += 0.1

    assert sent.count(True) == 1
    [(frame, audience)] = broadcaster.frames
    assert audience == {bob, carol}
    assert frame["payload"] == {
        "conversation_id": str(cid),
        "user_id": str(alice),
        "is_typing": True,
        "expires_in_s": 6,
    }

    clock.now += 2.0
    assert await typing.update(user_id=alice, conversation_id=cid, is_typing=True)


async def test_typing_stop_always_goes_out_and_resets_the_throttle() -> None:
    alice, bob, cid = uuid4(), uuid4(), uuid4()
    broadcaster, clock = FakeBroadcaster(), FakeClock()
    typing = TypingService(
        FakeMembers({cid: frozenset({alice, bob})}), broadcaster, monotonic=clock
    )

    await typing.update(user_id=alice, conversation_id=cid, is_typing=True)
    assert await typing.update(user_id=alice, conversation_id=cid, is_typing=False)
    assert await typing.update(user_id=alice, conversation_id=cid, is_typing=True)


async def test_typing_in_a_foreign_conversation_is_refused() -> None:
    typing = TypingService(FakeMembers({}), FakeBroadcaster())

    with pytest.raises(NotAMember):
        await typing.update(user_id=uuid4(), conversation_id=uuid4(), is_typing=True)
