# Fixloop

**A sandboxed autonomous coding and SQL agent that only returns what it has proven.**

You give Fixloop a failing test (or a question about a database snapshot). It plans a change,
writes it, runs it in a locked-down sandbox, reads the failure, repairs, and stops only when
the change is *verified by execution*: the target tests pass, nothing that passed before
breaks, protected tests were not touched, hidden tests pass and the result holds up in a
shuffled rerun. For SQL, the query must match the expected result on the real data *and* on
mutated copies of it. No model ever decides whether an answer is correct.

```text
$ uv run fixloop fix --repo examples/buggy-dates --tests tests/test_dates.py::test_parse_dates
task 01a20ab1-6fa1-7aaf-bba7-854a91c9b36e created
environment ready (built, 1.2s)
baseline: 1 failing target(s), strategy full
attempt 0 started
attempt 0 passed: all target tests pass with no regressions

verified: all target tests pass with no regressions
verification level: no_regressions
model calls 2, cost $0.0061, sandbox 4.8s
--- a/dates/parse.py
+++ b/dates/parse.py
-    return date(int(year), int(day), int(month))
+    return date(int(year), int(month), int(day))
```

Python, FastAPI, Docker (gVisor in production), Redis Streams, Postgres, Pydantic, Gemini.

---

## What it does

| task kind | input | "done" means |
|---|---|---|
| `python.fix` | a repository + failing pytest node ids | targets pass, no regressions, no test gaming, stable rerun |
| `python.implement` | a repository + tests describing new behaviour | the frozen tests pass |
| `sql.write` | DDL + seed data + a question + an oracle | the query matches the oracle on the data and on mutated copies |
| `sql.fix` | the above + a broken query | same, for the repaired query |

Sources can be inline files (small tasks, other agents), an uploaded archive, or a GitHub
repository through the Fixloop GitHub App, which can also be driven from a PR comment
(`/fixloop fix tests/test_x.py::test_y`) and answers with a **draft** pull request.

## How it works

```mermaid
flowchart LR
    client[CLI / API client / GitHub] --> api[API<br/>FastAPI]
    api -->|task row + outbox| pg[(Postgres<br/>source of truth)]
    api -->|task id| rq[(Redis Streams)]
    rq --> worker[Worker<br/>the loop]
    worker -->|plan / patch / repair| gemini[Gemini]
    worker -->|ExecRequest| rq
    rq --> runner[Runner<br/>only component that<br/>starts containers]
    runner --> sandbox[[Sandbox<br/>no network, read-only root,<br/>non-root, gVisor]]
    runner -->|artifacts| s3[(Object store)]
    worker --> pg
    pg -->|outbox| publisher[Publisher<br/>GitHub App, webhooks]
    sandbox -. prepare only .-> proxy[Package proxy<br/>allowlist, wheels only]
```

The loop for a Python task:

1. **Snapshot** the source (secrets are flagged and never shown to a model).
2. **Prepare** the environment once per dependency set: wheels only, through the allowlisted
   proxy, cached per tenant.
3. **Baseline**: run the suite on the untouched code. If the targets already pass, the answer is
   `nothing_to_fix`; if they flip between runs, it is `flaky_oracle`. Nothing is "fixed" against
   a test that cannot tell right from wrong.
4. **Plan, patch, verify** with anchored search/replace edits, checked against path and AST
   policies before anything runs.
5. **Repair** at most three times, always from the *best* state so far. The last repair, and
   any repair after a repeated failure, runs on the stronger model with a deep trace. A third
   identical failure stops the task (`no_progress`) instead of burning money.
6. **Finish** with a verified patch, or an honest outcome (`exhausted_repairs`,
   `exhausted_budget`, `policy_violation`, `env_failed`, ...) plus the best unverified diff.

Every model call and sandbox second is reserved against a budget *before* it is spent. A
crashed worker is replaced by one that resumes from the last checkpoint without paying for
anything twice. Details: [docs/architecture.md](docs/architecture.md).

---

## Quick start (Windows)

You need **Docker Desktop** (WSL2 backend), **uv** (`winget install astral-sh.uv`) and **Git**.

```powershell
git clone https://github.com/KaranSharma18/sandboxed-coding-sql-agent
cd sandboxed-coding-sql-agent
uv sync --all-packages
copy .env.example .env            # then open .env and set FIXLOOP_GEMINI__API_KEY
uv run poe dev                    # builds the sandbox images, starts the stack, waits until healthy
uv run poe seed                   # creates the local tenant and prints an API key
uv run fixloop login --api-url http://localhost:8080 --api-key fl_live_...   # paste the key
uv run fixloop fix --repo examples/buggy-dates --tests tests/test_dates.py::test_parse_dates
```

The first `poe dev` builds six sandbox images and takes a few minutes; later starts take
seconds. Open http://localhost:8080/docs for the interactive API, and http://localhost:9001 for
the MinIO console (user `fixloop`, password `fixloop-dev-secret`).

**No Gemini key yet?** Put these two lines in `.env`, run `docker compose up -d --force-recreate worker`
and the examples run against a scripted model, for free:

```text
FIXLOOP_GEMINI__BACKEND=scripted
FIXLOOP_GEMINI__SCRIPTED_PATH=/app/examples/demo-script.yaml
```

**Linux and macOS:** the same commands, with `cp .env.example .env`.

### More examples

