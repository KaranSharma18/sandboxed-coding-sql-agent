"""Reading repositories for GitHub-sourced tasks (worker side).

The worker never holds the GitHub App's private key. It asks the publisher for a short-lived
installation token restricted to `contents: read` on one repository, over a Redis
request/reply stream, and downloads the tarball for an exact commit SHA (ADR-23).
"""

from __future__ import annotations

import json
import uuid

import httpx
from redis.asyncio import Redis

from fixloop_core.errors import InfraError, InvalidInput
from fixloop_core.queue import GH_TOKEN_STREAM, publish


def reply_key(request_id: str) -> str:
    return f"fixloop:ghtoken:reply:{request_id}"


class GitHubSourceClient:
    def __init__(
        self, redis: Redis, api_url: str = "https://api.github.com", timeout_s: float = 60.0
    ) -> None:
        self.redis = redis
        self.api_url = api_url.rstrip("/")
        self.timeout_s = timeout_s

    async def token(self, installation_id: int, repo: str) -> str:
        request_id = uuid.uuid4().hex
        await publish(
            self.redis,
            GH_TOKEN_STREAM,
            {"request_id": request_id, "installation_id": str(installation_id), "repo": repo},
        )
        reply = await self.redis.blpop([reply_key(request_id)], timeout=30)
        if reply is None:
            raise InfraError("the publisher did not provide a GitHub token in time", retryable=True)
        data = json.loads(reply[1])
        if data.get("error"):
            raise InvalidInput(f"cannot read {repo}: {data['error']}")
        return str(data["token"])

    async def tarball(self, installation_id: int, repo: str, sha: str, max_bytes: int) -> bytes:
        token = await self.token(installation_id, repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        chunks: list[bytes] = []
        total = 0
        async with (
            httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True) as http,
            http.stream("GET", f"{self.api_url}/repos/{repo}/tarball/{sha}", headers=headers) as response,
        ):
            if response.status_code == 404:
                raise InvalidInput(f"{repo}@{sha} was not found (or the app cannot read it)")
            if response.status_code >= 400:
                raise InfraError(f"GitHub returned {response.status_code} for the tarball", retryable=True)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise InvalidInput(
                        f"the repository archive is larger than {max_bytes // (1024 * 1024)} MB"
                    )
                chunks.append(chunk)
        return b"".join(chunks)
