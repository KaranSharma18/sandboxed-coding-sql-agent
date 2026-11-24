"""Health, readiness, metrics and usage."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from fixloop_api.auth import Principal, get_state, require
from fixloop_api.schemas import UsageOut
from fixloop_api.state import ApiState
from fixloop_core.storage import repo

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="Liveness: the process is up")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: Postgres and Redis reachable, a runner is alive")
async def readyz(response: Response, state: ApiState = Depends(get_state)) -> dict[str, object]:
    checks: dict[str, object] = {}
    try:
        checks["postgres"] = await state.db.ping()
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        checks["redis"] = bool(await state.redis.ping())
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    try:
        runners = [key async for key in state.redis.scan_iter(match="fixloop:runner:*", count=100)]
        checks["runners"] = len(runners)
    except Exception:
        checks["runners"] = 0
    ready = checks["postgres"] is True and checks["redis"] is True
    response.status_code = 200 if ready else 503
    return {"ready": ready, "checks": checks}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/usage", response_model=UsageOut, tags=["usage"], summary="Spend and tasks per day")
async def usage(
    days: int = Query(default=30, ge=1, le=365),
    principal: Principal = Depends(require("usage:read")),
    state: ApiState = Depends(get_state),
) -> UsageOut:
    async with state.db.session() as s:
        rows = await repo.usage_by_day(s, principal.tenant.id, days)
    plans = state.config.limits.plans
    tenant_limits = (plans.get(principal.tenant.plan) or plans["standard"]).tenant
    remaining = await state.ledger.tenant_remaining_cost(str(principal.tenant.id), tenant_limits)
    return UsageOut(days=rows, remaining_today_usd=round(remaining, 4))
