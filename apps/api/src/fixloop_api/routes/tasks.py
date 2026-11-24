"""Task routes: create, read, stream events, fetch the patch, cancel, retry and delete."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import orjson
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from fixloop_api.auth import Principal, get_state, require
from fixloop_api.schemas import ArtifactOut, AttemptList, TaskList, TaskOut
from fixloop_api.state import ApiState
from fixloop_core.errors import Conflict, NotFound, Overloaded
from fixloop_core.events import channel
from fixloop_core.models import TaskStatus
from fixloop_core.objectstore import task_prefix
from fixloop_core.queue import TASK_STREAMS, WORKERS_GROUP, queue_stats
from fixloop_core.services.tasks import create_task
from fixloop_core.spec import TaskSpec
from fixloop_core.storage import repo

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])
OVERLOAD_OLDEST_S = 900  # shed new work when the oldest queued task has waited this long


def _task_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFound("no such task") from exc


async def _load(state: ApiState, principal: Principal, task_id: str):
    async with state.db.session() as s:
        task = await repo.get_task(s, _task_id(task_id), principal.tenant.id)
    if task is None:
        raise NotFound("no such task")
    return task


@router.post("", status_code=201, response_model=TaskOut, summary="Create a task")
async def create(
    spec: TaskSpec,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: Principal = Depends(require("tasks:write")),
    state: ApiState = Depends(get_state),
) -> TaskOut:
    stats = await queue_stats(state.redis, TASK_STREAMS[spec.options.priority], WORKERS_GROUP)
    if stats["oldest_age_s"] > OVERLOAD_OLDEST_S:
        raise Overloaded("the queue is backed up; try again shortly", retry_after=60)
    task, created = await create_task(
        state.db,
        state.redis,
        state.config,
        tenant=principal.tenant,
        spec=spec,
        idempotency_key=idempotency_key,
        ledger=state.ledger,
    )
    if not created:
        response.status_code = 200
    response.headers["Location"] = f"/v1/tasks/{task.id}"
    return TaskOut.of(task, state.settings.api.public_url)


@router.get("", response_model=TaskList, summary="List tasks, newest first")
async def list_(
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require("tasks:read")),
    state: ApiState = Depends(get_state),
) -> TaskList:
    async with state.db.session() as s:
        rows, next_cursor = await repo.list_tasks(
            s,
            principal.tenant.id,
            status=status,
            kind=kind,
            cursor=_task_id(cursor) if cursor else None,
            limit=limit,
        )
    return TaskList(
        items=[TaskOut.of(r, state.settings.api.public_url) for r in rows], next_cursor=next_cursor
    )


@router.get("/{task_id}", response_model=TaskOut, summary="Get a task")
async def get(
    task_id: str, principal: Principal = Depends(require("tasks:read")), state: ApiState = Depends(get_state)
) -> TaskOut:
    return TaskOut.of(await _load(state, principal, task_id), state.settings.api.public_url)


@router.get("/{task_id}/attempts", response_model=AttemptList, summary="The attempts of a task")
async def attempts(
    task_id: str, principal: Principal = Depends(require("tasks:read")), state: ApiState = Depends(get_state)
) -> AttemptList:
    task = await _load(state, principal, task_id)
    async with state.db.session() as s:
        records = await repo.list_attempts(s, task.id)
    return AttemptList(items=[r.model_dump(mode="json") for r in records])


@router.get(
    "/{task_id}/patch",
    summary="The verified patch (or the best unverified one) as a git diff",
    response_class=PlainTextResponse,
)
async def patch(
    task_id: str, principal: Principal = Depends(require("tasks:read")), state: ApiState = Depends(get_state)
) -> PlainTextResponse:
    task = await _load(state, principal, task_id)
    result = task.result or {}
    key = result.get("patch_key")
    if not key:
        raise NotFound(
            "this task has no patch" if TaskStatus(task.status).terminal else "the task is still running"
        )
    data = await state.blobs.get_bytes(key)
    headers = {
        "X-Fixloop-Verified": "false" if result.get("unverified") else "true",
        "Content-Disposition": f'attachment; filename="fixloop-{task.id}.diff"',
    }
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"), media_type="text/x-diff", headers=headers
    )


@router.get("/{task_id}/artifacts", response_model=list[ArtifactOut], summary="Stored artifacts of a task")
async def artifacts(
    task_id: str, principal: Principal = Depends(require("tasks:read")), state: ApiState = Depends(get_state)
) -> list[ArtifactOut]:
    task = await _load(state, principal, task_id)
    async with state.db.session() as s:
        rows = await repo.list_artifacts(s, task.id)
    base = state.settings.api.public_url.rstrip("/")
    return [
        ArtifactOut(
            id=str(r.id),
            kind=r.kind,
            size_bytes=r.size_bytes,
            content_type=r.content_type,
            sha256=r.sha256,
            created_at=r.created_at,
            download_url=f"{base}/v1/artifacts/{r.id}",
        )
        for r in rows
    ]


@router.post("/{task_id}/cancel", status_code=202, response_model=TaskOut, summary="Request cancellation")
async def cancel(
    task_id: str, principal: Principal = Depends(require("tasks:write")), state: ApiState = Depends(get_state)
) -> TaskOut:
    async with state.db.transaction() as s:
        task = await repo.request_cancel(s, _task_id(task_id), principal.tenant.id)
        if task is None:
            raise NotFound("no such task")
        if task.status == "queued":
            # Nobody is running it: finish it right here so it never starts.
            await repo.fail_task_unfenced(s, task.id, "cancelled", "cancelled before it started")
            await repo.append_event(
                s, task.id, "task.finished", {"outcome": "cancelled", "status": "cancelled"}
            )
            task = await repo.get_task(s, task.id)
            assert task is not None
    return TaskOut.of(task, state.settings.api.public_url)


@router.post("/{task_id}/retry", status_code=201, response_model=TaskOut, summary="Run the same spec again")
async def retry(
    task_id: str, principal: Principal = Depends(require("tasks:write")), state: ApiState = Depends(get_state)
) -> TaskOut:
    task = await _load(state, principal, task_id)
    if not TaskStatus(task.status).terminal:
        raise Conflict("the task is still running")
    spec = TaskSpec.model_validate(task.spec)
    new, _ = await create_task(
        state.db,
        state.redis,
        state.config,
        tenant=principal.tenant,
        spec=spec,
        idempotency_key=f"retry-of-{task.id}-{uuid.uuid4().hex[:8]}",
        retry_of=task.id,
        ledger=state.ledger,
        github=task.github,
    )
    return TaskOut.of(new, state.settings.api.public_url)


@router.delete("/{task_id}", status_code=204, summary="Delete a finished task and its stored data")
async def delete(
    task_id: str, principal: Principal = Depends(require("tasks:write")), state: ApiState = Depends(get_state)
) -> Response:
    task = await _load(state, principal, task_id)
    if not TaskStatus(task.status).terminal:
        raise Conflict("cancel the task before deleting it")
    await state.blobs.delete_prefix(task_prefix(str(principal.tenant.id), "tasks", str(task.id)) + "/")
    async with state.db.transaction() as s:
        await repo.delete_task(s, task.id, principal.tenant.id)
        await repo.audit(s, principal.key_id, "task.delete", str(task.id), tenant_id=principal.tenant.id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------ events (SSE)
@router.get("/{task_id}/events", summary="Task events as Server-Sent Events (replays, then follows live)")
async def events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(require("tasks:read")),
    state: ApiState = Depends(get_state),
) -> Response:
    task = await _load(state, principal, task_id)
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    if "application/json" in request.headers.get(
        "accept", ""
    ) and "text/event-stream" not in request.headers.get("accept", ""):
        async with state.db.session() as s:
            items = await repo.events_after(s, task.id, start)
        return Response(
            orjson.dumps([e.model_dump(mode="json") for e in items]), media_type="application/json"
        )
    return StreamingResponse(
        _stream(state, task.id, start, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event_id: int, type_: str, data: dict) -> bytes:
    return f"id: {event_id}\nevent: {type_}\ndata: {json.dumps(data, default=str)}\n\n".encode()


async def _stream(state: ApiState, task_id: uuid.UUID, after: int, request: Request) -> AsyncIterator[bytes]:
    """Subscribe first, then replay from Postgres, then follow: no event can fall in between."""
    pubsub = state.redis.pubsub()
    await pubsub.subscribe(channel(task_id))
    last = after
    try:
        async with state.db.session() as s:
            replay = await repo.events_after(s, task_id, after, limit=5000)
        for event in replay:
            last = event.seq
            yield _sse(event.seq, event.type, event.model_dump(mode="json"))
            if event.type == "task.finished":
                return
        idle = 0.0
        while not await request.is_disconnected():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
            if message is None:
                idle += 5.0
                if idle >= 15.0:
                    idle = 0.0
                    yield b": ping\n\n"
                    async with state.db.session() as s:  # catch anything a lost pub/sub message hid
                        for event in await repo.events_after(s, task_id, last):
                            last = event.seq
                            yield _sse(event.seq, event.type, event.model_dump(mode="json"))
                            if event.type == "task.finished":
                                return
                continue
            data = orjson.loads(message["data"])
            if int(data.get("seq", 0)) <= last:
                continue
            last = int(data["seq"])
            yield _sse(last, data.get("type", "event"), data)
            if data.get("type") == "task.finished":
                return
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await pubsub.unsubscribe(channel(task_id))
            await pubsub.aclose()
        except Exception:
            pass


artifact_router = APIRouter(prefix="/v1/artifacts", tags=["tasks"])


@artifact_router.get("/{artifact_id}", summary="Download an artifact (redirects to a short-lived URL)")
async def download(
    artifact_id: str,
    principal: Principal = Depends(require("tasks:read")),
    state: ApiState = Depends(get_state),
) -> RedirectResponse:
    try:
        uid = uuid.UUID(artifact_id)
    except ValueError as exc:
        raise NotFound("no such artifact") from exc
    async with state.db.session() as s:
        artifact = await repo.get_artifact(s, uid, principal.tenant.id)
    if artifact is None:
        raise NotFound("no such artifact")
    return RedirectResponse(await state.blobs.presign_get(artifact.key, ttl=300), status_code=307)
