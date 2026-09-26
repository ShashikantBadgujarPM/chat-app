"""Identity use cases (docs/design/06 §11, 07 §Auth).

Commit-then-raise: some failures must be persisted even though the request fails, for
example the failed-login counter, a reuse-detection family revocation, and their audit
rows. Raising inside `async with uow:` would roll those back, so those use cases return
the error from inside the transaction and raise it after the commit.
"""

import hashlib
import logging
import math
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from uuid import UUID, uuid4

from app.modules.identity.application.ports import IdentityUnitOfWork, IdentityUnitOfWorkFactory
from app.modules.identity.domain.errors import (
    AccessTokenExpired,
    AccountLocked,
    InvalidAccessTokenError,
    InvalidCredentials,
    InvalidRefreshToken,
    InvalidTimezone,
    RefreshSuperseded,
    RefreshTokenReused,
    SessionNotFound,
)
from app.modules.identity.domain.password_policy import validate_password
from app.modules.identity.domain.refresh_token import Session
from app.modules.identity.domain.user import User
from app.platform.clock import Clock
from app.platform.errors import AppError
from app.platform.security import (
    ExpiredAccessToken,
    InvalidAccessToken,
    PasswordHasher,
    TokenIssuer,
    generate_opaque_token,
    sha256_hex,
)

logger = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


@cache
def known_timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def username_fingerprint(identifier: str) -> str:
    """A truncated hash of a login identifier, for correlating attempts without logging
    what was typed (people sometimes type their password into the username field)."""
    return hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip_address: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str  # raw value; goes into the cookie only
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    tokens: IssuedTokens


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    session_id: UUID


@dataclass(frozen=True, slots=True)
class SessionView:
    session: Session
    current: bool


@dataclass(frozen=True, slots=True)
class IdentityDependencies:
    uow_factory: IdentityUnitOfWorkFactory
    hasher: PasswordHasher
    tokens: TokenIssuer
    clock: Clock
    refresh_token_ttl: timedelta


async def publish_session_revoked(
    uow: IdentityUnitOfWork, user_id: UUID, family_ids: set[UUID] | None
) -> None:
    """Close the sockets of ended sessions (docs/design/06 §11.6). Internal: the
    connection manager acts on it and doesn't forward it. None means every session."""
    if family_ids is not None and not family_ids:
        return
    await uow.events.publish(
        "session.revoked",
        recipient_user_ids=[user_id],
        payload={
            "user_id": str(user_id),
            "family_ids": "*" if family_ids is None else sorted(str(f) for f in family_ids),
        },
    )


class _UseCase:
    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    def _uow(self) -> IdentityUnitOfWork:
        return self._deps.uow_factory()

    async def _issue_tokens(
        self,
        uow: IdentityUnitOfWork,
        *,
        user_id: UUID,
        family_id: UUID,
        now: datetime,
        client: ClientInfo,
    ) -> tuple[IssuedTokens, UUID]:
        raw = generate_opaque_token()
        expires_at = now + self._deps.refresh_token_ttl
        stored = await uow.refresh_tokens.add(
            user_id=user_id,
            family_id=family_id,
            token_hash=sha256_hex(raw),
            issued_at=now,
            expires_at=expires_at,
            user_agent=client.user_agent,
            ip_address=client.ip_address,
        )
        access = self._deps.tokens.issue_access_token(
            user_id=user_id, session_id=family_id, now=now
        )
        issued = IssuedTokens(
            access_token=access,
            expires_in=self._deps.tokens.access_token_ttl_seconds,
            refresh_token=raw,
            refresh_expires_at=expires_at,
        )
        return issued, stored.id


class RegisterUser(_UseCase):
    async def __call__(
        self,
        *,
        username: str,
        email: str,
        display_name: str,
        password: str,
        timezone: str = "UTC",
    ) -> User:
        if timezone not in known_timezones():
            raise InvalidTimezone()
        validate_password(password)
        password_hash = await self._deps.hasher.hash(password)
        async with self._uow() as uow:
            user = await uow.users.add(
                username=username,
                email=email,
                display_name=display_name,
                password_hash=password_hash,
                timezone=timezone,
            )
        logger.info("User registered", extra={"event": "auth.registered", "user_id": str(user.id)})
        return user


