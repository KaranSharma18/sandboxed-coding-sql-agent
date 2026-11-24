"""Postgres adapter for the loop's `TaskStore` port.

Every write that changes a task is fenced (`WHERE fence = :claimed`): a worker that lost its
lease gets `LeaseLost` instead of silently overwriting the new owner's progress. The final write
(result, outbox items, the `task.finished` event) is one transaction, so a task is never
finished without its follow-up work being recorded, and never has follow-up work without being
finished (ADR-22).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from fixloop_core.events import EventBus
from fixloop_core.models import AttemptRecord, ExecRequest, ExecResult, OutboxItem, TaskEvent, TaskResult
from fixloop_core.ports import Claim, UploadInfo
from fixloop_core.storage import repo
from fixloop_core.storage import tables as t
from fixloop_core.storage.db import Database

LLM_CALL_COLUMNS = {c.name for c in t.LlmCall.__table__.columns}


class PgTaskStore:
    def __init__(self, db: Database, events: EventBus) -> None:
        self.db = db
        self.events = events

    async def checkpoint(
        self,
        claim: Claim,
        *,
        phase: str,
        state: dict[str, Any],
        usage: dict[str, Any],
        snapshot_hash: str | None = None,
        env_hash: str | None = None,
        best_attempt_no: int | None = None,
    ) -> None:
        async with self.db.transaction() as s:
            await repo.save_checkpoint(
                s,
                claim,
                phase=phase,
                state=state,
                usage=usage,
                snapshot_hash=snapshot_hash,
                env_hash=env_hash,
                best_attempt_no=best_attempt_no,
            )

    async def renew(self, claim: Claim, lease_seconds: int) -> None:
        async with self.db.transaction() as s:
            await repo.renew_lease(s, claim, lease_seconds)

    async def record_attempt(self, claim: Claim, record: AttemptRecord) -> None:
        async with self.db.transaction() as s:
            await repo.save_attempt(s, claim, record)

    async def list_attempts(self, task_id: uuid.UUID) -> list[AttemptRecord]:
        async with self.db.session() as s:
            return await repo.list_attempts(s, task_id)

    async def record_execution(
        self,
        task_id: uuid.UUID,
        attempt_no: int | None,
        request: ExecRequest,
        result: ExecResult,
        summary: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.transaction() as s:
            await repo.save_execution(
                s,
                task_id,
                attempt_no if attempt_no is not None and attempt_no >= 0 else None,
                request,
                result,
                summary,
            )

    async def record_llm_call(self, values: dict[str, Any]) -> None:
        clean = {k: v for k, v in values.items() if k in LLM_CALL_COLUMNS}
        for key in ("task_id", "tenant_id"):
            if isinstance(clean.get(key), str):
                clean[key] = uuid.UUID(clean[key])
        clean["error"] = (clean.get("error") or None) and str(clean["error"])[:2000]
        async with self.db.transaction() as s:
            await repo.save_llm_call(s, **clean)

    async def record_artifact(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        kind: str,
        key: str,
        sha256: str,
        size: int,
        content_type: str,
    ) -> None:
        from fixloop_core.models import ArtifactRef

        async with self.db.transaction() as s:
            await repo.save_artifact(
                s,
                tenant_id=tenant_id,
                task_id=task_id,
                kind=kind,
                ref=ArtifactRef(key=key, sha256=sha256, size=size, content_type=content_type),
            )

    async def cancel_requested(self, task_id: uuid.UUID) -> bool:
        async with self.db.session() as s:
            value = (
                await s.execute(select(t.Task.cancel_requested).where(t.Task.id == task_id))
            ).scalar_one_or_none()
        return bool(value)

    async def upload(self, upload_id: str, tenant_id: uuid.UUID) -> UploadInfo | None:
        async with self.db.session() as s:
            row = await repo.get_upload(s, upload_id, tenant_id)
        return UploadInfo(upload_id=row.id, key=row.key, kind=row.kind, size=row.max_bytes) if row else None

    async def failed_recently(self, tenant_id: uuid.UUID, spec_hash: str, exclude: uuid.UUID) -> bool:
        async with self.db.session() as s:
            found = await repo.recent_failed_same_spec(s, tenant_id, spec_hash)
        return found is not None and found != exclude

    async def finalize(
        self,
        claim: Claim,
        *,
        tenant_id: uuid.UUID,
        result: TaskResult,
        usage: dict[str, Any],
        best_attempt_no: int | None,
        outbox: list[OutboxItem],
    ) -> TaskEvent | None:
        async with self.db.transaction() as s:
            await repo.finalize_task(
                s,
                claim,
                status=result.outcome.status.value,
                outcome=result.outcome.value,
                result=result.model_dump(mode="json"),
                usage=usage,
                best_attempt_no=best_attempt_no,
            )
            for item in outbox:
                await repo.add_outbox(s, item, task_id=claim.task_id, tenant_id=tenant_id)
            event = await repo.append_event(
                s,
                claim.task_id,
                "task.finished",
                {
                    "outcome": result.outcome.value,
                    "status": result.outcome.status.value,
                    "reason": result.reason[:500],
                },
            )
        await self.events.publish(event)
        return event
