from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import TEST_JWT_SECRET, SettingsFactory


def test_missing_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(env="test")


def test_short_jwt_secret_is_rejected(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        make_settings(jwt_secret="x" * 31)


def test_short_previous_jwt_secret_is_rejected(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_PREVIOUS"):
        make_settings(jwt_secret_previous="short")


def test_jwt_secret_can_come_from_a_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "jwt_secret"
    secret_file.write_text(f"{TEST_JWT_SECRET}\n", encoding="utf-8")

    settings = Settings(env="test", jwt_secret_file=secret_file)

    assert settings.jwt_secret is not None
    assert settings.jwt_secret.get_secret_value() == TEST_JWT_SECRET


def test_jwt_secret_and_file_together_are_rejected(tmp_path: Path) -> None:
    secret_file = tmp_path / "jwt_secret"
    secret_file.write_text(TEST_JWT_SECRET, encoding="utf-8")

    with pytest.raises(ValidationError, match="only one"):
        Settings(env="test", jwt_secret=TEST_JWT_SECRET, jwt_secret_file=secret_file)


def test_secret_is_not_shown_in_repr(make_settings: SettingsFactory) -> None:
    settings = make_settings()

    assert TEST_JWT_SECRET not in repr(settings)
    assert TEST_JWT_SECRET not in str(settings.safe_summary())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"debug": True}, "DEBUG"),
        ({"allowed_origins": "*"}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": "https://chat.example,*"}, "ALLOWED_ORIGINS"),
    ],
)
def test_production_rejects_insecure_settings(
    make_settings: SettingsFactory, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(env="production", **overrides)


def test_insecure_cookie_is_allowed_only_in_development(make_settings: SettingsFactory) -> None:
    assert make_settings(env="development", cookie_secure=False).cookie_secure is False
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        make_settings(env="test", cookie_secure=False)


def test_env_defaults_to_production() -> None:
    settings = Settings(jwt_secret=TEST_JWT_SECRET)

    assert settings.env == "production"


def test_allowed_origins_are_read_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example")

    assert Settings().allowed_origins == ["https://a.example", "https://b.example"]
