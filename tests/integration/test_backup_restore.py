"""End-to-end: scripts/backup.sh then scripts/restore.sh against real PostgreSQL.

Runs the scripts in BACKUP_SOURCE=direct mode (host pg_dump/pg_restore via libpq
env vars). Compose mode differs only in wrapping the same commands in
`docker compose exec`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"


def _libpq_env(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    return {
        "PGHOST": parts.hostname or "localhost",
        "PGPORT": str(parts.port or 5432),
        "PGUSER": unquote(parts.username or ""),
        "PGPASSWORD": unquote(parts.password or ""),
        "PGDATABASE": parts.path.lstrip("/"),
    }


def _sql(url: str, query: str) -> list[Any]:
    async def go() -> list[Any]:
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            return list(await conn.fetch(query))
        finally:
            await conn.close()

    return asyncio.run(go())


def _run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(script), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def db_env(test_database_url: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    if not (shutil.which("pg_dump") and shutil.which("pg_restore")):
        pytest.skip("pg_dump/pg_restore not installed on this host")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = test_database_url
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    _sql(test_database_url, "DROP TABLE IF EXISTS backup_probe")
    _sql(test_database_url, "CREATE TABLE backup_probe (id int PRIMARY KEY, note text)")
    _sql(test_database_url, "INSERT INTO backup_probe VALUES (1, 'first flagship run')")
    yield {
        **_libpq_env(test_database_url),
        "BACKUP_SOURCE": "direct",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "GCS_BUCKET": "",
    }
    _sql(test_database_url, "DROP TABLE IF EXISTS backup_probe")
    command.downgrade(cfg, "base")


def test_backup_then_restore_round_trip(db_env: dict[str, str], test_database_url: str) -> None:
    backup_dir = Path(db_env["BACKUP_DIR"])
    backup_dir.mkdir(parents=True)
    stale = backup_dir / "rrc-20000101T000000Z.dump"
    stale.write_bytes(b"old")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    result = _run(BACKUP, env={**db_env, "RETENTION_DAYS": "14"})
    assert result.returncode == 0, result.stderr

    archives = sorted(backup_dir.glob("rrc-*.dump"))
    assert len(archives) == 1, "stale archive should be pruned, new one kept"
    archive = archives[0]
    assert archive != stale
    assert archive.stat().st_mode & 0o077 == 0, "archive must be owner-only"
    assert not list(backup_dir.glob("*.partial"))

    # Simulate disaster, then restore.
    _sql(test_database_url, "DROP TABLE backup_probe")
    _sql(test_database_url, "DELETE FROM alembic_version")

    restored = _run(RESTORE, str(archive), "--yes", env=db_env)
    assert restored.returncode == 0, restored.stderr

    rows = _sql(test_database_url, "SELECT id, note FROM backup_probe")
    assert [(r["id"], r["note"]) for r in rows] == [(1, "first flagship run")]
    version = _sql(test_database_url, "SELECT version_num FROM alembic_version")
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert [r["version_num"] for r in version] == [head]


def test_restore_refuses_without_confirmation(
    db_env: dict[str, str], test_database_url: str
) -> None:
    result = _run(BACKUP, env=db_env)
    assert result.returncode == 0, result.stderr
    archive = next(Path(db_env["BACKUP_DIR"]).glob("rrc-*.dump"))

    _sql(test_database_url, "INSERT INTO backup_probe VALUES (2, 'added after backup')")
    refused = _run(RESTORE, str(archive), env=db_env)
    assert refused.returncode != 0
    wrong_flag = _run(RESTORE, str(archive), "--force", env=db_env)
    assert wrong_flag.returncode != 0
    assert "--yes" in wrong_flag.stderr

    # Nothing was touched.
    assert len(_sql(test_database_url, "SELECT 1 FROM backup_probe")) == 2


def test_failed_restore_leaves_database_unchanged(
    db_env: dict[str, str], test_database_url: str, tmp_path: Path
) -> None:
    corrupt = tmp_path / "corrupt.dump"
    corrupt.write_bytes(b"PGDMP not really an archive")
    result = _run(RESTORE, str(corrupt), "--yes", env=db_env)
    assert result.returncode != 0
    assert len(_sql(test_database_url, "SELECT 1 FROM backup_probe")) == 1


def test_backup_fails_loudly_when_database_unreachable(db_env: dict[str, str]) -> None:
    result = _run(BACKUP, env={**db_env, "PGPORT": "1"})
    assert result.returncode != 0
    assert "ERROR" in result.stderr
    assert not list(Path(db_env["BACKUP_DIR"]).glob("rrc-*"))


def test_backup_rejects_bad_retention(db_env: dict[str, str]) -> None:
    result = _run(BACKUP, env={**db_env, "RETENTION_DAYS": "two weeks"})
    assert result.returncode != 0
    assert "RETENTION_DAYS" in result.stderr
