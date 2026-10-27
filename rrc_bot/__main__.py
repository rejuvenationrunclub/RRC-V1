"""Entry point: ``python -m rrc_bot`` (or the ``rrc-bot`` console script)."""

from __future__ import annotations

import logging
import sys

from rrc_bot.config import get_settings
from rrc_bot.logging_setup import configure_logging

log = logging.getLogger("rrc_bot")


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        token, guild_id = settings.require_bot_credentials()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2

    # Imported lazily so config/migration tooling never needs discord.py at import time.
    from rrc_bot.bot.client import RRCBot

    bot = RRCBot(guild_id=guild_id)
    # log_handler=None: keep our handler (with token redaction) instead of discord.py's.
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
