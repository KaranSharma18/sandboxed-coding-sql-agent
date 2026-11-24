"""Budgets as atomic reservations (design section 14, ADR-21).

Before every model call or execution the worker *reserves* the worst case. A Lua script checks
every dimension for the task, the tenant's day and the platform's day, and makes the reservation
atomically, so two concurrent steps can never both slip under a cap. After the step, real usage
replaces the reservation. Output is capped by `max_output_tokens`, so the reservation bounds the
real cost: the cap is a guarantee, not an estimate.

Units are integers inside Redis: cost in micro-dollars, sandbox time in milliseconds.
Postgres keeps the durable totals; a resumed task re-seeds Redis from them.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

import orjson
from redis.asyncio import Redis

from fixloop_core.config import TenantLimits
from fixloop_core.errors import BudgetExhausted
from fixloop_core.models import BudgetLimits, Usage
from fixloop_core.ports import Need

DIMENSION_NAMES = {
    "task:calls": "model_calls",
    "task:in_tokens": "input_tokens",
    "task:out_tokens": "output_tokens",
    "task:cost": "cost_usd",
    "task:sandbox_ms": "sandbox_seconds",
    "task:executions": "executions",
    "tenant:cost": "tenant_daily_cost",
    "tenant:sandbox_ms": "tenant_daily_sandbox_seconds",
    "global:cost": "global_daily_cost",
}
STALE_RESERVATION_S = 900
DAY_TTL_S = 2 * 86400


def task_limit_dims(limits: BudgetLimits) -> dict[str, int]:
    return {
        "calls": limits.max_model_calls,
        "in_tokens": limits.max_input_tokens,
        "out_tokens": limits.max_output_tokens,
        "cost": round(limits.max_cost_usd * 1_000_000),
        "sandbox_ms": limits.max_sandbox_seconds * 1000,
        "executions": limits.max_executions,
    }


def tenant_limit_dims(limits: TenantLimits) -> dict[str, int]:
    return {
        "cost": round(limits.daily_cost_usd * 1_000_000),
        "sandbox_ms": limits.sandbox_seconds_per_day * 1000,
    }


def _day() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


_RESERVE = """
local now = tonumber(ARGV[2])
local stale = tonumber(ARGV[3])
local need = cjson.decode(ARGV[4])
local ttl = tonumber(ARGV[8])
local scopes = {
  {KEYS[1], cjson.decode(ARGV[5]), 'task'},
  {KEYS[2], cjson.decode(ARGV[6]), 'tenant'},
  {KEYS[3], cjson.decode(ARGV[7]), 'global'},
}
for _, scope in ipairs(scopes) do
  local key = scope[1]
  local all = redis.call('HGETALL', key)
  for i = 1, #all, 2 do
    local field = all[i]
    if string.sub(field, 1, 2) == 'r:' then
      local rec = cjson.decode(all[i + 1])
      if now - rec.ts > stale then
        for dim, amount in pairs(rec.need) do
          redis.call('HINCRBY', key, 'reserved:' .. dim, -amount)
        end
        redis.call('HDEL', key, field)
      end
    end
  end
end
for _, scope in ipairs(scopes) do
  local key, limits = scope[1], scope[2]
  for dim, limit in pairs(limits) do
    local amount = tonumber(need[dim] or 0)
    if amount > 0 then
      local used = tonumber(redis.call('HGET', key, 'used:' .. dim) or '0')
      local reserved = tonumber(redis.call('HGET', key, 'reserved:' .. dim) or '0')
      if used + reserved + amount > tonumber(limit) then
        return scope[3] .. ':' .. dim
      end
    end
  end
end
for _, scope in ipairs(scopes) do
  local key, limits = scope[1], scope[2]
  local held = {}
  local any = false
  for dim, _ in pairs(limits) do
    local amount = tonumber(need[dim] or 0)
    if amount > 0 then
      redis.call('HINCRBY', key, 'reserved:' .. dim, amount)
      held[dim] = amount
      any = true
    end
  end
  if any then
    redis.call('HSET', key, 'r:' .. ARGV[1], cjson.encode({ts = now, need = held}))
  end
  redis.call('EXPIRE', key, ttl)
