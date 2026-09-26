"""Identity use cases against in-memory fakes of their ports (docs/design/11 §21.4)."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from app.modules.identity.application.services import (
    ClientInfo,
    IdentityDependencies,
    Login,
    RefreshSession,
)
from app.modules.identity.domain.errors import (
    AccountLocked,
    InvalidCredentials,
    RefreshSuperseded,
    RefreshTokenReused,
)
from app.modules.identity.domain.refresh_token import RefreshToken, Session
from app.modules.identity.domain.user import User, UserStatus
from app.platform.clock import FrozenClock
from app.platform.security import JwtTokenIssuer, sha256_hex

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CLIENT = ClientInfo(ip_address="10.0.0.1", user_agent="tests")


@dataclass
class Store:
    """The fake database. `committed` is what a real database would keep."""

    users: dict[UUID, User] = field(default_factory=dict)
    tokens: dict[UUID, tuple[RefreshToken, str]] = field(default_factory=dict)
    audit: list[str] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0


class FakeUsers:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self.store.users.get(user_id)

    async def get_by_username_or_email(self, identifier: str) -> User | None:
        lowered = identifier.lower()
        return next(
            (u for u in self.store.users.values() if lowered in (u.username.lower(), u.email)),
            None,
        )

    async def add(self, **kwargs: Any) -> User:  # pragma: no cover - not used here
        raise NotImplementedError

    async def record_failed_login(
        self, user_id: UUID, *, now: datetime, max_attempts: int, lock_for: timedelta
    ) -> tuple[int, datetime | None]:
        user = self.store.users[user_id]
        attempts = user.failed_login_attempts + 1
        locked_until = now + lock_for if attempts >= max_attempts else user.locked_until
        self.store.users[user_id] = replace(
            user, failed_login_attempts=attempts, locked_until=locked_until
        )
        return attempts, locked_until

    async def reset_failed_logins(self, user_id: UUID) -> None:
        user = self.store.users[user_id]
        self.store.users[user_id] = replace(user, failed_login_attempts=0, locked_until=None)

    async def set_password_hash(self, user_id: UUID, password_hash: str) -> None:
        self.store.users[user_id] = replace(self.store.users[user_id], password_hash=password_hash)


class FakeTokens:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def add(self, **kwargs: Any) -> RefreshToken:
        token = RefreshToken(
            id=uuid4(),
            user_id=kwargs["user_id"],
            family_id=kwargs["family_id"],
            issued_at=kwargs["issued_at"],
            expires_at=kwargs["expires_at"],
            revoked_at=None,
            replaced_by_id=None,
        )
        self.store.tokens[token.id] = (token, kwargs["token_hash"])
        return token

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        return next((t for t, h in self.store.tokens.values() if h == token_hash), None)

    async def mark_rotated(self, token_id: UUID, *, replaced_by_id: UUID, now: datetime) -> None:
        token, h = self.store.tokens[token_id]
        self.store.tokens[token_id] = (
            replace(token, revoked_at=now, replaced_by_id=replaced_by_id),
            h,
        )

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int:
        count = 0
        for token_id, (token, h) in list(self.store.tokens.items()):
            if token.family_id == family_id and token.revoked_at is None:
                self.store.tokens[token_id] = (replace(token, revoked_at=now), h)
                count += 1
        return count

    async def revoke_all_for_user(self, user_id: UUID, **kwargs: Any) -> int:  # pragma: no cover
        raise NotImplementedError

    async def list_active_sessions(
        self, user_id: UUID, *, now: datetime
    ) -> list[Session]:  # pragma: no cover
        raise NotImplementedError

    async def has_active_family(self, *args: Any, **kwargs: Any) -> bool:  # pragma: no cover
        raise NotImplementedError


class FakeAudit:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def record(self, action: str, **kwargs: Any) -> None:
        del kwargs
        self.store.audit.append(action)


class FakeUnitOfWork:
    """Stages writes on a copy and applies them to the store only on commit."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def __aenter__(self) -> Self:
        self._staged = Store(
            users=dict(self._store.users),
            tokens=dict(self._store.tokens),
            audit=list(self._store.audit),
        )
        self.users = FakeUsers(self._staged)
        self.refresh_tokens = FakeTokens(self._staged)
        self.audit = FakeAudit(self._staged)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self._store.users, self._store.tokens = self._staged.users, self._staged.tokens
            self._store.audit = self._staged.audit
            self._store.commits += 1
        else:
            self._store.rollbacks += 1


