# ADR-0021: Leases with fencing tokens in Postgres

Status: accepted

## Context

A paused worker can wake up after another worker took its task over.

## Decision

Claiming a task increments its `fence`. Every write the loop makes is `UPDATE ... WHERE fence = :mine`; a stale owner gets `LeaseLost` and stops. Leases are renewed every 20 s.

## Why

Fencing makes 'two owners' harmless instead of merely unlikely.

## Alternatives considered

- Redis locks: no fencing, so a paused owner can still write.

## Consequences

After three deliveries a task is dead-lettered and finished as `infra_error`.
