"""What every sandbox backend implements, and the types shared between backends."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fixloop_core.models import ExecStatus, Limits
from fixloop_runner.workspace import Workspace

# Only these variables reach a sandbox. Nothing is inherited from the runner's own environment,
# so no credential the runner holds can leak into code under test.
BASE_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
}


@dataclass
class RunSpec:
    exec_id: str
    tenant_id: str
    family: str  # python | sql
    image: str  # logical image name, for example python-3.12
    image_ref: str  # concrete image (tag or digest)
    argv: list[str]
    workspace: Workspace
    env_name: str | None
    env_writable: bool
    network: str  # none | proxy
    limits: Limits
    environment: dict[str, str] = field(default_factory=dict)
    protected_paths: list[str] = field(default_factory=list)
    deadline_ts: float = 0.0
    cancel_check: Callable[[], Awaitable[bool]] | None = None
    purpose: str = ""
    task_id: str = ""


@dataclass
class RunOutput:
    status: ExecStatus
    exit_code: int | None
    duration_s: float
    stdout: bytes = b""
    stderr: bytes = b""
    files: dict[str, bytes] = field(default_factory=dict)
    truncated: dict[str, bool] = field(default_factory=dict)
    protected_hashes: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    image_digest: str = ""


@dataclass
class ConformanceReport:
    backend: str
    isolation: str
    passed: bool
    checks: dict[str, dict[str, Any]]
    unsafe: bool = False

    def failures(self) -> list[str]:
        return [
            name for name, check in self.checks.items() if check.get("required", True) and not check.get("ok")
        ]


class SandboxBackend(Protocol):
    name: str

    def placeholders(self, family: str, env_name: str | None, image: str, exec_id: str) -> dict[str, str]: ...
    async def env_metadata(self, env_name: str) -> dict[str, Any] | None: ...
    async def mark_env_ready(self, env_name: str, metadata: dict[str, Any], image: str) -> None: ...
    async def delete_env(self, env_name: str) -> None: ...
    async def run(self, spec: RunSpec) -> RunOutput: ...
    async def reap(self, startup: bool = False) -> int: ...
    async def close(self) -> None: ...


class CappedBuffer:
    """Keep the head and the tail of a stream when it is larger than the cap.

    The head usually holds collection errors and the tail holds the summary; the middle is the
    part nobody needs, so that is what gets dropped.
    """

    def __init__(self, cap: int) -> None:
        self.cap = max(cap, 1024)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def write(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.cap // 2 - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.cap // 2:
                del self.tail[: len(self.tail) - self.cap // 2]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def value(self) -> bytes:
        if self.truncated:
            return bytes(self.head) + b"\n...[output truncated]...\n" + bytes(self.tail)
        return bytes(self.head) + bytes(self.tail)


class EnvIndex:
    """Which prepared environments this runner has, with their metadata and last use.

    Environments are cached per runner (ADR-15): the index lives next to them on the runner's
    disk. An environment that exists without an index entry is treated as unknown and rebuilt,
    because nothing proves its preparation finished.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def get(self, name: str) -> dict[str, Any] | None:
        return self._data.get(name)

    def names(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    async def put(self, name: str, metadata: dict[str, Any], image: str) -> None:
        async with self._lock:
            now = time.time()
            self._data[name] = {"metadata": metadata, "image": image, "created": now, "last_used": now}
            self._save()

    async def touch(self, name: str) -> None:
        async with self._lock:
            if name in self._data and time.time() - self._data[name].get("last_used", 0) > 60:
                self._data[name]["last_used"] = time.time()
                self._save()

    async def remove(self, name: str) -> None:
        async with self._lock:
            if self._data.pop(name, None) is not None:
                self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self.path)


def output_files_from_tar(data: bytes, prefix: str, cap: int) -> tuple[dict[str, bytes], bool]:
    """Regular files from a `get_archive` tar, under `prefix/`. Links and devices are ignored."""
    import io
    import tarfile

    files: dict[str, bytes] = {}
    total = 0
    truncated = False
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar:
            if not member.isreg():
                continue
            name = member.name
            if name.startswith(prefix + "/"):
                name = name[len(prefix) + 1 :]
            elif name == prefix:
                continue
            if not name or ".." in name.split("/") or name.startswith("/"):
                continue
            if total + member.size > cap:
                truncated = True
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            files[name] = handle.read()
            total += member.size
    return files, truncated
