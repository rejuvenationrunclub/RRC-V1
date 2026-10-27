from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def fake_discord_token() -> str:
    """A syntactically token-shaped string, built at runtime so scanners never see a literal."""
    return "M" + "T" * 25 + "." + "G" * 6 + "." + "x" * 38


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip RRC-related env vars so Settings tests see only what they set."""
    for key in list(os.environ):
        if key.startswith(("DISCORD_", "DATABASE_", "DISPLAY_", "LOG_")):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture(scope="session")
def test_database_url() -> str:
    # Environment wins; fall back to the developer's git-ignored .env.
    url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )
    if not url:
        if os.environ.get("RRC_REQUIRE_DB") == "1":
            pytest.fail("TEST_DATABASE_URL is required (RRC_REQUIRE_DB=1) but not set")
        pytest.skip("TEST_DATABASE_URL not set; start the DB with `docker compose up -d db`")
    return url
