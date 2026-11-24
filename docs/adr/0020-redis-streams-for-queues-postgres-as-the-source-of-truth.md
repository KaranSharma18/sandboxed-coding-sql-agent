# ADR-0020: Redis Streams for queues, Postgres as the source of truth

Status: accepted

## Context

We need at-least-once delivery, consumer groups, priorities and reclaiming of abandoned work.

## Decision

Task and exec queues are Redis Streams with consumer groups; messages are acknowledged after their effects are committed. Abandoned messages are reclaimed with XAUTOCLAIM; long runs keep theirs alive. A reconciler re-enqueues anything the queue lost, from Postgres.

## Why

Streams give the semantics we need with infrastructure we already run. Postgres being the truth means a Redis flush loses no task.

## Alternatives considered

- A dedicated broker (RabbitMQ, SQS): one more system to run.
- Polling Postgres: slower and lock-heavy.

## Consequences

Delivery is at least once, so every handler is idempotent.
