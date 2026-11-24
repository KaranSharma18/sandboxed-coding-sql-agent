"""Shared machinery for running one task: state, budgets, events, model calls and executions.

`TaskRun` is what every phase of the loop works with. It owns three promises:

* Every model call and every execution is reserved against the budget *before* it happens and
  settled with the real numbers after (ADR-17), so a task can never overspend by more than one
  call's estimate.
* State is checkpointed after every step that cost something, under the task's fence (ADR-21).
  A worker that crashes mid-task is replaced by one that continues from the last checkpoint:
  executions come back from the result store by their deterministic ids and completed model
  calls come back from saved state or the L1 cache, so nothing is paid for twice.
* Cancellation and the wall-clock limit are checked between steps and while waiting for a sandbox.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel

from fixloop_core.config import AppConfig, TenantLimits
from fixloop_core.errors import BudgetExhausted, InfraError, TaskCancelled
from fixloop_core.hashing import sha256_bytes
from fixloop_core.ids import exec_id as make_exec_id
from fixloop_core.llm.types import CallContext, LLMResult
from fixloop_core.models import (
    AttemptRecord,
    BudgetLimits,
    ExecRequest,
    ExecResult,
    ExecStatus,
    LoopState,
    Phase,
    SnapshotManifest,
    Usage,
)
from fixloop_core.ports import (
    BudgetPort,
    CachePort,
    Claim,
    EventSink,
    ExecPort,
    GitHubReader,
    Need,
    ObjectStore,
    TaskStore,
    task_prefix,
)
from fixloop_core.prompts import PromptLibrary, RenderedPrompt
from fixloop_core.snapshot import SnapshotBuild, SnapshotLimits, load_snapshot
from fixloop_core.spec import TaskSpec

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class LoopDeps:
    """Everything the loop needs from the outside world, as ports (wired by the worker app)."""

    config: AppConfig
    prompts: PromptLibrary
    store: TaskStore
    events: EventSink
    blobs: ObjectStore
    llm: Any  # LLMService-like: async generate(role, prompt, schema, ctx) -> LLMResult
    execs: ExecPort
    ledger: BudgetPort
    cache: CachePort
    github: GitHubReader | None = None
    worker_id: str = "worker"
    escalation_enabled: bool = True
    exec_slack_s: int = 120
    public_url: str = ""


@dataclass
class TaskRow:
    """The parts of a task row the loop reads (decoupled from the ORM)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_plan: str
    kind: str
    spec: dict[str, Any]
    spec_hash: str
    state: dict[str, Any]
    usage: dict[str, Any]
    budget: dict[str, Any]
    github: dict[str, Any] | None = None
    retry_of: uuid.UUID | None = None


