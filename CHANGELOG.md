# Changelog

All notable changes are listed here. The format follows Keep a Changelog; versions follow
Semantic Versioning.

## [0.1.0] - 2026-11-23

### Added
- Python tasks (`python.fix`, `python.implement`) with baseline, plan/patch/repair, escalation,
  hidden tests, shuffled reruns and changed-line coverage.
- SQL tasks (`sql.write`, `sql.fix`) on DuckDB and Postgres snapshots with static checks,
  read-only execution, properties, assertions and differential testing.
- Sandbox runner with Docker (gVisor in production) and a local backend for tests; isolation
  self-test; reaper; per-tenant environment cache.
- Allowlisted, wheels-only package proxy.
- Model layer on Gemini with routing, budgets, caching, retries, breakers and fallbacks; a
  scripted model for tests and demos.
- FastAPI service with keys and scopes, idempotency, rate limits, SSE, uploads, GitHub webhooks;
  `fixloop` CLI and `fixloop-admin`.
- Publisher: draft pull requests, comment commands, signed customer webhooks.
- Evaluation harness, Kubernetes manifests, CI, nightly and release workflows.
