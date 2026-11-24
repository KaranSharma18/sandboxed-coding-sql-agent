"""A small synchronous client for the Fixloop API (no dependency on the server packages)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx


class FixloopError(Exception):
    """An API error, carrying the problem details the server returned."""

    def __init__(self, status: int, code: str, detail: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.body = body or {}


class FixloopClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "fixloop-client/0.1"},
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> FixloopClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ plumbing
    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"detail": response.text[:500]}
            raise FixloopError(
                response.status_code, str(body.get("code", "error")), str(body.get("detail", "")), body
            )
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code in (429, 503) and attempt < 3:
                time.sleep(min(30.0, float(response.headers.get("Retry-After", "2"))))
                continue
            return self._check(response)
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------ tasks
    def create_task(self, spec: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
        return self._request("POST", "/v1/tasks", json=spec, headers=headers).json()

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/tasks/{task_id}").json()

    def list_tasks(self, limit: int = 20, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return self._request("GET", "/v1/tasks", params=params).json()

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/tasks/{task_id}/attempts").json()["items"]

    def patch(self, task_id: str) -> tuple[str, bool]:
        response = self._request("GET", f"/v1/tasks/{task_id}/patch")
        return response.text, response.headers.get("X-Fixloop-Verified") == "true"

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/tasks/{task_id}/cancel").json()

    def retry(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/tasks/{task_id}/retry").json()

    def usage(self, days: int = 30) -> dict[str, Any]:
        return self._request("GET", "/v1/usage", params={"days": days}).json()

    # ------------------------------------------------------------------ uploads
    def upload(self, data: bytes, kind: str, content_type: str = "application/octet-stream") -> str:
        started = self._request(
            "POST", "/v1/uploads", json={"kind": kind, "content_type": content_type, "size_bytes": len(data)}
        ).json()
        with httpx.Client(timeout=300) as raw:
            self._check(raw.put(started["upload_url"], content=data, headers=started.get("headers") or {}))
        return str(started["upload_id"])

    # ------------------------------------------------------------------ events
    def events(self, task_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        """Follow a task's events (Server-Sent Events), reconnecting with Last-Event-ID if dropped."""
        last = after
        while True:
            try:
                with self.http.stream(
                    "GET",
                    f"/v1/tasks/{task_id}/events",
                    params={"after": last},
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(60, read=60),
                ) as r:
                    if r.status_code >= 400:
                        r.read()
                        self._check(r)
                    event: dict[str, Any] = {}
                    for line in r.iter_lines():
                        if line.startswith("id: "):
                            event["id"] = int(line[4:])
                        elif line.startswith("data: "):
                            event["data"] = json.loads(line[6:])
                        elif line == "" and "data" in event:
                            last = event.get("id", last)
                            yield event["data"]
                            if event["data"].get("type") == "task.finished":
                                return
                            event = {}
            except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError):
                time.sleep(1.0)
                continue
            task = self.get_task(task_id)  # the stream ended without a finish event: check, then resume
            if task["status"] in ("succeeded", "failed", "cancelled"):
                return

    def wait(self, task_id: str, poll_s: float = 2.0, timeout_s: float = 3600) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            task = self.get_task(task_id)
            if task["status"] in ("succeeded", "failed", "cancelled"):
                return task
            time.sleep(poll_s)
        raise TimeoutError(f"task {task_id} did not finish in {timeout_s}s")
