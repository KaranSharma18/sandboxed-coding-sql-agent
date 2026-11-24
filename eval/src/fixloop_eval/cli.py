"""`fixloop-eval`: run a suite against a live Fixloop API, with a hard cost cap, and report.

uv run fixloop-eval run --suite fixtures --limit 10 --max-cost-usd 2
uv run fixloop-eval report eval/results/<run>.jsonl
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from fixloop_client.cli import client as make_client
from fixloop_eval.suite import ROOT, load_suite, materialize

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def run(
    suite: str = typer.Option("fixtures"),
    limit: int = typer.Option(0, help="0 = every case"),
    max_cost_usd: float = typer.Option(2.0, help="stop starting new cases past this spend"),
    out: Path = typer.Option(ROOT / "eval" / "results"),
    api_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
) -> None:
    """Run every case of a suite (sequentially, so the cost cap is exact) and write a JSONL file."""
    cases = load_suite(suite)[: limit or None]
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / f"{suite}-{run_id}.jsonl"
    spent = 0.0
    with make_client(api_url, api_key) as c, results_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            if spent >= max_cost_usd:
                console.print(f"[yellow]cost cap reached (${spent:.2f}); skipping the rest[/yellow]")
                break
            started = time.monotonic()
            spec = materialize(case, c.upload)
            task = c.create_task(spec, idempotency_key=f"eval-{run_id}-{case.name}-{uuid.uuid4().hex[:6]}")
            final = c.wait(task["id"], poll_s=3)
            result = final.get("result") or {}
            usage = final.get("usage") or {}
            spent += float(usage.get("cost_usd", 0))
            row = {
                "case": case.name,
                "tags": case.tags,
                "task_id": task["id"],
                "kind": spec["kind"],
                "expected": case.expect_outcome,
                "outcome": result.get("outcome"),
                "match": result.get("outcome") == case.expect_outcome,
                "level": (result.get("verification") or {}).get("level"),
                "attempts": result.get("attempts"),
                "cost_usd": usage.get("cost_usd", 0),
                "model_calls": usage.get("model_calls", 0),
                "sandbox_seconds": usage.get("sandbox_seconds", 0),
                "cache": result.get("cache"),
                "seconds": round(time.monotonic() - started, 1),
            }
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            mark = "[green]ok[/green]" if row["match"] else "[red]MISS[/red]"
            console.print(
                f"{mark} {case.name}: {row['outcome']} (expected {case.expect_outcome}), "
                f"${row['cost_usd']:.4f}, {row['seconds']}s"
            )
    console.print(f"\nresults: {results_path}")
    _print_report(_read(results_path))


@app.command()
def report(results: Path) -> None:
    """Summarize a results file: match rate, cost, latency and attempts."""
    _print_report(_read(results))


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("no results")
        return
    table = Table(
        "kind", "cases", "as expected", "verified", "mean cost $", "p50 s", "p95 s", "mean attempts"
    )
    for kind in [*sorted({r["kind"] for r in rows}), "all"]:
        subset = rows if kind == "all" else [r for r in rows if r["kind"] == kind]
        seconds = sorted(r["seconds"] for r in subset)
        p95 = seconds[min(len(seconds) - 1, round(0.95 * (len(seconds) - 1)))]
        table.add_row(
            kind,
            str(len(subset)),
            f"{sum(r['match'] for r in subset) / len(subset):.0%}",
            f"{sum(r['outcome'] == 'verified' for r in subset) / len(subset):.0%}",
            f"{statistics.mean(float(r['cost_usd']) for r in subset):.4f}",
            f"{statistics.median(seconds):.0f}",
            f"{p95:.0f}",
            f"{statistics.mean(float(r['attempts'] or 0) for r in subset):.2f}",
        )
    console.print(table)