class Login(_UseCase):
    async def __call__(self, *, identifier: str, password: str, client: ClientInfo) -> LoginResult:
        async with self._uow() as uow:
            outcome = await self._attempt(uow, identifier, password, client)
        if isinstance(outcome, AppError):
            raise outcome
        return outcome

    async def _attempt(
        self, uow: IdentityUnitOfWork, identifier: str, password: str, client: ClientInfo
    ) -> LoginResult | AppError:
        now = self._deps.clock.now()
        user = await uow.users.get_by_username_or_email(identifier)

        if user is None or not user.is_active:
            # Same cost as a real verification, so timing doesn't reveal existence.
            await self._deps.hasher.verify(self._deps.hasher.dummy_hash, password)
            await self._record_failure(
                uow, "unknown_user" if user is None else "disabled", identifier, None, client
            )
            return InvalidCredentials()

        if user.locked_until is not None and user.locked_until > now:
            await self._record_failure(uow, "locked", identifier, user.id, client)
            return AccountLocked(retry_after_seconds=_seconds_until(user.locked_until, now))

        if not await self._deps.hasher.verify(user.password_hash, password):
            attempts, locked_until = await uow.users.record_failed_login(
                user.id, now=now, max_attempts=MAX_FAILED_LOGINS, lock_for=LOCKOUT_DURATION
            )
            await self._record_failure(uow, "bad_password", identifier, user.id, client)
            if locked_until is not None and locked_until > now:
                if attempts == MAX_FAILED_LOGINS:
                    await uow.audit.record(
                        "auth.account_locked",
                        actor_user_id=user.id,
                        target_type="user",
                        target_id=user.id,
                        metadata={"attempts": attempts},
                        ip_address=client.ip_address,
                    )
                    logger.warning(
                        "Account locked",
                        extra={"event": "auth.account_locked", "user_id": str(user.id)},
                    )
                return AccountLocked(retry_after_seconds=_seconds_until(locked_until, now))
            return InvalidCredentials()

        if user.failed_login_attempts or user.locked_until is not None:
            await uow.users.reset_failed_logins(user.id)
        if self._deps.hasher.needs_rehash(user.password_hash):
            await uow.users.set_password_hash(user.id, await self._deps.hasher.hash(password))

        tokens, _ = await self._issue_tokens(
            uow, user_id=user.id, family_id=uuid4(), now=now, client=client
        )
        await uow.audit.record(
            "auth.login_succeeded",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            ip_address=client.ip_address,
        )
        logger.info(
            "Login succeeded", extra={"event": "auth.login_succeeded", "user_id": str(user.id)}
        )
        return LoginResult(user=user, tokens=tokens)

    async def _record_failure(
        self,
        uow: IdentityUnitOfWork,
        reason: str,
        identifier: str,
        user_id: UUID | None,
        client: ClientInfo,
    ) -> None:
        fingerprint = username_fingerprint(identifier)
        await uow.audit.record(
            "auth.login_failed",
            actor_user_id=user_id,
            target_type="user" if user_id else None,
            target_id=user_id,
            metadata={"reason": reason, "username_hash": fingerprint},
            ip_address=client.ip_address,
        )
        logger.warning(
            "Login failed",
            extra={
                "event": "auth.login_failed",
                "reason": reason,
                "username_hash": fingerprint,
                "ip": client.ip_address,
            },
        )


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


class RefreshSession(_UseCase):
    async def __call__(self, *, refresh_token: str, client: ClientInfo) -> IssuedTokens:
        async with self._uow() as uow:
            outcome = await self._attempt(uow, refresh_token, client)
        if isinstance(outcome, AppError):
            raise outcome
        return outcome

    async def _attempt(
        self, uow: IdentityUnitOfWork, raw: str, client: ClientInfo
    ) -> IssuedTokens | AppError:
        now = self._deps.clock.now()
        token = await uow.refresh_tokens.get_by_hash_for_update(sha256_hex(raw))
        if token is None:
            return InvalidRefreshToken()

        if token.revoked_at is not None:
            if token.was_just_rotated(now):
                # Another tab refreshed a moment ago (R-6): not theft.
                return RefreshSuperseded()
            revoked = await uow.refresh_tokens.revoke_family(token.family_id, now=now)
            await publish_session_revoked(uow, token.user_id, {token.family_id})
            await uow.audit.record(
                "auth.refresh_reuse_detected",
                actor_user_id=token.user_id,
                target_type="session",
                target_id=token.family_id,
                metadata={"revoked_count": revoked},
                ip_address=client.ip_address,
            )
            logger.warning(
                "Refresh token reuse detected; session revoked",
                extra={
                    "event": "auth.refresh_reuse_detected",
                    "user_id": str(token.user_id),
                    "family_id": str(token.family_id),
                },
            )
            return RefreshTokenReused()

        if token.is_expired(now):
            return InvalidRefreshToken()

        user = await uow.users.get_by_id(token.user_id)
        if user is None or not user.is_active:
            await uow.refresh_tokens.revoke_family(token.family_id, now=now)
            await publish_session_revoked(uow, token.user_id, {token.family_id})
            return InvalidRefreshToken()

        tokens, new_id = await self._issue_tokens(
            uow, user_id=user.id, family_id=token.family_id, now=now, client=client
        )
        await uow.refresh_tokens.mark_rotated(token.id, replaced_by_id=new_id, now=now)
        return tokens


