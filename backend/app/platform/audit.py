"""Append-only audit records (docs/design/05 §audit_logs, 10 §20 "Audit").

`AuditLogger` writes inside the caller's transaction, so an audit row exists exactly
when the change it describes was committed. Metadata passes through the same redaction
as logs, as a second line of defense against secrets ending up in the table.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.logging import redact_mapping
from app.platform.models_base import Base


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLogger:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        action: str,
        *,
        actor_user_id: UUID | None = None,
        target_type: str | None = None,
        target_id: UUID | None = None,
        metadata: Mapping[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        self._session.add(
            AuditLogModel(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                metadata_=redact_mapping(metadata or {}),
                ip_address=ip_address,
            )
        )
        await self._session.flush()
