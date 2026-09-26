from uuid import uuid4

from app.modules.conversations.domain.model import direct_key


def test_direct_key_is_order_independent() -> None:
    a, b = uuid4(), uuid4()

    assert direct_key(a, b) == direct_key(b, a)


def test_direct_key_differs_per_pair() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()

    assert direct_key(a, b) != direct_key(a, c)
