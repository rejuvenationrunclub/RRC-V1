from __future__ import annotations

import pytest
from pydantic import ValidationError

from rrc_bot.config import Settings
from tests.conftest import ROOT, fake_discord_token


def test_defaults_need_no_secrets(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.discord_token is None
    assert s.display_tz == "America/Phoenix"
    assert str(s.tz) == "America/Phoenix"


def test_token_is_never_rendered(clean_env: pytest.MonkeyPatch) -> None:
    token = fake_discord_token()
    clean_env.setenv("DISCORD_TOKEN", token)
    s = Settings(_env_file=None)
    assert token not in repr(s)
    assert token not in str(s.model_dump())
    assert s.discord_token is not None
    assert s.discord_token.get_secret_value() == token


def test_database_url_is_secret(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://u:hunter2@h/db")
    assert "hunter2" not in repr(Settings(_env_file=None))


def test_require_bot_credentials_names_missing_vars(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN, DISCORD_GUILD_ID"):
        Settings(_env_file=None).require_bot_credentials()

    clean_env.setenv("DISCORD_TOKEN", fake_discord_token())
    with pytest.raises(RuntimeError, match=r"variable\(s\): DISCORD_GUILD_ID\."):
        Settings(_env_file=None).require_bot_credentials()


def test_blank_token_treated_as_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISCORD_TOKEN", "   ")
    clean_env.setenv("DISCORD_GUILD_ID", "123")
    s = Settings(_env_file=None)
    assert s.discord_token is None
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN"):
        s.require_bot_credentials()


@pytest.mark.parametrize("var", ["DISCORD_APPLICATION_ID", "DISCORD_GUILD_ID"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_discord_ids_are_none(clean_env: pytest.MonkeyPatch, var: str, blank: str) -> None:
    clean_env.setenv(var, blank)
    s = Settings(_env_file=None)
    assert getattr(s, var.lower()) is None


def test_numeric_discord_ids_parse(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISCORD_APPLICATION_ID", "1234567890123456789")
    clean_env.setenv("DISCORD_GUILD_ID", "987654321")
    s = Settings(_env_file=None)
    assert s.discord_application_id == 1234567890123456789
    assert s.discord_guild_id == 987654321


def test_non_numeric_discord_id_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISCORD_APPLICATION_ID", "my-app")
    with pytest.raises(ValidationError, match="discord_application_id"):
        Settings(_env_file=None)


def test_env_example_loads_unedited(clean_env: pytest.MonkeyPatch) -> None:
    # vm-deploy.sh copies .env.example as-is on first deploy; it must not crash the bot.
    s = Settings(_env_file=ROOT / ".env.example")
    assert s.discord_token is None
    assert s.discord_application_id is None
    assert s.discord_guild_id is None


def test_require_bot_credentials_success(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISCORD_TOKEN", fake_discord_token())
    clean_env.setenv("DISCORD_GUILD_ID", "987654321")
    token, guild_id = Settings(_env_file=None).require_bot_credentials()
    assert token == fake_discord_token()
    assert guild_id == 987654321


def test_invalid_timezone_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISPLAY_TZ", "Mars/Olympus_Mons")
    with pytest.raises(ValidationError, match="unknown IANA time zone"):
        Settings(_env_file=None)
