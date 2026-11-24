"""The publisher against a mocked GitHub API: publishing, comment commands, tokens, webhooks."""

import asyncio
import json

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fixloop_core.config import load_config
from fixloop_core.github import GitHubApp
from fixloop_core.github_source import GitHubSourceClient
from fixloop_core.models import OutboxItem
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.settings import FixloopSettings, GitHubSettings, PublisherSettings
from fixloop_core.storage import repo
from fixloop_publisher import commands, webhooks
from fixloop_publisher.service import PublisherService
from tests.integration.runner_helpers import ROOT, shared_tmp

pytestmark = pytest.mark.integration
API = "https://api.github.test"
SHA = "a" * 40


def app_settings() -> GitHubSettings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return GitHubSettings(enabled=True, app_id=1234, private_key=pem, api_url=API, webhook_secret="s")


@pytest.fixture
def publisher(db, redis_queue):
    settings = FixloopSettings(
        github=app_settings(), publisher=PublisherSettings(allow_private_callbacks=True)
    )
    blobs = LocalObjectStore(shared_tmp("fixloop-pub-"))
    return PublisherService(
        settings, load_config(ROOT / "config"), db, redis_queue, blobs, GitHubApp(settings.github)
    )


def token_route(mock):
    return mock.post(path__regex=r"/app/installations/\d+/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"})
    )


def test_command_parsing():
    parsed = commands.parse("/fixloop fix tests/test_a.py::test_x tests/test_b.py --repairs 1\nthanks!")
    assert parsed.targets == ["tests/test_a.py::test_x", "tests/test_b.py"] and parsed.repairs == 1
    for bad in ("/fixloop", "/fixloop deploy", "/fixloop fix -p evil", "/fixloop fix ../x.py"):
        with pytest.raises(commands.CommandError):
            commands.parse(bad)


def test_webhook_signatures_and_ssrf_guard():
    body = b'{"a":1}'
    header = webhooks.sign("secret", body)
    assert webhooks.verify("secret", body, header) and not webhooks.verify("other", body, header)
    assert not webhooks.verify("secret", body, webhooks.sign("secret", body, timestamp=1))  # too old
    for url in ("http://example.com/hook", "https://127.0.0.1/hook", "https://10.0.0.5/x", "https://[::1]/x"):
        with pytest.raises(webhooks.UnsafeCallback):
            webhooks.resolve_public(url)


async def test_customer_webhook_delivery_is_signed(publisher, db):
    received = {}

    def receiver(request: httpx.Request) -> httpx.Response:
        received["body"], received["sig"] = request.content, request.headers[webhooks.SIGNATURE_HEADER]
        return httpx.Response(204)

    async with db.transaction() as s:
        tenant = await repo.create_tenant(s, "hooks")
    status = await webhooks.deliver(
        "http://localhost/hook",
        tenant.webhook_secret,
        "task.finished",
        {"x": 1},
        allow_private=True,
        transport=httpx.MockTransport(receiver),
    )
    assert status == 204 and webhooks.verify(tenant.webhook_secret, received["body"], received["sig"])


