"""ConnectionManager with fake sockets (M06): register, fan-out, overflow."""

import asyncio
from typing import Any, cast
from uuid import uuid4

from starlette.websockets import WebSocket, WebSocketState

from app.platform.tasks import TaskSupervisor
from app.realtime.connection_manager import ConnectionManager
from app.realtime.envelope import WSEvent


class FakeWebSocket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.sent: list[str] = []
        self.closed_with: int | None = None
        self.application_state = WebSocketState.CONNECTED
        self._gate = asyncio.Event()
        if not blocked:
            self._gate.set()

    async def send_text(self, frame: str) -> None:
        await self._gate.wait()  # a blocked socket never finishes sending
        self.sent.append(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = code
        self.application_state = WebSocketState.DISCONNECTED


def manager(queue_size: int = 4) -> ConnectionManager:
    return ConnectionManager(supervisor=TaskSupervisor(), queue_size=queue_size)


def connect(mgr: ConnectionManager, user_id: Any, *, blocked: bool = False) -> Any:
    socket = FakeWebSocket(blocked=blocked)
    connection = mgr.new_connection(
        cast(WebSocket, socket), user_id=user_id, session_family_id=uuid4()
    )
    mgr.register(connection)
    return connection, socket


def event(*recipients: Any) -> WSEvent:
    return WSEvent(type="message.created", payload={"x": 1}, id=1, recipient_user_ids=recipients)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_delivers_to_every_connection_of_every_recipient() -> None:
    mgr = manager()
    alice, bob, carol = uuid4(), uuid4(), uuid4()
    _, alice_tab_1 = connect(mgr, alice)
    _, alice_tab_2 = connect(mgr, alice)
    _, bob_tab = connect(mgr, bob)
    _, carol_tab = connect(mgr, carol)

    assert mgr.deliver(event(alice, bob)) == 3
    await settle()

    assert len(alice_tab_1.sent) == len(alice_tab_2.sent) == len(bob_tab.sent) == 1
    assert carol_tab.sent == []


async def test_recipients_without_a_connection_are_skipped() -> None:
    mgr = manager()

    assert mgr.deliver(event(uuid4())) == 0


async def test_unregister_stops_delivery_and_tracks_online_state() -> None:
    mgr = manager()
    user = uuid4()
    first, _ = connect(mgr, user)
    second, _ = connect(mgr, user)
    went_offline: list[Any] = []
    mgr.on_last_disconnection = went_offline.append

    mgr.unregister(first)
    assert mgr.is_online(user)
    mgr.unregister(second)

    assert not mgr.is_online(user)
    assert went_offline == [user]
    assert mgr.deliver(event(user)) == 0


async def test_a_full_queue_closes_that_connection_with_4008_only() -> None:
    mgr = manager(queue_size=2)
    user = uuid4()
    stuck, stuck_socket = connect(mgr, user, blocked=True)
    other_user = uuid4()
    _, healthy_socket = connect(mgr, other_user)

    for n in range(6):
        mgr.deliver(
            WSEvent(type="t", payload={"n": n}, id=n, recipient_user_ids=(user, other_user))
        )
        await settle()

    assert stuck.close_requested
    assert stuck_socket.closed_with == 4008
    assert len(healthy_socket.sent) == 6  # never delayed by the stuck one


async def test_close_is_scheduled_once_for_a_burst_of_overflows() -> None:
    mgr = manager(queue_size=1)
    user = uuid4()
    stuck, _ = connect(mgr, user, blocked=True)

    for n in range(20):
        mgr.deliver(WSEvent(type="t", payload={}, id=n, recipient_user_ids=(user,)))
    await settle()

    assert stuck.close_code == 4008