@dataclass
class TaskRun:
    deps: LoopDeps
    row: TaskRow
    claim: Claim
    spec: TaskSpec = field(init=False)
    state: LoopState = field(init=False)
    usage: Usage = field(init=False)
    limits: BudgetLimits = field(init=False)
    tenant_limits: TenantLimits = field(init=False)
    attempts: list[AttemptRecord] = field(default_factory=list)
    l1_enabled: bool = True
    _snapshot: SnapshotBuild | None = None
    _last_cancel_check: float = 0.0

    def __post_init__(self) -> None:
        self.spec = TaskSpec.model_validate(self.row.spec)
        self.state = LoopState.model_validate(self.row.state or {})
        self.usage = Usage.model_validate(self.row.usage or {})
        self.limits = BudgetLimits.model_validate(self.row.budget)
        plans = self.deps.config.limits.plans
        self.tenant_limits = (plans.get(self.row.tenant_plan) or plans["standard"]).tenant
        self.l1_enabled = self.spec.options.cache == "default"
        if not self.state.started_at:
            self.state.started_at = time.time()

    # ------------------------------------------------------------------ identity
    @property
    def task_id(self) -> uuid.UUID:
        return self.row.id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.row.tenant_id

    @property
    def config(self) -> AppConfig:
        return self.deps.config

    @property
    def prompts(self) -> PromptLibrary:
        return self.deps.prompts

    def key(self, *parts: str) -> str:
        return task_prefix(str(self.tenant_id), "tasks", str(self.task_id), *parts)

    def exec_id(self, attempt_no: int, purpose: str, fingerprint: str = "", rerun_no: int = 0) -> str:
        return make_exec_id(str(self.task_id), attempt_no, purpose, fingerprint, rerun_no)

    # ------------------------------------------------------------------ persistence and events
    async def checkpoint(self, phase: Phase | None = None) -> None:
        if phase is not None:
            self.state.phase = phase
        await self.deps.store.checkpoint(
            self.claim,
            phase=self.state.phase.value,
            state=self.state.model_dump(mode="json"),
            usage=self.usage.model_dump(mode="json"),
            snapshot_hash=self.state.snapshot.sha256 if self.state.snapshot else None,
            env_hash=self.state.env.env_hash if self.state.env else None,
            best_attempt_no=self.state.best_attempt_no,
        )

    async def enter(self, phase: Phase) -> None:
        await self.checkpoint(phase)
        await self.emit("phase", phase=phase.value)

    async def emit(self, type_: str, **payload: Any) -> None:
        try:
            await self.deps.events.emit(self.task_id, type_, payload)
        except Exception as exc:
            log.warning("event_emit_failed", type=type_, error=str(exc)[:200])

    async def record_attempt(self, record: AttemptRecord) -> None:
        self.attempts = [a for a in self.attempts if a.attempt_no != record.attempt_no] + [record]
        self.attempts.sort(key=lambda a: a.attempt_no)
        await self.deps.store.record_attempt(self.claim, record)

    # ------------------------------------------------------------------ guards
    async def is_cancelled(self) -> bool:
        return await self.deps.store.cancel_requested(self.task_id)

    async def guard(self) -> None:
        now = time.monotonic()
        if now - self._last_cancel_check > 2.0:
            self._last_cancel_check = now
            if await self.is_cancelled():
                raise TaskCancelled("the task was cancelled")
        if time.time() - self.state.started_at > self.limits.wall_clock_seconds:
            raise BudgetExhausted("wall_clock_seconds", message="the task reached its wall-clock limit")

    # ------------------------------------------------------------------ blobs
    async def put_json(self, key: str, value: Any) -> None:
        await self.deps.blobs.put_bytes(
            key, json.dumps(value, sort_keys=True, default=str).encode(), "application/json"
        )

    async def get_json(self, key: str) -> Any:
        return json.loads(await self.deps.blobs.get_bytes(key))

    async def artifact_text(self, result: ExecResult, name: str, limit: int = 50_000_000) -> str | None:
        ref = result.artifacts.get(name)
        if ref is None:
            return None
        data = await self.deps.blobs.get_bytes(ref.key, max_bytes=limit)
        if ref.sha256 and sha256_bytes(data) != ref.sha256:
            raise InfraError(f"artifact {name} does not match its hash", retryable=False)
        return data.decode("utf-8", errors="replace")

    async def snapshot(self) -> SnapshotBuild:
        """The task's snapshot, loaded once from the object store and hash-checked."""
        if self._snapshot is None:
            ref = self.state.snapshot
            if ref is None:
                raise InfraError("the snapshot is not built yet", retryable=False)
            intake = self.config.limits.intake
            data = await self.deps.blobs.get_bytes(ref.key)
            self._snapshot = load_snapshot(
                data,
                ref.sha256,
                SnapshotLimits(max_files=intake.max_files, max_unpacked_bytes=intake.unpacked_max_bytes),
            )
        return self._snapshot

    def remember_snapshot(self, build: SnapshotBuild) -> None:
        self._snapshot = build

    async def manifest(self) -> SnapshotManifest:
        return (await self.snapshot()).manifest

    # ------------------------------------------------------------------ model calls
    async def llm(
        self,
        role: str,
        prompt: RenderedPrompt,
        schema: type[T],
        *,
        attempt_no: int | None = None,
        escalated: bool = False,
    ) -> LLMResult[T]:
        await self.guard()
        ctx = CallContext(
            tenant_id=str(self.tenant_id),
            task_id=str(self.task_id),
            task_limits=self.limits,
            tenant_limits=self.tenant_limits,
            attempt_no=attempt_no,
            profile=self.spec.options.model_profile,
            escalated=escalated,
            use_cache=self.l1_enabled,
            format_reasks=self.limits.format_reasks_per_step,
            recorder=self._record_call,
        )
        try:
            result: LLMResult[T] = await self.deps.llm.generate(role, prompt, schema, ctx)
        finally:
            await self.checkpoint()  # usage is persisted even when the call ends in an error
        return result

    async def _record_call(self, record: dict[str, Any]) -> None:
        billed = not record.get("cache_hit") and record.get("finish_reason") not in ("ERROR", "CACHE")
        if billed:
            self.usage = self.usage.add(
                Usage(
                    model_calls=1,
                    input_tokens=int(record.get("input_tokens") or 0),
                    cached_tokens=int(record.get("cached_tokens") or 0),
                    output_tokens=int(record.get("output_tokens") or 0),
                    thinking_tokens=int(record.get("thinking_tokens") or 0),
                    cost_usd=float(record.get("cost_usd") or 0),
                )
            )
        call_id = uuid.uuid4().hex[:16]
        request_ref = response_ref = None
        if record.get("request_text"):
            request_ref = self.key("llm", f"{call_id}-{record['role']}-request.txt")
            await self.deps.blobs.put_bytes(request_ref, str(record["request_text"]).encode(), "text/plain")
        if record.get("response_text"):
            response_ref = self.key("llm", f"{call_id}-{record['role']}-response.txt")
            await self.deps.blobs.put_bytes(response_ref, str(record["response_text"]).encode(), "text/plain")
        values = {k: v for k, v in record.items() if k not in ("request_text", "response_text")}
        values.update(
            task_id=self.task_id, tenant_id=self.tenant_id, request_ref=request_ref, response_ref=response_ref
        )
        await self.deps.store.record_llm_call(values)

    # ------------------------------------------------------------------ executions
    async def execute(self, request: ExecRequest, attempt_no: int | None = None) -> ExecResult:
        """Run one sandbox execution (reserving sandbox time first). Resumed runs are not re-billed."""
        await self.guard()
        already = request.exec_id in self.state.exec_done
        rid = None
        if not already:
            rid = await self.deps.ledger.reserve(
                str(self.task_id),
                str(self.tenant_id),
                Need(executions=1, sandbox_seconds=float(request.limits.timeout_s)),
                self.limits,
                self.tenant_limits,
            )
        try:
            result = await self.deps.execs.run(request, self.is_cancelled)
        except BaseException:
            if rid is not None:
                await self.deps.ledger.release(str(self.task_id), str(self.tenant_id), rid)
            raise
        if rid is not None:
            await self.deps.ledger.settle(
                str(self.task_id),
                str(self.tenant_id),
                rid,
                Need(executions=1, sandbox_seconds=result.sandbox_seconds),
            )
            self.usage = self.usage.add(Usage(executions=1, sandbox_seconds=result.sandbox_seconds))
            self.state.exec_done = [*self.state.exec_done, request.exec_id][-200:]
        await self.deps.store.record_execution(self.task_id, attempt_no, request, result)
        await self.checkpoint()
        if result.status is ExecStatus.cancelled and await self.is_cancelled():
            raise TaskCancelled("the task was cancelled")
        return result

    async def execute_retrying(self, request: ExecRequest, attempt_no: int | None = None) -> ExecResult:
        """An execution that hit an infrastructure error is retried once under a fresh exec id."""
        result = await self.execute(request, attempt_no)
        if result.status is ExecStatus.infra_error:
            log.warning("execution_infra_error_retrying", exec_id=request.exec_id, error=result.error)
            retry = request.model_copy(update={"exec_id": request.exec_id[:40] + "-r1"})
            result = await self.execute(retry, attempt_no)
            if result.status is ExecStatus.infra_error:
                raise InfraError(f"the sandbox failed twice: {result.error}", retryable=False)
        return result

    def deadline(self, timeout_s: int) -> float:
        remaining = self.limits.wall_clock_seconds - (time.time() - self.state.started_at)
        return time.time() + max(30.0, min(remaining, timeout_s + self.deps.exec_slack_s))
