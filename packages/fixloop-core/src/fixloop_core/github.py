"""A small GitHub App client (publisher side): JWTs, installation tokens and the REST calls we use.

Least privilege by construction: every installation token is minted for *one* repository and
only the permissions the operation needs (`contents: read` for the worker's tarball download;
`contents`, `pull_requests` and `issues: write` for publishing). Tokens are cached until five
minutes before they expire.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt

from fixloop_core.errors import Forbidden, InfraError, NotFound
from fixloop_core.settings import GitHubSettings

READ = {"contents": "read"}
PUBLISH = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}
COMMENT = {"issues": "write", "pull_requests": "write", "metadata": "read"}
API_VERSION = "2022-11-28"


@dataclass
class Token:
    value: str
    expires_at: float


class GitHubApiError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.body = body


class GitHubApp:
    def __init__(self, settings: GitHubSettings, http: httpx.AsyncClient | None = None) -> None:
        if settings.app_id is None:
            raise ValueError("FIXLOOP_GITHUB__APP_ID is required")
        key = settings.private_key.get_secret_value() if settings.private_key else None
        if key is None and settings.private_key_path is not None:
            key = settings.private_key_path.read_text(encoding="utf-8")
        if not key:
            raise ValueError("a GitHub App private key is required (FIXLOOP_GITHUB__PRIVATE_KEY_PATH)")
        self.app_id = settings.app_id
        self.private_key = key
        self.api = settings.api_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=30)
        self._tokens: dict[tuple[int, str, str], Token] = {}

    async def close(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ auth
    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}, self.private_key, algorithm="RS256"
        )

    async def token(self, installation_id: int, repo: str, permissions: dict[str, str]) -> str:
        cache_key = (installation_id, repo, ",".join(f"{k}:{v}" for k, v in sorted(permissions.items())))
        cached = self._tokens.get(cache_key)
        if cached and cached.expires_at - time.time() > 300:
            return cached.value
        response = await self.http.post(
            f"{self.api}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            json={"repositories": [repo.split("/", 1)[1]], "permissions": permissions},
        )
        if response.status_code in (403, 404, 422):
            raise Forbidden(f"the GitHub App cannot access {repo} with {permissions}: {response.text[:200]}")
        if response.status_code >= 400:
            raise InfraError(f"GitHub token request failed with {response.status_code}", retryable=True)
        data = response.json()
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[cache_key] = Token(data["token"], expires)
        return data["token"]

    # ------------------------------------------------------------------ REST helpers
    async def call(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        response = await self.http.request(
            method,
            f"{self.api}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            **kwargs,
        )
        if response.status_code == 404:
            raise NotFound(f"GitHub: {path} not found")
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"message": response.text[:300]}
            raise GitHubApiError(response.status_code, str(body.get("message", "")), body)
        return response.json() if response.content else None

    async def permission(self, repo: str, user: str, token: str) -> str:
        data = await self.call("GET", f"/repos/{repo}/collaborators/{user}/permission", token)
        return str(data.get("permission", "none"))

    async def pull(self, repo: str, number: int, token: str) -> dict[str, Any]:
        return await self.call("GET", f"/repos/{repo}/pulls/{number}", token)

    async def repository(self, repo: str, token: str) -> dict[str, Any]:
        return await self.call("GET", f"/repos/{repo}", token)

    async def branch_sha(self, repo: str, branch: str, token: str) -> str:
        data = await self.call("GET", f"/repos/{repo}/commits/{branch}", token)
        return str(data["sha"])

    async def comment(self, repo: str, issue_number: int, body: str, token: str) -> dict[str, Any]:
        return await self.call(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", token, json={"body": body}
        )

    async def react(self, repo: str, comment_id: int, content: str, token: str) -> None:
        try:
            await self.call(
                "POST",
                f"/repos/{repo}/issues/comments/{comment_id}/reactions",
                token,
                json={"content": content},
            )
        except (GitHubApiError, NotFound):
            pass  # a missing reaction is cosmetic

    # ------------------------------------------------------------------ publishing (Git Data API)
    async def commit_files(
        self,
        repo: str,
        base_sha: str,
        files: dict[str, str | None],
        executable: set[str],
        message: str,
        token: str,
    ) -> str:
        """Create a commit on top of `base_sha` that sets (or deletes) exactly `files`."""
        base = await self.call("GET", f"/repos/{repo}/git/commits/{base_sha}", token)
        entries = []
        for path in sorted(files):
            content = files[path]
            if content is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = await self.call(
                "POST",
                f"/repos/{repo}/git/blobs",
                token,
                json={"content": base64.b64encode(content.encode("utf-8")).decode(), "encoding": "base64"},
            )
            entries.append(
                {
                    "path": path,
                    "mode": "100755" if path in executable else "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree = await self.call(
            "POST",
            f"/repos/{repo}/git/trees",
            token,
            json={"base_tree": base["tree"]["sha"], "tree": entries},
        )
        commit = await self.call(
            "POST",
            f"/repos/{repo}/git/commits",
            token,
            json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
        )
        return str(commit["sha"])

    async def upsert_branch(self, repo: str, branch: str, sha: str, token: str) -> None:
        try:
            await self.call(
                "POST", f"/repos/{repo}/git/refs", token, json={"ref": f"refs/heads/{branch}", "sha": sha}
            )
        except GitHubApiError as exc:
            if exc.status != 422:
                raise
            await self.call(
                "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", token, json={"sha": sha, "force": True}
            )

    async def open_draft_pr(
        self, repo: str, branch: str, base: str, title: str, body: str, token: str
    ) -> dict[str, Any]:
        try:
            return await self.call(
                "POST",
                f"/repos/{repo}/pulls",
                token,
                json={
                    "title": title,
                    "head": branch,
                    "base": base,
                    "body": body,
                    "draft": True,
                    "maintainer_can_modify": True,
                },
            )
        except GitHubApiError as exc:
            if exc.status != 422:
                raise
            if "draft" in str(exc.body).lower():  # drafts are not available on every plan
                return await self.call(
                    "POST",
                    f"/repos/{repo}/pulls",
                    token,
                    json={
                        "title": title,
                        "head": branch,
                        "base": base,
                        "body": body,
                        "maintainer_can_modify": True,
                    },
                )
            owner = repo.split("/", 1)[0]
            existing = await self.call(
                "GET", f"/repos/{repo}/pulls", token, params={"head": f"{owner}:{branch}", "state": "open"}
            )
            if existing:
                return existing[0]
            raise
