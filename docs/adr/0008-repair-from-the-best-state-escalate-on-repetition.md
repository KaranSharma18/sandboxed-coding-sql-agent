# ADR-0008: Repair from the best state, escalate on repetition

Status: accepted

## Context

A repair that makes things worse should not be built on, and repeating the same failure is a waste.

## Decision

Each attempt gets a progress score (runnable, targets passing, regressions, collection errors, change size). Repairs apply to the best state so far. The last repair always runs on the stronger model with a deep trace; so does the repair after a failure signature first repeats. A second repeat ends the task as `no_progress`.

## Why

Starting from the best state prevents compounding mistakes. Escalating only when needed keeps the average cost low while still giving hard tasks the strong model.

## Alternatives considered

- Always repair the last attempt: drifts.
- Always use the strong model: several times the cost for the same success on easy tasks.

## Consequences

Signatures normalize messages (numbers, addresses, paths) so 'the same failure' is detected reliably.