end
return ''
"""

_SETTLE = """
local actual = cjson.decode(ARGV[2])
local ttl = tonumber(ARGV[3])
local dims = {cjson.decode(ARGV[4]), cjson.decode(ARGV[5]), cjson.decode(ARGV[6])}
for index, key in ipairs(KEYS) do
  local rec = redis.call('HGET', key, 'r:' .. ARGV[1])
  if rec then
    local parsed = cjson.decode(rec)
    for dim, amount in pairs(parsed.need) do
      redis.call('HINCRBY', key, 'reserved:' .. dim, -amount)
    end
    redis.call('HDEL', key, 'r:' .. ARGV[1])
  end
  for _, dim in ipairs(dims[index]) do
    local amount = tonumber(actual[dim] or 0)
    if amount ~= 0 then
      redis.call('HINCRBY', key, 'used:' .. dim, amount)
    end
  end
  redis.call('EXPIRE', key, ttl)
end
return 1
"""

TASK_DIMS = ["calls", "in_tokens", "out_tokens", "cost", "sandbox_ms", "executions"]
TENANT_DIMS = ["cost", "sandbox_ms"]
GLOBAL_DIMS = ["cost"]


class BudgetLedger:
    """Redis-backed ledger (production)."""

    def __init__(self, redis: Redis, global_daily_cost_usd: float) -> None:
        self.redis = redis
        self.global_limit = {"cost": round(global_daily_cost_usd * 1_000_000)}
        self._reserve = redis.register_script(_RESERVE)
        self._settle = redis.register_script(_SETTLE)

    @staticmethod
    def task_key(task_id: str) -> str:
        return f"fixloop:budget:task:{task_id}"

    @staticmethod
    def tenant_key(tenant_id: str) -> str:
        return f"fixloop:budget:tenant:{tenant_id}:{_day()}"

    @staticmethod
    def global_key() -> str:
        return f"fixloop:budget:global:{_day()}"

    async def start_task(self, task_id: str, usage: Usage) -> None:
        """(Re)seed the task's counters from durable usage; drop reservations of a dead worker."""
        key = self.task_key(task_id)
        dims = Need(
            calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens + usage.thinking_tokens,
            cost_usd=usage.cost_usd,
            sandbox_seconds=usage.sandbox_seconds,
            executions=usage.executions,
        ).dims()
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping={f"used:{k}": v for k, v in dims.items()})
            pipe.expire(key, DAY_TTL_S)
            await pipe.execute()

    async def reserve(
        self, task_id: str, tenant_id: str, need: Need, task_limits: BudgetLimits, tenant_limits: TenantLimits
    ) -> str:
        rid = secrets.token_hex(8)
        failed = await self._reserve(
            keys=[self.task_key(task_id), self.tenant_key(tenant_id), self.global_key()],
            args=[
                rid,
                int(time.time()),
                STALE_RESERVATION_S,
                orjson.dumps(need.dims()),
                orjson.dumps(task_limit_dims(task_limits)),
                orjson.dumps(tenant_limit_dims(tenant_limits)),
                orjson.dumps(self.global_limit),
                DAY_TTL_S,
            ],
        )
        name = failed.decode() if isinstance(failed, bytes) else str(failed or "")
        if name:
            raise BudgetExhausted(DIMENSION_NAMES.get(name, name))
        return rid

    async def settle(self, task_id: str, tenant_id: str, rid: str, actual: Need) -> None:
        await self._settle(
            keys=[self.task_key(task_id), self.tenant_key(tenant_id), self.global_key()],
            args=[
                rid,
                orjson.dumps(actual.dims()),
                DAY_TTL_S,
                orjson.dumps(TASK_DIMS),
                orjson.dumps(TENANT_DIMS),
                orjson.dumps(GLOBAL_DIMS),
            ],
        )

    async def release(self, task_id: str, tenant_id: str, rid: str) -> None:
        await self.settle(task_id, tenant_id, rid, Need())

    async def tenant_remaining_cost(self, tenant_id: str, limits: TenantLimits) -> float:
        values = await self.redis.hmget(self.tenant_key(tenant_id), "used:cost", "reserved:cost")
        used = sum(int(v or 0) for v in values)
        return max(0.0, limits.daily_cost_usd - used / 1_000_000)

    async def global_spend(self) -> float:
        value = await self.redis.hget(self.global_key(), "used:cost")
        return int(value or 0) / 1_000_000


