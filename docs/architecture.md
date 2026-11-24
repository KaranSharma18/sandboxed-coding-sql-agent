# Architecture

This page explains how Fixloop is put together and why. The decisions themselves, with the
alternatives we rejected, are in [the ADRs](adr/README.md). Where the code differs from the
original design document, [implementation-notes.md](implementation-notes.md) says so.

## The one idea everything follows

**A model proposes; execution decides.** The model writes plans, edits and queries. Whether
any of it is correct is decided only by running it: tests, comparisons, assertions. That one
rule explains most of the system: the sandbox exists because we run untrusted code all the
time; the baseline exists because a test that already passes (or flips) cannot judge a fix;
the anti-gaming checks exist because the easiest way to "pass" a test is to change the test.

## Components and trust zones

```text
            ┌──────────────── control plane (trusted code, holds credentials) ──────────────────┐
 clients ──►│  api (FastAPI)  ──►  Postgres (source of truth)  ◄──  worker (the loop)  ──► Gemini │
 GitHub  ──►│       │                    ▲   outbox                    │                       │
            │       └──► Redis queue ◄───┼───────────────────────────────┤                       │
            │                            └──── publisher ──► GitHub, customer webhooks           │
            └──────────────────────────────────┬───────────────────────────────────────────────┘
                                               │ exec requests / results (Redis), artifacts (S3)
            ┌──────────────── execution plane (no database, no model key) ─────────────────────┐
            │  runner ──► Docker (gVisor) ──► sandbox: no network, read-only root, uid 10001    │
            │                                   └─ prepare only ─► pkgproxy ─► PyPI (wheels)    │
            └───────────────────────────────────────────────────────────────────────────────────┘
```

| component | does | holds | never does |
|---|---|---|---|
| api | validates and admits tasks, auth, rate limits, SSE, uploads, GitHub webhooks | DB, Redis, object store | run code, call a model |
| worker | runs the loop: context, model calls, verification, checkpoints | DB, Redis, object store, model key | start containers, hold the GitHub App key |
| runner | runs sandboxes from fixed command templates | Redis, object store, Docker | read Postgres, call a model |
| publisher | executes the outbox: draft PRs, comments, webhooks; mints GitHub tokens | DB, GitHub App key | run code |
| pkgproxy | serves allowlisted wheels to preparing sandboxes | a cache | serve sdists or anything off the allowlist |

The split is about blast radius. The component that runs hostile code (the runner) has the
least worth stealing: no database credentials, no model key, no GitHub key.

## A Python task, step by step

1. **Intake (api).** The spec is validated by Pydantic (node ids can never become pytest flags,
   paths can never escape the repository), size limits are checked (413), the tenant's queue
   and daily limits are checked (429), and the task row is inserted idempotently on
   `(tenant, Idempotency-Key)`. Only then is the task id put on the `interactive` or `batch`
   stream.
2. **Claim (worker).** A worker reads the id (3:1 weighted towards interactive), claims the
   task in Postgres, which increments its **fence**, and from then on every write it makes is
   conditioned on that fence. It renews the lease every 20 s.
3. **Snapshot.** Inline files, an uploaded archive or a GitHub tarball (for an exact SHA)
   become a content-addressed `tar.zst` in the object store. Archives are extracted
   defensively (no links out, no absolute paths, size and count limits). Files that look like
   secrets are flagged and never shown to a model; secret-looking strings are redacted from
   every prompt.
