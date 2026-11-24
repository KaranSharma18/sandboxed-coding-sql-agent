"""`fixloop-runner`: the sandbox runner process.

fixloop-runner              run the service (same as `fixloop-runner serve`)
fixloop-runner conformance  run the isolation self-test once and print the report
fixloop-runner reap         remove leftover sandboxes, volumes and stale environments
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys

import typer

from fixloop_core.config import load_config
from fixloop_core.objectstore import build_object_store
from fixloop_core.observability import configure_logging, start_metrics
from fixloop_core.settings import FixloopSettings, get_settings
from fixloop_runner.backends.base import SandboxBackend

app = typer.Typer(add_completion=False, help="Fixloop sandbox runner")


def build_backend(settings: FixloopSettings) -> SandboxBackend:
    problems = settings.check_production_safety()
    if problems:
        raise SystemExit("refusing to start: " + "; ".join(problems))
    if settings.runner.backend == "local":
        from fixloop_runner.backends.local import LocalBackend

        return LocalBackend(settings.runner)
    from fixloop_runner.backends.docker import DockerBackend

    try:
        return DockerBackend(settings.runner)
    except Exception as exc:
        raise SystemExit(
            f"cannot reach Docker ({type(exc).__name__}: {exc}). Start Docker Desktop, or set "
            "FIXLOOP_RUNNER__DOCKER_HOST."
        ) from exc


async def _serve() -> None:
    from redis.asyncio import Redis

    from fixloop_runner.service import RunnerService

    settings = get_settings()
    configure_logging(settings.observability, "runner")
    start_metrics(settings.observability.metrics_port)
    config = load_config(settings.config_dir)
    backend = build_backend(settings)
    store = build_object_store(
        settings.storage, settings.api.public_url, settings.api.upload_token_secret.get_secret_value()
    )
    redis = Redis.from_url(settings.redis.queue_url, socket_timeout=30, health_check_interval=30)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: stop.set())
    service = RunnerService(settings, config, backend, store, redis)
    try:
        await service.serve(stop)
    finally:
        await redis.aclose()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(_serve())


@app.command()
def serve() -> None:
    """Run the runner service."""
    asyncio.run(_serve())


@app.command()
def conformance() -> None:
    """Run the isolation self-test once. Exits 1 when a required check fails."""
    from fixloop_runner.conformance import run_conformance
    from fixloop_runner.service import summarize

    settings = get_settings()
    config = load_config(settings.config_dir)
    backend = build_backend(settings)
    report = asyncio.run(run_conformance(backend, config, settings.runner))
    typer.echo(json.dumps(summarize(report), indent=2))
    if report.unsafe:
        typer.echo("\nThe local backend has no isolation: it is for tests and your own code only.", err=True)
    sys.exit(0 if report.passed else 1)


@app.command()
def reap(
    all_mine: bool = typer.Option(False, "--all", help="also remove in-flight sandboxes of this runner"),
) -> None:
    """Remove leftover sandboxes, volumes and expired environments."""
    settings = get_settings()
    backend = build_backend(settings)
    removed = asyncio.run(backend.reap(startup=all_mine))
    typer.echo(f"removed {removed} item(s)")
