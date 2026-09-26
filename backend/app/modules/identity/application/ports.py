"""Ports the identity use cases depend on. Infrastructure implements them with
SQLAlchemy; unit tests implement them with in-memory fakes (docs/design/03 §5)."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from app.modules.identity.domain.refresh_token import RefreshToken, Session
from app.modules.identity.domain.user import User
from app.platform.events import EventPublisher


class UserRepositoryPort(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def get_by_username_or_email(self, identifier: str) -> User | None: ...

    async def add(
        self,
        *,
        username: str,
        email: str,
        display_name: str,
        password_hash: str,
        timezone: str,
    ) -> User:
        """Raises UsernameTaken / EmailTaken on a uniqueness conflict."""
        ...

    async def record_failed_login(
        self, user_id: UUID, *, now: datetime, max_attempts: int, lock_for: timedelta
    ) -> tuple[int, datetime | None]:
        """Atomically count a failure (R-18). Returns (attempts, locked_until)."""
        ...

    async def reset_failed_logins(self, user_id: UUID) -> None: ...

    async def get_active_by_id(self, user_id: UUID) -> User | None: ...

    async def search(
        self,
        query: str,
        *,
        exclude_id: UUID,
        after: tuple[float, UUID] | None,
        limit: int,
    ) -> list[tuple[User, float]]:
        """Active users matching `query`, best first, after the keyset `after`."""
        ...

    async def update_profile(
        self, user_id: UUID, *, display_name: str | None = None, timezone: str | None = None
    ) -> User: ...

    async def soft_delete(self, user_id: UUID, *, now: datetime) -> None: ...

    async def set_password_hash(self, user_id: UUID, password_hash: str) -> None: ...


class RefreshTokenRepositoryPort(Protocol):
    async def add(
        self,
        *,
        user_id: UUID,
        family_id: UUID,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> RefreshToken: ...

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        """Locks the row (SELECT … FOR UPDATE) for the rest of the transaction (R-6)."""
        ...

    async def mark_rotated(
        self, token_id: UUID, *, replaced_by_id: UUID, now: datetime
    ) -> None: ...

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int: ...

    async def revoke_all_for_user(
        self, user_id: UUID, *, now: datetime, except_family_id: UUID | None = None
    ) -> set[UUID]:
        """The families (login sessions) that were revoked."""
        ...

    async def list_active_sessions(self, user_id: UUID, *, now: datetime) -> list[Session]: ...

    async def has_active_family(self, user_id: UUID, family_id: UUID, *, now: datetime) -> bool: ...


class WsTicketRepositoryPort(Protocol):
    async def issue(
        self, *, user_id: UUID, session_family_id: UUID, ticket_hash: str, ttl_seconds: int
    ) -> None: ...

    async def consume(self, ticket_hash: str) -> tuple[UUID, UUID] | None:
        """Atomic single use (R-7): (user_id, session_family_id) or None."""
        ...


class AuditPort(Protocol):
    async def record(
        self,
        action: str,
        *,
        actor_user_id: UUID | None = None,
        target_type: str | None = None,
        target_id: UUID | None = None,
        metadata: Mapping[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None: ...


class IdentityUnitOfWork(Protocol):
    """One transaction, with the repositories bound to it."""

    # Read-only properties rather than plain attributes: Protocol attributes are
    # invariant, so a concrete repository type wouldn't satisfy them.
    @property
    def users(self) -> UserRepositoryPort: ...

    @property
    def refresh_tokens(self) -> RefreshTokenRepositoryPort: ...

    @property
    def audit(self) -> AuditPort: ...

    @property
    def ws_tickets(self) -> WsTicketRepositoryPort: ...

    @property
    def events(self) -> EventPublisher: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


IdentityUnitOfWorkFactory = Callable[[], IdentityUnitOfWork]
