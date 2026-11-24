"""Worker side of the execution protocol (design section 9).

The worker publishes an `ExecRequest` on the execution stream and waits. The runner stores the
`ExecResult` under a key named after the deterministic exec id and pushes a wake-up token. A
worker that resumes after a crash finds the stored result instead of running the code again.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from fixloop_core.errors import InfraError, TaskCancelled
from fixloop_core.models import ExecRequest, ExecResult
from fixloop_core.queue import EXEC_STREAM, publish

RESULT_TTL_S = 86400


def result_key(exec_id: str) -> str:
    return f"fixloop:exec:result:{exec_id}"


def done_key(exec_id: str) -> str:
    return f"fixloop:exec:done:{exec_id}"


def cancel_key(exec_id: str) -> str:
    return f"fixloop:exec:cancel:{exec_id}"


class ExecClient:
    def __init__(self, redis: Redis, slack_s: int = 120) -> None:
        self.redis = redis
        self.slack_s = slack_s

    async def stored(self, exec_id: str) -> ExecResult | None:
        raw = await self.redis.get(result_key(exec_id))
        return ExecResult.model_validate_json(raw) if raw else None

    async def run(
        self, request: ExecRequest, is_cancelled: Callable[[], Awaitable[bool]] | None = None
    ) -> ExecResult:
        existing = await self.stored(request.exec_id)
        if existing is not None:
            return existing
        await publish(self.redis, EXEC_STREAM, {"payload": request.model_dump_json()})
        deadline = request.deadline_ts + self.slack_s
        while True:
            await self.redis.blpop([done_key(request.exec_id)], timeout=5)
            result = await self.stored(request.exec_id)
            if result is not None:
                return result
            if is_cancelled is not None and await is_cancelled():
                await self.redis.set(cancel_key(request.exec_id), "1", ex=3600)
                raise TaskCancelled("cancelled while the sandbox was running")
            if time.time() > deadline:
                await self.redis.set(cancel_key(request.exec_id), "1", ex=3600)
                raise InfraError("no runner reported a result before the deadline", retryable=True)


async def store_result(redis: Redis, result: ExecResult) -> None:
    """Runner side: persist the result first, then wake the waiting worker."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(result_key(result.exec_id), result.model_dump_json(), ex=RESULT_TTL_S)
        pipe.lpush(done_key(result.exec_id), "1")
        pipe.expire(done_key(result.exec_id), 3600)
        await pipe.execute()
