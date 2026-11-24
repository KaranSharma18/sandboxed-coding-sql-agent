"""An allowlisted, wheels-only PyPI proxy (PEP 503 HTML and PEP 691 JSON simple API).

Why it exists (ADR-14): dependency installation is the one moment sandbox code needs the
network. Instead of opening the internet, the prepare sandbox sees exactly one host, and that
host only serves:
  * projects on `config/allowlist.txt` (everything else is a 404),
  * wheels only (an sdist runs arbitrary code in its build step; a wheel is only unpacked),
  * files whose SHA-256 matches what the upstream index published (checked on every download,
    cached by hash afterwards),
  * PEP 658 metadata files, so resolvers do not need to download wheels to resolve.
Every file served is logged (project, file, hash) for audit.

Settings (environment): PKGPROXY_UPSTREAM (https://pypi.org), PKGPROXY_ALLOWLIST
(config/allowlist.txt), PKGPROXY_CACHE_DIR (.fixloop-data/pkgcache), PKGPROXY_PORT (8080).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

log = logging.getLogger("fixloop.pkgproxy")
JSON_TYPE = "application/vnd.pypi.simple.v1+json"
_NORMALIZE = re.compile(r"[-_.]+")
INDEX_TTL_S = 600


def normalize(name: str) -> str:
    return _NORMALIZE.sub("-", name).lower().strip()


def read_allowlist(path: Path) -> frozenset[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(normalize(line))
    return frozenset(names)


@dataclass
class FileEntry:
    filename: str
    url: str
    sha256: str
    requires_python: str | None = None
    metadata_sha256: str | None = None
    yanked: bool | str = False


@dataclass
class Proxy:
    upstream: str
    allowlist: frozenset[str]
    cache_dir: Path
    client: httpx.AsyncClient
    _index: dict[str, tuple[float, list[FileEntry]]] = field(default_factory=dict)

    async def files(self, project: str) -> list[FileEntry]:
        cached = self._index.get(project)
        if cached and time.time() - cached[0] < INDEX_TTL_S:
            return cached[1]
        url = f"{self.upstream}/simple/{project}/"
        response = await self.client.get(url, headers={"Accept": JSON_TYPE})
        if response.status_code == 404:
            raise HTTPException(404, f"{project} does not exist upstream")
        response.raise_for_status()
        data = response.json()
        entries: list[FileEntry] = []
        for item in data.get("files", []):
            filename = item.get("filename", "")
            sha = (item.get("hashes") or {}).get("sha256")
            if not filename.endswith(".whl") or not sha:
                continue  # wheels with a published hash only
            metadata = item.get("core-metadata") or item.get("dist-info-metadata")
            entries.append(
                FileEntry(
                    filename=filename,
                    url=urljoin(url, item["url"]),
                    sha256=sha,
                    requires_python=item.get("requires-python"),
                    metadata_sha256=metadata.get("sha256") if isinstance(metadata, dict) else None,
                    yanked=item.get("yanked", False),
                )
            )
        self._index[project] = (time.time(), entries)
        return entries

    def check(self, project: str) -> str:
        name = normalize(project)
        if name not in self.allowlist:
            log.info("refused project=%s reason=not-allowlisted", name)
            raise HTTPException(404, f"{name} is not on the package allowlist")
        return name

    async def entry(self, project: str, filename: str) -> FileEntry:
        for entry in await self.files(project):
            if entry.filename == filename:
                return entry
        raise HTTPException(404, f"{filename} is not a wheel of {project}")

    async def fetch(self, url: str, sha256: str, target: Path) -> Path:
        if await asyncio.to_thread(target.exists):
            return target
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        tmp = target.with_suffix(target.suffix + f".{os.getpid()}.part")
        handle = await asyncio.to_thread(tmp.open, "wb")
        try:
            async with self.client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
        if digest.hexdigest() != sha256:
            tmp.unlink(missing_ok=True)
            log.error("hash_mismatch url=%s expected=%s got=%s", url, sha256, digest.hexdigest())
            raise HTTPException(502, "the upstream file does not match its published hash")
        os.replace(tmp, target)
        return target


def create_app(
    upstream: str | None = None,
    allowlist_path: Path | None = None,
    cache_dir: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    upstream = (upstream or os.environ.get("PKGPROXY_UPSTREAM", "https://pypi.org")).rstrip("/")
    allowlist_path = allowlist_path or Path(os.environ.get("PKGPROXY_ALLOWLIST", "config/allowlist.txt"))
    cache_dir = cache_dir or Path(os.environ.get("PKGPROXY_CACHE_DIR", ".fixloop-data/pkgcache"))
    proxy = Proxy(
        upstream=upstream,
        allowlist=read_allowlist(allowlist_path),
        cache_dir=cache_dir,
        client=httpx.AsyncClient(timeout=60, follow_redirects=True, transport=transport),
    )
    app = FastAPI(title="Fixloop package proxy", docs_url=None, redoc_url=None)
    app.state.proxy = proxy

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "projects": len(proxy.allowlist)}

    @app.get("/simple/")
    async def index(request: Request) -> Response:
        names = sorted(proxy.allowlist)
        if JSON_TYPE in request.headers.get("accept", ""):
            return JSONResponse(
                {"meta": {"api-version": "1.1"}, "projects": [{"name": n} for n in names]},
                media_type=JSON_TYPE,
            )
        links = "\n".join(f'<a href="/simple/{n}/">{n}</a><br/>' for n in names)
        return HTMLResponse(f"<!DOCTYPE html><html><body>{links}</body></html>")

    @app.get("/simple/{project}/")
    async def project_page(project: str, request: Request) -> Response:
        name = proxy.check(project)
        entries = await proxy.files(name)
        if JSON_TYPE in request.headers.get("accept", ""):
            files = []
            for e in entries:
                item: dict[str, Any] = {
                    "filename": e.filename,
                    "url": f"/files/{name}/{e.filename}",
                    "hashes": {"sha256": e.sha256},
                    "yanked": e.yanked,
                }
                if e.requires_python:
                    item["requires-python"] = e.requires_python
                if e.metadata_sha256:
                    item["core-metadata"] = {"sha256": e.metadata_sha256}
                files.append(item)
            return JSONResponse(
                {"meta": {"api-version": "1.1"}, "name": name, "files": files}, media_type=JSON_TYPE
            )
        rows = []
        for e in entries:
            attrs = f' data-requires-python="{html.escape(e.requires_python)}"' if e.requires_python else ""
            if e.metadata_sha256:
                attrs += f' data-core-metadata="sha256={e.metadata_sha256}"'
            if e.yanked:
                attrs += ' data-yanked=""'
            rows.append(
                f'<a href="/files/{name}/{e.filename}#sha256={e.sha256}"{attrs}>{e.filename}</a><br/>'
            )
        return HTMLResponse("<!DOCTYPE html><html><body>\n" + "\n".join(rows) + "\n</body></html>")

    @app.get("/files/{project}/{filename}")
    async def file(project: str, filename: str) -> Response:
        name = proxy.check(project)
        if filename.endswith(".metadata"):
            entry = await proxy.entry(name, filename[: -len(".metadata")])
            if not entry.metadata_sha256:
                raise HTTPException(404, "no metadata file for this wheel")
            path = await proxy.fetch(
                entry.url + ".metadata",
                entry.metadata_sha256,
                proxy.cache_dir / "metadata" / entry.metadata_sha256,
            )
            return FileResponse(path, media_type="text/plain")
        entry = await proxy.entry(name, filename)
        if not urlsplit(entry.url).scheme.startswith("http"):
            raise HTTPException(502, "unexpected upstream URL")
        path = await proxy.fetch(
            entry.url, entry.sha256, proxy.cache_dir / "wheels" / entry.sha256[:2] / entry.sha256
        )
        log.info("served project=%s file=%s sha256=%s", name, filename, entry.sha256)
        return FileResponse(path, media_type="application/octet-stream", filename=filename)

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("PKGPROXY_LOG_LEVEL", "INFO"),
        format=json.dumps({"ts": "%(asctime)s", "level": "%(levelname)s", "msg": "%(message)s"}),
    )
    uvicorn.run(
        create_app(),
        host=os.environ.get("PKGPROXY_HOST", "0.0.0.0"),  # noqa: S104 - container service
        port=int(os.environ.get("PKGPROXY_PORT", "8080")),
    )


if __name__ == "__main__":
    run()
