# ADR-0005: Baseline first

Status: accepted

## Context

If the target tests already pass, or pass only sometimes, any 'fix' is meaningless.

## Decision

Before any model call, the suite runs on the untouched code. Passing targets end the task as `nothing_to_fix`; failing targets are rerun shuffled and any that flips ends it as `flaky_oracle`. The baseline also records what passes today, which defines a regression.

## Why

This saves money on tasks that need no work and refuses to 'verify' against a coin flip. It also catches wrong node ids early (`invalid_input`).

## Alternatives considered

- Trusting the user's claim that tests fail: wastes runs and produces false verifications on flaky tests.

## Consequences

Every task pays for at least two sandbox runs before the first model call. They are short and the environment is cached.