```powershell
# implement a function from its tests
uv run fixloop fix --implement --repo examples/implement-slugify --tests tests/test_slugify.py

# write a SQL query and prove it on a DuckDB snapshot (with differential testing)
uv run fixloop sql --dialect duckdb --ddl examples/sql-duckdb/schema.sql `
  --seed customers=examples/sql-duckdb/customers.csv --seed orders=examples/sql-duckdb/orders.csv `
  --question "Revenue from paid orders and the number of paying customers per country, highest revenue first." `
  --reference-query examples/sql-duckdb/reference.sql --order-matters --sort-key revenue

uv run fixloop list                       # recent tasks
uv run fixloop status <task-id>           # attempts and their verdicts
uv run fixloop patch <task-id> --apply examples/buggy-dates   # git apply a verified patch
```

The same through the API:

```powershell
$key = "fl_live_..."
$body = Get-Content examples\task.json -Raw
Invoke-RestMethod -Method Post -Uri http://localhost:8080/v1/tasks -Body $body `
  -ContentType application/json -Headers @{Authorization = "Bearer $key"; "Idempotency-Key" = "demo-1"}
```

See [docs/api.md](docs/api.md) for every endpoint, the event stream and error format.

---

## Running the tests

| command | what it runs | needs |
|---|---|---|
| `uv run poe test` | unit tests (patching, policy, traces, verdicts, LLM layer, proxy) | nothing |
| `uv run poe test-int` | storage, queues, budgets, caches, API, publisher, runner (local backend) | Postgres + Redis |
| `uv run poe e2e` | the whole loop: API → worker → runner → scripted model, every outcome, crash-resume | Postgres + Redis |
| `uv run poe test-docker` | the Docker backend: conformance, limits, env volumes, reaping | Docker + images |
| `uv run pytest -m live` | contract tests against the real Gemini API | `FIXLOOP_GEMINI__API_KEY` |

Integration tests start throwaway Postgres and Redis automatically when their binaries are on
the machine (Linux CI). On Windows, start them with Docker and point the tests at them:

```powershell
docker compose up -d postgres redis-queue redis-cache
docker compose exec postgres createdb -U fixloop fixloop_test   # once; the tests empty this database
$env:FIXLOOP_TEST_DATABASE_URL = "postgresql+asyncpg://fixloop:fixloop@localhost:5432/fixloop_test"
$env:FIXLOOP_TEST_REDIS_URL = "redis://localhost:6379/1"
$env:FIXLOOP_TEST_REDIS_CACHE_URL = "redis://localhost:6380/1"
uv run poe test-int
```

`uv run poe lint` (ruff + import-linter layering contracts) and `uv run poe typecheck`
(pyright) run in CI next to the tests. More in [docs/development.md](docs/development.md).

## Repository layout

```text
packages/fixloop-core     domain models, the loop, verification, context, model layer, adapters
packages/fixloop-pytest   the pytest reporting plugin and the environment prepare script (sandbox side)
packages/fixloop-sqlexec  SQL execution inside the sandbox (DuckDB, Postgres)
packages/fixloop-client   Python client and the `fixloop` CLI
apps/api                  FastAPI service + `fixloop-admin`
apps/worker               runs the loop
apps/runner               the only component that starts containers (Docker or local backend)
apps/publisher            outbox dispatcher: GitHub App, customer webhooks
apps/pkgproxy             allowlisted, wheels-only package index
config/                   models and prices, policies, command templates, limits, allowlist
prompts/                  versioned prompt templates
migrations/               Alembic
images/                   service and sandbox Dockerfiles
deploy/                   Kubernetes (kustomize), runner host setup (gVisor), database roles
eval/                     evaluation harness and suites
docs/                     architecture, ADRs, runbooks, configuration, API, security
```

## Security model in brief

* Untrusted code runs only in sandboxes: no network, read-only root filesystem, non-root user,
  all capabilities dropped, no-new-privileges, memory/CPU/PID/file limits, and gVisor in
  production. A self-test proves these properties at start-up and every 15 minutes; a runner
  that fails it stops taking work.
* Dependencies are wheels from an allowlist, through a proxy, only while preparing.
* Repository content reaches the model wrapped as untrusted data; secrets are redacted first.
* The model never chooses a command: it can only edit files, and only within policy.
* Credentials live where they are used: the worker never holds the GitHub App key; the runner
  never holds the database or the model key.

Full threat model: [docs/security.md](docs/security.md).

## Known limitations

* Python and SQL (DuckDB, Postgres) only. JavaScript and other languages are not supported.
* Pull requests from forks are not run from comment commands.
* Expected SQL results can be uploaded as CSV; Parquet is supported for seed data only.
* A full test suite slower than the regression budget (180 s) falls back to a targeted
  selection of tests, and the result says so.

## Pushing this repository to GitHub

```powershell
git init -b main
git add .
git commit -m "Fixloop: initial import"
git remote add origin https://github.com/KaranSharma18/sandboxed-coding-sql-agent.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `.secrets/`, `*.pem` and local data. Turn on branch
protection for `main` so the `ci` workflow must pass before merging.

## Documentation

* [Architecture](docs/architecture.md) and [implementation notes](docs/implementation-notes.md)
* [Design decisions (ADRs)](docs/adr/README.md)
* [Configuration](docs/configuration.md) · [API](docs/api.md) · [Development](docs/development.md)
* [Security](docs/security.md) · [Evaluation](docs/evaluation.md) · [Runbooks](docs/runbooks/README.md)

Licensed under Apache-2.0.
