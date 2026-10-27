"""Fake `gcloud`, `docker` and `systemctl` executables for exercising deploy scripts.

Each stub appends its argv to $STUB_LOG and answers just enough of the CLI for
the scripts under test. Behaviour is steered with STUB_* environment variables.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

GCLOUD = r"""#!/usr/bin/env bash
echo "gcloud $*" >> "$STUB_LOG"
case "$*" in
  *"secrets versions access"*)
    if [ -n "${STUB_TOKEN:-}" ]; then printf '%s\n' "$STUB_TOKEN"; exit 0; fi
    echo "NOT_FOUND: no versions" >&2; exit 1 ;;
  *"billing projects describe"*) echo "${STUB_BILLING:-True}"; exit 0 ;;
  *"secrets versions list"*)
    [ -n "${STUB_HAS_TOKEN:-}" ] && echo "projects/p/secrets/discord-token/versions/1"; exit 0 ;;
esac
key=""
case "$*" in
  *"iam service-accounts describe"*) key=sa ;;
  *"storage buckets describe"*) key=bucket ;;
  *"secrets describe"*) key=secret ;;
  *"compute instances describe"*) key=vm ;;
esac
if [ -n "$key" ]; then
  case ",${STUB_EXISTING:-}," in *",$key,"*) exit 0 ;; *) exit 1 ;; esac
fi
exit 0
"""

DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  *"ps --status running --services"*) for s in ${STUB_RUNNING:-}; do echo "$s"; done ;;
  *"ps -q bot"*) [ -f "$STUB_STATE/bot_id" ] && cat "$STUB_STATE/bot_id" ;;
  *"up -d --force-recreate bot"*) echo "bot-$RANDOM$RANDOM" > "$STUB_STATE/bot_id" ;;
  *"up -d --build"*) [ -f "$STUB_STATE/bot_id" ] || echo "bot-first" > "$STUB_STATE/bot_id" ;;
  *" logs "*) printf '%s\n' "${STUB_BOT_LOG:-}" ;;
  *"exec -T db sh -c"*) printf 'PGDMP-fake-archive' ;;
  *"exec -T db pg_restore --list"*) cat > /dev/null ;;
esac
exit 0
"""

SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
exit 0
"""


def install_stubs(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("gcloud", GCLOUD), ("docker", DOCKER), ("systemctl", SYSTEMCTL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)


def copy_repo_subset(dest: Path) -> Path:
    """Copy just what the deploy scripts touch, so tests never write into the real checkout."""
    for rel in ("scripts", "deploy"):
        shutil.copytree(ROOT / rel, dest / rel)
    for rel in (".env.example", "docker-compose.yml"):
        shutil.copy2(ROOT / rel, dest / rel)
    return dest


def stub_env(bin_dir: Path, log: Path, state: Path, **extra: str) -> dict[str, str]:
    state.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_STATE": str(state),
        **extra,
    }
