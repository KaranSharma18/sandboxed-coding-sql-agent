"""`fixloop-api`: the public HTTP API.

uvicorn fixloop_api.main:app        (or simply `fixloop-api`)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from fixloop_api import problems
from fixloop_api.routes import github, system, tasks, uploads
from fixloop_api.state import ApiState
from fixloop_core.observability import configure_logging, configure_tracing
from fixloop_core.settings import FixloopSettings, get_settings

log = structlog.get_logger(__name__)


def create_app(settings: FixloopSettings | None = None, state: ApiState | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        problems_found = settings.check_production_safety()
        if problems_found:
            raise SystemExit("refusing to start: " + "; ".join(problems_found))
        app.state.fixloop = state or ApiState.build(settings)
        try:
            yield
        finally:
            if state is None:
                await app.state.fixloop.close()

    app = FastAPI(
        title="Fixloop API",
        version="0.1.0",
        lifespan=lifespan,
        description="Sandboxed autonomous coding and SQL verification agent. Submit a failing test or a "
        "question about a database snapshot; get back a change that is proven by execution.",
    )
    problems.install(app)
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.api.cors_origins, allow_methods=["*"], allow_headers=["*"]
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-Id"] = request_id
        if request.url.path not in ("/healthz", "/metrics"):
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=int((time.monotonic() - started) * 1000),
            )
        return response

    for router in (tasks.router, tasks.artifact_router, uploads.router, github.router, system.router):
        app.include_router(router)
    return app


def _app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.observability, "api")
    configure_tracing(settings.observability, "api")
    return create_app(settings)


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "fixloop_api.main:_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )


if __name__ == "__main__":
    run()
