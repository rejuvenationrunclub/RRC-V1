"""Rotating check-in codes (plan §3.6).

A code is an HMAC of the event's server-side secret and the current time step, so it
can't be guessed from earlier codes and nothing needs storing per step. The previous
step is accepted too, so a code read out just before it changes still works.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from rrc_bot.domain.timeutil import require_aware

# No 0/O or 1/I/L: easy to read aloud and type on a phone.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def time_step(now: datetime, period_s: int) -> int:
    return int(require_aware(now).timestamp()) // period_s


def seconds_left(now: datetime, period_s: int) -> int:
    return period_s - int(require_aware(now).timestamp()) % period_s


def code_for_step(secret: bytes, step: int) -> str:
    digest = hmac.new(secret, step.to_bytes(8, "big", signed=True), hashlib.sha256).digest()
    value = int.from_bytes(digest, "big")
    chars = []
    for _ in range(CODE_LENGTH):
        value, index = divmod(value, len(ALPHABET))
        chars.append(ALPHABET[index])
    return "".join(chars)


def current_code(secret: bytes, now: datetime, period_s: int) -> str:
    return code_for_step(secret, time_step(now, period_s))


def normalize(code: str) -> str:
    """Upper-case and drop spaces/dashes; members type codes on phones."""
    return "".join(ch for ch in code.upper() if ch not in " -_")


def matches(secret: bytes, code: str, now: datetime, period_s: int) -> bool:
    candidate = normalize(code)
    if len(candidate) != CODE_LENGTH:
        return False
    step = time_step(now, period_s)
    # Compare against both steps every time (no early exit) to keep timing uniform.
    ok = False
    for s in (step, step - 1):
        ok |= hmac.compare_digest(candidate, code_for_step(secret, s))
    return ok
