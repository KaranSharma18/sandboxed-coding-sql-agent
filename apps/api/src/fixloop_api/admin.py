"""`fixloop-admin`: operator commands (tenants, keys, GitHub links, dead letters).

fixloop-admin bootstrap-local              create the local tenant and print an API key
fixloop-admin create-tenant NAME [--plan standard]
fixloop-admin create-key TENANT [--scope tasks:read --scope tasks:write ...]
fixloop-admin revoke-key KEY_ID
fixloop-admin list-tenants
fixloop-admin link-installation INSTALLATION_ID TENANT
fixloop-admin replay-dlq [--stream tasks:interactive] [--limit 100]
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from fixloop_core.queue import DLQ_PREFIX, TASK_STREAMS, publish
from fixloop_core.settings import get_settings
from fixloop_core.storage import repo
from fixloop_core.storage.db import Database

app = typer.Typer(add_completion=False, help="Fixloop operator commands")
ALL_SCOPES = ["tasks:read", "tasks:write", "uploads:write", "usage:read"]
LOCAL_KEY_FILE = Path(".fixloop-data/local-api-key.txt")


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _run(coro):
    return asyncio.run(coro)


async def _db() -> Database:
    return Database(get_settings().database)


@app.command("bootstrap-local")
def bootstrap_local(
    name: str = "local",
    save: bool = typer.Option(True, help="also save the key to .fixloop-data/local-api-key.txt"),
) -> None:
    """Create the local development tenant (plan `local`) and an API key with every scope."""

    async def work() -> tuple[str, str, bool]:
        db = await _db()
        try:
            async with db.transaction() as s:
                tenant = await repo.tenant_by_name(s, name)
                created = tenant is None
                if tenant is None:
                    tenant = await repo.create_tenant(s, name, plan="local", is_local=True)
                _key_id, token = await repo.create_api_key(s, tenant.id, ALL_SCOPES, name="bootstrap-local")
                await repo.audit(s, "fixloop-admin", "bootstrap_local", str(tenant.id), tenant_id=tenant.id)
            return str(tenant.id), token, created
        finally:
            await db.close()

    tenant_id, token, created = _run(work())
    typer.echo(f"tenant {name} ({tenant_id}) {'created' if created else 'already existed'}")
    typer.echo(f"API key: {token}")
    if save:
        LOCAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_KEY_FILE.write_text(token + "\n", encoding="utf-8")
        typer.echo(f"saved to {LOCAL_KEY_FILE} (the fixloop CLI reads it automatically)")


@app.command("create-tenant")
def create_tenant(name: str, plan: str = typer.Option("standard")) -> None:
    async def work() -> str:
        db = await _db()
        try:
            async with db.transaction() as s:
                tenant = await repo.create_tenant(s, name, plan=plan)
                await repo.audit(s, "fixloop-admin", "create_tenant", str(tenant.id), tenant_id=tenant.id)
            return str(tenant.id)
        finally:
            await db.close()

    typer.echo(_run(work()))


@app.command("create-key")
def create_key(
    tenant: str, scope: list[str] = typer.Option(ALL_SCOPES, "--scope"), name: str = typer.Option("")
) -> None:
    """Create an API key for a tenant (by name). The key is shown once and never stored."""

    async def work() -> str:
        db = await _db()
        try:
            async with db.transaction() as s:
                row = await repo.tenant_by_name(s, tenant)
                if row is None:
                    raise typer.BadParameter(f"no tenant named {tenant!r}")
                key_id, token = await repo.create_api_key(s, row.id, list(scope), name=name)
                await repo.audit(s, "fixloop-admin", "create_key", key_id, tenant_id=row.id)
            return token
        finally:
            await db.close()

    typer.echo(_run(work()))


@app.command("revoke-key")
def revoke_key(key_id: str) -> None:
    async def work() -> bool:
        db = await _db()
        try:
            async with db.transaction() as s:
                done = await repo.revoke_api_key(s, key_id)
                await repo.audit(s, "fixloop-admin", "revoke_key", key_id)
            return done
        finally:
            await db.close()

    typer.echo("revoked" if _run(work()) else "no such key")


@app.command("list-tenants")
def list_tenants() -> None:
    async def work() -> list[dict[str, str]]:
        db = await _db()
        try:
            async with db.session() as s:
                return [
                    {"id": str(t.id), "name": t.name, "plan": t.plan, "status": t.status}
                    for t in await repo.list_tenants(s)
                ]
        finally:
            await db.close()

    typer.echo(json.dumps(_run(work()), indent=2))


@app.command("link-installation")
def link_installation(installation_id: int, tenant: str) -> None:
    """Attach a GitHub App installation to a tenant, so its comment commands create tasks."""

    async def work() -> bool:
        db = await _db()
        try:
            async with db.transaction() as s:
                row = await repo.tenant_by_name(s, tenant)
                if row is None:
                    raise typer.BadParameter(f"no tenant named {tenant!r}")
                if await repo.get_installation(s, installation_id) is None:
                    await repo.upsert_installation(s, installation_id, "", {}, "selected")
                linked = await repo.link_installation(s, installation_id, row.id)
                await repo.audit(
                    s, "fixloop-admin", "link_installation", str(installation_id), tenant_id=row.id
                )
            return linked
        finally:
            await db.close()

    typer.echo("linked" if _run(work()) else "failed")


@app.command("replay-dlq")
def replay_dlq(stream: str = typer.Option("tasks:interactive"), limit: int = typer.Option(100)) -> None:
    """Move dead-lettered task messages back onto their queue (after fixing the cause)."""
    from redis.asyncio import Redis

    async def work() -> int:
        redis = Redis.from_url(get_settings().redis.queue_url)
        try:
            name = stream.rsplit(":", 1)[-1]
            source = DLQ_PREFIX + name
            target = TASK_STREAMS.get(name, TASK_STREAMS["interactive"])
            moved = 0
            entries: list[Any] = list(await redis.xrange(source, count=limit) or [])
            for message_id, fields in entries:
                decoded = {_text(k): _text(v) for k, v in (fields or {}).items()}
                if "task_id" in decoded:
                    await publish(redis, target, {"task_id": decoded["task_id"]})
                    moved += 1
                await redis.xdel(source, message_id)
            return moved
        finally:
            await redis.aclose()

    typer.echo(f"re-enqueued {_run(work())} message(s)")


if __name__ == "__main__":
    app()
