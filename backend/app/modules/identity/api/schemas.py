"""Request and response bodies for /api/v1/auth and /api/v1/users/me (docs/design/07)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, StringConstraints

from app.modules.identity.application.services import SessionView
from app.modules.identity.domain.user import User
from app.platform.schemas import RequestModel, UtcDateTime

Username = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{3,32}$")]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
# The password policy (length, common passwords) runs in the domain and returns
# 422 weak_password. This cap only bounds request size before hashing.
Password = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
Timezone = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class RegisterRequest(RequestModel):
    username: Username
    email: Annotated[EmailStr, StringConstraints(max_length=254)]
    display_name: DisplayName
    password: Password
    timezone: Timezone = "UTC"


class LoginRequest(RequestModel):
    username_or_email: Annotated[str, StringConstraints(min_length=1, max_length=254)]
    password: Password


class ChangePasswordRequest(RequestModel):
    current_password: Password
    new_password: Password


class UpdateMeRequest(RequestModel):
    """Partial update: only the fields present are changed."""

    display_name: DisplayName | None = None
    timezone: Timezone | None = None


class DeleteMeRequest(RequestModel):
    password: Password


class PresenceOut(BaseModel):
    status: Literal["online", "offline"]
    last_seen_at: UtcDateTime | None


# Until presence exists (M08), everyone is reported offline with no last-seen time.
OFFLINE = PresenceOut(status="offline", last_seen_at=None)


class UserPublic(BaseModel):
    id: UUID
    username: str
    display_name: str
    presence: PresenceOut

    @classmethod
    def from_user(cls, user: User, presence: PresenceOut = OFFLINE) -> "UserPublic":
        return cls(
            id=user.id, username=user.username, display_name=user.display_name, presence=presence
        )


class UserMe(BaseModel):
    id: UUID
    username: str
    email: str
    display_name: str
    timezone: str
    created_at: UtcDateTime

    @classmethod
    def from_user(cls, user: User) -> "UserMe":
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            timezone=user.timezone,
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth token type
    expires_in: int = Field(description="Access-token lifetime in seconds")


class LoginResponse(TokenResponse):
    user: UserMe


class SessionOut(BaseModel):
    family_id: UUID
    created_at: UtcDateTime
    last_used_at: UtcDateTime
    user_agent: str | None
    ip_address: str | None
    current: bool

    @classmethod
    def from_view(cls, view: SessionView) -> "SessionOut":
        session = view.session
        return cls(
            family_id=session.family_id,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            user_agent=session.user_agent,
            ip_address=session.ip_address,
            current=view.current,
        )


class SessionsResponse(BaseModel):
    items: list[SessionOut]