class Logout(_UseCase):
    async def __call__(self, *, refresh_token: str | None, client: ClientInfo) -> None:
        """Idempotent: an unknown or already-revoked token is not an error."""
        if not refresh_token:
            return
        now = self._deps.clock.now()
        async with self._uow() as uow:
            token = await uow.refresh_tokens.get_by_hash_for_update(sha256_hex(refresh_token))
            if token is None or token.revoked_at is not None:
                return
            await uow.refresh_tokens.revoke_family(token.family_id, now=now)
            await publish_session_revoked(uow, token.user_id, {token.family_id})
            await uow.audit.record(
                "auth.logout",
                actor_user_id=token.user_id,
                target_type="session",
                target_id=token.family_id,
                ip_address=client.ip_address,
            )
        logger.info("Logged out", extra={"event": "auth.logout", "user_id": str(token.user_id)})


class LogoutAll(_UseCase):
    async def __call__(self, *, user_id: UUID, client: ClientInfo) -> None:
        now = self._deps.clock.now()
        async with self._uow() as uow:
            revoked = await uow.refresh_tokens.revoke_all_for_user(user_id, now=now)
            await publish_session_revoked(uow, user_id, None)
            await uow.audit.record(
                "auth.logout_all",
                actor_user_id=user_id,
                target_type="user",
                target_id=user_id,
                metadata={"revoked_count": len(revoked)},
                ip_address=client.ip_address,
            )
        logger.info(
            "Logged out everywhere", extra={"event": "auth.logout_all", "user_id": str(user_id)}
        )


class ListSessions(_UseCase):
    async def __call__(self, *, user_id: UUID, current_session_id: UUID) -> list[SessionView]:
        now = self._deps.clock.now()
        async with self._uow() as uow:
            sessions = await uow.refresh_tokens.list_active_sessions(user_id, now=now)
        return [
            SessionView(session=session, current=session.family_id == current_session_id)
            for session in sessions
        ]


class RevokeSession(_UseCase):
    async def __call__(self, *, user_id: UUID, family_id: UUID, client: ClientInfo) -> None:
        now = self._deps.clock.now()
        async with self._uow() as uow:
            # 404 for someone else's session: don't reveal that it exists.
            if not await uow.refresh_tokens.has_active_family(user_id, family_id, now=now):
                raise SessionNotFound()
            await uow.refresh_tokens.revoke_family(family_id, now=now)
            await publish_session_revoked(uow, user_id, {family_id})
            await uow.audit.record(
                "auth.session_revoked",
                actor_user_id=user_id,
                target_type="session",
                target_id=family_id,
                ip_address=client.ip_address,
            )


class ChangePassword(_UseCase):
    async def __call__(
        self,
        *,
        user: User,
        current_session_id: UUID,
        current_password: str,
        new_password: str,
        client: ClientInfo,
    ) -> None:
        if not await self._deps.hasher.verify(user.password_hash, current_password):
            raise InvalidCredentials()
        validate_password(new_password)
        new_hash = await self._deps.hasher.hash(new_password)
        now = self._deps.clock.now()
        async with self._uow() as uow:
            await uow.users.set_password_hash(user.id, new_hash)
            revoked = await uow.refresh_tokens.revoke_all_for_user(
                user.id, now=now, except_family_id=current_session_id
            )
            await publish_session_revoked(uow, user.id, revoked)
            await uow.audit.record(
                "auth.password_changed",
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
                metadata={"revoked_count": len(revoked)},
                ip_address=client.ip_address,
            )


class AuthenticateAccessToken(_UseCase):
    """Backs the `get_current_user` dependency."""

    async def __call__(self, token: str) -> CurrentUser:
        now = self._deps.clock.now()
        try:
            claims = self._deps.tokens.decode_access_token(token, now=now)
        except ExpiredAccessToken as exc:
            raise AccessTokenExpired() from exc
        except InvalidAccessToken as exc:
            logger.debug("Invalid access token", extra={"event": "auth.token_invalid"})
            raise InvalidAccessTokenError() from exc
        async with self._uow() as uow:
            user = await uow.users.get_by_id(claims.user_id)
        # Disabled or deleted accounts are rejected immediately, whatever the token's
        # remaining lifetime (docs/design/06 §11.1).
        if user is None or not user.is_active:
            raise InvalidAccessTokenError()
        return CurrentUser(user=user, session_id=claims.session_id)
