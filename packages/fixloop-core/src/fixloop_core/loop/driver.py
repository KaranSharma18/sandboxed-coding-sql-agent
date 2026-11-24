"""Running one claimed task to its final result (design section 5).

`run_task` picks the loop for the task's kind, maps every way a task can end to exactly one
outcome, builds the outbox items for what must happen after the task (a draft pull request, a
comment, a customer webhook) and finalizes everything in one fenced transaction. Side effects
outside Fixloop only ever happen from the outbox, after the result is committed (ADR-22).
"""

from __future__ import annotations

import time

import structlog

from fixloop_core.errors import (
    BudgetExhausted,
    EnvFailed,
    FlakyOracle,
    InfraError,
    InvalidInput,
    LeaseLost,
    TaskCancelled,
)
from fixloop_core.loop.python_task import PythonLoop, best_effort_result
from fixloop_core.loop.runtime import LoopDeps, TaskRow, TaskRun
from fixloop_core.loop.sql_task import SqlLoop, sql_best_effort
from fixloop_core.models import OutboxItem, Outcome, Phase, TaskResult
from fixloop_core.observability import ATTEMPTS_USED, BUDGET_EXHAUSTED, TASK_DURATION, TASKS
from fixloop_core.ports import Claim
from fixloop_core.spec import PYTHON_KINDS, GitHubSource

log = structlog.get_logger(__name__)


async def run_task(deps: LoopDeps, row: TaskRow, claim: Claim) -> TaskResult:
    """Run (or resume) a task. Raises LeaseLost and retryable InfraError; everything else finalizes."""
    run = TaskRun(deps, row, claim)
    run.attempts = await deps.store.list_attempts(row.id)
    await deps.ledger.start_task(str(row.id), run.usage)
    started = time.monotonic()
    if run.state.phase in (Phase.queued, Phase.snapshot) and not run.attempts:
        await run.emit("task.started", kind=row.kind, worker=deps.worker_id)
    else:
        await run.emit("task.resumed", phase=run.state.phase.value, worker=deps.worker_id)
    if run.l1_enabled and await deps.store.failed_recently(row.tenant_id, row.spec_hash, row.id):
        run.l1_enabled = False  # the cached answers already failed once: ask the model afresh
        run.state.warnings.append("the same task failed recently, so cached model answers were not reused")
    is_python = run.spec.kind in PYTHON_KINDS
    try:
        loop = PythonLoop(run) if is_python else SqlLoop(run)
        result = await loop.execute()
    except LeaseLost:
        raise
    except TaskCancelled as exc:
        result = await _best_effort(run, is_python, Outcome.cancelled, exc.message)
    except BudgetExhausted as exc:
        BUDGET_EXHAUSTED.labels(exc.dimension).inc()
        result = await _best_effort(
            run, is_python, Outcome.exhausted_budget, exc.message, dimension=exc.dimension
        )
    except InvalidInput as exc:
        result = TaskResult(outcome=Outcome.invalid_input, reason=exc.message, warnings=run.state.warnings)
    except EnvFailed as exc:
        result = TaskResult(outcome=Outcome.env_failed, reason=exc.message, warnings=run.state.warnings)
    except FlakyOracle as exc:
        result = TaskResult(outcome=Outcome.flaky_oracle, reason=exc.message, warnings=run.state.warnings)
    except InfraError as exc:
        if exc.retryable:
            raise
        result = await _best_effort(run, is_python, Outcome.infra_error, exc.message)
    result.warnings = list(dict.fromkeys(result.warnings))[:20]
    outbox = build_outbox(run, result, deps.public_url)
    await deps.store.finalize(
        claim,
        tenant_id=row.tenant_id,
        result=result,
        usage=run.usage.model_dump(mode="json"),
        best_attempt_no=run.state.best_attempt_no,
        outbox=outbox,
    )
    TASKS.labels(row.kind, result.outcome.value).inc()
    TASK_DURATION.labels(row.kind, result.outcome.value).observe(time.monotonic() - started)
    ATTEMPTS_USED.labels(row.kind, result.outcome.value).observe(len(run.attempts))
    log.info(
        "task_finished",
        task_id=str(row.id),
        outcome=result.outcome.value,
        attempts=len(run.attempts),
        cost_usd=round(run.usage.cost_usd, 4),
    )
    return result


