# ADR-0019: Idempotency keys for task creation

Status: accepted

## Context

Clients retry on timeouts; duplicate tasks waste money and confuse users.

## Decision

`POST /v1/tasks` accepts an `Idempotency-Key`. The same key with the same body returns the existing task (200); with a different body it is a 409.

## Why

Safe retries for every client, including other agents, without server-side guesswork.

## Alternatives considered

- Deduplicating on body hash alone: two genuinely separate runs would collapse.

## Consequences

Keys are unique per tenant.
