"""WebSocket tickets (docs/design/06 §11.6, ADR-006).

A browser can't set headers on a WebSocket, and a JWT in the URL would leak into logs.
So an authenticated REST call issues a 30-second, single-use ticket, and the gateway
consumes it atomically before treating the socket as authenticated.
"""

from dataclasses import dataclass
from uuid import UUID

from app.modules.identity.application.ports import IdentityUnitOfWorkFactory
from app.platform.security import generate_opaque_token, sha256_hex

TICKET_TTL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    ticket: str  # raw value: returned once, stored only as a hash
    expires_in: int


@dataclass(frozen=True, slots=True)
class TicketOwner:
    user_id: UUID
    session_family_id: UUID


class WsTicketService:
    def __init__(self, uow_factory: IdentityUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def issue(self, *, user_id: UUID, session_family_id: UUID) -> IssuedTicket:
        raw = generate_opaque_token()
        async with self._uow_factory() as uow:
            await uow.ws_tickets.issue(
                user_id=user_id,
                session_family_id=session_family_id,
                ticket_hash=sha256_hex(raw),
                ttl_seconds=TICKET_TTL_SECONDS,
            )
        return IssuedTicket(ticket=raw, expires_in=TICKET_TTL_SECONDS)

    async def consume(self, ticket: str) -> TicketOwner | None:
        async with self._uow_factory() as uow:
            owner = await uow.ws_tickets.consume(sha256_hex(ticket))
        if owner is None:
            return None
        return TicketOwner(user_id=owner[0], session_family_id=owner[1])
