from __future__ import annotations

import logging

from rrc_bot.logging_setup import RedactTokensFilter
from tests.conftest import fake_discord_token


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args or None, None)


def test_token_in_message_is_redacted() -> None:
    token = fake_discord_token()
    record = _record(f"connecting with {token}")
    assert RedactTokensFilter().filter(record) is True
    assert token not in record.getMessage()
    assert "[REDACTED_TOKEN]" in record.getMessage()


def test_token_in_args_is_redacted() -> None:
    token = fake_discord_token()
    record = _record("token=%s", token)
    RedactTokensFilter().filter(record)
    assert token not in record.getMessage()


def test_ordinary_messages_untouched() -> None:
    record = _record("member %s checked in to event %d", "runner#1", 42)
    RedactTokensFilter().filter(record)
    assert record.getMessage() == "member runner#1 checked in to event 42"
    assert record.args == ("runner#1", 42)
