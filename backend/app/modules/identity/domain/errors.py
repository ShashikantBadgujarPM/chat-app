"""Identity errors. Codes and statuses follow docs/design/07 §Auth."""

from app.platform.errors import (
    AccountLockedError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


class InvalidCredentials(AuthenticationError):
    # One message for unknown user, wrong password and disabled account (06 §11.5).
    default_code = "invalid_credentials"
    default_message = "The username or password is incorrect."


class AccountLocked(AccountLockedError):
    default_message = "Too many failed attempts. Try again later."


class InvalidAccessTokenError(AuthenticationError):
    default_code = "invalid_token"
    default_message = "The access token is invalid."


class AccessTokenExpired(AuthenticationError):
    default_code = "token_expired"
    default_message = "The access token has expired."


class ClearsRefreshCookie:
    """Marker: the error response also deletes the refresh cookie."""


class InvalidRefreshToken(ClearsRefreshCookie, AuthenticationError):
    default_code = "invalid_refresh_token"
    default_message = "The session is no longer valid. Please sign in again."


class RefreshTokenReused(ClearsRefreshCookie, AuthenticationError):
    default_code = "refresh_token_reused"
    default_message = "The session was revoked for security reasons. Please sign in again."


class RefreshSuperseded(ConflictError):
    default_code = "refresh_superseded"
    default_message = "The session was refreshed by another request. Retry once."


class UsernameTaken(ConflictError):
    default_code = "username_taken"
    default_message = "That username is already taken."


class EmailTaken(ConflictError):
    default_code = "email_taken"
    default_message = "That email address is already registered."


class WeakPassword(ValidationError):
    default_code = "weak_password"
    default_message = "The password does not meet the requirements."


class InvalidTimezone(ValidationError):
    default_code = "invalid_timezone"
    default_message = "Unknown time zone."


class SessionNotFound(NotFoundError):
    default_code = "session_not_found"
    default_message = "The session was not found."
