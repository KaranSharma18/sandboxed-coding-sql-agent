# Implementation notes

Decisions made while building, and the places where the code differs from the design document.
Each one says what we did and why, so a reviewer can disagree with the reason rather than guess it.

## Code layout

* **Flat modules instead of sub-packages.** The design sketched `context/`, `patching/`,
  `verify/` and friends as packages. At this size each is one readable file
  (`context.py`, `patching.py`, `verify_python.py`, ...). The layering rules are unchanged and
  enforced by import-linter: the loop (`fixloop_core.loop`) and the pure modules may not import
  Redis, SQLAlchemy, Docker, boto3, FastAPI or the Gemini SDK, even indirectly.
* **Ports.** `fixloop_core/ports.py` holds the protocols the loop is written against
  (`TaskStore`, `EventSink`, `ExecPort`, `BudgetPort`, `CachePort`, `GitHubReader`). Adapters:
  `storage/taskstore.py` (Postgres), `execclient.py`, `budget.py`, `cache.py`, `events.py`,
  `objectstore.py`. The worker's `wiring.py` is the only place adapters are chosen.

## Runner

* **The runner never touches Postgres.** It stores each `ExecResult` in Redis
  (`fixloop:exec:result:{exec_id}`, 24 h) and pushes `fixloop:exec:done:{exec_id}`; the worker
  waits with `BLPOP` and records the execution in Postgres. This keeps the trust zones clean.
* **No bind mounts.** The Docker backend creates fresh named volumes per run and fills and
  reads them through the archive API (`put_archive` / `get_archive`). That works the same when
  the runner itself is a container on Docker Desktop for Windows, where host paths do not mean
  what they seem.
* **Environments are cached per runner, per tenant.** Volumes are named
  `fixloop-env-{tenant hash}-{env hash}` and recorded in a local index; an environment that
  exists without an index entry is rebuilt, because nothing proves its preparation finished.
  There is no shared `env_cache` table: a cache miss on another runner costs one prepare.
* **The local backend** (`FIXLOOP_RUNNER__BACKEND=local`) runs commands as plain subprocesses.
  It is for tests and for trying Fixloop on your own code, needs a second explicit flag
  (`ALLOW_UNSAFE_LOCAL=true`) and is refused when `FIXLOOP_ENV=production`. When started as root
  it switches to an unprivileged user (65534), applies rlimits, copies the helpers, `uv` and the
  proxy CA bundle somewhere that user can read, and kills strays (Postgres daemonizes) by
  command line after each run.
* **Helpers on `PYTHONPATH`.** The pytest plugin and the SQL executor live in
  `/opt/fixloop/lib` in the images and are put on `PYTHONPATH`, so they work with any
  interpreter and never mix with the project's own dependencies.
* **Offline prepare.** Sandbox images ship pytest and its dependencies as wheels; a project with
  no third-party dependencies prepares without touching the network. A folder of wheels can
  also be used as the index (`FIXLOOP_RUNNER__INDEX_URL=/path/to/wheels`).
* **Protected files are re-hashed after every run**, so a test suite that rewrites its own
  tests at runtime (a `conftest.py` trick) fails as `test_gaming`.

## The loop

* **Repairs anchor on the best state.** The repair prompt shows the current content of the best
  attempt's files, and its edits are applied to that state, not to the last attempt.
* **Saved model outputs.** The plan and the edits of the current attempt are checkpointed before
  anything runs (`state.pending`). Together with deterministic exec ids, a resumed task never
  calls the model again for a step it already paid for, even when the L1 cache is off.
* **Executions are billed once.** Exec ids already accounted for are kept in the checkpoint
  (`state.exec_done`), so a resumed run that gets a stored result is not re-billed.
* **Failure signatures without a trace** (apply errors, policy violations, SQL errors) are a
  short hash of the normalized message (`text_signature`), so they fit the 64-character column
  and still detect "the same failure again".
* **Pre-existing collection errors.** A test file that already failed to import before the
  change does not fail every candidate; only new collection errors (or errors in a target's
  file) do.
* **Targeted selection is per candidate.** When the full suite is too slow, each candidate runs
  the targets, the tests that import the changed files, and a seeded sample; the baseline is
  restricted to the same selection before comparing, so unselected tests are not counted as
  regressions or as removed.
* **Dependency requests** from the plan or a repair are only honoured with
  `allow_dependency_changes`, only for allowlisted packages pinned with `==`; an approved
  request re-prepares the environment before the candidate runs.

## Caches

* **L1** (model responses) is bypassed when a task with the same spec failed in the last 24 h:
  replaying the same answers would only repeat the failure.
* **`options.cache = "no_llm_cache"`** turns off L1, L2 and L4 (every layer that serves a
  model's answer). L3 is the exec result store, which is always on within a task.
* **L4 values are anchored edits** computed from the verified diff; "the edits apply cleanly and
  the files still parse" is the precondition for trying a memo on a different snapshot.
* Cache values above 1 MB are not cached.

## Data

* **Uploads** have their own table and presigned URLs. With the local object store the URLs
  point back at the API (`/v1/blobs/...`, HMAC-signed, expiring).
* **Mixed line endings** are normalized to LF when a file is edited; untouched files keep their
  bytes.
* **Expected SQL rows** may be uploaded as CSV only; Parquet is supported for seed data.
* **Row-level security** policies exist on tenant-scoped tables. Services connect as the schema
  owner and enforce tenancy in every query; the policies protect reporting roles
  (`deploy/postgres/roles.sql`), which only see the tenant they set in `app.tenant_id`.

## GitHub

* The outbox dispatcher lives in the publisher, with four kinds: `github_publish`,
  `github_comment`, `github_command`, `customer_webhook`.
* The worker gets read-only, single-repository installation tokens from the publisher over a
  Redis request/reply stream (`fixloop:q:ghtoken`); it never sees the App's private key.
* Publishing writes the exact verified file contents (stored as `final_overlay.json`) with the
  Git Data API, keeps executable bits from the snapshot manifest, reuses the branch and the PR
  on a retry, and falls back to a normal PR where drafts are not available.
* Comment commands from forks are refused for now.

## Not implemented yet

* Check runs on the PR (the publisher comments instead).
* Languages other than Python; SQL engines other than DuckDB and Postgres.
* Hosted dashboards: Prometheus metrics are exposed (`/metrics` on the API, port 9100 on the
  other services) and a scrape config is included; the dashboards themselves are up to you.
