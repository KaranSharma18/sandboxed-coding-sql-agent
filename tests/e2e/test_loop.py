"""The whole loop, end to end, with a scripted model (design milestone M6).

Each test submits a task through the intake service, lets the real worker and the local runner
process it, and checks the outcome, the attempts and the spend.
"""

import asyncio
import json

import pytest

from fixloop_core.llm.scripted import ScriptStep
from fixloop_core.services.tasks import create_task
from fixloop_core.spec import TaskSpec
from fixloop_core.storage import repo

pytestmark = pytest.mark.e2e

PROJECT = {
    "calc.py": "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n",
    "tests/test_calc.py": (
        "from calc import add, sub\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_sub():\n    assert sub(5, 3) == 2\n"
    ),
}
PLAN = {
    "hypothesis": "add subtracts",
    "approach": "use +",
    "files_to_change": ["calc.py"],
    "files_to_read": [],
    "tests_expected_to_pass": ["tests/test_calc.py::test_add"],
    "risk": "low",
    "dependency_requests": [],
}
GOOD = {
    "summary": "add now adds",
    "edits": [
        {
            "path": "calc.py",
            "action": "replace",
            "search": "def add(a, b):\n    return a - b",
            "replace": "def add(a, b):\n    return a + b",
        }
    ],
}


def python_spec(files=None, targets=("tests/test_calc.py::test_add",), **options) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "kind": "python.fix",
            "description": "add() returns the wrong value",
            "source": {"type": "inline", "files": files or PROJECT},
            "oracle": {"type": "pytest", "target_tests": list(targets)},
            "options": {"max_repairs": 3, **options},
        }
    )


def repair(diagnosis: str, search: str, replace: str) -> dict:
    return {
        "diagnosis": diagnosis,
        "what_is_different": "different",
        "edits": [{"path": "calc.py", "action": "replace", "search": search, "replace": replace}],
    }


async def submit(stack, spec: TaskSpec, key: str):
    task, created = await create_task(
        stack.resources.db,
        stack.resources.redis,
        stack.resources.config,
        tenant=stack.tenant,
        spec=spec,
        idempotency_key=key,
    )
    assert created
    return task.id


async def wait_done(stack, task_id, timeout=240):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with stack.resources.db.session() as s:
            task = await repo.get_task(s, task_id)
        if task.status in ("succeeded", "failed", "cancelled"):
            async with stack.resources.db.session() as s:
                attempts = await repo.list_attempts(s, task_id)
            return task, attempts
        await asyncio.sleep(0.3)
    raise AssertionError(f"task {task_id} did not finish (phase {task.phase})")


