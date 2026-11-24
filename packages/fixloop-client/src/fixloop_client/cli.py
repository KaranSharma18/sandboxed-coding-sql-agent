"""`fixloop`: submit tasks and watch them from the terminal.

fixloop login --api-url http://localhost:8080 --api-key fl_live_...
fixloop fix --repo examples/buggy-dates --tests tests/test_dates.py::test_parse_dates
fixloop sql --dialect duckdb --ddl schema.sql --seed orders=orders.csv --question "..." --expected out.csv
fixloop watch TASK_ID | fixloop status TASK_ID | fixloop patch TASK_ID [--apply] | fixloop list
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from fixloop_client.client import FixloopClient, FixloopError
from fixloop_client.repo import source_for

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Fixloop: verified fixes for failing tests and SQL"
)
console = Console()
err_console = Console(stderr=True)
CONFIG_FILE = Path.home() / ".fixloop" / "config.json"
LOCAL_KEY_FILE = Path(".fixloop-data/local-api-key.txt")
DONE = ("succeeded", "failed", "cancelled")


def _config() -> dict[str, str]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def client(api_url: str | None = None, api_key: str | None = None) -> FixloopClient:
    config = _config()
    url = api_url or os.environ.get("FIXLOOP_API_URL") or config.get("api_url") or "http://localhost:8080"
    key = api_key or os.environ.get("FIXLOOP_API_KEY") or config.get("api_key")
    if not key and LOCAL_KEY_FILE.exists():
        key = LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        console.print(
            "[red]No API key.[/red] Run `uv run poe seed` (local) or `fixloop login --api-key ...`."
        )
        raise typer.Exit(2)
    return FixloopClient(url, key)


ApiUrl = typer.Option(None, "--api-url", help="defaults to $FIXLOOP_API_URL, then ~/.fixloop/config.json")
ApiKey = typer.Option(None, "--api-key", help="defaults to $FIXLOOP_API_KEY, then ~/.fixloop/config.json")


@app.command()
def login(
    api_url: str = typer.Option("http://localhost:8080"),
    api_key: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    """Save the API URL and key to ~/.fixloop/config.json."""
    with FixloopClient(api_url, api_key) as c:
        try:
            c.list_tasks(limit=1)
        except FixloopError as exc:
            console.print(f"[red]login failed:[/red] {exc.detail}")
            raise typer.Exit(1) from exc
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"api_url": api_url, "api_key": api_key}), encoding="utf-8")
    console.print(f"saved to {CONFIG_FILE}")


@app.command()
def fix(
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="the repository folder"),
    tests: list[str] = typer.Option(..., "--tests", "-t", help="target test node id (repeatable)"),
    description: str = typer.Option("", "--description", "-d", help="what is wrong, in your words"),
    implement: bool = typer.Option(False, help="python.implement: the tests describe new behaviour"),
    python: str = typer.Option("3.12", help="Python version for the sandbox"),
    regression: str = typer.Option("full", help="full | related | none"),
    profile: str = typer.Option("default", help="economy | default | quality"),
    max_repairs: int = typer.Option(3, min=0, max=3),
    hidden_tests: Path | None = typer.Option(
        None, exists=True, file_okay=False, help="folder of hidden tests"
    ),
    allow_dependency_changes: bool = typer.Option(False),
    watch_it: bool = typer.Option(True, "--watch/--no-watch"),
    apply: bool = typer.Option(False, help="apply a verified patch to the repository with git apply"),
    idempotency_key: str | None = typer.Option(None),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Fix a Python repository until the target tests pass, proven in a sandbox."""
    with client(api_url, api_key) as c:
        console.print(f"packing [bold]{repo}[/bold] ...")
        source = source_for(repo, c.upload)
        oracle: dict[str, Any] = {
            "type": "pytest",
            "target_tests": tests,
            "regression": regression,
            "python_version": python,
        }
        if hidden_tests is not None:
            from fixloop_client.repo import list_files, make_archive

            oracle["hidden_tests"] = c.upload(
                make_archive(hidden_tests, list_files(hidden_tests)), "hidden_tests", "application/gzip"
            )
        spec = {
            "kind": "python.implement" if implement else "python.fix",
            "description": description,
            "source": source,
            "oracle": oracle,
            "options": {
                "max_repairs": max_repairs,
                "model_profile": profile,
                "allow_dependency_changes": allow_dependency_changes,
            },
        }
        task = _create(c, spec, idempotency_key)
        if watch_it:
            task = _watch(c, task["id"])
            _show_result(c, task, repo if apply else None)


