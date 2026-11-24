"""Repository functions over the Postgres schema.

Two rules hold everywhere:
- Tenant-facing reads always filter by `tenant_id`.
- Worker writes to a task always include the fencing token (`WHERE fence = :mine`). A write that
  matches no row means this worker lost its lease, and raises `LeaseLost` instead of writing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fixloop_core.errors import Conflict, LeaseLost
from fixloop_core.models import (
    ArtifactRef,
    AttemptRecord,
    ExecRequest,
    ExecResult,
    OutboxItem,
    TaskEvent,
    TaskStatus,
)
from fixloop_core.ports import Claim
from fixloop_core.storage import tables as t


def _rowcount(result: Any) -> int:
    """Rows touched by an UPDATE or DELETE (typed loosely by SQLAlchemy's Result)."""
    return int(getattr(result, "rowcount", 0) or 0)


def now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------ tenants and keys
async def create_tenant(
    s: AsyncSession, name: str, plan: str = "standard", is_local: bool = False
) -> t.Tenant:
    tenant = t.Tenant(
        name=name,
        plan=plan,
        is_local=is_local,
        webhook_secret=secrets.token_urlsafe(32),
        limits={},
        allowlist_extra=[],
    )
    s.add(tenant)
    await s.flush()
    return tenant


async def get_tenant(s: AsyncSession, tenant_id: uuid.UUID) -> t.Tenant | None:
    return await s.get(t.Tenant, tenant_id)


async def tenant_by_name(s: AsyncSession, name: str) -> t.Tenant | None:
    return (await s.execute(select(t.Tenant).where(t.Tenant.name == name))).scalar_one_or_none()


def hash_secret(secret: str) -> bytes:
    """API key secrets are 256-bit random values, so a fast hash is enough (no password hashing needed)."""
    return hashlib.sha256(secret.encode()).digest()


def parse_api_key(token: str) -> tuple[str, str] | None:
    # fl_live_<key id>_<secret>; the secret itself may contain underscores.
    if not token.startswith("fl_live_"):
        return None
    rest = token[len("fl_live_") :]
    key_id, _, secret = rest.partition("_")
    if not key_id or len(secret) < 20:
        return None
    return key_id, secret


async def create_api_key(
    s: AsyncSession, tenant_id: uuid.UUID, scopes: list[str], name: str = ""
) -> tuple[str, str]:
    key_id = "k" + secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    s.add(t.ApiKey(id=key_id, tenant_id=tenant_id, name=name, secret_hash=hash_secret(secret), scopes=scopes))
    await s.flush()
    return key_id, f"fl_live_{key_id}_{secret}"


async def verify_api_key(s: AsyncSession, token: str) -> t.ApiKey | None:
    parsed = parse_api_key(token)
    if parsed is None:
        return None
    key = await s.get(t.ApiKey, parsed[0])
    if key is None or key.revoked_at is not None:
        return None
    if not hmac.compare_digest(key.secret_hash, hash_secret(parsed[1])):
        return None
    return key


async def touch_api_key(s: AsyncSession, key_id: str) -> None:
    await s.execute(update(t.ApiKey).where(t.ApiKey.id == key_id).values(last_used_at=now()))


async def revoke_api_key(s: AsyncSession, key_id: str) -> bool:
    result = await s.execute(
        update(t.ApiKey).where(t.ApiKey.id == key_id, t.ApiKey.revoked_at.is_(None)).values(revoked_at=now())
    )
    return _rowcount(result) > 0


# ------------------------------------------------------------------------------------ tasks
async def insert_task(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    spec: dict[str, Any],
    spec_hash: str,
    idempotency_key: str,
    priority: str,
    budget: dict[str, Any],
    github: dict[str, Any] | None = None,
    retry_of: uuid.UUID | None = None,
) -> tuple[t.Task, bool]:
    """Insert a task, or return the existing one for a repeated idempotency key with the same body."""
    existing = (
        await s.execute(
            select(t.Task).where(t.Task.tenant_id == tenant_id, t.Task.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.spec_hash != spec_hash:
            raise Conflict("this Idempotency-Key was already used with a different request body")
        return existing, False
    task = t.Task(
        tenant_id=tenant_id,
        kind=kind,
        spec=spec,
        spec_hash=spec_hash,
        idempotency_key=idempotency_key,
        priority=priority,
        budget=budget,
        usage={},
        state={},
        github=github,
        retry_of=retry_of,
    )
    s.add(task)
    try:
        async with s.begin_nested():
            await s.flush()
    except IntegrityError:
        existing = (
            await s.execute(
                select(t.Task).where(t.Task.tenant_id == tenant_id, t.Task.idempotency_key == idempotency_key)
            )
        ).scalar_one()
        if existing.spec_hash != spec_hash:
            raise Conflict("this Idempotency-Key was already used with a different request body") from None
        return existing, False
    return task, True


async def get_task(s: AsyncSession, task_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> t.Task | None:
    query = select(t.Task).where(t.Task.id == task_id)
    if tenant_id is not None:
        query = query.where(t.Task.tenant_id == tenant_id)
    return (await s.execute(query)).scalar_one_or_none()


async def list_tasks(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    kind: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = 50,
) -> tuple[list[t.Task], str | None]:
    query = select(t.Task).where(t.Task.tenant_id == tenant_id)
    if status:
        query = query.where(t.Task.status == status)
    if kind:
        query = query.where(t.Task.kind == kind)
    if created_after:
        query = query.where(t.Task.created_at >= created_after)
    if created_before:
        query = query.where(t.Task.created_at < created_before)
    if cursor:
        query = query.where(t.Task.id < cursor)
    rows = list((await s.execute(query.order_by(t.Task.id.desc()).limit(limit + 1))).scalars())
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def count_tasks(s: AsyncSession, tenant_id: uuid.UUID, statuses: list[str]) -> int:
    return int(
        (
            await s.execute(
                select(func.count())
                .select_from(t.Task)
                .where(t.Task.tenant_id == tenant_id, t.Task.status.in_(statuses))
            )
        ).scalar_one()
    )


async def tasks_created_since(s: AsyncSession, tenant_id: uuid.UUID, since: datetime) -> int:
    return int(
        (
            await s.execute(
                select(func.count())
                .select_from(t.Task)
                .where(t.Task.tenant_id == tenant_id, t.Task.created_at >= since)
            )
        ).scalar_one()
    )


async def recent_failed_same_spec(s: AsyncSession, tenant_id: uuid.UUID, spec_hash: str) -> uuid.UUID | None:
    """A failed task with the same spec in the last 24 h: a resubmission should explore, not replay L1."""
    row = (
        await s.execute(
            select(t.Task.id)
            .where(
                t.Task.tenant_id == tenant_id,
                t.Task.spec_hash == spec_hash,
                t.Task.status == "failed",
                t.Task.created_at >= now() - timedelta(hours=24),
            )
            .order_by(t.Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def request_cancel(s: AsyncSession, task_id: uuid.UUID, tenant_id: uuid.UUID) -> t.Task | None:
    task = await get_task(s, task_id, tenant_id)
    if task is None:
        return None
    if not TaskStatus(task.status).terminal:
        await s.execute(update(t.Task).where(t.Task.id == task_id).values(cancel_requested=True))
        task.cancel_requested = True
    return task


async def claim_task(s: AsyncSession, task_id: uuid.UUID, worker: str, lease_seconds: int) -> Claim | None:
    """Take (or retake) a task. Increments the fence so any previous owner's writes start failing."""
    expires = now() + timedelta(seconds=lease_seconds)
    result = await s.execute(
        update(t.Task)
        .where(
            t.Task.id == task_id,
            t.Task.status.in_(["queued", "running"]),
            or_(
                t.Task.lease_expires_at.is_(None),
                t.Task.lease_expires_at < now(),
                t.Task.lease_owner == worker,
            ),
        )
        .values(
            fence=t.Task.fence + 1,
            lease_owner=worker,
            lease_expires_at=expires,
            status="running",
            started_at=func.coalesce(t.Task.started_at, func.now()),
            deliveries=t.Task.deliveries + 1,
        )
        .returning(t.Task.fence)
    )
    fence = result.scalar_one_or_none()
    return Claim(task_id, worker, int(fence)) if fence is not None else None


async def _fenced(s: AsyncSession, claim: Claim, **values: Any) -> None:
    result = await s.execute(
        update(t.Task).where(t.Task.id == claim.task_id, t.Task.fence == claim.fence).values(**values)
    )
    if _rowcount(result) == 0:
        raise LeaseLost(f"task {claim.task_id}: fence {claim.fence} is stale")


async def renew_lease(s: AsyncSession, claim: Claim, lease_seconds: int) -> None:
    await _fenced(s, claim, lease_expires_at=now() + timedelta(seconds=lease_seconds))


async def save_checkpoint(
    s: AsyncSession,
    claim: Claim,
    *,
    phase: str,
    state: dict[str, Any],
    usage: dict[str, Any],
    snapshot_hash: str | None = None,
    env_hash: str | None = None,
    best_attempt_no: int | None = None,
) -> None:
    values: dict[str, Any] = {"phase": phase, "state": state, "usage": usage}
    if snapshot_hash:
        values["snapshot_hash"] = snapshot_hash
    if env_hash:
        values["env_hash"] = env_hash
    if best_attempt_no is not None:
        values["best_attempt_no"] = best_attempt_no
    await _fenced(s, claim, **values)


async def finalize_task(
    s: AsyncSession,
    claim: Claim,
    *,
    status: str,
    outcome: str,
    result: dict[str, Any],
    usage: dict[str, Any],
    best_attempt_no: int | None,
) -> None:
    await _fenced(
        s,
        claim,
        status=status,
        outcome=outcome,
        result=result,
        usage=usage,
        phase="done",
        best_attempt_no=best_attempt_no,
        finished_at=now(),
        lease_owner=None,
        lease_expires_at=None,
    )


async def fail_task_unfenced(s: AsyncSession, task_id: uuid.UUID, outcome: str, reason: str) -> bool:
    """Used for dead-lettered tasks: no worker owns them anymore."""
    result = await s.execute(
        update(t.Task)
        .where(t.Task.id == task_id, t.Task.status.in_(["queued", "running"]))
        .values(
            status="failed" if outcome != "cancelled" else "cancelled",
            outcome=outcome,
            result={"outcome": outcome, "reason": reason},
            phase="done",
            finished_at=now(),
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    return _rowcount(result) > 0


async def release_lease(s: AsyncSession, claim: Claim) -> None:
    await s.execute(
        update(t.Task)
        .where(t.Task.id == claim.task_id, t.Task.fence == claim.fence)
        .values(lease_expires_at=now())
    )


async def tasks_to_reconcile(s: AsyncSession, idle_seconds: int = 120, limit: int = 200) -> list[t.Task]:
    cutoff = now() - timedelta(seconds=idle_seconds)
    query = (
        select(t.Task)
        .where(
            t.Task.status.in_(["queued", "running"]),
            or_(
                and_(t.Task.status == "queued", t.Task.updated_at < cutoff),
                and_(
                    t.Task.status == "running",
                    or_(t.Task.lease_expires_at.is_(None), t.Task.lease_expires_at < now()),
                ),
            ),
        )
        .limit(limit)
    )
    return list((await s.execute(query)).scalars())


async def touch_task(s: AsyncSession, task_id: uuid.UUID) -> None:
    await s.execute(update(t.Task).where(t.Task.id == task_id).values(updated_at=func.now()))


# ------------------------------------------------------------------------------------ attempts and executions
async def save_attempt(s: AsyncSession, claim: Claim, record: AttemptRecord) -> None:
    await _fenced(s, claim, updated_at=func.now())
    values = {
        "task_id": claim.task_id,
        "attempt_no": record.attempt_no,
        "kind": record.kind.value,
        "record": record.model_dump(mode="json"),
        "fingerprint": record.fingerprint,
        "failure_class": record.failure_class.value if record.failure_class else None,
        "failure_signature": record.failure_signature,
        "passed": bool(record.verdict and record.verdict.passed),
        "cost_usd": record.cost_usd,
    }
    statement = pg_insert(t.Attempt).values(**values)
    await s.execute(
        statement.on_conflict_do_update(
            index_elements=["task_id", "attempt_no"],
            set_={k: statement.excluded[k] for k in values if k not in ("task_id", "attempt_no")},
        )
    )


async def list_attempts(s: AsyncSession, task_id: uuid.UUID) -> list[AttemptRecord]:
    rows = (
        await s.execute(select(t.Attempt).where(t.Attempt.task_id == task_id).order_by(t.Attempt.attempt_no))
    ).scalars()
    return [AttemptRecord.model_validate(row.record) for row in rows]


async def save_execution(
    s: AsyncSession,
    task_id: uuid.UUID,
    attempt_no: int | None,
    request: ExecRequest,
    result: ExecResult,
    summary: dict[str, Any] | None = None,
) -> None:
    req = request.model_dump(mode="json")
    for item in req.get("overlay", []) + req.get("extra_files", []):  # contents live in the object store
        item["content_b64"] = ""
    values = {
        "id": result.exec_id,
        "task_id": task_id,
        "attempt_no": attempt_no,
        "purpose": request.purpose.value,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "sandbox_seconds": result.sandbox_seconds,
        "request": req,
        "result": result.model_dump(mode="json"),
        "summary": summary,
        "runner_id": result.runner_id,
        "image_digest": result.image_digest,
        "cache_hit": result.cache_hit,
    }
    statement = pg_insert(t.Execution).values(**values)
    await s.execute(
        statement.on_conflict_do_update(
            index_elements=["id"],
            set_={
                k: statement.excluded[k]
                for k in (
                    "status",
                    "exit_code",
                    "duration_ms",
                    "sandbox_seconds",
                    "result",
                    "summary",
                    "runner_id",
                    "image_digest",
                    "cache_hit",
                )
            },
        )
    )


async def get_execution(s: AsyncSession, exec_id: str) -> t.Execution | None:
    return await s.get(t.Execution, exec_id)


async def save_llm_call(s: AsyncSession, **values: Any) -> None:
    s.add(t.LlmCall(**values))


# ------------------------------------------------------------------------------------ events
async def append_event(s: AsyncSession, task_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> TaskEvent:
    """Append an event with the next per-task sequence number (retrying on a concurrent writer)."""
    for _ in range(5):
        seq = int(
            (
                await s.execute(
                    select(func.coalesce(func.max(t.Event.seq), 0) + 1).where(t.Event.task_id == task_id)
                )
            ).scalar_one()
        )
        try:
            async with s.begin_nested():
                s.add(t.Event(task_id=task_id, seq=seq, type=type_, payload=payload))
                await s.flush()
            return TaskEvent(task_id=str(task_id), seq=seq, type=type_, payload=payload)
        except IntegrityError:
            continue
    raise Conflict("could not append event after retries")


async def events_after(
    s: AsyncSession, task_id: uuid.UUID, after_seq: int, limit: int = 500
) -> list[TaskEvent]:
    rows = (
        await s.execute(
            select(t.Event)
            .where(t.Event.task_id == task_id, t.Event.seq > after_seq)
            .order_by(t.Event.seq)
            .limit(limit)
        )
    ).scalars()
    return [
        TaskEvent(task_id=str(r.task_id), seq=r.seq, type=r.type, payload=r.payload, created_at=r.created_at)
        for r in rows
    ]


# ------------------------------------------------------------------------------------ artifacts and uploads
async def save_artifact(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID | None,
    kind: str,
    ref: ArtifactRef,
    ttl_days: int = 30,
) -> t.Artifact:
    artifact = t.Artifact(
        tenant_id=tenant_id,
        task_id=task_id,
        kind=kind,
        key=ref.key,
        sha256=ref.sha256,
        size_bytes=ref.size,
        content_type=ref.content_type,
        expires_at=now() + timedelta(days=ttl_days),
    )
    s.add(artifact)
    await s.flush()
    return artifact


async def get_artifact(s: AsyncSession, artifact_id: uuid.UUID, tenant_id: uuid.UUID) -> t.Artifact | None:
    return (
        await s.execute(
            select(t.Artifact).where(t.Artifact.id == artifact_id, t.Artifact.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def list_artifacts(s: AsyncSession, task_id: uuid.UUID) -> list[t.Artifact]:
    return list(
        (
            await s.execute(
                select(t.Artifact).where(t.Artifact.task_id == task_id).order_by(t.Artifact.created_at)
            )
        ).scalars()
    )


async def create_upload(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    max_bytes: int,
    content_type: str,
    ttl_seconds: int = 3600,
) -> t.Upload:
    upload_id = "up_" + secrets.token_hex(12)
    upload = t.Upload(
        id=upload_id,
        tenant_id=tenant_id,
        kind=kind,
        key=f"tenants/{tenant_id}/uploads/{upload_id}",
        max_bytes=max_bytes,
        content_type=content_type,
        expires_at=now() + timedelta(seconds=ttl_seconds),
    )
    s.add(upload)
    await s.flush()
    return upload


async def get_upload(s: AsyncSession, upload_id: str, tenant_id: uuid.UUID) -> t.Upload | None:
    return (
        await s.execute(select(t.Upload).where(t.Upload.id == upload_id, t.Upload.tenant_id == tenant_id))
    ).scalar_one_or_none()


# ------------------------------------------------------------------------------------ outbox
async def add_outbox(
    s: AsyncSession, item: OutboxItem, *, task_id: uuid.UUID | None, tenant_id: uuid.UUID | None
) -> None:
    await s.execute(
        pg_insert(t.Outbox)
        .values(
            kind=item.kind,
            task_id=task_id,
            tenant_id=tenant_id,
            payload=item.payload,
            idempotency_key=item.idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )


async def claim_outbox(s: AsyncSession, kinds: list[str], limit: int = 10) -> list[t.Outbox]:
    rows = list(
        (
            await s.execute(
                select(t.Outbox)
                .where(
                    t.Outbox.status == "pending",
                    t.Outbox.next_attempt_at <= func.now(),
                    t.Outbox.kind.in_(kinds),
                )
                .order_by(t.Outbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for row in rows:
        row.status = "working"
        row.attempts += 1
        row.next_attempt_at = now()  # the claim time: requeue_stuck_outbox measures from here
    await s.flush()
    return rows


async def complete_outbox(s: AsyncSession, outbox_id: uuid.UUID) -> None:
    await s.execute(
        update(t.Outbox)
        .where(t.Outbox.id == outbox_id)
        .values(status="done", delivered_at=now(), last_error=None)
    )


async def fail_outbox(s: AsyncSession, outbox_id: uuid.UUID, error: str, retry_at: datetime | None) -> None:
    values: dict[str, Any] = {"last_error": error[:2000]}
    if retry_at is None:
        values["status"] = "failed"
    else:
        values.update(status="pending", next_attempt_at=retry_at)
    await s.execute(update(t.Outbox).where(t.Outbox.id == outbox_id).values(**values))


async def requeue_stuck_outbox(s: AsyncSession, older_than_seconds: int = 300) -> int:
    result = await s.execute(
        update(t.Outbox)
        .where(
            t.Outbox.status == "working",
            t.Outbox.next_attempt_at < now() - timedelta(seconds=older_than_seconds),
        )
        .values(status="pending")
    )
    return _rowcount(result)


# ------------------------------------------------------------------ deliveries, github, audit
async def record_delivery(s: AsyncSession, source: str, delivery_id: str) -> bool:
    result = await s.execute(
        pg_insert(t.InboundDelivery)
        .values(source=source, delivery_id=delivery_id)
        .on_conflict_do_nothing()
        .returning(t.InboundDelivery.delivery_id)
    )
    return result.scalar_one_or_none() is not None


async def upsert_installation(
    s: AsyncSession, installation_id: int, account_login: str, permissions: dict[str, Any], selection: str
) -> None:
    statement = pg_insert(t.GitHubInstallation).values(
        installation_id=installation_id,
        account_login=account_login,
        permissions=permissions,
        repository_selection=selection,
    )
    await s.execute(
        statement.on_conflict_do_update(
            index_elements=["installation_id"],
            set_={
                "account_login": account_login,
                "permissions": permissions,
                "repository_selection": selection,
                "suspended_at": None,
            },
        )
    )


async def set_installation_suspended(s: AsyncSession, installation_id: int, suspended: bool) -> None:
    await s.execute(
        update(t.GitHubInstallation)
        .where(t.GitHubInstallation.installation_id == installation_id)
        .values(suspended_at=now() if suspended else None)
    )


async def get_installation(s: AsyncSession, installation_id: int) -> t.GitHubInstallation | None:
    return await s.get(t.GitHubInstallation, installation_id)


async def cancel_installation_tasks(s: AsyncSession, installation_id: int) -> int:
    result = await s.execute(
        update(t.Task)
        .where(
            t.Task.status.in_(["queued", "running"]),
            t.Task.spec["source"]["installation_id"].astext == str(installation_id),
        )
        .values(cancel_requested=True)
    )
    return _rowcount(result)


async def audit(
    s: AsyncSession,
    actor: str,
    action: str,
    target: str = "",
    details: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | None = None,
    ip: str | None = None,
) -> None:
    s.add(
        t.AuditLog(
            actor=actor, action=action, target=target, details=details or {}, tenant_id=tenant_id, ip=ip
        )
    )


async def usage_by_day(s: AsyncSession, tenant_id: uuid.UUID, days: int = 30) -> list[dict[str, Any]]:
    rows = await s.execute(
        text("""
        SELECT date_trunc('day', created_at) AS day, count(*) AS tasks,
               coalesce(sum((usage->>'cost_usd')::numeric), 0) AS cost_usd,
               coalesce(sum((usage->>'model_calls')::int), 0) AS model_calls,
               coalesce(sum((usage->>'sandbox_seconds')::numeric), 0) AS sandbox_seconds,
               count(*) FILTER (WHERE outcome = 'verified') AS verified
        FROM tasks WHERE tenant_id = :tenant AND created_at >= now() - make_interval(days => :days)
        GROUP BY 1 ORDER BY 1 DESC"""),
        {"tenant": tenant_id, "days": days},
    )
    return [
        {
            "day": r.day.date().isoformat(),
            "tasks": r.tasks,
            "verified": r.verified,
            "cost_usd": float(r.cost_usd),
            "model_calls": r.model_calls,
            "sandbox_seconds": float(r.sandbox_seconds),
        }
        for r in rows
    ]


async def delete_task(s: AsyncSession, task_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Delete a task row; attempts, executions, calls, events and artifacts go with it (ON DELETE CASCADE)."""
    result = await s.execute(sa_delete(t.Task).where(t.Task.id == task_id, t.Task.tenant_id == tenant_id))
    return _rowcount(result) > 0


async def list_tenants(s: AsyncSession) -> list[t.Tenant]:
    return list((await s.execute(select(t.Tenant).order_by(t.Tenant.created_at))).scalars())


async def link_installation(s: AsyncSession, installation_id: int, tenant_id: uuid.UUID) -> bool:
    result = await s.execute(
        update(t.GitHubInstallation)
        .where(t.GitHubInstallation.installation_id == installation_id)
        .values(tenant_id=tenant_id)
    )
    return _rowcount(result) > 0
