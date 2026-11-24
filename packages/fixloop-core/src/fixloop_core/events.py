"""Task events: written to Postgres first, then fanned out through Redis pub/sub.

A reconnecting SSE client sends `Last-Event-ID`, gets everything after it from Postgres, then
live events, so nothing is lost and nothing depends on Redis being durable.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
from redis.asyncio import Redis

from fixloop_core.models import TaskEvent
from fixloop_core.storage import repo
from fixloop_core.storage.db import Database


def channel(task_id: uuid.UUID | str) -> str:
    return f"fixloop:events:{task_id}"


class EventBus:
    def __init__(self, db: Database, redis: Redis | None) -> None:
        self.db = db
        self.redis = redis

    async def emit(self, task_id: uuid.UUID, type_: str, payload: dict[str, Any] | None = None) -> TaskEvent:
        async with self.db.transaction() as session:
            event = await repo.append_event(session, task_id, type_, payload or {})
        await self.publish(event)
        return event

    async def publish(self, event: TaskEvent) -> None:
        if self.redis is None:
            return
        with contextlib.suppress(Exception):  # live fan-out is best effort; Postgres has the record
            await self.redis.publish(channel(event.task_id), orjson.dumps(event.model_dump(mode="json")))

    async def listen(self, task_id: uuid.UUID) -> AsyncIterator[TaskEvent]:
        if self.redis is None:
            return
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel(task_id))
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if message is None:
                    yield TaskEvent(task_id=str(task_id), seq=-1, type="heartbeat")
                    continue
                data = message.get("data")
                if isinstance(data, (bytes, str)):
                    yield TaskEvent.model_validate(orjson.loads(data))
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel(task_id))
                await pubsub.aclose()
