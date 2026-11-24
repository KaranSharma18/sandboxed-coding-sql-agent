# ADR-0022: A transactional outbox for every external side effect

Status: accepted

## Context

Opening a PR or calling a webhook must happen exactly when (and only when) the task result is committed.

## Decision

The final transaction writes the result, the outbox items and the `task.finished` event together. The publisher executes the items with retries and backoff; every handler is idempotent (it finds the branch or PR it already made).

## Why

No PR without a recorded result, no recorded result without its PR.

## Alternatives considered

- Calling GitHub from the worker after commit: lost on a crash in between.

## Consequences

An item that keeps failing ends as `failed` with its last error, for operators.