4. **Dependencies and protection.** Requirements are found (`requirements*.txt`, PEP 621
   extras, PEP 735 groups), includes are inlined, local installs and index options are dropped,
   URLs and VCS links are refused, and every name must be on the allowlist. Protected paths
   (tests, CI config, the oracle's files) are expanded and hashed.
5. **Prepare (runner).** The environment is built once per *(tenant, environment hash)*:
   `uv venv` + `uv pip install --only-binary :all:` through the proxy, on a network that
   reaches nothing but the proxy. Later runs mount it read-only.
6. **Baseline.** The suite runs on the untouched code (two reports: our JSONL plugin and
   JUnit). Targets that pass mean `nothing_to_fix`; the failing targets are rerun shuffled and
   any that flips means `flaky_oracle`. The baseline decides the regression strategy: the full
   suite if it fits in 180 s, otherwise a targeted selection (target modules, modules that
   import the changed files, then a seeded sample).
7. **Caches.** A verified fix for the same snapshot, oracle and environment (L2) or a fix
   remembered for the same failure signature (L4) is tried first, and always re-verified.
8. **Plan → patch.** The prompt is assembled deterministically, stable parts first (task,
   repository map, target tests, original files), so the provider's prefix cache hits and our
   own exact-match L1 cache works. The patch is a list of anchored edits; it must apply
   cleanly, still parse, change something new, and pass the policy (no protected files, no
   test gaming such as `pytest.skip` or `sys.exit` in the code under test, size limits). A
   failure here is a re-ask, not an attempt.
9. **Verify.** The candidate (best state + new edits) runs targets and regressions in one
   sandbox run. Verdict checks run cheapest first: status, protected files unchanged, report
   present, no new collection errors, the two reports agree with each other and with the exit
   code, no collected test disappeared, targets pass, no regressions. Then hidden tests (only
   their count of failures ever reaches the model), a shuffled rerun of the targets, and
   changed-line coverage (a warning when the change never ran).
10. **Repair.** The next prompt shows the attempt history, the last and best diffs, the
    current content of the best state and a compacted trace (or a digest of a long one). The
    edits apply to the *best* state. Escalation: the last repair, and the repair after a
    failure signature first repeats, use the stronger model and get a deep trace (local
    variables per frame). A second repeat stops the task.
11. **Finalize.** Result, outbox items and the `task.finished` event are written in one fenced
    transaction. The publisher then opens the draft PR, posts comments or calls the webhook.

## A SQL task

The same skeleton with different checks: the snapshot database is built from DDL and seeds
(plus three mutated copies when a reference query allows differential testing). A candidate
must parse, use only known tables and columns, avoid denied functions (and time functions
unless `as_of` is set), pass an `EXPLAIN` cost guard, run as a read-only role within a statement
timeout, and then match the expected rows (with tolerances, ordering rules and column
matching), satisfy properties and assertions, and finally match the reference on every mutated
copy. A query that is right only by accident (wrong join type, missing `DISTINCT`, hard-coded
values) usually fails on the mutated copies.

## Data and protocols

**Postgres** (source of truth): `tenants`, `api_keys` (hashed secrets), `tasks` (spec, state
checkpoint, usage, result, fence, lease), `attempts`, `executions`, `llm_calls` (with object
keys of the exact prompt and response), `events` (per-task sequence), `artifacts`, `uploads`,
`outbox`, `github_installations`, `inbound_deliveries` (webhook dedupe), `audit_log`.

**Redis, two instances** on purpose:

* `redis-queue` (no eviction, AOF): streams `fixloop:q:tasks:{interactive,batch}`,
  `fixloop:q:exec`, `fixloop:q:ghtoken`; exec results `fixloop:exec:result:{exec_id}` with a
  wake-up list `fixloop:exec:done:{exec_id}`; cancel flags; budget counters (Lua); rate limits;
  single-flight locks; event pub/sub; runner heartbeats.
* `redis-cache` (LRU, no persistence): L1 model responses, L2/L4 fixes and memos. Losing it
  costs money, never correctness.

**Object store** (S3/MinIO, or a folder in development), always tenant-prefixed:
`tenants/{tenant}/snapshots/{sha}.tar.zst`, `tenants/{tenant}/tasks/{task}/...` (manifest,
baseline, attempt diffs and traces, execution artifacts, prompts and responses, the patch).

**Exec protocol.** The worker publishes an `ExecRequest` whose id is a hash of
(task, attempt, purpose, fingerprint, rerun). The runner stores the `ExecResult` under that id
and pushes a wake-up token. A resumed worker that sends the same request finds the stored
result, so nothing runs twice. Cancellation is a Redis flag the runner polls every second.

## Failure handling

| what fails | what happens |
|---|---|
| a worker dies mid-task | its lease expires; the reconciler re-enqueues; the next worker claims a higher fence, resumes from the checkpoint; stored exec results and saved model outputs mean no double spend |
| an old worker comes back | its fenced writes raise `LeaseLost`; it stops without writing |
| a runner dies mid-run | its stream message is reclaimed after 90 s; the reaper removes leftovers at start-up |
| a sandbox infrastructure error | retried once under a fresh exec id, then `infra_error` |
| the model provider is down | retries with full jitter, per-model circuit breakers, the fallback model, then the task is handed back for later |
| Redis queue loses messages | the reconciler re-enqueues from Postgres every minute |
| GitHub or a webhook receiver is down | outbox retries with backoff up to 8 times, then `failed` with the last error |
| a task crashes workers repeatedly | after 3 deliveries it is dead-lettered and finished as `infra_error` |

## Scaling

The API and workers are stateless and scale horizontally (workers on stream lag with KEDA).
Runners scale with sandbox slots per host. Postgres load is small (a few writes per step);
the heavy data (snapshots, artifacts) lives in the object store. Budgets and rate limits are
global through Redis, so adding workers never multiplies spend.
