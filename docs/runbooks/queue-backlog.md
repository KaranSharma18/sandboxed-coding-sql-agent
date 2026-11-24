# Tasks are piling up in the queue

**Signals:** `fixloop_queue_oldest_seconds` rising; the API answering 503 `overloaded`.

1. Are workers alive? `kubectl get pods -l app=fixloop-worker` (or `docker compose ps`). Look at
   `task_handed_back` and `lease_lost` in worker logs.
2. Are runners accepting work? Each runner writes `fixloop:runner:<id>` every 10 s with
   `accepting`. A runner that fails conformance stops accepting (see
   [runner-conformance.md](runner-conformance.md)).
3. Are sandboxes slow? `fixloop_exec_seconds` by purpose: a slow `prepare` usually means the
   package proxy or PyPI is slow.
4. Scale: workers on stream lag (KEDA), runners by adding hosts. Budgets are global, so scaling
   does not multiply spend.
5. Lost messages are re-enqueued from Postgres by the reconciler every minute; nothing needs to
   be replayed by hand. Messages in `fixloop:dlq:*` were dead-lettered: inspect, fix the cause,
   then `fixloop-admin replay-dlq`.
