"""Password hashing, access tokens and opaque tokens (docs/design/06 §11, 10 §20).

`PasswordHasher` and `TokenIssuer` are ports (Protocols): application services depend
on them, and tests can substitute fakes.
"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import Settings

OPAQUE_TOKEN_BYTES = 32  # 256 bits

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "chat-app"
JWT_AUDIENCE = "chat-app-api"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a claim value, not a secret


def generate_opaque_token() -> str:
    """A 256-bit random, URL-safe token (refresh tokens, WS tickets)."""
    return secrets.token_urlsafe(OPAQUE_TOKEN_BYTES)


def sha256_hex(value: str) -> str:
    """How opaque tokens are stored. A fast hash is enough for 256-bit random values;
    Argon2 is only needed for low-entropy passwords."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- passwords -----------------------------------------------------------------------


class PasswordHasher(Protocol):
    # A valid hash of a random value. Verifying against it when the user doesn't
    # exist makes both login failure paths cost the same.
    dummy_hash: str

    async def hash(self, password: str) -> str: ...
    async def verify(self, password_hash: str, password: str) -> bool: ...
    def needs_rehash(self, password_hash: str) -> bool: ...


class Argon2PasswordHasher:
    """Argon2id. Hashing is CPU- and memory-bound, so it runs in a worker thread to
    keep the event loop responsive (docs/design/06 §11.4)."""

    def __init__(self, *, time_cost: int, memory_cost_kib: int, parallelism: int) -> None:
        self._hasher = _Argon2(
            time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
        )
        # Verified against when the username is unknown, so both failure paths cost
        # the same and response timing doesn't reveal which accounts exist.
        self.dummy_hash = self._hasher.hash(generate_opaque_token())

    @classmethod
    def from_settings(cls, settings: Settings) -> "Argon2PasswordHasher":
        return cls(
            time_cost=settings.argon2_time_cost,
            memory_cost_kib=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await asyncio.to_thread(self._verify_sync, password_hash, password)

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True


# --- access tokens -------------------------------------------------------------------


class InvalidAccessToken(Exception):
    """The token is malformed, forged, of the wrong type, or for another audience."""


class ExpiredAccessToken(InvalidAccessToken):
    pass


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: UUID
    session_id: UUID  # `sid`: the refresh-token family, i.e. the login session
    token_id: UUID  # `jti`, for log correlation
    issued_at: datetime
    expires_at: datetime


class TokenIssuer(Protocol):
    @property
    def access_token_ttl_seconds(self) -> int: ...
    def issue_access_token(self, *, user_id: UUID, session_id: UUID, now: datetime) -> str: ...
    def decode_access_token(self, token: str, *, now: datetime) -> AccessTokenClaims: ...


class JwtTokenIssuer:
    def __init__(
        self,
        *,
        secret: str,
        previous_secret: str | None = None,
        access_token_ttl_seconds: int,
    ) -> None:
        self._secret = secret
        self._previous_secret = previous_secret
        self._ttl = access_token_ttl_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "JwtTokenIssuer":
        assert settings.jwt_secret is not None  # noqa: S101 - enforced by Settings
        previous = settings.jwt_secret_previous
        return cls(
            secret=settings.jwt_secret.get_secret_value(),
            previous_secret=previous.get_secret_value() if previous else None,
            access_token_ttl_seconds=settings.access_token_ttl_seconds,
        )

    @property
    def access_token_ttl_seconds(self) -> int:
        return self._ttl

    def issue_access_token(self, *, user_id: UUID, session_id: UUID, now: datetime) -> str:
        claims = {
            "sub": str(user_id),
            "sid": str(session_id),
            "jti": str(uuid4()),
            "type": ACCESS_TOKEN_TYPE,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._ttl)).timestamp()),
        }
        return jwt.encode(claims, self._secret, algorithm=JWT_ALGORITHM)

    def decode_access_token(self, token: str, *, now: datetime) -> AccessTokenClaims:
        secrets_to_try = [self._secret]
        if self._previous_secret:
            secrets_to_try.append(self._previous_secret)

        last_error: Exception | None = None
        for secret in secrets_to_try:
            try:
                claims = self._decode(token, secret, now)
            except jwt.InvalidSignatureError as exc:
                last_error = exc  # maybe signed with the other secret
                continue
            except jwt.ExpiredSignatureError as exc:
                raise ExpiredAccessToken("token expired") from exc
            except jwt.PyJWTError as exc:
                raise InvalidAccessToken(type(exc).__name__) from exc
            return claims
        raise InvalidAccessToken("bad signature") from last_error

    def _decode(self, token: str, secret: str, now: datetime) -> AccessTokenClaims:
        payload: dict[str, Any] = jwt.decode(
            token,
            secret,
            # Pinned: never trust the token header's `alg` (alg=none, key confusion).
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["sub", "sid", "jti", "type", "iat", "exp", "iss", "aud"],
                # Time checks use the injected clock (below), never PyJWT's wall clock,
                # so tests with a frozen or advanced clock behave like production.
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        if payload["type"] != ACCESS_TOKEN_TYPE:
            raise jwt.InvalidTokenError("wrong token type")
        expires_at = datetime.fromtimestamp(payload["exp"], tz=now.tzinfo)
        if expires_at <= now:
            raise jwt.ExpiredSignatureError("token expired")
        try:
            return AccessTokenClaims(
                user_id=UUID(payload["sub"]),
                session_id=UUID(payload["sid"]),
                token_id=UUID(payload["jti"]),
                issued_at=datetime.fromtimestamp(payload["iat"], tz=now.tzinfo),
                expires_at=expires_at,
            )
        except (ValueError, TypeError) as exc:
            raise jwt.InvalidTokenError("malformed claims") from exc
