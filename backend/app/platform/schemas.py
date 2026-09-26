"""Shared API schema types (docs/design/07 §13.1)."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _to_rfc3339_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# Timestamps are always returned as RFC 3339 in UTC with a `Z` suffix.
UtcDateTime = Annotated[datetime, PlainSerializer(_to_rfc3339_utc, return_type=str)]


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected (docs/design/10 §20)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
