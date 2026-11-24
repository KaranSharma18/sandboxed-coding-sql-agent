"""The publisher: executes outbox items and answers the worker's GitHub token requests.

Outbox kinds (ADR-22):
    github_publish    commit the verified files to a branch, open a draft PR, comment with the link
    github_comment    post a comment on an issue or pull request
    github_command    a `/fixloop ...` comment: check permission, create the task, acknowledge
    customer_webhook  POST a signed event to a tenant's callback URL

Every handler is idempotent (re-running it after a crash finds the branch, the PR or the task it
already made), failures are retried with exponential backoff, and an item that keeps failing
ends as `failed` with its last error, visible to operators.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from fixloop_core.config import AppConfig
from fixloop_core.errors import FixloopError, Forbidden, NotFound
from fixloop_core.github import COMMENT, PUBLISH, READ, GitHubApiError, GitHubApp
from fixloop_core.github_source import reply_key
from fixloop_core.objectstore import ObjectStore
from fixloop_core.queue import GH_TOKEN_STREAM, PUBLISHERS_GROUP, StreamConsumer
from fixloop_core.services.tasks import create_task
from fixloop_core.settings import FixloopSettings
from fixloop_core.spec import TaskSpec
from fixloop_core.storage import repo
from fixloop_core.storage import tables as t
from fixloop_core.storage.db import Database
from fixloop_publisher import commands, webhooks

log = structlog.get_logger(__name__)
GITHUB_KINDS = ["github_publish", "github_comment", "github_command"]
MAX_ATTEMPTS = 8


def backoff(attempt: int) -> datetime:
    delay = min(3600, 15 * (2 ** max(0, attempt - 1))) * random.uniform(0.8, 1.2)
    return datetime.now(UTC) + timedelta(seconds=delay)


class PublisherService:
    def __init__(
        self,
        settings: FixloopSettings,
        config: AppConfig,
        db: Database,
        redis: Redis,
        blobs: ObjectStore,
        github: GitHubApp | None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.db = db
        self.redis = redis
        self.blobs = blobs
        self.github = github
        self.kinds = ["customer_webhook"] + (GITHUB_KINDS if github is not None else [])

    async def serve(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._outbox_loop(stop))]
        if self.github is not None:
            tasks.append(asyncio.create_task(self._token_loop(stop)))
        log.info("publisher_started", kinds=self.kinds)
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ outbox
    async def _outbox_loop(self, stop: asyncio.Event) -> None:
        last_requeue = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                if loop.time() - last_requeue > 60:
                    last_requeue = loop.time()
                    async with self.db.transaction() as s:
                        await repo.requeue_stuck_outbox(s, older_than_seconds=300)
                processed = await self.run_once()
            except Exception:
                log.exception("outbox_loop_failed")
                processed = 0
            if not processed:
                await asyncio.sleep(self.settings.publisher.poll_interval_s)

    async def run_once(self, limit: int = 10) -> int:
        async with self.db.transaction() as s:
            items = await repo.claim_outbox(s, self.kinds, limit)
            claimed = [
                (item.id, item.kind, dict(item.payload), item.attempts, item.tenant_id) for item in items
            ]
        await asyncio.gather(*(self._dispatch(*item) for item in claimed))
        return len(claimed)

    async def _dispatch(
        self,
        item_id: uuid.UUID,
        kind: str,
        payload: dict[str, Any],
        attempts: int,
        tenant_id: uuid.UUID | None,
    ) -> None:
        handler = {
            "github_publish": self.publish,
            "github_comment": self.comment,
            "github_command": self.command,
            "customer_webhook": self.webhook,
        }[kind]
        try:
            await handler(payload, tenant_id)
        except Exception as exc:
            permanent = isinstance(exc, (Forbidden, NotFound, webhooks.UnsafeCallback)) or (
                isinstance(exc, GitHubApiError) and exc.status in (401, 403, 404, 422)
            )
            retry_at = None if permanent or attempts >= MAX_ATTEMPTS else backoff(attempts)
            log.warning(
                "outbox_item_failed",
                kind=kind,
                attempts=attempts,
                permanent=permanent,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            async with self.db.transaction() as s:
                await repo.fail_outbox(s, item_id, f"{type(exc).__name__}: {exc}", retry_at)
            return
        async with self.db.transaction() as s:
            await repo.complete_outbox(s, item_id)

    # ------------------------------------------------------------------ handlers
    async def webhook(self, payload: dict[str, Any], tenant_id: uuid.UUID | None) -> None:
        async with self.db.session() as s:
            tenant = await s.get(t.Tenant, tenant_id or uuid.UUID(payload["tenant_id"]))
        if tenant is None:
            raise NotFound("the tenant no longer exists")
        status = await webhooks.deliver(
            payload["url"],
            tenant.webhook_secret,
            payload["event"],
            payload["data"],
            timeout_s=self.settings.publisher.webhook_timeout_s,
            allow_private=self.settings.publisher.allow_private_callbacks,
        )
        if not 200 <= status < 300:
            raise FixloopError(f"the callback answered {status}")

    async def comment(self, payload: dict[str, Any], _: uuid.UUID | None) -> None:
        assert self.github is not None
        token = await self.github.token(int(payload["installation_id"]), payload["repo"], COMMENT)
        await self.github.comment(payload["repo"], int(payload["issue_number"]), payload["body"], token)

    async def publish(self, payload: dict[str, Any], _: uuid.UUID | None) -> None:
        """Commit the verified files on a new branch and open a draft pull request (idempotent)."""
        assert self.github is not None
        repo_name, installation = payload["repo"], int(payload["installation_id"])
        token = await self.github.token(installation, repo_name, PUBLISH)
        files: dict[str, str | None] = json.loads(await self.blobs.get_bytes(payload["files_key"]))
        executable: set[str] = set()
        if payload.get("manifest_key"):
            manifest = json.loads(await self.blobs.get_bytes(payload["manifest_key"]))
            executable = {e["path"] for e in manifest.get("entries", []) if e.get("executable")}
        task_id = payload["task_id"]
        branch = f"fixloop/{task_id[:8]}"
        summary = (payload.get("summary") or "verified fix").strip()
        message = f"Fixloop: {summary[:60]}\n\n{summary}\n\nFixloop-Task: {task_id}"
        commit = await self.github.commit_files(
            repo_name, payload["base_sha"], files, executable, message, token
        )
        await self.github.upsert_branch(repo_name, branch, commit, token)
        base = payload.get("base_ref") or (await self.github.repository(repo_name, token))["default_branch"]
        verification = payload.get("verification") or {}
        body = "\n".join(
            [
                f"Fixloop verified this change in a sandbox (level `{verification.get('level', '?')}`).",
                "",
                f"**Summary:** {summary}",
                "",
                f"- Result: {verification.get('message', '')}",
                *[f"- Note: {w}" for w in (verification.get("warnings") or [])[:5]],
                "",
                f"Task: {payload.get('task_url') or task_id}",
                "",
                "_This is a draft: review it like any other contribution._",
            ]
        )
        pr = await self.github.open_draft_pr(repo_name, branch, base, f"Fixloop: {summary[:60]}", body, token)
        if payload.get("issue_number"):
            await self.github.comment(
                repo_name,
                int(payload["issue_number"]),
                f"✅ Fixloop opened draft PR #{pr['number']} with a verified fix: {pr['html_url']}",
                token,
            )
        log.info("published", repo=repo_name, pr=pr.get("number"), task_id=task_id)

    async def command(self, payload: dict[str, Any], _: uuid.UUID | None) -> None:
        """Handle `/fixloop fix ...`: permission check, then create the task on the right commit."""
        assert self.github is not None
        repo_name, installation = payload["repo"], int(payload["installation_id"])
        issue = int(payload["issue_number"])
        token = await self.github.token(installation, repo_name, {**COMMENT, "contents": "read"})

        async def reply(text: str) -> None:
            await self.github.comment(repo_name, issue, text, token)  # type: ignore[union-attr]

        try:
            parsed = commands.parse(payload["body"])
        except commands.CommandError as exc:
            await reply(f"⚠️ {exc}")
            return
        permission = await self.github.permission(repo_name, payload["author"], token)
        if permission not in commands.ALLOWED_PERMISSIONS:
            await reply(f"⚠️ @{payload['author']}, only collaborators with write access can run Fixloop here.")
            return
        async with self.db.session() as s:
            link = await repo.get_installation(s, installation)
            tenant = await s.get(t.Tenant, link.tenant_id) if link and link.tenant_id else None
        if tenant is None or (link and link.suspended_at):
            await reply(
                "⚠️ This GitHub App installation is not linked to a Fixloop tenant "
                "(`fixloop-admin link-installation`)."
            )
            return
        if payload.get("is_pull_request"):
            pr = await self.github.pull(repo_name, issue, token)
            if pr["head"]["repo"] is None or pr["head"]["repo"]["full_name"] != repo_name:
                await reply("⚠️ Pull requests from forks are not supported yet.")
                return
            ref, sha = pr["head"]["ref"], pr["head"]["sha"]
        else:
            ref = (await self.github.repository(repo_name, token))["default_branch"]
            sha = await self.github.branch_sha(repo_name, ref, token)
        spec = TaskSpec.model_validate(
            {
                "kind": "python.fix",
                "description": f"Requested by @{payload['author']} in {repo_name}#{issue}",
                "source": {
                    "type": "github",
                    "installation_id": installation,
                    "repo": repo_name,
                    "ref": ref,
                    "resolved_sha": sha,
                },
                "oracle": {"type": "pytest", "target_tests": parsed.targets},
                "options": {"max_repairs": parsed.repairs, "open_pr": True},
            }
        )
        context = {
            "installation_id": installation,
            "repo": repo_name,
            "issue_number": issue,
            "comment_id": payload.get("comment_id"),
            "author": payload["author"],
        }
        task, created = await create_task(
            self.db,
            self.redis,
            self.config,
            tenant=tenant,
            spec=spec,
            idempotency_key=f"github:{payload.get('delivery_id')}",
            github=context,
        )
        if created:
            await self.github.react(repo_name, int(payload.get("comment_id") or 0), "rocket", token)
            await reply(
                f"🔧 Fixloop is on it: task `{task.id}` on `{sha[:10]}` for "
                + ", ".join(f"`{x}`" for x in parsed.targets[:5])
                + "."
            )

    # ------------------------------------------------------------------ token requests from workers
    async def _token_loop(self, stop: asyncio.Event) -> None:
        consumer = StreamConsumer(
            self.redis,
            [GH_TOKEN_STREAM],
            PUBLISHERS_GROUP,
            f"publisher-{uuid.uuid4().hex[:6]}",
            block_ms=2000,
            claim_idle_ms=30_000,
        )
        await consumer.setup()
        while not stop.is_set():
            try:
                message = await consumer.next()
            except RedisError:
                await asyncio.sleep(2)
                continue
            if message is None:
                continue
            fields = message.fields
            reply: dict[str, str]
            try:
                reply = {"token": await self._read_token(int(fields["installation_id"]), fields["repo"])}
            except Exception as exc:
                reply = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            key = reply_key(fields.get("request_id", ""))
            await self.redis.lpush(key, json.dumps(reply))
            await self.redis.expire(key, 60)
            await consumer.ack(message)

    async def _read_token(self, installation_id: int, repo_name: str) -> str:
        assert self.github is not None
        async with self.db.session() as s:
            link = await repo.get_installation(s, installation_id)
        if link is None or link.suspended_at is not None:
            raise Forbidden("the installation is unknown or suspended")
        return await self.github.token(installation_id, repo_name, READ)