async def test_verified_on_the_first_attempt(stack):
    stack.script([ScriptStep(role="plan", output=PLAN), ScriptStep(role="patch", output=GOOD)])
    task_id = await submit(stack, python_spec(), "verified-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "verified", task.result
    assert task.result["verification"]["level"] == "no_regressions"
    assert task.result["verification"]["stable"] is True
    assert [a.attempt_no for a in attempts] == [0]
    patch = await stack.resources.deps.blobs.get_bytes(task.result["patch_key"])
    assert b"-    return a - b" in patch and b"+    return a + b" in patch
    assert task.usage["model_calls"] == 2 and task.usage["executions"] >= 4
    async with stack.resources.db.session() as s:
        events = await repo.events_after(s, task_id, 0)
    types = [e.type for e in events]
    assert types[0] == "task.created" and types[-1] == "task.finished" and "baseline.done" in types


async def test_repair_from_a_wrong_first_patch_and_l2_cache_hit(stack):
    wrong = {
        "summary": "try",
        "edits": [
            {
                "path": "calc.py",
                "action": "replace",
                "search": "def add(a, b):\n    return a - b",
                "replace": "def add(a, b):\n    return a * b",
            }
        ],
    }
    stack.script(
        [
            ScriptStep(role="plan", output=PLAN),
            ScriptStep(role="patch", output=wrong),
            ScriptStep(role="repair", output=repair("multiplies", "return a * b", "return a + b")),
        ]
    )
    task_id = await submit(stack, python_spec(), "repair-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "verified", task.result
    assert [a.attempt_no for a in attempts] == [0, 1]
    assert attempts[0].failure_class.value == "targets_failed" and attempts[1].verdict.passed

    # The same task again: the verified fix comes from the L2 cache and is re-verified, no model calls.
    backend = stack.script([])
    again = await submit(stack, python_spec(), "repair-2")
    task2, attempts2 = await wait_done(stack, again)
    assert task2.outcome == "verified" and task2.result["cache"] == "l2" and attempts2 == []
    assert backend.requests == [] and task2.usage.get("model_calls", 0) == 0


async def test_nothing_to_fix(stack):
    stack.script([])
    task_id = await submit(stack, python_spec(targets=("tests/test_calc.py::test_sub",)), "nothing-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "nothing_to_fix" and attempts == []


async def test_invalid_target_is_invalid_input(stack):
    stack.script([])
    task_id = await submit(stack, python_spec(targets=("tests/test_calc.py::test_missing",)), "invalid-1")
    task, _ = await wait_done(stack, task_id)
    assert task.outcome == "invalid_input" and "not collected" in task.result["reason"]


async def test_repeated_failure_escalates_then_stops(stack):
    bad1 = {
        "summary": "x",
        "edits": [
            {
                "path": "calc.py",
                "action": "replace",
                "search": "def add(a, b):\n    return a - b",
                "replace": "def add(a, b):\n    return b - a",
            }
        ],
    }
    stack.script(
        [
            ScriptStep(role="plan", output=PLAN),
            ScriptStep(role="patch", output=bad1),
            ScriptStep(role="repair", output=repair("r1", "return b - a", "return -(a - b) - 2 * b + 2 * b")),
        ]
    )
    # attempt 0: add(2,3) = 1, attempt 1: add(2,3) = 1 again -> same signature -> the next repair escalates.
    # Repairs apply to the BEST state (attempt 0 here), so the escalated repair anchors on attempt 0's code.
    backend = stack.backend
    backend.steps.append(ScriptStep(role="repair", output=repair("r2", "return b - a", "return (b - a) * 1")))
    task_id = await submit(stack, python_spec(), "noprogress-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "no_progress", task.result
    assert len(attempts) == 3 and attempts[2].kind.value == "escalated_repair"
    repair_models = [r.model for r in backend.requests if r.role == "repair"]
    assert repair_models[-1] == "gemini-3.1-pro-preview"
    assert task.result["unverified"] is True and task.result["patch_key"]


async def test_editing_a_protected_test_is_refused(stack):
    cheat = {
        "summary": "cheat",
        "edits": [
            {
                "path": "tests/test_calc.py",
                "action": "replace",
                "search": "assert add(2, 3) == 5",
                "replace": "assert True",
            }
        ],
    }
    stack.script(
        [
            ScriptStep(role="plan", output=PLAN),
            ScriptStep(role="patch", output=cheat, times=3),
            ScriptStep(
                role="repair",
                output={"diagnosis": "d", "what_is_different": "w", "edits": cheat["edits"]},
                times=3,
            ),
        ]
    )
    task_id = await submit(stack, python_spec(), "policy-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "policy_violation", task.result
    assert all(a.failure_class.value == "policy_violation" for a in attempts)
    assert task.usage["executions"] <= 4  # nothing that edits a protected test is ever executed


async def test_sql_write_verified_with_differential_runs(stack):
    orders = "id,customer,amount\n1,ann,10\n2,bob,20\n3,ann,5\n4,cid,7\n"
    customers = "name,region\nann,eu\nbob,us\ncid,eu\n"
    spec = TaskSpec.model_validate(
        {
            "kind": "sql.write",
            "description": "Total order amount per region, largest first.",
            "source": {
                "type": "sql_snapshot",
                "ddl": "CREATE TABLE orders (id INTEGER, customer VARCHAR, amount INTEGER);\n"
                "CREATE TABLE customers (name VARCHAR, region VARCHAR);",
                "seeds": [
                    {"table": "orders", "inline_csv": orders},
                    {"table": "customers", "inline_csv": customers},
                ],
            },
            "oracle": {
                "type": "sql",
                "dialect": "duckdb",
                "reference_query": "SELECT c.region, SUM(o.amount) AS total FROM orders o JOIN customers c "
                "ON c.name = o.customer GROUP BY c.region ORDER BY total DESC",
                "order_matters": True,
                "sort_key": ["total"],
            },
        }
    )
    wrong = {
        "reasoning": "r",
        "query": "SELECT 'eu' AS region, 22 AS total UNION ALL SELECT 'us', 20",
        "output_columns": ["region", "total"],
        "assumptions": [],
    }
    right = {
        "diagnosis": "use the data",
        "query": "SELECT c.region, SUM(o.amount) AS total FROM orders o "
        "JOIN customers c ON c.name = o.customer GROUP BY c.region ORDER BY total DESC",
    }
    stack.script(
        [ScriptStep(role="sql_candidate", output=wrong), ScriptStep(role="sql_repair", output=right)]
    )
    task_id = await submit(stack, spec, "sql-1")
    task, attempts = await wait_done(stack, task_id)
    assert task.outcome == "verified", json.dumps(task.result)[:1500]
    assert task.result["verification"]["level"] == "differential_passed"
    assert attempts[0].verdict.passed is False and attempts[-1].verdict.passed
    assert task.result["preview"]["rows"][0] == ["eu", 22]


async def test_a_crashed_worker_is_resumed_without_paying_twice(stack):
    """Kill the loop right before the patch call, then resume on a new claim (a new fence)."""
    from fixloop_core.llm.scripted import ScriptedBackend
    from fixloop_core.loop.driver import run_task
    from fixloop_core.loop.runtime import TaskRow
    from fixloop_core.services.tasks import effective_budget, spec_hash

    spec = python_spec()
    resources = stack.resources
    plan_limits = resources.config.limits.plans["local"]
    async with (
        resources.db.transaction() as s
    ):  # inserted directly: no queue message, so only this test runs it
        task, _ = await repo.insert_task(
            s,
            tenant_id=stack.tenant.id,
            kind=spec.kind,
            spec=spec.model_dump(mode="json"),
            spec_hash=spec_hash(spec),
            idempotency_key="resume-1",
            priority="interactive",
            budget=effective_budget(plan_limits.task, spec).model_dump(mode="json"),
            github=None,
        )

    class WorkerDied(RuntimeError):
        pass

    def crash_on_patch(request):
        if request.role == "plan":
            return PLAN
        raise WorkerDied("simulated crash right before the patch call returns")

    async def claim_and_run():
        async with resources.db.transaction() as s:
            claim = await repo.claim_task(s, task.id, "resume-worker", 60)
            row_task = await repo.get_task(s, task.id)
        row = TaskRow(
            id=task.id,
            tenant_id=stack.tenant.id,
            tenant_plan="local",
            kind=spec.kind,
            spec=row_task.spec,
            spec_hash=row_task.spec_hash,
            state=row_task.state,
            usage=row_task.usage,
            budget=row_task.budget,
        )
        return await run_task(resources.deps, row, claim)

    resources.deps.llm.backend = ScriptedBackend(responder=crash_on_patch)
    with pytest.raises(WorkerDied):
        await claim_and_run()
    async with resources.db.session() as s:
        crashed = await repo.get_task(s, task.id)
    executions_before = crashed.usage["executions"]
    assert crashed.state["pending"]["plan"]["hypothesis"] == "add subtracts"

    resumed_backend = stack.script([ScriptStep(role="patch", output=GOOD)])
    result = await claim_and_run()
    assert result.outcome.value == "verified"
    assert [r.role for r in resumed_backend.requests] == ["patch"]  # the plan was not asked for again
    async with resources.db.session() as s:
        done = await repo.get_task(s, task.id)
    assert done.fence == 2 and done.usage["model_calls"] == 2  # plan once (first run) + patch once (resumed)
    assert done.usage["executions"] - executions_before == 2  # only the candidate and its rerun are new
