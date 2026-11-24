# Architecture decision records

Each record says what we decided, why, and what we gave up. They mirror the *Design decisions* tab of the
design document. A decision that changes gets a new record that supersedes the old one.

| # | decision |
|---|---|
| [0001](0001-python-and-a-uv-workspace-monorepo.md) | Python and a uv workspace monorepo |
| [0002](0002-gemini-through-the-official-google-genai-sdk-routed-per-role.md) | Gemini through the official google-genai SDK, routed per role |
| [0003](0003-a-bounded-loop-one-attempt-plus-at-most-three-repairs.md) | A bounded loop: one attempt plus at most three repairs |
| [0004](0004-verification-by-execution-never-by-a-model.md) | Verification by execution, never by a model |
| [0005](0005-baseline-first.md) | Baseline first |
| [0006](0006-anchored-search-replace-edits.md) | Anchored search/replace edits |
| [0007](0007-strict-structured-outputs-and-format-re-asks.md) | Strict structured outputs and format re-asks |
| [0008](0008-repair-from-the-best-state-escalate-on-repetition.md) | Repair from the best state, escalate on repetition |
| [0009](0009-deterministic-context-assembly-with-a-stable-prefix.md) | Deterministic context assembly with a stable prefix |
| [0010](0010-gemini-3-settings-default-temperature-thinking-per-role.md) | Gemini 3 settings: default temperature, thinking per role |
| [0011](0011-separate-control-and-execution-planes-only-the-runner-starts.md) | Separate control and execution planes; only the runner starts containers |
| [0012](0012-gvisor-in-production-hardened-runc-in-development.md) | gVisor in production, hardened runc in development |
| [0013](0013-a-continuous-isolation-self-test-that-fails-closed.md) | A continuous isolation self-test that fails closed |
| [0014](0014-wheels-only-from-an-allowlist-through-a-proxy.md) | Wheels only, from an allowlist, through a proxy |
| [0015](0015-environments-cached-per-runner-and-per-tenant.md) | Environments cached per runner and per tenant |
| [0016](0016-a-pytest-reporting-plugin-cross-checked-with-junit.md) | A pytest reporting plugin cross-checked with JUnit |
| [0017](0017-budgets-reserve-before-spending-settle-after.md) | Budgets: reserve before spending, settle after |
| [0018](0018-exact-match-caches-answers-are-always-re-verified.md) | Exact-match caches; answers are always re-verified |
| [0019](0019-idempotency-keys-for-task-creation.md) | Idempotency keys for task creation |
| [0020](0020-redis-streams-for-queues-postgres-as-the-source-of-truth.md) | Redis Streams for queues, Postgres as the source of truth |
| [0021](0021-leases-with-fencing-tokens-in-postgres.md) | Leases with fencing tokens in Postgres |
| [0022](0022-a-transactional-outbox-for-every-external-side-effect.md) | A transactional outbox for every external side effect |
| [0023](0023-a-least-privilege-github-app-that-only-opens-draft-prs.md) | A least-privilege GitHub App that only opens draft PRs |
| [0024](0024-sql-verification-static-checks-read-only-execution-different.md) | SQL verification: static checks, read-only execution, differential data |
