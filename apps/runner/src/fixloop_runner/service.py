"""The runner service: take execution requests off the queue and run them in sandboxes.

Flow for one request (design section 9):
    1. Already have a stored result for this exec id? Re-announce it (a redelivery after a crash).
    2. Past its deadline or cancelled? Answer without running anything.
    3. Ensure the environment (prepare once per tenant and env hash, cached on this runner).
    4. Materialize the workspace: snapshot + candidate overlay + hidden tests + platform files,
       every piece checked against its hash.
    5. Build the argument list from the fixed template, run it, collect artifacts.
    6. Upload artifacts, store the result in Redis, wake the worker, acknowledge the message.

The runner never talks to Postgres or to a model: it only needs the queue, the object store and
the container runtime (ADR-11). Every failure becomes a result the worker can reason about;
nothing is silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from fixloop_core.config import AppConfig
from fixloop_core.errors import InfraError
from fixloop_core.execclient import cancel_key, result_key, store_result
from fixloop_core.models import ArtifactRef, ExecPurpose, ExecRequest, ExecResult, ExecStatus
from fixloop_core.objectstore import ObjectStore, task_prefix
from fixloop_core.observability import EXEC_QUEUE_WAIT, EXEC_SECONDS, EXECUTIONS, SLOTS_IN_USE
from fixloop_core.queue import EXEC_STREAM, RUNNERS_GROUP, Message, StreamConsumer
from fixloop_core.settings import FixloopSettings
from fixloop_runner.backends.base import ConformanceReport, RunOutput, RunSpec, SandboxBackend
from fixloop_runner.conformance import run_conformance
from fixloop_runner.envs import EnvManager
from fixloop_runner.templates import TemplateError, build_argv
from fixloop_runner.workspace import materialize

log = structlog.get_logger(__name__)
TAIL_CHARS = 20_000
CONTENT_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".xml": "application/xml",
    ".txt": "text/plain; charset=utf-8",
}


def _tail(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[-TAIL_CHARS:]


class RunnerService:
    def __init__(
        self,
        settings: FixloopSettings,
        config: AppConfig,
        backend: SandboxBackend,
        store: ObjectStore,
        redis: Redis,
    ) -> None:
        self.settings = settings
        self.runner = settings.runner
        self.config = config
        self.backend = backend
        self.store = store
        self.redis = redis
        self.envs = EnvManager(backend, store, config)
        self.slots = asyncio.Semaphore(self.runner.slots)
        self.consumer = StreamConsumer(
            redis, [EXEC_STREAM], RUNNERS_GROUP, self.runner.runner_id, block_ms=2000, claim_idle_ms=90_000
        )
        self.inflight: dict[str, Message] = {}
        self.conformance: ConformanceReport | None = None
        self.busy = 0

    # ------------------------------------------------------------------ lifecycle
    async def check_conformance(self) -> ConformanceReport:
        report = await run_conformance(self.backend, self.config, self.runner)
        self.conformance = report
        if report.unsafe:
            log.warning(
                "sandbox_unsafe_local_backend",
                detail="the local backend has no isolation; never run untrusted code with it",
                failures=report.failures(),
            )
        elif not report.passed:
            log.error(
                "sandbox_conformance_failed",
                failures=report.failures(),
                checks={k: v for k, v in report.checks.items() if not v.get("ok")},
            )
        else:
            log.info("sandbox_conformance_passed", isolation=report.isolation)
        return report

    @property
    def accepting(self) -> bool:
        if self.conformance is None:
            return False
        return self.conformance.passed or self.conformance.unsafe

    async def serve(self, stop: asyncio.Event) -> None:
        await self.consumer.setup()
        removed = await self.backend.reap(startup=True)
        if removed:
            log.info("reaped_leftovers", count=removed)
        await self.check_conformance()
        background = [asyncio.create_task(self._heartbeat(stop)), asyncio.create_task(self._periodic(stop))]
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                if not self.accepting:
                    await asyncio.sleep(5)
                    continue
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
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            if running:
                log.info("draining", inflight=len(running))
                _, pending = await asyncio.wait(running, timeout=self.runner.drain_timeout_s)
                for task in pending:
                    task.cancel()
            for task in background:
                task.cancel()
            await self.backend.close()

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        key = f"fixloop:runner:{self.runner.runner_id}"
        while not stop.is_set():
            with contextlib.suppress(RedisError):
                # Keep in-flight messages from looking abandoned while long runs are going on.
                for message in list(self.inflight.values()):
                    await self.redis.xclaim(
                        message.stream, RUNNERS_GROUP, self.runner.runner_id, 0, [message.id], justid=True
                    )
                status = {
                    "backend": self.backend.name,
                    "slots": self.runner.slots,
                    "busy": self.busy,
                    "accepting": self.accepting,
                    "at": time.time(),
                    "isolation": self.conformance.isolation if self.conformance else "unknown",
                }
                await self.redis.set(key, json.dumps(status), ex=30)
            SLOTS_IN_USE.labels(self.runner.runner_id).set(self.busy)
            await asyncio.sleep(10)

    async def _periodic(self, stop: asyncio.Event) -> None:
        last_conformance = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(60)
            try:
                await self.backend.reap()
                if time.monotonic() - last_conformance > self.runner.conformance_interval_s:
                    last_conformance = time.monotonic()
                    await self.check_conformance()
            except Exception:
                log.exception("housekeeping_failed")

    # ------------------------------------------------------------------ one message
    async def _handle(self, message: Message) -> None:
        self.busy += 1
        self.inflight[message.id] = message
        try:
            try:
                request = ExecRequest.model_validate_json(message.fields.get("payload", ""))
            except ValidationError as exc:
                await self.consumer.dead_letter(message, f"invalid exec request: {exc}")
                return
            try:
                EXEC_QUEUE_WAIT.observe(max(0.0, time.time() - int(message.id.split("-")[0]) / 1000))
            except ValueError:
                pass
            structlog.contextvars.bind_contextvars(exec_id=request.exec_id, task_id=request.task_id)
            try:
                result = await self.execute(request)
            except Exception as exc:
                log.exception("execution_failed")
                result = ExecResult(
                    exec_id=request.exec_id,
                    status=ExecStatus.infra_error,
                    error=f"runner error: {type(exc).__name__}: {str(exc)[:500]}",
                    runner_id=self.runner.runner_id,
                    backend=self.backend.name,
                )
            EXECUTIONS.labels(request.purpose.value, result.status.value).inc()
            EXEC_SECONDS.labels(request.purpose.value).observe(result.duration_ms / 1000)
            await store_result(self.redis, result)
            await self.consumer.ack(message)
            log.info(
                "execution_done",
                status=result.status.value,
                exit_code=result.exit_code,
                seconds=round(result.duration_ms / 1000, 2),
                env_hit=result.cache_hit,
            )
        finally:
            structlog.contextvars.unbind_contextvars("exec_id", "task_id")
            self.inflight.pop(message.id, None)
            self.busy -= 1
            self.slots.release()

    async def execute(self, request: ExecRequest) -> ExecResult:
        existing = await self.redis.get(result_key(request.exec_id))
        if existing:
            return ExecResult.model_validate_json(existing)
        base: dict[str, Any] = {
            "exec_id": request.exec_id,
            "runner_id": self.runner.runner_id,
            "backend": self.backend.name,
            "image": request.prepare.image,
        }
        if request.deadline_ts and time.time() > request.deadline_ts:
            return ExecResult(
                **base, status=ExecStatus.cancelled, error="the deadline passed before the run started"
            )

        async def cancelled() -> bool:
            try:
                return bool(await self.redis.exists(cancel_key(request.exec_id)))
            except RedisError:
                return False

        if await cancelled():
            return ExecResult(**base, status=ExecStatus.cancelled, error="cancelled before the run started")
        try:
            image_ref = self._image_ref(request.prepare.image)
            template = self.config.templates.templates.get(request.command.template)
            if template is None:
                raise TemplateError(f"unknown command template {request.command.template!r}")
            if template.image_family != request.prepare.family:
                raise TemplateError("the command template does not match the environment family")
        except (TemplateError, InfraError) as exc:
            return ExecResult(**base, status=ExecStatus.infra_error, error=str(exc))

        outcome = await self.envs.ensure(request, image_ref, cancelled)
        artifacts: dict[str, ArtifactRef] = {}
        if outcome.output is not None and (request.purpose is ExecPurpose.prepare or outcome.env is None):
            artifacts = await self._upload(request, outcome.output, prefix="prepare-")
        if outcome.env is None:
            output = outcome.output
            status = (
                output.status
                if output and output.status in (ExecStatus.infra_error, ExecStatus.cancelled)
                else ExecStatus.completed
            )
            return ExecResult(
                **base,
                status=status,
                exit_code=output.exit_code if output else None,
                duration_ms=int(outcome.seconds * 1000),
                artifacts=artifacts,
                stdout_tail=_tail(output.stdout) if output else "",
                stderr_tail=_tail(output.stderr) if output else "",
                env_error=outcome.error,
                error=output.error if output else None,
                sandbox_seconds=outcome.seconds,
                image_digest=output.image_digest if output else "",
            )
        if request.purpose is ExecPurpose.prepare:
            output = outcome.output
            return ExecResult(
                **base,
                status=ExecStatus.completed,
                exit_code=0,
                env=outcome.env,
                cache_hit=outcome.hit,
                artifacts=artifacts,
                duration_ms=int(outcome.seconds * 1000),
                sandbox_seconds=outcome.seconds,
                image_digest=output.image_digest if output else "",
            )

        placeholders = self.backend.placeholders(
            request.prepare.family, outcome.name, request.prepare.image, request.exec_id
        )
        try:
            argv = build_argv(template, request.command.args, placeholders)
        except TemplateError as exc:
            return ExecResult(**base, status=ExecStatus.infra_error, error=f"invalid command: {exc}")
        workspace = await materialize(request, self.store)
        limits = request.limits.model_copy(
            update={"timeout_s": min(request.limits.timeout_s, self.config.limits.sandbox.timeout_s_max)}
        )
        spec = RunSpec(
            exec_id=request.exec_id,
            tenant_id=request.tenant_id,
            family=request.prepare.family,
            image=request.prepare.image,
            image_ref=image_ref,
            argv=argv,
            workspace=workspace,
            env_name=outcome.name,
            env_writable=False,
            network=template.network,
            limits=limits,
            protected_paths=sorted(request.protected_hashes),
            deadline_ts=request.deadline_ts,
            cancel_check=cancelled,
            purpose=request.purpose.value,
            task_id=request.task_id,
        )
        output = await self.backend.run(spec)
        artifacts.update(await self._upload(request, output))
        modified = sorted(
            path
            for path, digest in request.protected_hashes.items()
            if output.protected_hashes.get(path) != digest
        )
        return ExecResult(
            **base,
            status=output.status,
            exit_code=output.exit_code,
            duration_ms=int(output.duration_s * 1000),
            stdout_tail=_tail(output.stdout),
            stderr_tail=_tail(output.stderr),
            artifacts=artifacts,
            truncated=output.truncated,
            protected_modified=modified,
            image_digest=output.image_digest,
            error=output.error,
            env=outcome.env,
            cache_hit=outcome.hit,
            sandbox_seconds=round(output.duration_s + (0 if outcome.hit else outcome.seconds), 3),
        )

    def _image_ref(self, logical: str) -> str:
        if logical not in self.config.templates.images:
            raise InfraError(f"unknown sandbox image {logical!r}", retryable=False)
        overrides = self.runner.image_overrides
        if (
            self.runner.require_pinned_images
            and logical not in overrides
            and logical not in self.config.pinned_images
        ):
            raise InfraError(f"image {logical} has no pinned digest in config/images.lock", retryable=False)
        return self.config.image_ref(logical, overrides)

    async def _upload(
        self, request: ExecRequest, output: RunOutput, prefix: str = ""
    ) -> dict[str, ArtifactRef]:
        refs: dict[str, ArtifactRef] = {}
        files: dict[str, bytes] = dict(output.files)
        if output.stdout:
            files["stdout.txt"] = output.stdout
        if output.stderr:
            files["stderr.txt"] = output.stderr
        for name, data in files.items():
            key = task_prefix(
                request.tenant_id, "tasks", request.task_id, "execs", request.exec_id, prefix + name
            )
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            refs[prefix + name] = await self.store.put_bytes(
                key, data, CONTENT_TYPES.get(suffix, "application/octet-stream")
            )
        return refs


def summarize(report: ConformanceReport) -> dict[str, Any]:
    return {
        "backend": report.backend,
        "isolation": report.isolation,
        "passed": report.passed,
        "unsafe": report.unsafe,
        "failures": report.failures(),
        "checks": report.checks,
    }
