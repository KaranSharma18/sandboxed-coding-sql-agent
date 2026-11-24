# ADR-0011: Separate control and execution planes; only the runner starts containers

Status: accepted

## Context

The component that runs hostile code should have nothing worth stealing.

## Decision

Only the runner talks to the container runtime. It reads requests from Redis, reads inputs from the object store and writes results back. It never connects to Postgres and never holds a model or GitHub key. A local subprocess backend exists for tests and demos, behind two explicit flags, and is refused in production.

## Why

A sandbox escape on a runner host reaches a queue and an object-store key scoped to inputs and artifacts, not customer data at large or credentials.

## Alternatives considered

- Workers starting containers themselves: simpler, but the worker holds every credential.

## Consequences

Exec results travel through Redis keys named by deterministic exec ids, which also gives idempotency.
