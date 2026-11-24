"""API keys, scopes and per-tenant rate limits.

Keys look like `fl_live_<key id>_<secret>`. Only a SHA-256 of the secret is stored; verified
keys are cached in memory for `key_cache_ttl_s`, so a revoked key stops working within that
time on every API replica. Rate limits are a fixed one-minute window per tenant in Redis and
are reported with the IETF `RateLimit-*` headers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import Depends, Request, Response

from fixloop_api.state import ApiState
from fixloop_core.errors import Forbidden, QuotaExceeded, Unauthorized
from fixloop_core.storage import repo
from fixloop_core.storage import tables as t

SCOPES = {"tasks:read", "tasks:write", "uploads:write", "usage:read", "admin"}


@dataclass(frozen=True)
class Principal:
    tenant: t.Tenant
    key_id: str
    scopes: frozenset[str]


def get_state(request: Request) -> ApiState:
    return request.app.state.fixloop


async def authenticate(
    request: Request, response: Response, state: ApiState = Depends(get_state)
) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise Unauthorized("send an API key as `Authorization: Bearer fl_live_...`")
    token = header[7:].strip()
    now = time.monotonic()
    cached = state.key_cache.get(token)
    principal: Principal
    if cached and now - cached[0] < state.settings.api.key_cache_ttl_s:
        principal = cached[1]  # type: ignore[assignment]
    else:
        async with state.db.transaction() as s:
            key = await repo.verify_api_key(s, token)
            tenant = await repo.get_tenant(s, key.tenant_id) if key else None
            if key is not None:
                await repo.touch_api_key(s, key.id)
        if key is None or tenant is None or tenant.status != "active":
            state.key_cache.pop(token, None)
            raise Unauthorized("the API key is not valid")
        principal = Principal(tenant=tenant, key_id=key.id, scopes=frozenset(key.scopes))
        if len(state.key_cache) > 10_000:
            state.key_cache.clear()
        state.key_cache[token] = (now, principal)
    await _rate_limit(state, principal, response)
    return principal


async def _rate_limit(state: ApiState, principal: Principal, response: Response) -> None:
    plans = state.config.limits.plans
    limit = (plans.get(principal.tenant.plan) or plans["standard"]).tenant.api_requests_per_minute
    window = int(time.time() // 60)
    key = f"fixloop:rl:api:{principal.tenant.id}:{window}"
    count = int(await state.redis.incr(key))
    if count == 1:
        await state.redis.expire(key, 120)
    reset = 60 - int(time.time() % 60)
    response.headers["RateLimit-Limit"] = str(limit)
    response.headers["RateLimit-Remaining"] = str(max(0, limit - count))
    response.headers["RateLimit-Reset"] = str(reset)
    if count > limit:
        raise QuotaExceeded(f"rate limit of {limit} requests per minute exceeded", retry_after=reset)


def require(scope: str):
    async def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        if scope not in principal.scopes and "admin" not in principal.scopes:
            raise Forbidden(f"this key lacks the `{scope}` scope")
        return principal

    return dependency
