"""Through the front door: the HTTP API, the worker, the runner and the scripted model together."""

import httpx
import pytest

from fixloop_api.main import create_app
from fixloop_api.state import ApiState
from fixloop_core.llm.scripted import ScriptStep
from fixloop_core.storage import repo
from tests.e2e.test_loop import GOOD, PLAN, PROJECT

pytestmark = pytest.mark.e2e


async def test_submit_watch_and_download_the_patch(stack):
    stack.script([ScriptStep(role="plan", output=PLAN), ScriptStep(role="patch", output=GOOD)])
    settings = stack.resources.settings
    state = ApiState.build(settings)
    app = create_app(settings, state)
    app.state.fixloop = state
    async with stack.resources.db.transaction() as s:
        _, key = await repo.create_api_key(s, stack.tenant.id, ["tasks:read", "tasks:write"])
    headers = {"Authorization": f"Bearer {key}"}
    spec = {
        "kind": "python.fix",
        "description": "add() is wrong",
        "source": {"type": "inline", "files": PROJECT},
        "oracle": {"type": "pytest", "target_tests": ["tests/test_calc.py::test_add"]},
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=300
        ) as client:
            created = await client.post("/v1/tasks", json=spec, headers=headers)
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]
            async with client.stream("GET", f"/v1/tasks/{task_id}/events", headers=headers) as stream:
                types = [line[7:] async for line in stream.aiter_lines() if line.startswith("event: ")]
            assert types[-1] == "task.finished" and "attempt.finished" in types
            task = (await client.get(f"/v1/tasks/{task_id}", headers=headers)).json()
            assert task["outcome"] == "verified"
            patch = await client.get(f"/v1/tasks/{task_id}/patch", headers=headers)
            assert patch.headers["X-Fixloop-Verified"] == "true" and "+    return a + b" in patch.text
            attempts = (await client.get(f"/v1/tasks/{task_id}/attempts", headers=headers)).json()["items"]
            assert len(attempts) == 1 and attempts[0]["verdict"]["passed"]
    finally:
        await state.close()