async def test_publish_opens_a_draft_pr_idempotently(publisher, db):
    await publisher.blobs.put_bytes(
        "tenants/t/tasks/x/final_overlay.json",
        json.dumps({"calc.py": "def add(a, b):\n    return a + b\n"}).encode(),
    )
    async with db.transaction() as s:
        await repo.add_outbox(
            s,
            OutboxItem(
                kind="github_publish",
                idempotency_key="pub-1",
                payload={
                    "task_id": "01234567-aaaa",
                    "installation_id": 7,
                    "repo": "octo/calc",
                    "base_sha": SHA,
                    "base_ref": "main",
                    "files_key": "tenants/t/tasks/x/final_overlay.json",
                    "summary": "add adds",
                    "verification": {"level": "no_regressions", "message": "all target tests pass"},
                    "issue_number": 3,
                },
            ),
            task_id=None,
            tenant_id=None,
        )
    with respx.mock(base_url=API, assert_all_called=True) as mock:
        token_route(mock)
        mock.get(f"/repos/octo/calc/git/commits/{SHA}").respond(200, json={"tree": {"sha": "t0"}})
        blob = mock.post("/repos/octo/calc/git/blobs").respond(201, json={"sha": "b1"})
        tree = mock.post("/repos/octo/calc/git/trees").respond(201, json={"sha": "t1"})
        mock.post("/repos/octo/calc/git/commits").respond(201, json={"sha": "c1"})
        mock.post("/repos/octo/calc/git/refs").respond(422, json={"message": "Reference already exists"})
        mock.patch("/repos/octo/calc/git/refs/heads/fixloop/01234567").respond(200, json={})
        pulls = mock.post("/repos/octo/calc/pulls").respond(
            201, json={"number": 9, "html_url": "https://gh/pr/9"}
        )
        comment = mock.post("/repos/octo/calc/issues/3/comments").respond(201, json={"id": 1})
        assert await publisher.run_once() == 1
    assert json.loads(tree.calls[0].request.content)["tree"][0]["path"] == "calc.py"
    assert json.loads(pulls.calls[0].request.content)["draft"] is True
    assert "#9" in json.loads(comment.calls[0].request.content)["body"] and blob.called
    async with db.session() as s:
        assert await repo.claim_outbox(s, ["github_publish"]) == []


async def test_comment_command_creates_a_task_for_collaborators_only(publisher, db):
    async with db.transaction() as s:
        tenant = await repo.create_tenant(s, "gh-tenant")
        await repo.upsert_installation(s, 7, "octo", {}, "selected")
        await repo.link_installation(s, 7, tenant.id)
        for delivery, author in (("d-ok", "maintainer"), ("d-no", "visitor")):
            await repo.add_outbox(
                s,
                OutboxItem(
                    kind="github_command",
                    idempotency_key=delivery,
                    payload={
                        "installation_id": 7,
                        "repo": "octo/calc",
                        "issue_number": 3,
                        "is_pull_request": True,
                        "comment_id": 11,
                        "author": author,
                        "body": "/fixloop fix tests/test_calc.py::test_add",
                        "delivery_id": delivery,
                    },
                ),
                task_id=None,
                tenant_id=None,
            )
    with respx.mock(base_url=API) as mock:
        token_route(mock)
        mock.get("/repos/octo/calc/collaborators/maintainer/permission").respond(
            200, json={"permission": "write"}
        )
        mock.get("/repos/octo/calc/collaborators/visitor/permission").respond(
            200, json={"permission": "read"}
        )
        mock.get("/repos/octo/calc/pulls/3").respond(
            200, json={"head": {"ref": "feature", "sha": SHA, "repo": {"full_name": "octo/calc"}}}
        )
        mock.post("/repos/octo/calc/issues/comments/11/reactions").respond(201, json={})
        comments = mock.post("/repos/octo/calc/issues/3/comments").respond(201, json={"id": 2})
        assert await publisher.run_once() == 2
    bodies = [json.loads(c.request.content)["body"] for c in comments.calls]
    assert any("only collaborators with write access" in b for b in bodies)
    assert any("Fixloop is on it" in b for b in bodies)
    async with db.session() as s:
        tasks, _ = await repo.list_tasks(s, tenant.id)
    assert len(tasks) == 1
    source = tasks[0].spec["source"]
    assert (
        source["resolved_sha"] == SHA and source["ref"] == "feature" and tasks[0].spec["options"]["open_pr"]
    )
    assert tasks[0].github["issue_number"] == 3


async def test_worker_gets_read_only_tokens_through_the_publisher(publisher, db, redis_queue):
    async with db.transaction() as s:
        await repo.upsert_installation(s, 7, "octo", {}, "selected")
    stop = asyncio.Event()
    with respx.mock(base_url=API) as mock:
        route = token_route(mock)
        loop = asyncio.create_task(publisher._token_loop(stop))
        try:
            token = await asyncio.wait_for(GitHubSourceClient(redis_queue, API).token(7, "octo/calc"), 15)
        finally:
            stop.set()
            loop.cancel()
    assert token == "ghs_test"
    assert json.loads(route.calls[0].request.content) == {
        "repositories": ["calc"],
        "permissions": {"contents": "read"},
    }
