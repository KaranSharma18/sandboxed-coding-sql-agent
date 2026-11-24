import asyncio

import pytest

from fixloop_core.budget import BudgetLedger, MemoryBudgetLedger, Need
from fixloop_core.cache import Cache, cache_key
from fixloop_core.config import TenantLimits
from fixloop_core.errors import BudgetExhausted
from fixloop_core.execclient import ExecClient, store_result
from fixloop_core.models import (
    BudgetLimits,
    CommandSpec,
    ExecPurpose,
    ExecRequest,
    ExecResult,
    ExecStatus,
    PrepareSpec,
    Usage,
)
from fixloop_core.queue import EXEC_STREAM, RUNNERS_GROUP, StreamConsumer, enqueue_task, ensure_group

pytestmark = pytest.mark.integration
TENANT = TenantLimits(
    running_tasks=4,
    queued_tasks=100,
    daily_cost_usd=1.0,
    tasks_per_day=500,
    api_requests_per_minute=60,
    sandbox_seconds_per_day=1000,
)
LIMITS = BudgetLimits(max_model_calls=3, max_cost_usd=0.5)


@pytest.mark.parametrize("ledger_kind", ["redis", "memory"])
async def test_budget_reserve_and_settle(redis_queue, ledger_kind):
    ledger = BudgetLedger(redis_queue, 100.0) if ledger_kind == "redis" else MemoryBudgetLedger(100.0)
    await ledger.start_task("t1", Usage())
    rids = [await ledger.reserve("t1", "ten", Need(calls=1, cost_usd=0.1), LIMITS, TENANT) for _ in range(3)]
    with pytest.raises(BudgetExhausted) as info:
        await ledger.reserve("t1", "ten", Need(calls=1), LIMITS, TENANT)
    assert info.value.dimension == "model_calls"
    await ledger.settle("t1", "ten", rids[0], Need(calls=1, cost_usd=0.05))
    with pytest.raises(BudgetExhausted):
        await ledger.reserve("t1", "ten", Need(calls=1), LIMITS, TENANT)  # settled usage still counts
    await ledger.release("t1", "ten", rids[1])
    await ledger.reserve("t1", "ten", Need(calls=1, cost_usd=0.1), LIMITS, TENANT)
    remaining = await ledger.tenant_remaining_cost("ten", TENANT)
    assert remaining == pytest.approx(1.0 - 0.05 - 0.1 - 0.1)


async def test_budget_concurrency_never_oversubscribes(redis_queue):
    ledger = BudgetLedger(redis_queue, 100.0)
    await ledger.start_task("t2", Usage())
    results = await asyncio.gather(
        *[ledger.reserve("t2", "ten", Need(calls=1), LIMITS, TENANT) for _ in range(20)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, str) for r in results) == 3


async def test_cache_roundtrip_and_compression(redis_cache, redis_queue):
    cache = Cache(redis_cache, redis_queue)
    key = cache_key("llm", "tenant-a", "model", "prompt")
    assert key != cache_key("llm", "tenant-b", "model", "prompt")
    await cache.put("llm", key, {"text": "x" * 10_000})
    assert (await cache.get("llm", key))["text"] == "x" * 10_000
    async with cache.single_flight(key) as first:
        assert first


async def test_stream_consumer_and_exec_roundtrip(redis_queue):
    await enqueue_task(redis_queue, "task-1", "batch")
    consumer = StreamConsumer(
        redis_queue, ["fixloop:q:tasks:interactive", "fixloop:q:tasks:batch"], "workers", "w1", block_ms=100
    )
    await consumer.setup()
    message = await consumer.next()
    assert message is not None and message.fields["task_id"] == "task-1"
    await consumer.ack(message)

    await ensure_group(redis_queue, EXEC_STREAM, RUNNERS_GROUP)
    request = ExecRequest(
        exec_id="ex_1",
        tenant_id="t",
        task_id="task-1",
        purpose=ExecPurpose.baseline,
        prepare=PrepareSpec(family="python", image="python-3.12", env_hash="e"),
        command=CommandSpec(template="pytest-default"),
        deadline_ts=10**10,
    )

    async def fake_runner():
        runner = StreamConsumer(redis_queue, [EXEC_STREAM], RUNNERS_GROUP, "r1", block_ms=100)
        while (msg := await runner.next()) is None:
            await asyncio.sleep(0.05)
        got = ExecRequest.model_validate_json(msg.fields["payload"])
        await store_result(
            redis_queue, ExecResult(exec_id=got.exec_id, status=ExecStatus.completed, exit_code=0)
        )
        await runner.ack(msg)

    runner_task = asyncio.create_task(fake_runner())
    result = await ExecClient(redis_queue).run(request)
    await runner_task
    assert result.status is ExecStatus.completed
    assert (await ExecClient(redis_queue).run(request)).exec_id == "ex_1"  # resumed: stored result, no rerun
