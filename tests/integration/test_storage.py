import uuid

import pytest

from fixloop_core.errors import Conflict, LeaseLost
from fixloop_core.models import AttemptKind, AttemptRecord, OutboxItem
from fixloop_core.storage import repo

pytestmark = pytest.mark.integration


async def make_task(db, key="k1", spec_hash="h1"):
    async with db.transaction() as s:
        tenant = await repo.create_tenant(s, f"tenant-{uuid.uuid4().hex[:6]}")
        task, created = await repo.insert_task(
            s,
            tenant_id=tenant.id,
            kind="python.fix",
            spec={"x": 1},
            spec_hash=spec_hash,
            idempotency_key=key,
            priority="interactive",
            budget={},
        )
    return tenant, task, created


async def test_api_keys_roundtrip(db):
    async with db.transaction() as s:
        tenant = await repo.create_tenant(s, "acme")
        key_id, token = await repo.create_api_key(s, tenant.id, ["tasks:read", "tasks:write"])
    async with db.session() as s:
        key = await repo.verify_api_key(s, token)
        assert key is not None and key.tenant_id == tenant.id and "tasks:write" in key.scopes
        assert await repo.verify_api_key(s, token[:-2] + "xx") is None
    async with db.transaction() as s:
        assert await repo.revoke_api_key(s, key_id)
    async with db.session() as s:
        assert await repo.verify_api_key(s, token) is None


async def test_idempotency(db):
    tenant, task, created = await make_task(db)
    assert created
    async with db.transaction() as s:
        again, created_again = await repo.insert_task(
            s,
            tenant_id=tenant.id,
            kind="python.fix",
            spec={"x": 1},
            spec_hash="h1",
            idempotency_key="k1",
            priority="interactive",
            budget={},
        )
        assert again.id == task.id and not created_again
    with pytest.raises(Conflict):
        async with db.transaction() as s:
            await repo.insert_task(
                s,
                tenant_id=tenant.id,
                kind="python.fix",
                spec={"x": 2},
                spec_hash="h2",
                idempotency_key="k1",
                priority="interactive",
                budget={},
            )


async def test_fencing_stops_a_stale_worker(db):
    _, task, _ = await make_task(db)
    async with db.transaction() as s:
        first = await repo.claim_task(s, task.id, "worker-a", lease_seconds=0)
    assert first is not None
    async with db.transaction() as s:  # the lease expired immediately, so another worker can take over
        second = await repo.claim_task(s, task.id, "worker-b", lease_seconds=60)
    assert second is not None and second.fence == first.fence + 1
    with pytest.raises(LeaseLost):
        async with db.transaction() as s:
            await repo.save_checkpoint(s, first, phase="plan", state={}, usage={})
    async with db.transaction() as s:
        await repo.save_checkpoint(s, second, phase="plan", state={"phase": "plan"}, usage={"model_calls": 1})
        await repo.save_attempt(
            s, second, AttemptRecord(attempt_no=0, kind=AttemptKind.initial, fingerprint="fp")
        )
    async with db.transaction() as s:
        await repo.finalize_task(
            s,
            second,
            status="succeeded",
            outcome="verified",
            result={"outcome": "verified"},
            usage={},
            best_attempt_no=0,
        )
    async with db.session() as s:
        stored = await repo.get_task(s, task.id)
        assert stored.status == "succeeded" and stored.lease_owner is None
        assert [a.attempt_no for a in await repo.list_attempts(s, task.id)] == [0]


async def test_events_are_sequenced(db):
    _, task, _ = await make_task(db)
    for i in range(3):
        async with db.transaction() as s:
            event = await repo.append_event(s, task.id, "phase.started", {"i": i})
    assert event.seq == 3
    async with db.session() as s:
        assert [e.seq for e in await repo.events_after(s, task.id, 1)] == [2, 3]


async def test_outbox_is_claimed_once(db):
    _, task, _ = await make_task(db)
    async with db.transaction() as s:
        item = OutboxItem(kind="customer_webhook", payload={"a": 1}, idempotency_key="hook-1")
        await repo.add_outbox(s, item, task_id=task.id, tenant_id=None)
        await repo.add_outbox(s, item, task_id=task.id, tenant_id=None)  # duplicate ignored
    async with db.transaction() as s:
        claimed = await repo.claim_outbox(s, ["customer_webhook"])
    async with db.transaction() as s:
        assert await repo.claim_outbox(s, ["customer_webhook"]) == []
    assert len(claimed) == 1
    async with db.transaction() as s:
        await repo.complete_outbox(s, claimed[0].id)


async def test_deliveries_dedupe(db):
    async with db.transaction() as s:
        assert await repo.record_delivery(s, "github", "d-1")
    async with db.transaction() as s:
        assert not await repo.record_delivery(s, "github", "d-1")
