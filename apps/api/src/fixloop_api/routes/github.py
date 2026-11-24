"""GitHub App webhooks (design section 12).

The handler does as little as possible inside the request: verify the signature, drop
redeliveries, record installation changes, and turn a `/fixloop ...` comment into an outbox
item. Permission checks, resolving the pull request's head commit and creating the task happen
in the publisher, which holds the App's credentials. GitHub gets its 2xx within milliseconds.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import structlog
from fastapi import APIRouter, Depends, Header, Request

from fixloop_api.auth import get_state
from fixloop_api.state import ApiState
from fixloop_core.errors import Forbidden, NotFound, Unauthorized
from fixloop_core.models import OutboxItem
from fixloop_core.storage import repo

router = APIRouter(tags=["github"])
log = structlog.get_logger(__name__)
COMMAND_PREFIX = "/fixloop"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@router.post("/v1/github/webhook", status_code=202, summary="GitHub App webhook receiver")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
    state: ApiState = Depends(get_state),
) -> dict[str, str]:
    settings = state.settings.github
    if not settings.enabled or settings.webhook_secret is None:
        raise NotFound("the GitHub App is not configured")
    body = await request.body()
    if not verify_signature(settings.webhook_secret.get_secret_value(), body, x_hub_signature_256):
        raise Unauthorized("bad webhook signature")
    if not x_github_delivery:
        raise Forbidden("missing X-GitHub-Delivery")
    payload = json.loads(body)
    async with state.db.transaction() as s:
        if not await repo.record_delivery(s, "github", x_github_delivery):
            return {"status": "duplicate"}
        installation = (payload.get("installation") or {}).get("id")
        action = payload.get("action", "")
        if x_github_event == "installation" and installation:
            account = (payload["installation"].get("account") or {}).get("login", "")
            if action in ("created", "new_permissions_accepted", "unsuspend"):
                await repo.upsert_installation(
                    s,
                    int(installation),
                    account,
                    payload["installation"].get("permissions", {}),
                    payload["installation"].get("repository_selection", "selected"),
                )
            elif action in ("suspend", "deleted"):
                await repo.set_installation_suspended(s, int(installation), True)
                cancelled = await repo.cancel_installation_tasks(s, int(installation))
                log.info(
                    "installation_disabled", installation=installation, action=action, cancelled=cancelled
                )
            return {"status": "ok"}
        if x_github_event == "issue_comment" and action == "created" and installation:
            comment = payload.get("comment") or {}
            text = (comment.get("body") or "").strip()
            if not text.startswith(COMMAND_PREFIX):
                return {"status": "ignored"}
            if (comment.get("user") or {}).get("type") == "Bot":
                return {"status": "ignored"}  # never react to bots, including ourselves
            issue = payload.get("issue") or {}
            item = OutboxItem(
                kind="github_command",
                idempotency_key=f"github-command:{x_github_delivery}",
                payload={
                    "installation_id": int(installation),
                    "repo": (payload.get("repository") or {}).get("full_name"),
                    "issue_number": issue.get("number"),
                    "is_pull_request": "pull_request" in issue,
                    "comment_id": comment.get("id"),
                    "author": (comment.get("user") or {}).get("login"),
                    "body": text[:2000],
                    "delivery_id": x_github_delivery,
                },
            )
            await repo.add_outbox(s, item, task_id=None, tenant_id=None)
            return {"status": "queued"}
    return {"status": "ignored"}
