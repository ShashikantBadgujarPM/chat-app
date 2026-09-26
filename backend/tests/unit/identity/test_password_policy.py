import pytest

from app.modules.identity.domain.errors import WeakPassword
from app.modules.identity.domain.password_policy import MAX_LENGTH, MIN_LENGTH, validate_password


@pytest.mark.parametrize(
    "password",
    ["x" * MIN_LENGTH, "correct horse battery staple", "ünïcödé-pässwörd", "x" * MAX_LENGTH],
)
def test_accepts_passwords_within_the_limits(password: str) -> None:
    validate_password(password)


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("x" * (MIN_LENGTH - 1), "too_short"),
        ("", "too_short"),
        ("x" * (MAX_LENGTH + 1), "too_long"),
        ("password123", "too_common"),
        ("QWERTYUIOP", "too_common"),
    ],
)
def test_rejects_weak_passwords(password: str, reason: str) -> None:
    with pytest.raises(WeakPassword) as caught:
        validate_password(password)

    assert caught.value.code == "weak_password"
    assert caught.value.details["reason"] == reason


def test_no_composition_rules() -> None:
    # NIST SP 800-63B: length matters, character classes don't.
    validate_password("alllowercaseletters")
