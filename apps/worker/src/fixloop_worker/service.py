"""The worker service: take task ids off the queue, claim them, run the loop.

Postgres is the source of truth; the queue only says "look at this task". A worker:
  1. reads a task id from the interactive or batch stream (3:1 weighted, so batch never starves),
  2. claims the task, which bumps its fence (any older owner's writes now fail),
  3. runs or resumes the loop, renewing the lease and keeping the message alive meanwhile,
  4. acknowledges the message once the task is finished or handed back.

A task delivered more than `MAX_DELIVERIES` times is dead-lettered and finished as infra_error,
so a task that crashes workers cannot loop forever. A reconciler re-enqueues tasks whose
messages were lost (a queue flush) or whose worker died.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import structlog
from redis.exceptions import RedisError

from fixloop_core.errors import InfraError, LeaseLost
from fixloop_core.loop.driver import run_task
from fixloop_core.loop.runtime import TaskRow
from fixloop_core.models import Outcome, TaskResult, TaskStatus
from fixloop_core.observability import QUEUE_DEPTH, QUEUE_OLDEST
from fixloop_core.ports import Claim
from fixloop_core.queue import TASK_STREAMS, WORKERS_GROUP, Message, StreamConsumer, enqueue_task, queue_stats
from fixloop_core.storage import repo
from fixloop_worker.wiring import Resources

log = structlog.get_logger(__name__)
MAX_DELIVERIES = 3


class WorkerService:
    def __init__(self, resources: Resources) -> None:
        self.r = resources
        self.settings = resources.settings.worker
        self.consumer = StreamConsumer(
            resources.redis,
            [TASK_STREAMS["interactive"], TASK_STREAMS["batch"]],
            WORKERS_GROUP,
            self.settings.worker_id,
            block_ms=2000,
            claim_idle_ms=max(120_000, self.settings.lease_seconds * 3000),
        )
        self.slots = asyncio.Semaphore(self.settings.concurrency)
        self.running: dict[str, asyncio.Task[None]] = {}

    async def serve(self, stop: asyncio.Event) -> None:
        await self.consumer.setup()
        housekeeping = asyncio.create_task(self._housekeeping(stop))
        log.info("worker_started", worker=self.settings.worker_id, concurrency=self.settings.concurrency)
        try:
            while not stop.is_set():
                await self.slots.acquire()
                try:
                    message = await self.consumer.next()
                except RedisError as exc:
                    self.slots.release()
                    log.warning("queue_unavailable", error=str(exc))
                    await asyncio.sleep(2)
                    continue
                if message is None:
                    self.slots.release()
                    continue
                task = asyncio.create_task(self._handle(message))
                self.running[message.id] = task
                task.add_done_callback(lambda _t, mid=message.id: self.running.pop(mid, None))
        finally:
            housekeeping.cancel()
            if self.running:
                log.info("worker_draining", tasks=len(self.running))
                # In-flight tasks keep their checkpoints; whoever claims them next resumes them.
                for task in list(self.running.values()):
                    task.cancel()
                await asyncio.gather(*self.running.values(), return_exceptions=True)

    # ------------------------------------------------------------------ one task
    async def _handle(self, message: Message) -> None:
        try:
            await self._handle_inner(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("task_handling_failed", message_id=message.id)
        finally:
            self.slots.release()

    async def _handle_inner(self, message: Message) -> None:
        db = self.r.db
        try:
            task_id = uuid.UUID(message.fields.get("task_id", ""))
        except ValueError:
            await self.consumer.dead_letter(message, "message has no valid task_id")
            return
        async with db.session() as s:
            task = await repo.get_task(s, task_id)
        if task is None or TaskStatus(task.status).terminal:
            await self.consumer.ack(message)
            return
        async with db.transaction() as s:
            claim = await repo.claim_task(s, task_id, self.settings.worker_id, self.settings.lease_seconds)
        if claim is None:
            await self.consumer.ack(
                message
            )  # a live worker owns it; the reconciler covers it if that one dies
            return
        async with db.session() as s:
            task = await repo.get_task(s, task_id)
            tenant = await repo.get_tenant(s, task.tenant_id) if task else None
        if task is None or tenant is None:
            await self.consumer.ack(message)
            return
        structlog.contextvars.bind_contextvars(task_id=str(task_id))
        try:
            if task.deliveries > MAX_DELIVERIES:
                await self._give_up(
                    claim, task, "the task failed on several workers in a row and was dead-lettered"
                )
                await self.consumer.dead_letter(message, "too many deliveries")
                return
            row = TaskRow(
                id=task.id,
                tenant_id=task.tenant_id,
                tenant_plan=tenant.plan,
                kind=task.kind,
                spec=task.spec,
                spec_hash=task.spec_hash,
                state=task.state or {},
                usage=task.usage or {},
                budget=task.budget,
                github=task.github,
                retry_of=task.retry_of,
            )
            runner = asyncio.create_task(run_task(self.r.deps, row, claim))
            keeper = asyncio.create_task(self._keep_alive(claim, message, runner))
            try:
                await runner
            except LeaseLost:
                log.warning("lease_lost", detail="another worker took the task over")
            except asyncio.CancelledError:
                if keeper.done() and keeper.exception() is None and keeper.result() == "lost":
                    log.warning("lease_lost_cancelled")
                else:
                    raise
            except (InfraError, Exception) as exc:
                log.warning("task_handed_back", error=f"{type(exc).__name__}: {str(exc)[:300]}")
                async with db.transaction() as s:
                    await repo.release_lease(s, claim)
                await asyncio.sleep(min(30, 2**task.deliveries))
                await enqueue_task(self.r.redis, str(task_id), task.priority)
            finally:
                keeper.cancel()
            await self.consumer.ack(message)
        finally:
            structlog.contextvars.unbind_contextvars("task_id")

    async def _keep_alive(self, claim: Claim, message: Message, runner: asyncio.Task[TaskResult]) -> str:
        interval = self.settings.heartbeat_seconds
        while not runner.done():
            await asyncio.sleep(interval)
            try:
                async with self.r.db.transaction() as s:
                    await repo.renew_lease(s, claim, self.settings.lease_seconds)
            except LeaseLost:
                runner.cancel()
                return "lost"
            except Exception as exc:
                log.warning("lease_renewal_failed", error=str(exc)[:200])
            with contextlib.suppress(RedisError):
                await self.r.redis.xclaim(
                    message.stream, WORKERS_GROUP, self.settings.worker_id, 0, [message.id], justid=True
                )
        return "done"

    async def _give_up(self, claim: Claim, task, reason: str) -> None:
        result = TaskResult(outcome=Outcome.infra_error, reason=reason)
        await self.r.deps.store.finalize(
            claim,
            tenant_id=task.tenant_id,
            result=result,
            usage=task.usage or {},
            best_attempt_no=task.best_attempt_no,
            outbox=[],
        )

    # ------------------------------------------------------------------ housekeeping
    async def _housekeeping(self, stop: asyncio.Event) -> None:
        last = 0.0
        while not stop.is_set():
            await asyncio.sleep(5)
            try:
                for name, stream in TASK_STREAMS.items():
                    stats = await queue_stats(self.r.redis, stream, WORKERS_GROUP)
                    QUEUE_DEPTH.labels(name).set(stats["depth"])
                    QUEUE_OLDEST.labels(name).set(stats["oldest_age_s"])
                if time.monotonic() - last >= self.settings.reconcile_interval_s:
                    last = time.monotonic()
                    await self.reconcile()
            except Exception:
                log.exception("housekeeping_failed")

    async def reconcile(self) -> int:
        """Re-enqueue tasks the queue has lost track of. One worker at a time does this."""
        lock = await self.r.redis.set(
            "fixloop:lock:reconciler",
            self.settings.worker_id,
            nx=True,
            ex=max(30, self.settings.reconcile_interval_s),
        )
        if not lock:
            return 0
        async with self.r.db.transaction() as s:
            tasks = await repo.tasks_to_reconcile(s, idle_seconds=max(120, self.settings.lease_seconds * 2))
            for task in tasks:
                await repo.touch_task(s, task.id)
        for task in tasks:
            await enqueue_task(self.r.redis, str(task.id), task.priority)
        if tasks:
            log.info("reconciled", count=len(tasks))
        return len(tasks)
