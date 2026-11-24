"""Logging, metrics and tracing setup shared by every service (design section 20).

- Logs: structured JSON via structlog, with a redaction processor for anything that looks like a
  key or token. Customer code and prompt text never go to logs.
- Metrics: Prometheus, defined once here so every service exports the same names.
- Tracing: OpenTelemetry when `FIXLOOP_OBSERVABILITY__OTLP_ENDPOINT` is set and the `otel` extra is installed.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from fixloop_core.settings import ObservabilitySettings

_SECRET_VALUE = re.compile(
    r"(fl_live_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z\-_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
)
_SECRET_KEYS = re.compile(r"(api_?key|token|secret|password|authorization|private_key)", re.I)


def _redact(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(event.items()):
        if _SECRET_KEYS.search(key):
            event[key] = "<redacted>"
        elif isinstance(value, str):
            event[key] = _SECRET_VALUE.sub("<redacted>", value)
    return event


def configure_logging(settings: ObservabilitySettings, service: str) -> structlog.stdlib.BoundLogger:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "docker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer = structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer()
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.format_exc_info,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(service=service)


def start_metrics(port: int | None) -> None:
    if port:
        start_http_server(port)


def configure_tracing(settings: ObservabilitySettings, service: str) -> None:
    if not settings.otlp_endpoint:
        return
    try:
        from opentelemetry import trace

        # Optional extra (`fixloop-core[otel]`): absent in the default install.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # pyright: ignore[reportMissingImports]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # pyright: ignore[reportMissingImports]
    except ImportError:
        structlog.get_logger().warning("otel_not_installed", hint="install fixloop-core[otel]")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint)))
    trace.set_tracer_provider(provider)


# ------------------------------------------------------------------------------------ metrics (design s20)
TASKS = Counter("fixloop_tasks_total", "Finished tasks", ["kind", "outcome"])
TASK_DURATION = Histogram(
    "fixloop_task_duration_seconds",
    "Task wall time",
    ["kind", "outcome"],
    buckets=(5, 15, 30, 60, 120, 180, 300, 600, 900, 1800),
)
ATTEMPTS_USED = Histogram(
    "fixloop_attempts_used", "Candidate attempts per task", ["kind", "outcome"], buckets=(0, 1, 2, 3, 4)
)
LLM_CALLS = Counter("fixloop_llm_calls_total", "Model calls", ["role", "model", "result"])
LLM_TOKENS = Counter("fixloop_llm_tokens_total", "Model tokens", ["model", "type"])
LLM_COST = Counter("fixloop_llm_cost_usd_total", "Model spend", ["model", "role"])
LLM_LATENCY = Histogram(
    "fixloop_llm_latency_seconds",
    "Model latency",
    ["role", "model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160),
)
LLM_VALIDATION_FAILURES = Counter("fixloop_llm_validation_failures_total", "Schema failures", ["role"])
EXECUTIONS = Counter("fixloop_exec_total", "Sandbox executions", ["purpose", "status"])
EXEC_SECONDS = Histogram(
    "fixloop_exec_seconds",
    "Sandbox execution time",
    ["purpose"],
    buckets=(1, 2, 5, 10, 20, 40, 80, 160, 320, 640),
)
EXEC_QUEUE_WAIT = Histogram("fixloop_exec_queue_wait_seconds", "Wait before a runner picked a request")
SLOTS_IN_USE = Gauge("fixloop_sandbox_slots_in_use", "Busy sandbox slots", ["runner"])
CONFORMANCE_FAILURES = Counter(
    "fixloop_conformance_failures_total", "Isolation self-test failures", ["check"]
)
QUEUE_DEPTH = Gauge("fixloop_queue_depth", "Undelivered messages", ["stream"])
QUEUE_OLDEST = Gauge("fixloop_queue_oldest_seconds", "Age of the oldest undelivered message", ["stream"])
BUDGET_EXHAUSTED = Counter("fixloop_budget_exhausted_total", "Budget stops", ["dimension"])
POLICY_VIOLATIONS = Counter("fixloop_policy_violations_total", "Policy violations", ["code"])
