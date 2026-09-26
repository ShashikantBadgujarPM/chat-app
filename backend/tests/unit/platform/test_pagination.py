import base64

import pytest

from app.platform.pagination import InvalidCursor, decode_cursor, encode_cursor


def test_round_trip() -> None:
    values = [0.4375, "0b7d7a4e-8a1e-4a4a-9d64-4f5b8d7e1a2b"]

    assert decode_cursor(encode_cursor(values), length=2) == values


def test_cursor_is_url_safe() -> None:
    cursor = encode_cursor(["?&=/+", 1])

    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 !!!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b'{"a": 1}').decode(),  # not a list
        encode_cursor([1, 2, 3]),  # wrong length
        "x" * 600,
        "",
    ],
)
def test_tampered_cursors_raise_invalid_cursor(cursor: str) -> None:
    with pytest.raises(InvalidCursor) as caught:
        decode_cursor(cursor, length=2)

    assert caught.value.status_code == 400
