"""Test data factories: plain async functions with keyword defaults (docs/design/11 §21.3)."""

from itertools import count

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.user import User
from app.modules.identity.infrastructure.user_repository import UserRepository

_sequence = count(1)

# Not a real hash; M00/M01 tests never verify passwords.
FAKE_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$fake$fake"


async def make_user(session: AsyncSession, **overrides: str) -> User:
    n = next(_sequence)
    values = {
        "username": f"user{n}",
        "email": f"user{n}@example.test",
        "display_name": f"User {n}",
        "password_hash": FAKE_PASSWORD_HASH,
    }
    values.update(overrides)
    return await UserRepository(session).add(**values)
