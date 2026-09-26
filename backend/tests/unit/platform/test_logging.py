import asyncio
import json
import logging
import sys

from app.platform.logging import (
    REDACTED,
    ConsoleFormatter,
    ContextFilter,
    JsonFormatter,
    RedactingFilter,
    bind_log_context,
    get_log_context,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJlLXZhbHVl"


def make_record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("tests.logger", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def apply_filters(record: logging.LogRecord) -> logging.LogRecord:
    ContextFilter(service="api", env="test", version="abc123").filter(record)
    RedactingFilter().filter(record)
    return record


class TestRedactingFilter:
    def test_masks_sensitive_extra_keys(self) -> None:
        record = apply_filters(
            make_record(password="hunter2", access_token="t", authorization="Bearer x", other=1)
        )

        assert record.password == REDACTED
        assert record.access_token == REDACTED
        assert record.authorization == REDACTED
        assert record.other == 1

    def test_masks_sensitive_keys_inside_nested_mappings(self) -> None:
        record = apply_filters(make_record(payload={"user": "a", "auth": {"refresh_token": "r"}}))

        assert record.payload == {"user": "a", "auth": {"refresh_token": REDACTED}}

    def test_masks_jwt_looking_strings_in_the_message(self) -> None:
        record = logging.LogRecord(
            "tests", logging.INFO, __file__, 1, "got header %s", (f"Bearer {JWT}",), None
        )
        RedactingFilter().filter(record)

        assert JWT not in record.getMessage()
        assert REDACTED in record.getMessage()


class TestJsonFormatter:
    def test_emits_the_standard_fields_as_one_json_line(self) -> None:
        with bind_log_context(request_id="req-12345678", user_id="user-1"):
            record = apply_filters(make_record(event="http.request", status=200))
        line = JsonFormatter().format(record)

        assert "\n" not in line
        entry = json.loads(line)
        assert entry["ts"].endswith("Z")
        assert entry["level"] == "INFO"
        assert entry["logger"] == "tests.logger"
        assert entry["event"] == "http.request"
        assert entry["msg"] == "hello"
        assert entry["request_id"] == "req-12345678"
        assert entry["user_id"] == "user-1"
        assert entry["service"] == "api"
        assert entry["env"] == "test"
        assert entry["version"] == "abc123"
        assert entry["status"] == 200

    def test_includes_the_stack_trace_on_exceptions(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "tests", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
        entry = json.loads(JsonFormatter().format(apply_filters(record)))

        assert "ValueError: boom" in entry["exc_info"]


def test_console_formatter_renders_context_as_key_value_pairs() -> None:
    with bind_log_context(request_id="req-12345678"):
        record = apply_filters(make_record(event="app.startup"))
    line = ConsoleFormatter().format(record)

    assert "[app.startup]" in line
    assert "request_id=req-12345678" in line


class TestLogContext:
    def test_bind_restores_the_previous_context(self) -> None:
        with bind_log_context(request_id="outer-123"):
            with bind_log_context(user_id="u1"):
                assert get_log_context().request_id == "outer-123"
                assert get_log_context().user_id == "u1"
            assert get_log_context().user_id is None
        assert get_log_context().request_id is None

    async def test_is_isolated_between_concurrent_tasks(self) -> None:
        async def handle(request_id: str) -> str | None:
            with bind_log_context(request_id=request_id):
                # Yield so the two tasks interleave while both contexts are bound.
                await asyncio.sleep(0.01)
                return get_log_context().request_id

        results = await asyncio.gather(handle("request-a"), handle("request-b"))

        assert results == ["request-a", "request-b"]
        assert get_log_context().request_id is None


def test_masks_ws_tickets_in_query_strings() -> None:
    record = logging.LogRecord(
        "tests", logging.INFO, __file__, 1, "GET %s", ("/ws?ticket=abc123SECRET&x=1",), None
    )
    RedactingFilter().filter(record)

    assert "abc123SECRET" not in record.getMessage()
    assert "ticket=[REDACTED]&x=1" in record.getMessage()
