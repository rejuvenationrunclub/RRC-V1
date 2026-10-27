"""Logging configuration with a last-line-of-defence token redactor."""

from __future__ import annotations

import logging
import re

# Discord bot tokens: base64 user id, timestamp, HMAC, separated by dots.
_TOKEN_RE = re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}")


class RedactTokensFilter(logging.Filter):
    """Replace anything shaped like a Discord token before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_RE.sub("[REDACTED_TOKEN]", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    handler.addFilter(RedactTokensFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # discord.py is chatty at DEBUG; keep it at INFO unless we explicitly debug it.
    logging.getLogger("discord").setLevel(max(logging.INFO, root.level))
