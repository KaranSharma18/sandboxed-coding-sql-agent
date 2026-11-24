"""Exact-match caches (design section 13, ADR-18).

Rule zero: a cache may save work, never change an answer. Every key is a content hash of every
input that can affect the output, scoped to one tenant and versioned (`v1`). Values are JSON,
zstd-compressed above 4 KB; values over 1 MB are simply not cached.

Layers:
  L1  model response      (prompt + model + versions)                   7 days
  L2  verified fix        (snapshot + oracle + environment + kind)      30 days, always re-verified
  L3  execution result    (image + env + command + workspace + oracle)  24 hours, never for final checks
  L4  failure memo        (signature + oracle + env -> fixes with AST preconditions)  30 days
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any

import orjson
import zstandard
from redis.asyncio import Redis

from fixloop_core.cachekeys import (  # noqa: F401
    _ZSTD_PREFIX,
    CACHE_REQUESTS,
    CACHE_VERSION,
    CALLS_AVOIDED,
    MAX_VALUE_BYTES,
    TTL,
    cache_key,
)


def _encode(value: Any) -> bytes | None:
    raw = orjson.dumps(value)
    if len(raw) > 4096:
        raw = _ZSTD_PREFIX + zstandard.ZstdCompressor(level=6).compress(raw)
    return raw if len(raw) <= MAX_VALUE_BYTES else None


def _decode(raw: bytes) -> Any:
    if raw.startswith(_ZSTD_PREFIX):
        raw = zstandard.ZstdDecompressor().decompress(raw[len(_ZSTD_PREFIX) :])
    return orjson.loads(raw)


class Cache:
    """Redis cache on the `redis-cache` instance; single-flight locks live on `redis-queue`."""

    def __init__(self, cache: Redis, locks: Redis) -> None:
        self.cache = cache
        self.locks = locks

    async def get(self, layer: str, key: str) -> Any | None:
        try:
            raw = await self.cache.get(key)
        except Exception:
            CACHE_REQUESTS.labels(layer, "error").inc()
            return None
        if raw is None:
            CACHE_REQUESTS.labels(layer, "miss").inc()
            return None
        try:
            value = _decode(raw if isinstance(raw, bytes) else str(raw).encode())
        except Exception:
            await self.delete(key)
            CACHE_REQUESTS.labels(layer, "corrupt").inc()
            return None
        CACHE_REQUESTS.labels(layer, "hit").inc()
        return value

    async def put(self, layer: str, key: str, value: Any) -> None:
        encoded = _encode(value)
        if encoded is None:
            return
        with contextlib.suppress(Exception):
            await self.cache.set(key, encoded, ex=TTL.get(layer, 86400))

    async def delete(self, key: str) -> None:
        with contextlib.suppress(Exception):
            await self.cache.delete(key)

    @contextlib.asynccontextmanager
    async def single_flight(
        self, key: str, ttl_ms: int = 120_000, wait_s: float = 120.0
    ) -> AsyncIterator[bool]:
        """Yield True if this caller computes; False after waiting for another caller to finish."""
        lock = f"fixloop:lock:{key.rsplit(':', 1)[-1]}"
        token = secrets.token_hex(8)
        acquired = bool(await self.locks.set(lock, token, nx=True, px=ttl_ms))
        if acquired:
            try:
                yield True
            finally:
                with contextlib.suppress(Exception):
                    if (await self.locks.get(lock)) in (token, token.encode()):
                        await self.locks.delete(lock)
            return
        deadline = time.monotonic() + wait_s
        delay = 0.2
        while time.monotonic() < deadline and await self.locks.exists(lock):
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)
        yield False


class MemoryCache:
    """In-process cache with the same interface (tests and single-process local mode)."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[float, bytes]] = {}

    async def get(self, layer: str, key: str) -> Any | None:
        item = self.data.get(key)
        if item is None or item[0] < time.time():
            CACHE_REQUESTS.labels(layer, "miss").inc()
            return None
        CACHE_REQUESTS.labels(layer, "hit").inc()
        return _decode(item[1])

    async def put(self, layer: str, key: str, value: Any) -> None:
        encoded = _encode(value)
        if encoded is not None:
            self.data[key] = (time.time() + TTL.get(layer, 86400), encoded)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    @contextlib.asynccontextmanager
    async def single_flight(
        self, key: str, ttl_ms: int = 120_000, wait_s: float = 120.0
    ) -> AsyncIterator[bool]:
        yield True
