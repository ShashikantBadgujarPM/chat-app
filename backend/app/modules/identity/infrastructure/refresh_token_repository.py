"""Persistence for refresh tokens and the sessions derived from them."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.refresh_token import RefreshToken, Session
from app.modules.identity.infrastructure.models import RefreshTokenModel


def _to_domain(row: RefreshTokenModel) -> RefreshToken:
    return RefreshToken(
        id=row.id,
        user_id=row.user_id,
        family_id=row.family_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        replaced_by_id=row.replaced_by_id,
    )


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UUID,
        family_id: UUID,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> RefreshToken:
        row = RefreshTokenModel(
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            user_agent=user_agent[:512] if user_agent else None,
            ip_address=ip_address,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        # The row lock makes two concurrent refreshes with the same cookie run one
        # after the other, so the second sees the first one's rotation (R-6).
        statement = (
            select(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash == token_hash)
            .with_for_update()
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    async def mark_rotated(self, token_id: UUID, *, replaced_by_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.id == token_id)
            .values(revoked_at=now, replaced_by_id=replaced_by_id)
        )

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> int:
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.family_id == family_id, RefreshTokenModel.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return result.rowcount  # type: ignore[attr-defined, no-any-return]

    async def revoke_all_for_user(
        self, user_id: UUID, *, now: datetime, except_family_id: UUID | None = None
    ) -> int:
        conditions = [RefreshTokenModel.user_id == user_id, RefreshTokenModel.revoked_at.is_(None)]
        if except_family_id is not None:
            conditions.append(RefreshTokenModel.family_id != except_family_id)
        result = await self._session.execute(
            update(RefreshTokenModel).where(*conditions).values(revoked_at=now)
        )
        return result.rowcount  # type: ignore[attr-defined, no-any-return]

    async def list_active_sessions(self, user_id: UUID, *, now: datetime) -> list[Session]:
        """One entry per family that still has a live (unrevoked, unexpired) token.

        created_at = the family's first token; last_used_at = its latest token, since
        every refresh issues a new one (docs/design/07 §Auth `Session`).
        """
        live = RefreshTokenModel.revoked_at.is_(None) & (RefreshTokenModel.expires_at > now)
        families = (
            select(
                RefreshTokenModel.family_id,
                func.min(RefreshTokenModel.issued_at).label("created_at"),
                func.max(RefreshTokenModel.issued_at).label("last_used_at"),
            )
            .where(RefreshTokenModel.user_id == user_id)
            .group_by(RefreshTokenModel.family_id)
            .having(func.bool_or(live))
            .subquery()
        )
        latest = (
            select(
                RefreshTokenModel.family_id,
                RefreshTokenModel.user_agent,
                func.host(RefreshTokenModel.ip_address).label("ip_address"),
            )
            .where(RefreshTokenModel.user_id == user_id, live)
            .ext(distinct_on(RefreshTokenModel.family_id))
            .order_by(RefreshTokenModel.family_id, RefreshTokenModel.issued_at.desc())
            .subquery()
        )
        statement = (
            select(
                families.c.family_id,
                families.c.created_at,
                families.c.last_used_at,
                latest.c.user_agent,
                latest.c.ip_address,
            )
            .join(latest, latest.c.family_id == families.c.family_id)
            .order_by(families.c.last_used_at.desc())
        )
        rows = (await self._session.execute(statement)).all()
        return [
            Session(
                family_id=row.family_id,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                user_agent=row.user_agent,
                ip_address=row.ip_address,
            )
            for row in rows
        ]

    async def has_active_family(self, user_id: UUID, family_id: UUID, *, now: datetime) -> bool:
        statement = select(
            exists().where(
                and_(
                    RefreshTokenModel.user_id == user_id,
                    RefreshTokenModel.family_id == family_id,
                    RefreshTokenModel.revoked_at.is_(None),
                    RefreshTokenModel.expires_at > now,
                )
            )
        )
        return bool((await self._session.execute(statement)).scalar())
