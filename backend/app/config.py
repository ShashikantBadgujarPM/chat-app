"""Application settings, read from the environment and validated at startup.

The rules here are deliberately fail-fast: a misconfigured process should refuse to
start rather than run insecurely (see docs/design/10 §20 "Secure configuration" and
docs/design/12 §22.4).
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]

MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    # Environment variables only; `.env` is loaded by Docker Compose, not by the app.
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # Defaulting to production means a forgotten ENV gets the strictest checks.
    env: Environment = "production"
    debug: bool = False
    app_version: str = "dev"

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    # Required. Either JWT_SECRET or JWT_SECRET_FILE (for Docker secrets) must be set.
    jwt_secret: SecretStr | None = None
    jwt_secret_file: Path | None = None
    # Accepted for verification only, during a key rotation window.
    jwt_secret_previous: SecretStr | None = None

    access_token_ttl_seconds: int = Field(default=900, gt=0)
    refresh_token_ttl_days: int = Field(default=14, gt=0)

    # Argon2id cost (docs/design/06 §11.4). Pinned here so a library upgrade can't
    # silently change it; tests lower it to keep the suite fast.
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost_kib: int = Field(default=65536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)

    # Rate limits (docs/design/07 §13.4). Buckets are per process (ADR-002).
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = Field(default=10, ge=1)
    rate_limit_register_per_hour: int = Field(default=5, ge=1)
    rate_limit_refresh_per_minute: int = Field(default=30, ge=1)
    rate_limit_authenticated_per_minute: int = Field(default=300, ge=1)
    rate_limit_message_send_per_10s: int = Field(default=30, ge=1)

    cookie_secure: bool = True
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    max_request_body_bytes: int = Field(default=64 * 1024, gt=0)

    # The application role (DML only). The owner URL is read by Alembic alone, from
    # DATABASE_OWNER_URL, so the API process never holds that credential.
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        # ALLOWED_ORIGINS is a comma-separated list, e.g. "https://a.example,https://b.example".
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value

    @model_validator(mode="after")
    def _validate(self) -> Self:
        self._resolve_jwt_secret()

        if self.jwt_secret_previous is not None:
            _require_secret_length("JWT_SECRET_PREVIOUS", self.jwt_secret_previous)

        if not self.cookie_secure and self.env != "development":
            raise ValueError("COOKIE_SECURE=false is only allowed when ENV=development")

        if self.env == "production":
            if self.debug:
                raise ValueError("DEBUG=true is not allowed when ENV=production")
            if "*" in self.allowed_origins:
                raise ValueError("ALLOWED_ORIGINS must not contain '*' when ENV=production")
        return self

    def _resolve_jwt_secret(self) -> None:
        if self.jwt_secret is not None and self.jwt_secret_file is not None:
            raise ValueError("Set only one of JWT_SECRET or JWT_SECRET_FILE")
        if self.jwt_secret is None and self.jwt_secret_file is not None:
            try:
                raw = self.jwt_secret_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"JWT_SECRET_FILE could not be read: {exc.strerror}") from exc
            # The model is frozen; this is the one place a derived value is filled in.
            object.__setattr__(self, "jwt_secret", SecretStr(raw))
        if self.jwt_secret is None:
            raise ValueError("JWT_SECRET (or JWT_SECRET_FILE) is required")
        _require_secret_length("JWT_SECRET", self.jwt_secret)

    def safe_summary(self) -> dict[str, object]:
        """Non-secret settings, for the startup log line."""
        return {
            "env": self.env,
            "debug": self.debug,
            "version": self.app_version,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "cookie_secure": self.cookie_secure,
            "allowed_origins": self.allowed_origins,
            "max_request_body_bytes": self.max_request_body_bytes,
            "db_pool_size": self.db_pool_size,
            "db_max_overflow": self.db_max_overflow,
        }


def _require_secret_length(name: str, secret: SecretStr) -> None:
    if len(secret.get_secret_value().encode("utf-8")) < MIN_SECRET_BYTES:
        raise ValueError(f"{name} must be at least {MIN_SECRET_BYTES} bytes")


@lru_cache
def get_settings() -> Settings:
    return Settings()
