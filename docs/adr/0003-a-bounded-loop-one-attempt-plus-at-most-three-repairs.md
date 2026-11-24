# ADR-0003: A bounded loop: one attempt plus at most three repairs

Status: accepted

## Context

Agents that retry until something passes spend without limit and drift into test gaming.

## Decision

A task gets one first attempt and at most three repairs. Format re-asks (schema, apply errors, policy violations, duplicates) do not count as attempts but do count against the budget.

## Why

Most fixable bugs are fixed in the first two attempts; beyond four, success rates are flat while cost keeps climbing. A hard bound makes cost predictable and makes `exhausted_repairs` an honest answer.

## Alternatives considered

- Unbounded retries until a budget runs out: spends the whole budget on hopeless tasks.
- No repairs: misses the many bugs that need one round of feedback.

## Consequences

Users can lower `max_repairs`; nobody can raise it above three.
