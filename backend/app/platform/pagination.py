"""Opaque keyset cursors and the `Page` response model (docs/design/07 §13.1).

A cursor is base64url JSON of the last row's sort key plus its id. Keyset pagination
means inserts between page fetches can't cause duplicates or skipped rows the way
OFFSET pagination can. Cursors aren't signed: a tampered one can only select a
different position the caller could reach anyway, and decoding rejects malformed
input with a 400.
"""

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel

from app.platform.errors import BadRequestError


class InvalidCursor(BadRequestError):
    default_code = "invalid_cursor"
    default_message = "The pagination cursor is invalid."


def encode_cursor(values: list[Any]) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str, *, length: int) -> list[Any]:
    """Decode a cursor made by `encode_cursor`; anything else raises InvalidCursor."""
    if len(cursor) > 512:
        raise InvalidCursor()
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise InvalidCursor() from exc
    if not isinstance(values, list) or len(values) != length:
        raise InvalidCursor()
    return values


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None
