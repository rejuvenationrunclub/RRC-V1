"""Runtime configuration.

Every value comes from the environment (or a local, git-ignored ``.env``).
Secrets are held as ``SecretStr`` so they never appear in reprs, logs or
tracebacks. Nothing here has a real credential as a default.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Discord. The token is optional at load time so that migrations and
    # tests can run without one; the bot refuses to start without it.
    discord_token: SecretStr | None = None
    discord_application_id: int | None = None
    discord_guild_id: int | None = None

    # Database (SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host/db)
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://rrc:rrc@localhost:5432/rrc"),
    )

    # Calendar display only. Storage is always UTC.
    display_tz: str = "America/Phoenix"

    log_level: LogLevel = "INFO"

    @field_validator("display_tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value

    @field_validator("discord_token")
    @classmethod
    def _blank_token_is_none(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    # .env.example ships these as `KEY=` with no value; an empty string is not an int.
    @field_validator("discord_application_id", "discord_guild_id", mode="before")
    @classmethod
    def _blank_id_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_tz)

    def require_bot_credentials(self) -> tuple[str, int]:
        """Return (token, guild_id) or raise a clear error naming what is missing."""
        token, guild_id = self.discord_token, self.discord_guild_id
        if token is None or guild_id is None:
            missing = [
                name
                for name, value in (("DISCORD_TOKEN", token), ("DISCORD_GUILD_ID", guild_id))
                if value is None
            ]
            raise RuntimeError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". See .env.example and README 'Discord Developer Portal setup'."
            )
        return token.get_secret_value(), guild_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
