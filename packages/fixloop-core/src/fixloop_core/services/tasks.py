"""Task intake: the one place a task is created (API requests, CLI calls and GitHub commands).

Order of checks, cheapest first: size limits (413), tenant admission (429), then the insert.
The insert is idempotent on (tenant, idempotency key): the same key with the same spec returns
the existing task, the same key with a different spec is a 409 (ADR-19). Only a newly created
task is enqueued, after its row is committed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis

from fixloop_core.config import AppConfig, TaskPlanLimits
from fixloop_core.errors import QuotaExceeded, TooLarge
from fixloop_core.hashing import hash_json
from fixloop_core.models import BudgetLimits
from fixloop_core.queue import enqueue_task
from fixloop_core.spec import InlineSource, TaskSpec
from fixloop_core.storage import repo
from fixloop_core.storage import tables as t
from fixloop_core.storage.db import Database


def effective_budget(plan: TaskPlanLimits, spec: TaskSpec) -> BudgetLimits:
    """The plan's per-task limits, lowered (never raised) by the task's own overrides."""
    budget = plan.budget_for(spec.is_sql)
    updates: dict[str, Any] = {"max_repairs": min(spec.options.max_repairs, budget.max_repairs)}
    override = spec.budget
    if override is not None:
        pairs = {
            "max_model_calls": override.max_model_calls,
            "max_input_tokens": override.max_input_tokens,
            "max_output_tokens": override.max_output_tokens,
            "max_cost_usd": override.max_cost_usd,
            "max_sandbox_seconds": override.max_sandbox_seconds,
            "wall_clock_seconds": override.wall_clock_seconds,
        }
        for name, value in pairs.items():
            if value is not None:
                updates[name] = min(value, getattr(budget, name))
    return budget.model_copy(update=updates)


def check_size_limits(spec: TaskSpec, config: AppConfig) -> None:
    intake = config.limits.intake
    if len(spec.description) > intake.description_max_chars:
        raise TooLarge(f"the description is longer than {intake.description_max_chars} characters")
    if isinstance(spec.source, InlineSource):
        if len(spec.source.files) > intake.inline_max_files:
            raise TooLarge(
                f"inline sources are limited to {intake.inline_max_files} files; upload an archive"
            )
        size = sum(len(path) + len(text.encode("utf-8")) for path, text in spec.source.files.items())
        if size > intake.inline_max_bytes:
            raise TooLarge(
                f"inline sources are limited to {intake.inline_max_bytes // 1024} KB; upload an archive"
            )


def spec_hash(spec: TaskSpec) -> str:
    return hash_json(spec.model_dump(mode="json"))


async def create_task(
    db: Database,
    redis: Redis,
    config: AppConfig,
    *,
    tenant: t.Tenant,
    spec: TaskSpec,
    idempotency_key: str | None,
    github: dict[str, Any] | None = None,
    retry_of: uuid.UUID | None = None,
    ledger: Any = None,
) -> tuple[t.Task, bool]:
    check_size_limits(spec, config)
    plans = config.limits.plans
    plan = plans.get(tenant.plan) or plans["standard"]
    budget = effective_budget(plan.task, spec)
    if ledger is not None and await ledger.tenant_remaining_cost(str(tenant.id), plan.tenant) <= 0:
        raise QuotaExceeded("the tenant's daily spend limit is reached", retry_after=_seconds_to_midnight())
    spec_json = spec.model_dump(mode="json")
    digest = hash_json(spec_json)
    key = idempotency_key or f"auto-{uuid.uuid4()}"
    async with db.transaction() as s:
        active = await repo.count_tasks(s, tenant.id, ["queued", "running"])
        if active >= plan.tenant.queued_tasks:
            raise QuotaExceeded(f"the tenant already has {active} unfinished tasks", retry_after=30)
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if await repo.tasks_created_since(s, tenant.id, midnight) >= plan.tenant.tasks_per_day:
            raise QuotaExceeded(
                "the tenant's daily task limit is reached", retry_after=_seconds_to_midnight()
            )
        task, created = await repo.insert_task(
            s,
            tenant_id=tenant.id,
            kind=spec.kind,
            spec=spec_json,
            spec_hash=digest,
            idempotency_key=key,
            priority=spec.options.priority,
            budget=budget.model_dump(mode="json"),
            github=github,
            retry_of=retry_of,
        )
        if created:
            await repo.append_event(
                s, task.id, "task.created", {"kind": spec.kind, "priority": spec.options.priority}
            )
    if created:
        await enqueue_task(redis, str(task.id), spec.options.priority)
    return task, created


def _seconds_to_midnight() -> int:
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))
