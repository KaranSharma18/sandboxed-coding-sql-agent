"""The public API: auth, scopes, idempotency, problem details, rate limits, uploads, webhooks."""

import hashlib
import hmac
import json

import httpx
import pytest
import pytest_asyncio

from fixloop_api.main import create_app
from fixloop_api.state import ApiState
from fixloop_core.settings import (
    ApiSettings,
    DatabaseSettings,
    FixloopSettings,
    GitHubSettings,
    RedisSettings,
    StorageSettings,
)
from fixloop_core.storage import repo
from tests.integration.runner_helpers import shared_tmp

pytestmark = pytest.mark.integration
SPEC = {
    "kind": "python.fix",
    "source": {
        "type": "inline",
        "files": {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
    },
    "oracle": {"type": "pytest", "target_tests": ["tests/test_calc.py::test_add"]},
}
WEBHOOK_SECRET = "whsec-test"


@pytest_asyncio.fixture
async def api(services, db, redis_queue):
    settings = FixloopSettings(
        env="test",
        database=DatabaseSettings(url=services.database_url),
        redis=RedisSettings(queue_url=services.redis_url, cache_url=services.redis_cache_url),
        storage=StorageSettings(backend="local", local_root=shared_tmp("fixloop-api-obj-")),
        api=ApiSettings(public_url="http://testserver"),
        github=GitHubSettings(enabled=True, webhook_secret=WEBHOOK_SECRET),
    )
    state = ApiState.build(settings)
    app = create_app(settings, state)
    app.state.fixloop = state
    async with db.transaction() as s:
        tenant = await repo.create_tenant(s, "api-test", plan="local")
        _, full = await repo.create_api_key(
            s, tenant.id, ["tasks:read", "tasks:write", "uploads:write", "usage:read"]
        )
        _, read_only = await repo.create_api_key(s, tenant.id, ["tasks:read"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield (
            client,
            {"Authorization": f"Bearer {full}"},
            {"Authorization": f"Bearer {read_only}"},
            state,
            tenant,
        )
    await state.close()


async def test_auth_and_scopes(api):
    client, _full, read_only, *_ = api
    assert (await client.get("/v1/tasks")).status_code == 401
    bad = await client.get("/v1/tasks", headers={"Authorization": "Bearer fl_live_nope_nope"})
    assert bad.status_code == 401 and bad.headers["content-type"].startswith("application/problem+json")
    denied = await client.post("/v1/tasks", json=SPEC, headers=read_only)
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    ok = await client.get("/v1/tasks", headers=read_only)
    assert ok.status_code == 200 and "RateLimit-Remaining" in ok.headers


async def test_create_is_idempotent_and_conflicts_on_a_different_body(api):
    client, full, *_ = api
    headers = {**full, "Idempotency-Key": "k-1"}
    first = await client.post("/v1/tasks", json=SPEC, headers=headers)
    assert first.status_code == 201, first.text
    again = await client.post("/v1/tasks", json=SPEC, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    other = dict(SPEC, description="different")
    conflict = await client.post("/v1/tasks", json=other, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "conflict"
    task = (await client.get(f"/v1/tasks/{first.json()['id']}", headers=full)).json()
    assert task["status"] == "queued" and task["links"]["events"].endswith("/events")


async def test_validation_errors_are_problem_details(api):
    client, full, *_ = api
    bad = dict(SPEC, oracle={"type": "pytest", "target_tests": ["-p evil"]})
    response = await client.post("/v1/tasks", json=bad, headers=full)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    too_big = dict(SPEC, source={"type": "inline", "files": {f"f{i}.py": "x" for i in range(500)}})
    response = await client.post("/v1/tasks", json=too_big, headers=full)
    assert response.status_code == 413 and response.json()["code"] == "too_large"


async def test_cancel_a_queued_task_and_read_its_events(api):
    client, full, *_ = api
    task = (await client.post("/v1/tasks", json=SPEC, headers=full)).json()
    cancelled = await client.post(f"/v1/tasks/{task['id']}/cancel", headers=full)
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    events = await client.get(
        f"/v1/tasks/{task['id']}/events", headers={**full, "Accept": "application/json"}
    )
    assert [e["type"] for e in events.json()] == ["task.created", "task.finished"]
    stream = await client.get(f"/v1/tasks/{task['id']}/events", headers=full)
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: task.finished" in stream.text and "id: 2" in stream.text
    assert (await client.delete(f"/v1/tasks/{task['id']}", headers=full)).status_code == 204
    assert (await client.get(f"/v1/tasks/{task['id']}", headers=full)).status_code == 404


async def test_rate_limit(api):
    client, full, _, state, _ = api
    state.config.limits.plans["local"].tenant.api_requests_per_minute = 3
    codes = [(await client.get("/v1/tasks", headers=full)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


async def test_upload_roundtrip_with_the_local_store(api):
    client, full, _, state, tenant = api
    started = await client.post(
        "/v1/uploads",
        json={"kind": "repo", "size_bytes": 5, "content_type": "application/gzip"},
        headers=full,
    )
    assert started.status_code == 201
    url = started.json()["upload_url"].replace("http://testserver", "")
    assert (await client.put(url, content=b"hello")).status_code == 204
    assert (await client.put(url.replace("signature=", "signature=x"), content=b"evil")).status_code == 403
    async with state.db.session() as s:
        upload = await repo.get_upload(s, started.json()["upload_id"], tenant.id)
    assert await state.blobs.get_bytes(upload.key) == b"hello"


async def test_github_webhook_signature_dedupe_and_commands(api):
    client, *_, state, _ = api
    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 42},
            "repository": {"full_name": "o/r"},
            "issue": {"number": 7, "pull_request": {}},
            "comment": {
                "id": 1,
                "body": "/fixloop fix tests/test_x.py::test_y",
                "user": {"login": "dev", "type": "User"},
            },
        }
    ).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "issue_comment",
        "X-GitHub-Delivery": "d-1",
        "X-Hub-Signature-256": signature,
        "Content-Type": "application/json",
    }
    assert (
        await client.post(
            "/v1/github/webhook", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=bad"}
        )
    ).status_code == 401
    first = await client.post("/v1/github/webhook", content=body, headers=headers)
    assert first.json() == {"status": "queued"}
    second = await client.post("/v1/github/webhook", content=body, headers=headers)
    assert second.json() == {"status": "duplicate"}
    async with state.db.transaction() as s:
        items = await repo.claim_outbox(s, ["github_command"])
    assert len(items) == 1 and items[0].payload["issue_number"] == 7


async def test_health_ready_usage(api):
    client, full, *_ = api
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["postgres"] is True
    usage = await client.get("/v1/usage", headers=full)
    assert usage.status_code == 200 and "remaining_today_usd" in usage.json()
    spec = (await client.get("/openapi.json")).json()
    assert "/v1/tasks/{task_id}/events" in spec["paths"]
