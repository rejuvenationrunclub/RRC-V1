"""Guards against committing secrets. Complements the gitleaks job in CI."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN_RE = re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}")


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.splitlines() if (ROOT / p).is_file()]


def test_no_discord_tokens_in_repository() -> None:
    offenders = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN_RE.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"Discord-token-shaped strings found in: {offenders}"


def test_dotenv_is_ignored_and_example_has_no_values() -> None:
    gitignore = (ROOT / ".gitignore").read_text()
    assert re.search(r"^\.env$", gitignore, re.M)
    example = (ROOT / ".env.example").read_text()
    token_line = re.search(r"^DISCORD_TOKEN=(.*)$", example, re.M)
    assert token_line is not None and token_line.group(1).strip() == ""
