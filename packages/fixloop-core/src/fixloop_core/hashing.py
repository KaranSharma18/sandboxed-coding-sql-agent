"""Content hashing helpers.

Cache keys, snapshot ids, fingerprints and spec hashes are all sha256 over canonical bytes.
Canonical JSON (sorted keys, no whitespace) makes the hash independent of dict ordering.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson
from pydantic import BaseModel


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Serialize to canonical JSON bytes: sorted keys, UTF-8, pydantic models dumped in JSON mode."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)


def hash_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def stable_hash(*parts: Any) -> str:
    """Hash a sequence of parts; each part is canonical-JSON encoded and separated by a unit separator."""
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            digest.update(part)
        elif isinstance(part, str):
            digest.update(part.encode("utf-8"))
        else:
            digest.update(canonical_json(part))
        digest.update(b"\x1f")
    return digest.hexdigest()
