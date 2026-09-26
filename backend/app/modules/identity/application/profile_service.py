"""Profiles and user search (docs/design/07 §Users, M03)."""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.modules.identity.application.services import (
    ClientInfo,
    IdentityDependencies,
    known_timezones,
    publish_session_revoked,
)
from app.modules.identity.domain.errors import InvalidCredentials, InvalidTimezone
from app.modules.identity.domain.user import User
from app.platform.errors import NotFoundError, ValidationError
from app.platform.pagination import InvalidCursor, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

MIN_QUERY_LENGTH = 2


class UserNotFound(NotFoundError):
    default_code = "user_not_found"
    default_message = "The user was not found."


class QueryTooShort(ValidationError):
    default_code = "query_too_short"
    default_message = f"Search for at least {MIN_QUERY_LENGTH} characters."


@dataclass(frozen=True, slots=True)
class UserSearchPage:
    users: list[User]
    next_cursor: str | None


class ProfileService:
    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    async def update_me(
        self, user_id: UUID, *, display_name: str | None, timezone: str | None
    ) -> User:
        if timezone is not None and timezone not in known_timezones():
            raise InvalidTimezone()
        async with self._deps.uow_factory() as uow:
            return await uow.users.update_profile(
                user_id, display_name=display_name, timezone=timezone
            )

    async def delete_me(self, user: User, *, password: str, client: ClientInfo) -> None:
        """Soft delete: frees the username and email, and ends every session."""
        if not await self._deps.hasher.verify(user.password_hash, password):
            raise InvalidCredentials()
        now = self._deps.clock.now()
        async with self._deps.uow_factory() as uow:
            await uow.users.soft_delete(user.id, now=now)
            revoked = await uow.refresh_tokens.revoke_all_for_user(user.id, now=now)
            await publish_session_revoked(uow, user.id, None)
            await uow.audit.record(
                "auth.account_deleted",
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
                metadata={"revoked_count": len(revoked)},
                ip_address=client.ip_address,
            )
        logger.info(
            "Account deleted", extra={"event": "auth.account_deleted", "user_id": str(user.id)}
        )

    async def get_public_profile(self, user_id: UUID) -> User:
        async with self._deps.uow_factory() as uow:
            user = await uow.users.get_active_by_id(user_id)
        if user is None:
            raise UserNotFound()
        return user

    async def search_users(
        self, *, caller_id: UUID, query: str, limit: int, cursor: str | None
    ) -> UserSearchPage:
        query = query.strip()
        if len(query) < MIN_QUERY_LENGTH:
            raise QueryTooShort()
        after: tuple[float, UUID] | None = None
        if cursor is not None:
            score, raw_id = decode_cursor(cursor, length=2)
            try:
                after = (float(score), UUID(str(raw_id)))
            except (TypeError, ValueError) as exc:
                raise InvalidCursor() from exc

        async with self._deps.uow_factory() as uow:
            # One extra row tells whether there is a next page.
            rows = await uow.users.search(query, exclude_id=caller_id, after=after, limit=limit + 1)
        page, more = rows[:limit], len(rows) > limit
        next_cursor = None
        if more and page:
            last_user, last_score = page[-1]
            next_cursor = encode_cursor([last_score, str(last_user.id)])
        return UserSearchPage(users=[user for user, _ in page], next_cursor=next_cursor)
