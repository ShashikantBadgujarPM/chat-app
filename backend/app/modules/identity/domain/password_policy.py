"""Password policy (docs/design/06 §11.4): length only, no composition rules (NIST
SP 800-63B), plus a check against well-known passwords."""

from app.modules.identity.domain.errors import WeakPassword

MIN_LENGTH = 10
# Bounds the hashing cost a single request can cause.
MAX_LENGTH = 128

# Common passwords that satisfy the length rule. A small local list, compared
# case-insensitively; not a replacement for a breach-corpus check.
COMMON_PASSWORDS = frozenset(
    {
        "0123456789",
        "1234567890",
        "12345678910",
        "123456789a",
        "1q2w3e4r5t",
        "1qaz2wsx3edc",
        "abcdefghij",
        "abcd123456",
        "administrator",
        "baseball123",
        "changeme123",
        "football123",
        "iloveyou123",
        "letmein123",
        "password1!",
        "password12",
        "password123",
        "password1234",
        "passw0rd123",
        "princess123",
        "qwerty1234",
        "qwerty12345",
        "qwertyuiop",
        "sunshine123",
        "superman123",
        "trustno1234",
        "welcome123",
        "zaq12wsxcde",
    }
)


def validate_password(password: str) -> None:
    """Raises WeakPassword with a `details.reason` of too_short, too_long or too_common."""
    if len(password) < MIN_LENGTH:
        raise WeakPassword(
            f"The password must be at least {MIN_LENGTH} characters.",
            details={"reason": "too_short", "min_length": MIN_LENGTH},
        )
    if len(password) > MAX_LENGTH:
        raise WeakPassword(
            f"The password must be at most {MAX_LENGTH} characters.",
            details={"reason": "too_long", "max_length": MAX_LENGTH},
        )
    if password.lower() in COMMON_PASSWORDS:
        raise WeakPassword(
            "That password is too common.",
            details={"reason": "too_common"},
        )
