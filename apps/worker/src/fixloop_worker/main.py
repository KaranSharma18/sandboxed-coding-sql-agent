"""`fixloop-worker`: the task worker process."""

from __future__ import annotations

import asyncio
import signal

from fixloop_core.observability import configure_logging, configure_tracing, start_metrics
from fixloop_core.settings import get_settings
from fixloop_worker.service import WorkerService
from fixloop_worker.wiring import build_resources


async def _serve() -> None:
    settings = get_settings()
    problems = settings.check_production_safety()
    if problems:
        raise SystemExit("refusing to start: " + "; ".join(problems))
    configure_logging(settings.observability, "worker")
    configure_tracing(settings.observability, "worker")
    start_metrics(settings.observability.metrics_port)
    resources = build_resources(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: stop.set())
    try:
        await WorkerService(resources).serve(stop)
    finally:
        await resources.close()


def run() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    run()
