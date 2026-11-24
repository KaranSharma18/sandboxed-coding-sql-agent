"""Retries with full jitter, per-model circuit breakers and shared token buckets."""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field

from redis.asyncio import Redis

from fixloop_core.config import BreakerConfig, RateLimit


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None = None) -> float:
    """Full jitter (sleep a random time up to the exponential bound), honoring Retry-After."""
    if retry_after is not None:
        return min(max(retry_after, 0.0), cap)
    return random.uniform(0, min(cap, base * (2**attempt)))


@dataclass
class CircuitBreaker:
    """Opens after N consecutive failures or a failure rate over a sliding window."""

    config: BreakerConfig
    consecutive: int = 0
    window: deque[bool] = field(default_factory=deque)
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        # Half-open after the cool-down: let a probe through.
        return time.monotonic() - self.opened_at >= self.config.open_seconds

    def record(self, success: bool) -> None:
        self.window.append(success)
        while len(self.window) > self.config.window:
            self.window.popleft()
        if success:
            self.consecutive = 0
            self.opened_at = None
            return
        self.consecutive += 1
        failures = self.window.count(False)
        rate_tripped = (
            len(self.window) >= self.config.window and failures / len(self.window) >= self.config.failure_rate
        )
        if self.consecutive >= self.config.consecutive_failures or rate_tripped:
            self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.allow() else "open"


class Breakers:
    def __init__(self, config: BreakerConfig) -> None:
        self.config = config
        self._by_model: dict[str, CircuitBreaker] = {}

    def for_model(self, model: str) -> CircuitBreaker:
        return self._by_model.setdefault(model, CircuitBreaker(self.config))


_BUCKET = """
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local need = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1] or capacity)
local ts = tonumber(data[2] or now)
tokens = math.min(capacity, tokens + (now - ts) * rate)
if tokens >= need then
  tokens = tokens - need
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], 120)
  return 0
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 120)
return math.ceil((need - tokens) / rate * 1000)
"""


class RateLimiter:
    """Shared request and token buckets per model, so many workers never hit 429 in lockstep."""

    def __init__(self, redis: Redis | None, limits: dict[str, RateLimit]) -> None:
        self.redis = redis
        self.limits = limits
        self._script = redis.register_script(_BUCKET) if redis is not None else None

    async def acquire(self, model: str, tokens: int, max_wait_s: float = 60.0) -> None:
        limit = self.limits.get(model)
        if limit is None or self._script is None:
            return
        deadline = time.monotonic() + max_wait_s
        for key, rate_per_min, need in (
            (f"fixloop:rl:model:{model}:rpm", limit.rpm, 1),
            (f"fixloop:rl:model:{model}:tpm", limit.tpm, max(1, tokens)),
        ):
            while True:
                wait_ms = int(
                    await self._script(  # type: ignore[arg-type]
                        keys=[key],
                        args=[time.time(), rate_per_min / 60.0, rate_per_min, min(need, rate_per_min)],
                    )
                )
                if wait_ms <= 0 or time.monotonic() > deadline:
                    break
                await asyncio.sleep(min(wait_ms / 1000, 5.0))
