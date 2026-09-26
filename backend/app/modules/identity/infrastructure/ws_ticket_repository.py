"""WebSocket tickets (docs/design/06 §11.6, 05 §ws_tickets). Times come from the
database clock, like every expiry comparison against `now()`."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ISSUE = text(
    """
    INSERT INTO ws_tickets (user_id, session_family_id, ticket_hash, expires_at)
    VALUES (:user_id, :family_id, :ticket_hash, now() + make_interval(secs => :ttl))
    """
)

# R-7: one atomic statement, so a replayed ticket can never open a second socket.
# The account must still be active and the login session (refresh-token family) live.
_CONSUME = text(
    """
    UPDATE ws_tickets AS t
    SET consumed_at = now()
    FROM users AS u
    WHERE t.ticket_hash = :ticket_hash
      AND t.consumed_at IS NULL
      AND t.expires_at > now()
      AND u.id = t.user_id
      AND u.deleted_at IS NULL
      AND u.status = 'active'
      AND EXISTS (
          SELECT 1 FROM refresh_tokens AS r
          WHERE r.family_id = t.session_family_id
            AND r.revoked_at IS NULL
            AND r.expires_at > now()
      )
    RETURNING t.user_id, t.session_family_id
    """
)


class WsTicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self, *, user_id: UUID, session_family_id: UUID, ticket_hash: str, ttl_seconds: int
    ) -> None:
        await self._session.execute(
            _ISSUE,
            {
                "user_id": user_id,
                "family_id": session_family_id,
                "ticket_hash": ticket_hash,
                "ttl": ttl_seconds,
            },
        )

    async def consume(self, ticket_hash: str) -> tuple[UUID, UUID] | None:
        """(user_id, session_family_id), or None if invalid, expired, used or revoked."""
        row = (await self._session.execute(_CONSUME, {"ticket_hash": ticket_hash})).one_or_none()
        return (row.user_id, row.session_family_id) if row is not None else None
