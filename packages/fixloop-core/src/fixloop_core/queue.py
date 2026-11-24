"""Redis Streams: task and execution queues with consumer groups (ADR-20).

A message is acknowledged only after its effects are committed to Postgres. Messages held by a
consumer that stopped working are reclaimed with XAUTOCLAIM. Postgres stays the source of truth:
a reconciler re-enqueues anything the queues lose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio import Redis
from redis.exceptions import ResponseError

TASK_STREAMS = {"interactive": "fixloop:q:tasks:interactive", "batch": "fixloop:q:tasks:batch"}
EXEC_STREAM = "fixloop:q:exec"
GH_TOKEN_STREAM = "fixloop:q:ghtoken"  # noqa: S105 - a stream name, not a secret
OUTBOX_NUDGE_STREAM = "fixloop:q:outbox"
WORKERS_GROUP = "workers"
RUNNERS_GROUP = "runners"
PUBLISHERS_GROUP = "publishers"
DLQ_PREFIX = "fixloop:dlq:"
STREAM_MAXLEN = 100_000


@dataclass
class Message:
    stream: str
    id: str
    fields: dict[str, str]

    def json(self, key: str = "payload") -> Any:
        return orjson.loads(self.fields[key])


async def ensure_group(redis: Redis, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def publish(redis: Redis, stream: str, fields: dict[str, Any]) -> str:
    encoded: dict[Any, Any] = {
        k: (v if isinstance(v, str) else orjson.dumps(v).decode()) for k, v in fields.items()
    }
    message_id = await redis.xadd(stream, encoded, maxlen=STREAM_MAXLEN, approximate=True)
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


async def enqueue_task(redis: Redis, task_id: str, priority: str = "interactive") -> str:
    return await publish(
        redis, TASK_STREAMS.get(priority, TASK_STREAMS["interactive"]), {"task_id": str(task_id)}
    )


def _decode(fields: dict[Any, Any]) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
        for k, v in fields.items()
    }


class StreamConsumer:
    """Reads from one or more streams in priority order, with weighted fairness and reclaiming.

    `weights` gives how many claims in a cycle go to each stream; the default 3:1 means a flood of
    batch work never starves interactive tasks, while batch tasks still make progress.
    """

    def __init__(
        self,
        redis: Redis,
        streams: list[str],
        group: str,
        consumer: str,
        *,
        block_ms: int = 2000,
        claim_idle_ms: int = 180_000,
        weights: list[int] | None = None,
    ) -> None:
        self.redis = redis
        self.streams = streams
        self.group = group
        self.consumer = consumer
        self.block_ms = block_ms
        self.claim_idle_ms = claim_idle_ms
        self.weights = weights or [3] + [1] * (len(streams) - 1)
        self._cycle = [s for s, w in zip(streams, self.weights, strict=False) for _ in range(w)]
        self._turn = 0
        self._last_claim = 0.0

    async def setup(self) -> None:
        for stream in self.streams:
            await ensure_group(self.redis, stream, self.group)

    async def _reclaim(self) -> Message | None:
        if time.monotonic() - self._last_claim < 5:
            return None
        self._last_claim = time.monotonic()
        for stream in self.streams:
            try:
                result = await self.redis.xautoclaim(
                    stream, self.group, self.consumer, self.claim_idle_ms, start_id="0-0", count=1
                )
            except ResponseError:
                continue
            claimed = result[1] if len(result) > 1 else []
            for message_id, fields in claimed:
                if fields:
                    mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                    return Message(stream, mid, _decode(fields))
        return None

    async def next(self) -> Message | None:
        reclaimed = await self._reclaim()
        if reclaimed:
            return reclaimed
        preferred = self._cycle[self._turn % len(self._cycle)]
        self._turn += 1
        order = [preferred] + [s for s in self.streams if s != preferred]
        # Non-blocking pass in preference order, then one blocking read across all streams.
        for stream in order:
            response = await self.redis.xreadgroup(self.group, self.consumer, {stream: ">"}, count=1)
            if response:
                return self._first(response)
        response = await self.redis.xreadgroup(
            self.group, self.consumer, dict.fromkeys(order, ">"), count=1, block=self.block_ms
        )
        return self._first(response) if response else None

    @staticmethod
    def _first(response: Any) -> Message | None:
        for stream, messages in response:
            for message_id, fields in messages:
                name = stream.decode() if isinstance(stream, bytes) else str(stream)
                mid = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
                return Message(name, mid, _decode(fields))
        return None

    async def ack(self, message: Message) -> None:
        await self.redis.xack(message.stream, self.group, message.id)

    async def dead_letter(self, message: Message, reason: str) -> None:
        await publish(
            self.redis,
            DLQ_PREFIX + message.stream.rsplit(":", 1)[-1],
            {**message.fields, "reason": reason[:500], "original_id": message.id},
        )
        await self.ack(message)


def _get(info: dict[Any, Any], key: str) -> Any:
    value = info.get(key, info.get(key.encode()))
    return value.decode() if isinstance(value, bytes) else value


async def queue_stats(redis: Redis, stream: str, group: str) -> dict[str, Any]:
    """Undelivered depth and the age of the oldest undelivered entry (for backpressure and metrics)."""
    try:
        groups = await redis.xinfo_groups(stream)
    except ResponseError:
        return {"depth": 0, "pending": 0, "oldest_age_s": 0.0}
    lag, pending, last_id = 0, 0, "0-0"
    for info in groups:
        if _get(info, "name") == group:
            lag = int(_get(info, "lag") or 0)
            pending = int(_get(info, "pending") or 0)
            last_id = str(_get(info, "last-delivered-id") or "0-0")
    entries = await redis.xrange(stream, min=f"({last_id}", count=1)
    oldest_age = 0.0
    if entries:
        first = entries[0][0].decode() if isinstance(entries[0][0], bytes) else str(entries[0][0])
        oldest_age = max(0.0, time.time() - int(first.split("-")[0]) / 1000)
    return {"depth": lag, "pending": pending, "oldest_age_s": round(oldest_age, 1)}