@app.command()
def sql(
    dialect: str = typer.Option("duckdb", help="duckdb | postgres"),
    ddl: Path = typer.Option(..., exists=True, dir_okay=False, help="file with CREATE TABLE statements"),
    seed: list[str] = typer.Option([], "--seed", help="TABLE=path.csv (repeatable)"),
    question: str = typer.Option(..., "--question", "-q", help="what the query must answer"),
    expected: Path | None = typer.Option(
        None, exists=True, dir_okay=False, help="CSV of the expected result"
    ),
    reference_query: Path | None = typer.Option(
        None, exists=True, dir_okay=False, help="file with a trusted query whose result is the answer"
    ),
    broken_query: Path | None = typer.Option(
        None, exists=True, dir_okay=False, help="sql.fix: the query to repair"
    ),
    order_matters: bool = typer.Option(False),
    sort_key: list[str] = typer.Option([], "--sort-key"),
    watch_it: bool = typer.Option(True, "--watch/--no-watch"),
    idempotency_key: str | None = typer.Option(None),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Write (or fix) a SQL query and verify it on a snapshot database."""
    with client(api_url, api_key) as c:
        seeds = []
        for item in seed:
            table, _, path = item.partition("=")
            data = Path(path).read_bytes()
            fmt = "parquet" if path.endswith(".parquet") else "csv"
            if fmt == "csv" and len(data) < 1_500_000:
                seeds.append({"table": table, "inline_csv": data.decode("utf-8")})
            else:
                seeds.append({"table": table, "format": fmt, "upload_id": c.upload(data, "seed")})
        oracle: dict[str, Any] = {
            "type": "sql",
            "dialect": dialect,
            "order_matters": order_matters,
            "sort_key": sort_key,
        }
        if expected is not None:
            rows = list(csv.reader(io.StringIO(expected.read_text(encoding="utf-8-sig"))))
            oracle["expected"] = {"columns": rows[0], "rows": [[_cell(v) for v in r] for r in rows[1:] if r]}
        if reference_query is not None:
            oracle["reference_query"] = reference_query.read_text(encoding="utf-8")
        if broken_query is not None:
            oracle["broken_query"] = broken_query.read_text(encoding="utf-8")
        spec = {
            "kind": "sql.fix" if broken_query else "sql.write",
            "description": question,
            "source": {"type": "sql_snapshot", "ddl": ddl.read_text(encoding="utf-8"), "seeds": seeds},
            "oracle": oracle,
        }
        task = _create(c, spec, idempotency_key)
        if watch_it:
            _show_result(c, _watch(c, task["id"]), None)


def _cell(value: str) -> Any:
    if value == "":
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def _create(c: FixloopClient, spec: dict[str, Any], key: str | None) -> dict[str, Any]:
    try:
        task = c.create_task(spec, key)
    except FixloopError as exc:
        console.print(f"[red]rejected ({exc.status} {exc.code}):[/red] {exc.detail}")
        for issue in exc.body.get("errors", [])[:10]:
            console.print(f"  - {'.'.join(issue.get('loc', []))}: {issue.get('msg')}")
        raise typer.Exit(1) from exc
    console.print(f"task [bold]{task['id']}[/bold] created")
    return task


def _describe(event: dict[str, Any]) -> str | None:
    kind, data = event.get("type"), event.get("payload") or {}
    if kind == "phase":
        return f"[dim]phase[/dim] {data.get('phase')}"
    if kind == "snapshot.ready":
        return f"snapshot ready ({data.get('files', data.get('tables', '?'))} files/tables)"
    if kind == "env.ready":
        return f"environment ready ({'cached' if data.get('cache_hit') else 'built'}, {data.get('seconds')}s)"
    if kind == "baseline.done":
        failing, strategy = data.get("failing_targets", "?"), data.get("strategy", "-")
        return f"baseline: {failing} failing target(s), strategy {strategy}"
    if kind == "attempt.started":
        tag = " [magenta](escalated)[/magenta]" if data.get("escalated") else ""
        return f"attempt {data.get('attempt_no')} started{tag}"
    if kind == "attempt.finished":
        if data.get("passed"):
            return f"[green]attempt {data.get('attempt_no')} passed[/green]: {data.get('message')}"
        repeated = " (same failure again)" if data.get("repeated") else ""
        return (
            f"[yellow]attempt {data.get('attempt_no')} failed[/yellow] [{data.get('failure_class')}]"
            f"{repeated}: {data.get('message')}"
        )
    if kind == "cache.checked":
        return f"cached answer ({data.get('layer')}) {'verified' if data.get('passed') else 'did not verify'}"
    if kind == "task.finished":
        return f"[bold]finished: {data.get('outcome')}[/bold]"
    return None


def _watch(c: FixloopClient, task_id: str) -> dict[str, Any]:
    try:
        for event in c.events(task_id):
            line = _describe(event)
            if line:
                console.print(line)
    except KeyboardInterrupt:
        console.print(f"\nstopped watching; the task keeps running: fixloop watch {task_id}")
        raise typer.Exit(130) from None
    return c.get_task(task_id)


def _show_result(c: FixloopClient, task: dict[str, Any], apply_to: Path | None) -> None:
    result = task.get("result") or {}
    outcome = result.get("outcome", task.get("status"))
    color = "green" if outcome == "verified" else ("cyan" if task["status"] == "succeeded" else "red")
    console.print(f"\n[{color}][bold]{outcome}[/bold][/{color}]: {result.get('reason', '')}")
    verification = result.get("verification") or {}
    if verification:
        console.print(f"verification level: {verification.get('level')}")
    for warning in result.get("warnings", [])[:8]:
        console.print(f"[yellow]note:[/yellow] {warning}")
    usage = task.get("usage") or {}
    console.print(
        f"[dim]model calls {usage.get('model_calls', 0)}, cost ${usage.get('cost_usd', 0):.4f}, "
        f"sandbox {usage.get('sandbox_seconds', 0):.1f}s[/dim]"
    )
    if result.get("query"):
        console.print(Syntax(result["query"], "sql", word_wrap=True))
        preview = result.get("preview") or {}
        if preview.get("rows"):
            table = Table(*[str(col) for col in preview.get("columns", [])])
            for row in preview["rows"][:10]:
                table.add_row(*[str(v) for v in row])
            console.print(table)
    if result.get("patch_key"):
        diff, verified = c.patch(task["id"])
        console.print(Syntax(diff, "diff", word_wrap=True))
        if not verified:
            console.print("[yellow]this patch is NOT verified (best attempt only)[/yellow]")
        elif apply_to is not None:
            _git_apply(apply_to, diff)


def _git_apply(repo: Path, diff: str) -> None:
    git = shutil.which("git")
    if git is None:
        console.print("[red]git is not installed; save the patch and apply it by hand[/red]")
        return
    result = subprocess.run(
        [git, "-C", str(repo), "apply", "--whitespace=nowarn", "-"],
        input=diff.encode(),
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        console.print(f"[green]applied to {repo}[/green]")
    else:
        console.print(f"[red]git apply failed:[/red] {result.stderr.decode(errors='replace')[:500]}")


@app.command()
def watch(task_id: str, api_url: str | None = ApiUrl, api_key: str | None = ApiKey) -> None:
    """Follow a task's progress until it finishes."""
    with client(api_url, api_key) as c:
        _show_result(c, _watch(c, task_id), None)


@app.command()
def status(
    task_id: str,
    as_json: bool = typer.Option(False, "--json"),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Show a task (add --json for the raw document)."""
    with client(api_url, api_key) as c:
        task = c.get_task(task_id)
        if as_json:
            console.print_json(json.dumps(task))
        else:
            console.print(
                f"{task['id']}  {task['kind']}  {task['status']}  phase={task['phase']}  "
                f"outcome={task.get('outcome')}"
            )
            for attempt in c.attempts(task_id):
                verdict = attempt.get("verdict") or {}
                console.print(
                    f"  attempt {attempt['attempt_no']} ({attempt['kind']}): "
                    f"{'passed' if verdict.get('passed') else attempt.get('failure_class')} "
                    f"- {verdict.get('message', '')[:120]}"
                )


@app.command()
def patch(
    task_id: str,
    apply: Path | None = typer.Option(None, "--apply", help="repository to apply it to"),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Print a task's patch as a git diff, or apply it with --apply REPO."""
    with client(api_url, api_key) as c:
        diff, verified = c.patch(task_id)
        if apply is None:
            sys.stdout.write(diff)
            if not verified:
                err_console.print("[yellow]# not verified[/yellow]")
        elif not verified:
            console.print("[red]refusing to apply an unverified patch[/red]")
            raise typer.Exit(1)
        else:
            _git_apply(apply, diff)


@app.command("list")
def list_(
    limit: int = 20,
    status_filter: str | None = typer.Option(None, "--status"),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Recent tasks."""
    with client(api_url, api_key) as c:
        table = Table("id", "kind", "status", "outcome", "created")
        for task in c.list_tasks(limit, status_filter)["items"]:
            table.add_row(
                task["id"], task["kind"], task["status"], task.get("outcome") or "", task["created_at"][:19]
            )
        console.print(table)


@app.command()
def cancel(task_id: str, api_url: str | None = ApiUrl, api_key: str | None = ApiKey) -> None:
    with client(api_url, api_key) as c:
        console.print(f"cancellation requested ({c.cancel(task_id)['status']})")


@app.command()
def retry(
    task_id: str,
    watch_it: bool = typer.Option(True, "--watch/--no-watch"),
    api_url: str | None = ApiUrl,
    api_key: str | None = ApiKey,
) -> None:
    """Run a finished task's spec again as a new task."""
    with client(api_url, api_key) as c:
        task = c.retry(task_id)
        console.print(f"task [bold]{task['id']}[/bold] created")
        if watch_it:
            _show_result(c, _watch(c, task["id"]), None)


if __name__ == "__main__":
    app()