class FakeHasher:
    dummy_hash = "dummy"

    def __init__(self) -> None:
        self.verified_against: list[str] = []

    async def hash(self, password: str) -> str:
        return f"hashed:{password}"

    async def verify(self, password_hash: str, password: str) -> bool:
        self.verified_against.append(password_hash)
        return password_hash == f"hashed:{password}"

    def needs_rehash(self, password_hash: str) -> bool:
        return False


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def hasher() -> FakeHasher:
    return FakeHasher()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def deps(store: Store, hasher: FakeHasher, clock: FrozenClock) -> IdentityDependencies:
    return IdentityDependencies(
        uow_factory=lambda: FakeUnitOfWork(store),  # type: ignore[arg-type, return-value]
        hasher=hasher,
        tokens=JwtTokenIssuer(secret="s" * 32, access_token_ttl_seconds=900),
        clock=clock,
        refresh_token_ttl=timedelta(days=14),
    )


def add_user(store: Store, **overrides: Any) -> User:
    values: dict[str, Any] = {
        "id": uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "display_name": "Alice",
        "password_hash": "hashed:right password!",
        "status": UserStatus.ACTIVE,
        "timezone": "UTC",
        "failed_login_attempts": 0,
        "locked_until": None,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    values.update(overrides)
    user = User(**values)
    store.users[user.id] = user
    return user


class TestLogin:
    async def test_unknown_user_verifies_against_the_dummy_hash(
        self, deps: IdentityDependencies, hasher: FakeHasher
    ) -> None:
        with pytest.raises(InvalidCredentials):
            await Login(deps)(identifier="nobody", password="whatever", client=CLIENT)

        assert hasher.verified_against == ["dummy"]

    async def test_failure_is_committed_before_the_error_is_raised(
        self, deps: IdentityDependencies, store: Store
    ) -> None:
        user = add_user(store)

        with pytest.raises(InvalidCredentials):
            await Login(deps)(identifier="alice", password="wrong", client=CLIENT)

        # Raising inside the transaction would have rolled these back.
        assert store.commits == 1
        assert store.rollbacks == 0
        assert store.users[user.id].failed_login_attempts == 1
        assert store.audit == ["auth.login_failed"]

    async def test_fifth_failure_locks_and_is_audited(
        self, deps: IdentityDependencies, store: Store
    ) -> None:
        add_user(store, failed_login_attempts=4)

        with pytest.raises(AccountLocked) as caught:
            await Login(deps)(identifier="alice", password="wrong", client=CLIENT)

        assert caught.value.retry_after_seconds == 15 * 60
        assert store.audit == ["auth.login_failed", "auth.account_locked"]

    async def test_locked_account_is_refused_without_verifying(
        self, deps: IdentityDependencies, store: Store, hasher: FakeHasher
    ) -> None:
        add_user(store, locked_until=NOW + timedelta(minutes=5))

        with pytest.raises(AccountLocked):
            await Login(deps)(identifier="alice", password="right password!", client=CLIENT)

        assert hasher.verified_against == []

    async def test_success_issues_a_new_family_and_resets_the_counter(
        self, deps: IdentityDependencies, store: Store
    ) -> None:
        user = add_user(store, failed_login_attempts=2)

        result = await Login(deps)(identifier="alice", password="right password!", client=CLIENT)

        assert store.users[user.id].failed_login_attempts == 0
        [(token, token_hash)] = store.tokens.values()
        assert token_hash == sha256_hex(result.tokens.refresh_token)  # raw never stored
        claims = deps.tokens.decode_access_token(result.tokens.access_token, now=NOW)
        assert claims.session_id == token.family_id


class TestRefresh:
    async def login(self, deps: IdentityDependencies, store: Store) -> str:
        add_user(store)
        result = await Login(deps)(identifier="alice", password="right password!", client=CLIENT)
        return result.tokens.refresh_token

    async def test_recent_rotation_is_superseded_and_nothing_is_revoked(
        self, deps: IdentityDependencies, store: Store
    ) -> None:
        old = await self.login(deps, store)
        await RefreshSession(deps)(refresh_token=old, client=CLIENT)

        with pytest.raises(RefreshSuperseded):
            await RefreshSession(deps)(refresh_token=old, client=CLIENT)

        live = [t for t, _ in store.tokens.values() if t.revoked_at is None]
        assert len(live) == 1

    async def test_reuse_revocation_is_committed_before_the_error(
        self, deps: IdentityDependencies, store: Store, clock: FrozenClock
    ) -> None:
        old = await self.login(deps, store)
        await RefreshSession(deps)(refresh_token=old, client=CLIENT)
        clock.advance(timedelta(seconds=11))

        with pytest.raises(RefreshTokenReused):
            await RefreshSession(deps)(refresh_token=old, client=CLIENT)

        assert all(t.revoked_at is not None for t, _ in store.tokens.values())
        assert "auth.refresh_reuse_detected" in store.audit
        assert store.rollbacks == 0