async def _best_effort(
    run: TaskRun, is_python: bool, outcome: Outcome, reason: str, dimension: str | None = None
) -> TaskResult:
    if is_python:
        try:
            return await best_effort_result(run, outcome, reason, dimension=dimension)
        except Exception:
            return TaskResult(
                outcome=outcome, reason=reason, budget_dimension=dimension, warnings=run.state.warnings
            )
    return sql_best_effort(run, outcome, reason, dimension=dimension)


def result_summary(result: TaskResult) -> dict[str, object]:
    verification = result.verification
    return {
        "outcome": result.outcome.value,
        "status": result.outcome.status.value,
        "reason": result.reason,
        "verification_level": verification.level.value if verification else None,
        "attempts": result.attempts,
        "unverified": result.unverified,
        "cache": result.cache,
    }


def comment_body(result: TaskResult, task_url: str) -> str:
    icon = {"verified": "✅", "nothing_to_fix": "ℹ️", "executed_only": "ℹ️"}.get(  # noqa: RUF001 - emoji
        result.outcome.value, "⚠️"
    )
    lines = [f"{icon} **Fixloop: {result.outcome.value.replace('_', ' ')}**", "", result.reason[:800]]
    if result.verification is not None:
        lines += ["", f"Verification level: `{result.verification.level.value}`"]
    if result.unverified and result.patch_key:
        lines += [
            "",
            "A best-effort patch is attached to the task. It is **not verified** and was not pushed.",
        ]
    if task_url:
        lines += ["", f"Details: {task_url}"]
    return "\n".join(lines)


def build_outbox(run: TaskRun, result: TaskResult, public_url: str = "") -> list[OutboxItem]:
    task = str(run.task_id)
    items: list[OutboxItem] = []
    task_url = f"{public_url.rstrip('/')}/v1/tasks/{task}" if public_url else ""
    callback = run.spec.options.callback_url
    if callback is not None:
        items.append(
            OutboxItem(
                kind="customer_webhook",
                idempotency_key=f"{task}:webhook:task.finished",
                payload={
                    "url": str(callback),
                    "event": "task.finished",
                    "tenant_id": str(run.tenant_id),
                    "data": {"task_id": task, **result_summary(result)},
                },
            )
        )
    source = run.spec.source
    github = run.row.github or {}
    publishing = (
        isinstance(source, GitHubSource)
        and run.spec.options.open_pr
        and result.patch_key is not None
        and result.outcome is Outcome.verified
    )
    if publishing:
        assert isinstance(source, GitHubSource)
        items.append(
            OutboxItem(
                kind="github_publish",
                idempotency_key=f"{task}:github:publish",
                payload={
                    "task_id": task,
                    "tenant_id": str(run.tenant_id),
                    "installation_id": source.installation_id,
                    "repo": source.repo,
                    "base_sha": source.resolved_sha,
                    "base_ref": source.ref,
                    "patch_key": result.patch_key,
                    "files_key": run.key("final_overlay.json"),
                    "manifest_key": run.state.manifest_key,
                    "summary": result.patch_summary or result.reason,
                    "verification": result.verification.model_dump(mode="json")
                    if result.verification
                    else None,
                    "issue_number": github.get("issue_number"),
                    "task_url": task_url,
                },
            )
        )
    if github.get("issue_number") and not publishing:
        items.append(
            OutboxItem(
                kind="github_comment",
                idempotency_key=f"{task}:github:comment:finished",
                payload={
                    "installation_id": github.get("installation_id"),
                    "repo": github.get("repo"),
                    "issue_number": github["issue_number"],
                    "body": comment_body(result, task_url),
                    "task_id": task,
                },
            )
        )
    return items


__all__ = ["LoopDeps", "TaskRow", "build_outbox", "run_task"]
