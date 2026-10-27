from __future__ import annotations

import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import fake_discord_token
from tests.scripts.stubs import copy_repo_subset, install_stubs, stub_env

GUILD = "123456789012345678"


@dataclass
class Run:
    code: int
    out: str
    err: str
    calls: list[str]


@dataclass
class Deploy:
    repo: Path
    prefix: Path
    state: Path

    @property
    def env(self) -> dict[str, str]:
        values = {}
        for line in (self.repo / ".env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key] = value
        return values


@pytest.fixture
def deploy(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = copy_repo_subset(tmp_path / "repo")
    prefix = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    install_stubs(bin_dir)
    ctx = Deploy(repo=repo, prefix=prefix, state=tmp_path / "state")

    def run(*args: str, **stub: str) -> Run:
        log = tmp_path / "calls.log"
        log.write_text("")
        extra = {"RRC_ROOT_PREFIX": str(prefix), "RRC_CONNECT_TIMEOUT_S": "6", **stub}
        proc = subprocess.run(  # noqa: S603
            ["bash", str(repo / "scripts" / "vm-deploy.sh"), *args],
            env=stub_env(bin_dir, log, ctx.state, **extra),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return Run(proc.returncode, proc.stdout, proc.stderr, log.read_text().splitlines())

    return ctx, run


CONNECTED = "2026-10-27T03:00:00+0000 INFO rrc_bot.bot.client: Connected as RRC Bot#4242 (id=1)"


def test_first_deploy_without_token_stops_with_instructions(deploy) -> None:  # type: ignore[no-untyped-def]
    ctx, run = deploy
    r = run("--guild-id", GUILD)
    assert r.code != 0
    assert "Secret Manager" in r.err
    assert not [c for c in r.calls if " up " in c], "must not start the bot without a token"
    env = ctx.env
    assert env["DISCORD_GUILD_ID"] == GUILD
    assert env["DISCORD_TOKEN"] == ""


def test_first_deploy_happy_path(deploy) -> None:  # type: ignore[no-untyped-def]
    ctx, run = deploy
    token = fake_discord_token()
    r = run(
        "--guild-id",
        GUILD,
        "--bucket",
        "gs://rrc-backups-p",
        STUB_TOKEN=token,
        STUB_BOT_LOG=CONNECTED,
    )
    assert r.code == 0, r.err
    assert "SUCCESS: Connected as RRC Bot#4242" in r.out

    env_file = ctx.repo / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    env = ctx.env
    assert env["DISCORD_TOKEN"] == token
    assert env["DISCORD_GUILD_ID"] == GUILD
    assert re.fullmatch(r"[0-9a-f]{48}", env["POSTGRES_PASSWORD"])
    assert env["POSTGRES_PASSWORD"] in env["DATABASE_URL"]
    assert "change-me" not in env_file.read_text()

    # The token is never echoed anywhere.
    assert token not in r.out and token not in r.err
    assert not any(token in c for c in r.calls)

    assert (ctx.prefix / "etc/rrc-backup.env").read_text() == "GCS_BUCKET=gs://rrc-backups-p\n"
    units = ctx.prefix / "etc/systemd/system"
    assert (units / "rrc-backup.service").exists() and (units / "rrc-backup.timer").exists()
    assert "systemctl enable --now rrc-backup.timer" in r.calls
    assert any("docker compose --profile bot up -d --build" in c for c in r.calls)


def test_redeploy_keeps_password_and_skips_recreate_when_nothing_changed(deploy) -> None:  # type: ignore[no-untyped-def]
    ctx, run = deploy
    token = fake_discord_token()
    assert run("--guild-id", GUILD, STUB_TOKEN=token, STUB_BOT_LOG=CONNECTED).code == 0
    password = ctx.env["POSTGRES_PASSWORD"]

    r = run(STUB_TOKEN=token, STUB_RUNNING="db bot", STUB_BOT_LOG="")
    assert r.code == 0, r.err
    assert ctx.env["POSTGRES_PASSWORD"] == password
    assert ctx.env["DISCORD_GUILD_ID"] == GUILD  # remembered without --guild-id
    assert not [c for c in r.calls if "--force-recreate" in c]
    assert "bot unchanged and still running" in r.out


def test_redeploy_backs_up_first_when_installed(deploy) -> None:  # type: ignore[no-untyped-def]
    ctx, run = deploy
    token = fake_discord_token()
    assert run("--guild-id", GUILD, STUB_TOKEN=token, STUB_BOT_LOG=CONNECTED).code == 0

    r = run(STUB_TOKEN=token, STUB_RUNNING="db bot")
    assert r.code == 0, r.err
    archives = list((ctx.prefix / "var/backups/rrc").glob("rrc-*.dump"))
    assert len(archives) == 1
    dump_idx = next(i for i, c in enumerate(r.calls) if "pg_dump" in c)
    up_idx = next(i for i, c in enumerate(r.calls) if " up -d --build" in c)
    assert dump_idx < up_idx, "backup must happen before containers change"


def test_token_rotation_recreates_bot(deploy) -> None:  # type: ignore[no-untyped-def]
    ctx, run = deploy
    old, new = fake_discord_token(), fake_discord_token().replace("x", "y")
    assert run("--guild-id", GUILD, STUB_TOKEN=old, STUB_BOT_LOG=CONNECTED).code == 0

    r = run(STUB_TOKEN=new, STUB_RUNNING="db bot", STUB_BOT_LOG=CONNECTED)
    assert r.code == 0, r.err
    assert ctx.env["DISCORD_TOKEN"] == new
    assert any("up -d --force-recreate bot" in c for c in r.calls)


@pytest.mark.parametrize(
    ("log_line", "hint"),
    [
        ("discord.errors.PrivilegedIntentsRequired: Shard ID None", "Server Members Intent"),
        ("discord.errors.LoginFailure: Improper token has been passed.", "NEW VERSION"),
    ],
)
def test_explains_common_discord_failures(deploy, log_line: str, hint: str) -> None:  # type: ignore[no-untyped-def]
    _, run = deploy
    r = run("--guild-id", GUILD, STUB_TOKEN=fake_discord_token(), STUB_BOT_LOG=log_line)
    assert r.code != 0
    assert hint in r.err


def test_times_out_when_bot_never_connects(deploy) -> None:  # type: ignore[no-untyped-def]
    _, run = deploy
    r = run("--guild-id", GUILD, STUB_TOKEN=fake_discord_token(), RRC_CONNECT_TIMEOUT_S="2")
    assert r.code != 0
    assert "did not report 'Connected as'" in r.err


@pytest.mark.parametrize(
    "args",
    [
        (),  # first run needs a guild id
        ("--guild-id", "my-server"),
        ("--guild-id",),
        ("--bucket", "rrc-backups"),
        ("--unknown",),
    ],
)
def test_rejects_bad_arguments(deploy, args: tuple[str, ...]) -> None:  # type: ignore[no-untyped-def]
    _, run = deploy
    r = run(*args, STUB_TOKEN=fake_discord_token(), STUB_BOT_LOG=CONNECTED)
    assert r.code != 0
    assert not [c for c in r.calls if " up " in c]


def test_blank_secret_is_treated_as_missing(deploy) -> None:  # type: ignore[no-untyped-def]
    _, run = deploy
    r = run("--guild-id", GUILD, STUB_TOKEN="   ")
    assert r.code != 0
    assert "Secret Manager" in r.err
