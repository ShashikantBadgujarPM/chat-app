"""The dev seed goes through the real use cases and is idempotent."""

import pytest
from sqlalchemy import func, select

from app.modules.conversations.infrastructure.models import ConversationModel
from app.modules.identity.infrastructure.models import UserModel
from app.modules.messaging.infrastructure.models import MessageModel
from app.platform.db import SessionFactory
from app.seed import DIRECT_MESSAGES, GROUP_MESSAGES, USERS, seed
from tests.api.conftest import FAST_ARGON2
from tests.conftest import SettingsFactory

pytestmark = pytest.mark.real_commits


async def counts(session_factory: SessionFactory) -> tuple[int, int, int]:
    async with session_factory() as session:
        return (
            (await session.execute(select(func.count()).select_from(UserModel))).scalar_one(),
            (
                await session.execute(select(func.count()).select_from(ConversationModel))
            ).scalar_one(),
            (await session.execute(select(func.count()).select_from(MessageModel))).scalar_one(),
        )


async def test_seed_creates_demo_data_once(
    session_factory: SessionFactory, make_settings: SettingsFactory
) -> None:
    settings = make_settings(**FAST_ARGON2)

    first = await seed(settings, session_factory, password="seed-password")
    second = await seed(settings, session_factory, password="seed-password")

    assert sorted(first.users_created) == sorted(u for u, _, _ in USERS)
    assert first.messages_created == len(DIRECT_MESSAGES) + len(GROUP_MESSAGES)
    assert second.users_created == []
    assert second.conversations_created == []
    assert second.messages_created == 0
    assert await counts(session_factory) == (
        len(USERS),
        2,
        len(DIRECT_MESSAGES) + len(GROUP_MESSAGES),
    )
