# ADR-0017: Budgets: reserve before spending, settle after

Status: accepted

## Context

Concurrent workers checking 'remaining budget' and then spending race each other.

## Decision

Every model call and execution first reserves its worst-case cost in Redis with a Lua script that checks task, tenant-day and global-day limits atomically; afterwards it settles with the real numbers. Stale reservations expire.

## Why

No interleaving of workers can overspend by more than in-flight reservations, and reservations are bounded.

## Alternatives considered

- Check-then-spend: races.
- Postgres counters: too slow and lock-heavy for per-call accounting.

## Consequences

A task stops with `exhausted_budget` naming the dimension that ran out.
