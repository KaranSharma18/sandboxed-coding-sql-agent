"""Identifiers.

- Rows use UUIDv7: the first 48 bits are a millisecond timestamp, so ids sort by creation time
  and B-tree indexes stay compact (random UUIDv4 keys scatter inserts across the index).
- Execution ids are *deterministic*: the same task, attempt, purpose and candidate always map to
  the same id. That is what makes a retried or resumed execution request idempotent: the runner
  recognizes the id and returns the stored result instead of running the code again.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Return a version 7 UUID (RFC 9562): unix_ts_ms(48) | ver(4) | rand_a(12) | var(2) | rand_b(62)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68 & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def short_id(value: uuid.UUID | str, length: int = 10) -> str:
    """A short, display-friendly id taken from the random tail of a UUID (the head is a timestamp)."""
    hex_value = value.hex if isinstance(value, uuid.UUID) else uuid.UUID(str(value)).hex
    return hex_value[-length:]


def exec_id(
    task_id: str, attempt_no: int | None, purpose: str, fingerprint: str = "", rerun_no: int = 0
) -> str:
    """Deterministic execution id: identical inputs always produce the identical id."""
    raw = "\x1f".join(
        [
            str(task_id),
            str(attempt_no if attempt_no is not None else "-"),
            purpose,
            fingerprint,
            str(rerun_no),
        ]
    )
    return "ex_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def new_request_id() -> str:
    return "rq_" + uuid.uuid4().hex
