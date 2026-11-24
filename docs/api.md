# API

Base URL in development: `http://localhost:8080`. Interactive docs: `/docs` (OpenAPI at
`/openapi.json`).

## Authentication and scopes

Send `Authorization: Bearer fl_live_<key id>_<secret>`. Create keys with
`fixloop-admin create-key <tenant> --scope tasks:read --scope tasks:write ...`.

| scope | allows |
|---|---|
| `tasks:read` | read tasks, attempts, events, patches, artifacts |
| `tasks:write` | create, cancel, retry and delete tasks |
| `uploads:write` | start uploads |
| `usage:read` | read spend and usage |

Every response carries `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset`
(requests per minute per tenant, set by the plan in `config/limits.yaml`).

## Errors

Errors are RFC 9457 problem details (`application/problem+json`):

```json
{"type": "https://fixloop.dev/problems/conflict", "title": "conflict", "status": 409,
 "detail": "this Idempotency-Key was already used with a different request body", "code": "conflict"}
```

| status | code | when |
|---|---|---|
| 401 | `unauthorized` | missing or invalid key |
| 403 | `forbidden` | the key lacks the scope |
| 404 | `not_found` | no such task (or not yours) |
| 409 | `conflict` | idempotency key reused with a different body; deleting a running task |
| 413 | `too_large` | inline source or upload over the limits |
| 422 | `invalid_request` / `invalid_input` | schema errors (with an `errors` list) or an impossible task |
| 429 | `quota_exceeded` | rate limit, queued-task limit, daily task or spend limit (`Retry-After` set) |
| 503 | `overloaded` | the queue is backed up; retry after `Retry-After` |

## Tasks

| method and path | does |
|---|---|
| `POST /v1/tasks` | create a task (`Idempotency-Key` header recommended): 201 new, 200 existing |
| `GET /v1/tasks?status=&kind=&cursor=&limit=` | list, newest first, cursor-paginated |
| `GET /v1/tasks/{id}` | status, phase, outcome, result, usage, links |
| `GET /v1/tasks/{id}/events` | Server-Sent Events: replays from `?after=` or `Last-Event-ID`, then follows live; `Accept: application/json` returns the list |
| `GET /v1/tasks/{id}/attempts` | every attempt with its plan or diagnosis, edits, verdict and cost |
| `GET /v1/tasks/{id}/patch` | the patch as a git diff; `X-Fixloop-Verified: true/false` |
| `GET /v1/tasks/{id}/artifacts` | stored artifacts; `GET /v1/artifacts/{id}` redirects to a short-lived URL |
| `POST /v1/tasks/{id}/cancel` | request cancellation (a queued task is cancelled at once) |
| `POST /v1/tasks/{id}/retry` | run the same spec again as a new task |
| `DELETE /v1/tasks/{id}` | delete a finished task and its stored data |

### The task spec

```json
{
  "kind": "python.fix",
  "description": "parse_dates returns wrong dates",
  "source": {"type": "inline", "files": {"dates/parse.py": "...", "tests/test_dates.py": "..."}},
  "oracle": {"type": "pytest", "target_tests": ["tests/test_dates.py::test_parse_dates"],
             "regression": "full", "python_version": "3.12"},
  "options": {"max_repairs": 3, "model_profile": "default", "open_pr": false,
              "callback_url": "https://example.com/fixloop-hook"}
}
```

Sources: `inline` (files), `upload` (`upload_id` of a `.tar`, `.tar.gz` or `.tar.zst`),
`github` (`installation_id`, `repo`, `ref`, optional `resolved_sha`), `sql_snapshot` (`ddl` and
`seeds`). SQL oracles: `expected` rows, `expected_rows` (upload), `reference_query`,
`properties` (columns, row_count, unique, not_null, values_in, sorted_by, sum_equals,
assertion), plus comparison options (`order_matters`, `sort_key`, tolerances, string
normalization, `allow_extra_columns`, `match_columns_by`). The full schema is in `/docs`.

### Outcomes

`verified`, `nothing_to_fix`, `executed_only` (status `succeeded`); `exhausted_repairs`,
`exhausted_budget`, `no_progress`, `policy_violation`, `env_failed`, `flaky_oracle`,
`invalid_input`, `infra_error` (status `failed`); `cancelled`. A failed Python task still
returns its best attempt's diff with `unverified: true`.

### Events

`task.created`, `task.started` / `task.resumed`, `phase` (`snapshot`, `prepare_env`,
`baseline`, `plan`, `patch`, `execute`, `verify`, `repair`, ...), `snapshot.ready`,
`env.ready`, `baseline.done`, `cache.checked`, `attempt.started`, `attempt.finished`,
`task.finished`. Each event has a per-task `seq`, used as the SSE `id`.

## Uploads

```text
POST /v1/uploads {"kind": "repo", "content_type": "application/gzip", "size_bytes": 123456}
-> {"upload_id": "up_...", "upload_url": "...", "method": "PUT", "headers": {...}, "expires_at": "..."}
PUT <upload_url> with the bytes
```

Then refer to it: `"source": {"type": "upload", "upload_id": "up_..."}`. Kinds: `repo`,
`hidden_tests`, `seed`, `expected_rows`, `parquet`.

## Customer webhooks

With `options.callback_url` (HTTPS, public addresses only) Fixloop POSTs
`{"event": "task.finished", "data": {...}, "sent_at": ...}` with the header
`Fixloop-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`. The secret is
the tenant's `webhook_secret`. Reject signatures older than five minutes. Verification in Python:

```python
from fixloop_publisher.webhooks import verify

verify(secret, request_body_bytes, request.headers["Fixloop-Signature"])
```

## GitHub

`POST /v1/github/webhook` receives the App's webhooks (signature-checked, deduplicated by
delivery id). A collaborator with write access comments `/fixloop fix <node id> [...]
[--repairs N]` on an issue or a pull request; Fixloop answers with a comment and, when the fix is
verified, a draft PR into the PR's branch (or the default branch).

## System

`GET /healthz` (process alive), `GET /readyz` (Postgres, Redis, live runners), `GET /metrics`
(Prometheus), `GET /v1/usage?days=30`.
