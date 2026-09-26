"""Development seed data: `uv run python -m app.seed [--reset]`.

Everything goes through the real use cases (RegisterUser, ConversationService,
MessagingService), not raw SQL, so passwords are properly hashed, every rule applies,
and the seed keeps working as the schema evolves.

Idempotent: running it again changes nothing. Users are looked up before they are
registered, DMs are get-or-create, groups are matched by title, and messages use fixed
client_message_ids, which the send path already de-duplicates (R-5).

`--reset` empties every table first (it needs DATABASE_OWNER_URL, and refuses to run
when ENV=production).
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid5

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.config import Settings, get_settings, load_local_env
from app.modules.conversations.application.conversation_service import ConversationService
from app.modules.conversations.infrastructure.unit_of_work import SqlConversationsUnitOfWork
from app.modules.identity.application.services import IdentityDependencies, RegisterUser
from app.modules.identity.infrastructure.unit_of_work import SqlIdentityUnitOfWork
from app.modules.identity.infrastructure.user_repository import UserRepository
from app.modules.messaging.application.messaging_service import MessagingService
from app.modules.messaging.infrastructure.unit_of_work import SqlMessagingUnitOfWork
from app.platform.clock import SystemClock
from app.platform.db import SessionFactory, UnitOfWork, create_engine, create_session_factory
from app.platform.logging import configure_logging
from app.platform.security import Argon2PasswordHasher, JwtTokenIssuer

logger = logging.getLogger("app.seed")

# Demo users only; app passwords need at least 10 characters.
DEFAULT_PASSWORD = "rootpassword"  # noqa: S105 - dev seed data, not a secret
SEED_NAMESPACE = UUID("5eed0000-0000-4000-8000-000000000000")

# username, display name, time zone
USERS = [
    ("alice", "Alice Sharma", "Asia/Kolkata"),
    ("bob", "Bob Mehta", "Asia/Kolkata"),
    ("carol", "Carol D'Souza", "Europe/London"),
    ("rahul", "Rahul Verma", "Asia/Kolkata"),
]

DIRECT_MESSAGES = [  # (sender, body) in the alice <-> bob DM
    ("alice", "Hi Bob! Did you get a chance to look at the release notes?"),
    ("bob", "Yes, looks good. Just two small comments."),
    ("alice", "Great, send them over when you can."),
]

GROUP_TITLE = "Launch team"
GROUP_MEMBERS = ["bob", "carol", "rahul"]  # alice owns it
GROUP_MESSAGES = [
    ("alice", "Welcome to the launch channel, everyone."),
    ("carol", "Thanks! Staging is green on my side."),
    ("alice", "Hey Rahul, please check the deployment."),
    ("rahul", "On it."),
]


@dataclass
class SeedReport:
    users_created: list[str] = field(default_factory=list)
    conversations_created: list[str] = field(default_factory=list)
    messages_created: int = 0


def _client_id(*parts: str) -> UUID:
    """A fixed id per seed message, so re-running never duplicates it."""
    return uuid5(SEED_NAMESPACE, ":".join(parts))


def _identity_deps(settings: Settings, session_factory: SessionFactory) -> IdentityDependencies:
    return IdentityDependencies(
        uow_factory=lambda: SqlIdentityUnitOfWork(session_factory),
        hasher=Argon2PasswordHasher.from_settings(settings),
        tokens=JwtTokenIssuer.from_settings(settings),
        clock=SystemClock(),
        refresh_token_ttl=timedelta(days=settings.refresh_token_ttl_days),
    )


async def seed(settings: Settings, session_factory: SessionFactory, *, password: str) -> SeedReport:
    report = SeedReport()
    register = RegisterUser(_identity_deps(settings, session_factory))

    ids: dict[str, UUID] = {}
    for username, display_name, timezone in USERS:
        async with UnitOfWork(session_factory) as uow:
            existing = await UserRepository(uow.session).get_by_username_or_email(username)
        if existing is None:
            user = await register(
                username=username,
                email=f"{username}@example.com",
                display_name=display_name,
                password=password,
                timezone=timezone,
            )
            report.users_created.append(username)
            ids[username] = user.id
        else:
            ids[username] = existing.id

    conversations = ConversationService(
        lambda: SqlConversationsUnitOfWork(session_factory), SystemClock()
    )
    messaging = MessagingService(lambda: SqlMessagingUnitOfWork(session_factory), SystemClock())

    async def post(conversation_id: UUID, key: str, messages: list[tuple[str, str]]) -> None:
        for index, (sender, body) in enumerate(messages):
            _, created = await messaging.send(
                sender_id=ids[sender],
                conversation_id=conversation_id,
                body=body,
                reply_to_id=None,
                client_message_id=_client_id(key, str(index)),
            )
            report.messages_created += int(created)

    dm, created = await conversations.get_or_create_direct(
        caller_id=ids["alice"], other_id=ids["bob"]
    )
    if created:
        report.conversations_created.append("DM alice <-> bob")
    await post(dm.conversation.id, "dm:alice:bob", DIRECT_MESSAGES)

    page = await conversations.list_for_user(caller_id=ids["alice"], limit=100, cursor=None)
    group = next((v for v in page.items if v.conversation.title == GROUP_TITLE), None)
    if group is None:
        group = await conversations.create_group(
            caller_id=ids["alice"],
            title=GROUP_TITLE,
            member_ids=[ids[name] for name in GROUP_MEMBERS],
        )
        report.conversations_created.append(f"group {GROUP_TITLE!r}")
    await post(group.conversation.id, "group:launch", GROUP_MESSAGES)
    return report


def reset(owner_url: str) -> list[str]:
    """Empty every application table (dev only). Returns the tables truncated."""
    dsn = make_url(owner_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        ]
        if tables:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tables)
                )
            )
    return tables


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="empty every table first (dev only)")
    args = parser.parse_args(argv)

    load_local_env()
    settings = get_settings()
    configure_logging(settings, service="seed")
    password = os.environ.get("SEED_PASSWORD", DEFAULT_PASSWORD)

    if args.reset:
        if settings.env == "production":
            print("Refusing to --reset with ENV=production.", file=sys.stderr)
            return 2
        owner_url = os.environ.get("DATABASE_OWNER_URL")
        if not owner_url:
            print("--reset needs DATABASE_OWNER_URL (the owner role).", file=sys.stderr)
            return 2
        truncated = reset(owner_url)
        print(f"Reset: emptied {len(truncated)} tables.")

    engine = create_engine(settings)
    try:
        report = await seed(settings, create_session_factory(engine), password=password)
    finally:
        await engine.dispose()

    print("Seed complete.")
    print(f"  users created:         {', '.join(report.users_created) or 'none (already there)'}")
    created = ", ".join(report.conversations_created) or "none (already there)"
    print(f"  conversations created: {created}")
    print(f"  messages created:      {report.messages_created}")
    print(f"  log in as any of {', '.join(u for u, _, _ in USERS)} with password {password!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