class MemoryBudgetLedger:
    """Same semantics in plain Python, for unit tests and the scripted local mode."""

    def __init__(self, global_daily_cost_usd: float = 1e9) -> None:
        self.global_limit = {"cost": round(global_daily_cost_usd * 1_000_000)}
        self.keys: dict[str, dict[str, int]] = {}
        self.reservations: dict[str, dict[str, dict[str, int]]] = {}

    def _scope(self, key: str) -> dict[str, int]:
        return self.keys.setdefault(key, {})

    async def start_task(self, task_id: str, usage: Usage) -> None:
        dims = Need(
            calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens + usage.thinking_tokens,
            cost_usd=usage.cost_usd,
            sandbox_seconds=usage.sandbox_seconds,
            executions=usage.executions,
        ).dims()
        self.keys[f"task:{task_id}"] = {f"used:{k}": v for k, v in dims.items()}

    async def reserve(
        self, task_id: str, tenant_id: str, need: Need, task_limits: BudgetLimits, tenant_limits: TenantLimits
    ) -> str:
        scopes = [
            (f"task:{task_id}", task_limit_dims(task_limits), "task"),
            (f"tenant:{tenant_id}:{_day()}", tenant_limit_dims(tenant_limits), "tenant"),
            (f"global:{_day()}", self.global_limit, "global"),
        ]
        amounts = need.dims()
        for key, limits, label in scopes:
            scope = self._scope(key)
            for dim, limit in limits.items():
                amount = amounts.get(dim, 0)
                if (
                    amount > 0
                    and scope.get(f"used:{dim}", 0) + scope.get(f"reserved:{dim}", 0) + amount > limit
                ):
                    raise BudgetExhausted(DIMENSION_NAMES.get(f"{label}:{dim}", dim))
        rid = secrets.token_hex(8)
        self.reservations[rid] = {}
        for key, limits, _ in scopes:
            scope = self._scope(key)
            held = {dim: amounts[dim] for dim in limits if amounts.get(dim, 0) > 0}
            for dim, amount in held.items():
                scope[f"reserved:{dim}"] = scope.get(f"reserved:{dim}", 0) + amount
            self.reservations[rid][key] = held
        return rid

    async def settle(self, task_id: str, tenant_id: str, rid: str, actual: Need) -> None:
        for key, held in self.reservations.pop(rid, {}).items():
            scope = self._scope(key)
            for dim, amount in held.items():
                scope[f"reserved:{dim}"] -= amount
        dims = actual.dims()
        for key, tracked in (
            (f"task:{task_id}", TASK_DIMS),
            (f"tenant:{tenant_id}:{_day()}", TENANT_DIMS),
            (f"global:{_day()}", GLOBAL_DIMS),
        ):
            scope = self._scope(key)
            for dim in tracked:
                scope[f"used:{dim}"] = scope.get(f"used:{dim}", 0) + dims.get(dim, 0)

    async def release(self, task_id: str, tenant_id: str, rid: str) -> None:
        await self.settle(task_id, tenant_id, rid, Need())

    async def tenant_remaining_cost(self, tenant_id: str, limits: TenantLimits) -> float:
        scope = self._scope(f"tenant:{tenant_id}:{_day()}")
        return max(
            0.0, limits.daily_cost_usd - (scope.get("used:cost", 0) + scope.get("reserved:cost", 0)) / 1e6
        )

    async def global_spend(self) -> float:
        return self._scope(f"global:{_day()}").get("used:cost", 0) / 1e6
