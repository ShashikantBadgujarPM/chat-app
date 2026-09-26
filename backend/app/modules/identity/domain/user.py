"""The User entity. Pure domain code: no SQLAlchemy, no FastAPI."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    username: str
    email: str
    display_name: str
    # Needed to verify logins (M02), but must never appear in a repr or a log line.
    password_hash: str = field(repr=False)
    status: UserStatus
    timezone: str
    failed_login_attempts: int
    locked_until: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE and self.deleted_at is None
