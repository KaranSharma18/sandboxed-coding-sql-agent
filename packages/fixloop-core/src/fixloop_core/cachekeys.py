"""Cache key builders and TTLs (pure: no Redis), shared by the cache adapter and the loop.

Rule zero: a cache may save work, never change an answer. Every key is a content hash of every
input that can affect the output, scoped to one tenant and versioned.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter

from fixloop_core.hashing import stable_hash

CACHE_VERSION = "v1"
TTL = {"llm": 7 * 86400, "fix": 30 * 86400, "exec": 86400, "memo": 30 * 86400, "ctx": 7 * 86400}
MAX_VALUE_BYTES = 1_048_576
_ZSTD_PREFIX = b"Z1"

CACHE_REQUESTS = Counter("fixloop_cache_requests_total", "Cache lookups", ["layer", "result"])
CALLS_AVOIDED = Counter("fixloop_llm_calls_avoided_total", "Model calls avoided by caching", ["layer"])


def cache_key(layer: str, tenant_id: str, *parts: Any) -> str:
    return f"fixloop:{CACHE_VERSION}:{tenant_id}:{layer}:{stable_hash(layer, *parts)}"
