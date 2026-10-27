from __future__ import annotations

from rrc_bot.bot.client import build_intents


def test_intents_are_least_privilege() -> None:
    intents = build_intents()
    assert intents.guilds is True
    assert intents.members is True  # leave/rejoin detection, role re-application
    # Never read message content or presences (plan §0, §6).
    assert intents.message_content is False
    assert intents.presences is False
    assert intents.guild_messages is False
    assert intents.dm_messages is False
