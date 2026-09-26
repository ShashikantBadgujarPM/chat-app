"""Refresh tokens and login sessions (docs/design/06 §11.1-11.3)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

# A token rotated this recently is treated as a benign concurrent refresh (another tab)
# rather than theft: 409 refresh_superseded instead of revoking the family (R-6).
REFRESH_GRACE_PERIOD = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class RefreshToken:
    id: UUID
    user_id: UUID
    family_id: UUID
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by_id: UUID | None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def was_just_rotated(self, now: datetime) -> bool:
        """Revoked by rotation (not logout) within the grace period."""
        return (
            self.revoked_at is not None
            and self.replaced_by_id is not None
            and now - self.revoked_at < REFRESH_GRACE_PERIOD
        )


@dataclass(frozen=True, slots=True)
class Session:
    """One login: a refresh-token family (its id is the access token's `sid`)."""

    family_id: UUID
    created_at: datetime
    last_used_at: datetime
    user_agent: str | None
    ip_address: str | None
