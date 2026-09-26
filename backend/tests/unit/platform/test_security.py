import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.platform.security import (
    Argon2PasswordHasher,
    ExpiredAccessToken,
    InvalidAccessToken,
    JwtTokenIssuer,
    generate_opaque_token,
    sha256_hex,
)

SECRET = "unit-test-secret-that-is-long-enough-000000"
OTHER_SECRET = "a-different-secret-that-is-long-enough-11111"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def issuer(**kwargs: object) -> JwtTokenIssuer:
    options: dict[str, object] = {"secret": SECRET, "access_token_ttl_seconds": 900}
    options.update(kwargs)
    return JwtTokenIssuer(**options)  # type: ignore[arg-type]


def forge(claims: dict[str, object], *, secret: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, secret, algorithm=algorithm)


def valid_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "sub": str(uuid4()),
        "sid": str(uuid4()),
        "jti": str(uuid4()),
        "type": "access",
        "iss": "chat-app",
        "aud": "chat-app-api",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=15)).timestamp()),
    }
    claims.update(overrides)
    return claims


class TestJwtTokenIssuer:
    def test_round_trip(self) -> None:
        user_id, session_id = uuid4(), uuid4()
        token = issuer().issue_access_token(user_id=user_id, session_id=session_id, now=NOW)

        claims = issuer().decode_access_token(token, now=NOW + timedelta(minutes=1))

        assert claims.user_id == user_id
        assert claims.session_id == session_id
        assert claims.expires_at == NOW + timedelta(seconds=900)

    def test_expired_token_is_rejected(self) -> None:
        token = issuer().issue_access_token(user_id=uuid4(), session_id=uuid4(), now=NOW)

        with pytest.raises(ExpiredAccessToken):
            issuer().decode_access_token(token, now=NOW + timedelta(seconds=900))

    @pytest.mark.parametrize(
        "overrides",
        [
            {"aud": "someone-else"},
            {"iss": "someone-else"},
            {"type": "refresh"},
            {"sub": "not-a-uuid"},
        ],
    )
    def test_wrong_claims_are_rejected(self, overrides: dict[str, object]) -> None:
        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(forge(valid_claims(**overrides)), now=NOW)

    @pytest.mark.parametrize("missing", ["sub", "sid", "jti", "type", "exp", "aud", "iss"])
    def test_missing_claims_are_rejected(self, missing: str) -> None:
        claims = valid_claims()
        del claims[missing]

        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(forge(claims), now=NOW)

    def test_wrong_key_is_rejected(self) -> None:
        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(forge(valid_claims(), secret=OTHER_SECRET), now=NOW)

    def test_alg_none_is_rejected(self) -> None:
        def b64(part: dict[str, object]) -> str:
            return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

        unsigned = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(valid_claims())}."

        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(unsigned, now=NOW)

    @pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
    def test_other_algorithms_are_rejected(self) -> None:
        # HS512 with the right secret: still refused, because only HS256 is accepted.
        token = forge(valid_claims(), algorithm="HS512")

        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(token, now=NOW)

    def test_token_signed_with_the_previous_secret_is_accepted_during_rotation(self) -> None:
        old = issuer(secret=OTHER_SECRET).issue_access_token(
            user_id=uuid4(), session_id=uuid4(), now=NOW
        )

        rotated = issuer(previous_secret=OTHER_SECRET)
        assert rotated.decode_access_token(old, now=NOW).user_id is not None
        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(old, now=NOW)

    @pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "eyJ.eyJ.sig"])
    def test_garbage_is_rejected(self, garbage: str) -> None:
        with pytest.raises(InvalidAccessToken):
            issuer().decode_access_token(garbage, now=NOW)


def test_opaque_tokens_are_long_random_and_url_safe() -> None:
    tokens = {generate_opaque_token() for _ in range(1000)}

    assert len(tokens) == 1000
    for token in tokens:
        # 32 random bytes -> 43 base64url characters.
        assert len(token) == 43
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_sha256_hex_is_stable_and_not_the_input() -> None:
    assert sha256_hex("abc") == sha256_hex("abc")
    assert sha256_hex("abc") != "abc"
    assert len(sha256_hex("abc")) == 64


class TestArgon2PasswordHasher:
    @pytest.fixture
    def hasher(self) -> Argon2PasswordHasher:
        return Argon2PasswordHasher(time_cost=1, memory_cost_kib=8, parallelism=1)

    async def test_hash_and_verify(self, hasher: Argon2PasswordHasher) -> None:
        password_hash = await hasher.hash("correct horse battery")

        assert password_hash.startswith("$argon2id$")
        assert await hasher.verify(password_hash, "correct horse battery")
        assert not await hasher.verify(password_hash, "wrong password here")

    async def test_verify_rejects_a_malformed_hash(self, hasher: Argon2PasswordHasher) -> None:
        assert not await hasher.verify("not-a-hash", "anything")

    async def test_needs_rehash_when_parameters_change(self, hasher: Argon2PasswordHasher) -> None:
        weak_hash = await hasher.hash("correct horse battery")
        stronger = Argon2PasswordHasher(time_cost=2, memory_cost_kib=16, parallelism=1)

        assert not hasher.needs_rehash(weak_hash)
        assert stronger.needs_rehash(weak_hash)

    async def test_dummy_hash_is_a_valid_hash(self, hasher: Argon2PasswordHasher) -> None:
        assert hasher.dummy_hash.startswith("$argon2id$")
        assert not await hasher.verify(hasher.dummy_hash, "anything")
