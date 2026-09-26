"""Logging setup: stdlib `logging` configured through `dictConfig` (docs/design/10 §19).

- `LogContext` lives in a ContextVar, so every log line inside a request (or WS
  connection, or worker batch) carries its ids without passing them around.
- `ContextFilter` copies that context, plus static service fields, onto each record.
- `RedactingFilter` masks secrets as a second line of defense.
- `JsonFormatter` (production) and `ConsoleFormatter` (development) render records.

Filters are attached to the handler, not to loggers, so they also apply to records
propagated from third-party loggers such as uvicorn's.
"""

import json
import logging
import logging.config
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

REDACTED = "[REDACTED]"

# Attributes every LogRecord has. Anything else on a record came in through `extra=`.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)
# Fields this module adds itself; rendered in fixed positions rather than as extras.
_CONTEXT_FIELDS = ("request_id", "user_id", "connection_id", "batch_id")
_SERVICE_FIELDS = ("service", "env", "version")
_OWN_FIELDS = frozenset({"event", *_CONTEXT_FIELDS, *_SERVICE_FIELDS})

_SENSITIVE_KEY = re.compile(r"password|token|secret|authorization|cookie|ticket|refresh", re.I)
_JWT_LIKE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
# WS tickets travel in the query string (docs/design/06 §11.6).
_TICKET_PARAM = re.compile(r"(ticket=)[^&\s'\"]+")


@dataclass(frozen=True, slots=True)
class LogContext:
    request_id: str | None = None
    user_id: str | None = None
    connection_id: str | None = None
    batch_id: str | None = None


_log_context: ContextVar[LogContext] = ContextVar("log_context", default=LogContext())  # noqa: B039 - immutable default


def get_log_context() -> LogContext:
    return _log_context.get()


@contextmanager
def bind_log_context(**changes: str | None) -> Iterator[LogContext]:
    """Set context fields for the duration of the block, then restore the previous values."""
    token = _log_context.set(replace(_log_context.get(), **changes))
    try:
        yield _log_context.get()
    finally:
        _log_context.reset(token)


def update_log_context(**changes: str | None) -> None:
    """Add fields to the context of the current request (or connection, or batch).

    Only call this inside a `bind_log_context` block, such as the one the request-id
    middleware opens for every request: that block's exit restores the previous
    context, so these fields don't leak into the next request.
    """
    _log_context.set(replace(_log_context.get(), **changes))


class ContextFilter(logging.Filter):
    def __init__(self, service: str, env: str, version: str) -> None:
        super().__init__()
        self._static = {"service": service, "env": env, "version": version}

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in asdict(get_log_context()).items():
            setattr(record, key, value)
        for key, value in self._static.items():
            setattr(record, key, value)
        return True


class RedactingFilter(logging.Filter):
    """Masks sensitive `extra` values and JWT-looking strings in the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(vars(record).items()):
            if key in _STANDARD_RECORD_ATTRS:
                continue
            if _SENSITIVE_KEY.search(key):
                setattr(record, key, REDACTED)
            elif isinstance(value, Mapping):
                setattr(record, key, redact_mapping(value))
        message = record.getMessage()
        redacted = _TICKET_PARAM.sub(rf"\g<1>{REDACTED}", _JWT_LIKE.sub(REDACTED, message))
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def redact_mapping(value: Mapping[Any, Any]) -> dict[Any, Any]:
    """Copy of `value` with sensitive keys masked, recursively."""
    result: dict[Any, Any] = {}
    for key, item in value.items():
        if isinstance(key, str) and _SENSITIVE_KEY.search(key):
            result[key] = REDACTED
        elif isinstance(item, Mapping):
            result[key] = redact_mapping(item)
        else:
            result[key] = item
    return result


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD_RECORD_ATTRS and key not in _OWN_FIELDS
    }


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the field names from docs/design/10 §19.3."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
        }
        for field in (*_CONTEXT_FIELDS, *_SERVICE_FIELDS):
            entry[field] = getattr(record, field, None)
        entry.update(_extras(record))
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["exc_info"] = record.exc_text
        return json.dumps(entry, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable `ts level logger event msg key=value ...` for development."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [_timestamp(record), f"{record.levelname:<8}", record.name]
        event = getattr(record, "event", None)
        if event:
            parts.append(f"[{event}]")
        parts.append(record.getMessage())
        pairs = {field: getattr(record, field, None) for field in _CONTEXT_FIELDS}
        pairs.update(_extras(record))
        parts.extend(f"{key}={value}" for key, value in pairs.items() if value is not None)
        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(settings: Settings, *, service: str) -> None:
    formatter = JsonFormatter if settings.log_format == "json" else ConsoleFormatter
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "context": {
                    "()": ContextFilter,
                    "service": service,
                    "env": settings.env,
                    "version": settings.app_version,
                },
                "redact": {"()": RedactingFilter},
            },
            "formatters": {"default": {"()": formatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                    # Order matters: add context first, then redact everything.
                    "filters": ["context", "redact"],
                }
            },
            "root": {"level": settings.log_level, "handlers": ["stdout"]},
            "loggers": {
                # INFO would echo SQL with bound parameters, which can contain PII.
                "sqlalchemy.engine": {"level": "WARNING"},
                # uvicorn's own handlers are dropped so its records use ours.
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                # Replaced by AccessLogMiddleware.
                "uvicorn.access": {"handlers": [], "propagate": False, "level": "CRITICAL"},
                # Its DEBUG lines include raw request lines (with the WS ticket), so it
                # never goes below INFO, whatever LOG_LEVEL says.
                "websockets": {"level": "INFO"},
            },
        }
    )
